"""Read unread prospect replies from the IMAP inbox.

``fetch_replies`` connects to Gmail (or any IMAP server) with the same
credentials as SMTP — a Gmail app password works for both — pulls UNSEEN
messages, parses them, and returns ONLY the ones from people we actually
contacted (an address matching an ``OutreachRecord`` or an existing
``ReplyRecord``). Returned messages are marked ``\\Seen`` so the next pass
doesn't re-read them.

Hard safety properties:
  * Reads only; sends nothing. Gated on ``settings.reply_detection`` (on by
    default) rather than on ``auto_reply``, because knowing a prospect answered
    must not depend on whether we auto-answer them. Still a NO-OP unless both
    ``smtp_host`` and ``smtp_user`` are configured.
  * Never raises — any IMAP / parse failure degrades to ``[]`` so the runner
    loop is safe on the unattended cloud schedule.
"""
from __future__ import annotations

import email
import imaplib
import logging
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr

from config import get_settings
from db.models import OutreachRecord, ReplyRecord
from db.session import get_session

logger = logging.getLogger(__name__)

#: How far back to sweep ALREADY-READ mail for replies the owner opened first. Bounded
#: so the search stays cheap and cannot re-import ancient threads: a reply that matters
#: is answered within days, and every hit is deduped on message_id anyway.
_SEEN_LOOKBACK_DAYS = 14


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _plain_body(msg: Message) -> str:
    """Best-effort extraction of the text/plain body."""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = str(part.get("Content-Disposition") or "")
                if ctype == "text/plain" and "attachment" not in disp:
                    payload = part.get_payload(decode=True)
                    if payload is not None:
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace").strip()
            return ""
        payload = msg.get_payload(decode=True)
        if payload is None:
            return str(msg.get_payload() or "").strip()
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace").strip()
    except Exception:
        return ""


def _known_senders() -> set[str]:
    """Lowercased set of addresses we've actually contacted or conversed with."""
    known: set[str] = set()
    try:
        with get_session() as session:
            for (addr,) in session.query(OutreachRecord.email).all():
                if addr:
                    known.add(addr.strip().lower())
            for (addr,) in session.query(ReplyRecord.email).all():
                if addr:
                    known.add(addr.strip().lower())
    except Exception as exc:
        logger.warning("inbox: could not load known senders: %s", exc)
    return known


def fetch_replies(limit: int = 20) -> list[dict]:
    """Fetch UNSEEN replies from known prospects. Never raises.

    Returns a list of ``{from_email, subject, body, message_id, references}``
    dicts, and marks the returned messages ``\\Seen``.
    """
    settings = get_settings()
    # Deliberately NOT gated on ``auto_reply``. Reading the inbox sends nothing; it is
    # what lets the system know a prospect answered. Answering is gated separately in
    # ``reply.sender.send_reply``. See the ``reply_detection`` note in config.py.
    if not settings.reply_detection and not settings.auto_reply:
        logger.info("fetch_replies: reply detection disabled — no-op")
        return []
    if not settings.smtp_host or not settings.smtp_user:
        logger.info("fetch_replies: SMTP host/user not configured — no-op")
        return []

    # Derived from smtp_host unless COPILOT_IMAP_HOST is set explicitly, because IMAP
    # logs in with the SMTP credentials: a host from one provider and a password from
    # another fails every time, and fails silently (see the except clause below).
    imap_host = settings.resolved_imap_host()
    if not imap_host:
        logger.info("fetch_replies: no IMAP host and smtp_host is empty — no-op")
        return []

    known = _known_senders()
    out: list[dict] = []
    conn: imaplib.IMAP4_SSL | None = None
    try:
        conn = imaplib.IMAP4_SSL(imap_host, settings.imap_port)
        conn.login(settings.smtp_user, settings.smtp_password)
        conn.select("INBOX")
        # Two searches, because "unread" and "replied to us" are different questions.
        #
        # UNSEEN alone loses any reply the owner opens first, permanently — not until
        # the next pass, but forever, since nothing ever restores the flag. Measured
        # 2026-08-07: the first real reply in a month arrived at 16:58 UTC, was read and
        # answered by hand at 17:30, and was therefore invisible to every subsequent
        # run. The lead stays replied=False, so followup.runner keeps selecting it and
        # nudges someone already mid-conversation.
        #
        # Recently-read messages are therefore swept too, but flagged human_handled so
        # the runner records them (stopping follow-ups) WITHOUT auto-answering: a thread
        # the owner has already opened is one the owner is handling, and two voices from
        # one address is worse than no reply. Duplicate records are prevented by
        # message_id in reply.runner._record_inbound, since a read message keeps
        # matching this search on every pass.
        ids: list[tuple[bytes, bool]] = []
        typ, data = conn.search(None, "UNSEEN")
        if typ == "OK" and data and data[0]:
            ids += [(i, False) for i in data[0].split()[:limit]]

        since = (datetime.now(UTC) - timedelta(days=_SEEN_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        typ, data = conn.search(None, "SEEN", "SINCE", since)
        if typ == "OK" and data and data[0]:
            ids += [(i, True) for i in data[0].split()[-limit:]]

        if not ids:
            return []

        for msg_id, human_handled in ids:
            try:
                typ, raw = conn.fetch(msg_id, "(RFC822)")
                if typ != "OK" or not raw or not raw[0]:
                    continue
                msg = email.message_from_bytes(raw[0][1])
                from_email = parseaddr(_decode(msg.get("From")))[1].strip().lower()
                if not from_email or from_email not in known:
                    # Not one of ours — leave it UNSEEN and untouched.
                    continue
                out.append(
                    {
                        "from_email": from_email,
                        "subject": _decode(msg.get("Subject")),
                        "body": _plain_body(msg),
                        "message_id": (msg.get("Message-ID") or "").strip() or None,
                        "references": (msg.get("References") or "").strip() or None,
                        # True when the owner had already read this before we saw it:
                        # record it, but let the human own the conversation.
                        "human_handled": human_handled,
                    }
                )
                # Only mark ours as seen; unknown senders stay UNSEEN. Already-seen
                # messages need no store call.
                if not human_handled:
                    conn.store(msg_id, "+FLAGS", "\\Seen")
            except Exception as exc:  # one bad message shouldn't kill the batch
                logger.warning("fetch_replies: skipped a message: %s", exc)
                continue
    except Exception as exc:  # never raise into the runner
        # Name the host. This branch is the *only* evidence that reading failed — the
        # caller sees an empty list either way — and "which host did it even try" was
        # the first thing needed to diagnose it.
        logger.warning("fetch_replies: IMAP error against %s: %s", imap_host, exc)
        return out
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn.logout()
            except Exception:
                pass
    return out
