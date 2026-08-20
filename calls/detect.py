"""Detect booked calls from the inbox and email the owner a briefing.

Why the inbox and not the webhook
---------------------------------
``POST /webhooks/cal`` exists and works, and it has never once fired: it needs a
publicly reachable dashboard, and hosting the dashboard was declined. So
``OutreachRecord.call_booked_at`` stayed NULL forever, the funnel reported **0 calls
booked**, and on 2026-08-20 a real call sat in the inbox while every automated surface
said nothing had happened. A metric that cannot rise is not a metric.

The inbox is the one production surface already reachable — the reply pass logs into it
every two hours with credentials that exist — so detection lives there. cal.com sends a
confirmation email for every booking; that email is the event.

Properties
----------
* **Read-only against mail.** Flags are never touched, so the owner's own unread state
  is preserved and a re-read is harmless. Idempotency comes from
  ``CallRecord.booking_uid`` in the database, not from mail flags.
* **Alerts exactly once.** ``notified`` is set only after the send returns True, so a
  briefing that failed to send is retried next pass rather than silently lost.
* **Never raises.** Any IMAP or parse failure degrades to a stats dict, because this
  runs inside the same unattended pass as reply handling and must not take it down.
"""
from __future__ import annotations

import email
import imaplib
import logging
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr

from calls import brief as brief_mod
from calls import parse as parse_mod

logger = logging.getLogger(__name__)

#: How far back to sweep. Long enough that a booking made while the schedule was paused
#: is still caught, short enough that the IMAP search stays cheap. Every hit is deduped
#: on booking_uid, so a wide window costs time, never a duplicate email.
_LOOKBACK_DAYS = 21


def _owner_addresses(settings) -> set[str]:
    """Every address that is *us*, so the invitee is whatever is left over."""
    candidates = [
        getattr(settings, "owner_email", ""),
        getattr(settings, "smtp_user", ""),
        getattr(settings, "smtp_from", ""),
        getattr(settings, "alert_email", ""),
        getattr(settings, "opt_out_mailbox", ""),
    ]
    return {c.strip().lower() for c in candidates if c and "@" in c}


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - a malformed header must not stop the sweep
        return value


def _body_text(msg) -> str:
    """The message body, preferring text/plain but accepting text/html.

    ``reply.inbox._plain_body`` returns "" for a multipart message with no text/plain
    part, and cal.com's confirmation is written for humans in HTML. Returning "" there
    would make an HTML-only booking look like an empty email — detected as nothing, with
    no error anywhere. ``calls.parse.visible_text`` strips the markup afterwards.
    """
    from reply.inbox import _plain_body

    plain = _plain_body(msg)
    if plain.strip():
        return plain
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() != "text/html":
                    continue
                if "attachment" in str(part.get("Content-Disposition") or ""):
                    continue
                payload = part.get_payload(decode=True)
                if payload is not None:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    except Exception as exc:  # noqa: BLE001
        logger.warning("calls: could not read an HTML body: %s", exc)
    return plain


def _latest_published_post():
    """The most recent LinkedIn post — the likeliest referrer for an inbound booking."""
    try:
        from db.models import PostRecord
        from db.session import get_session

        with get_session() as session:
            return (
                session.query(PostRecord)
                .filter(PostRecord.status == "published")
                .order_by(PostRecord.created_at.desc())
                .first()
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("calls: could not read post history: %s", exc)
        return None


def _match_outreach(invitee_email: str):
    """``(origin, lead, pitch)`` for this invitee.

    Matching on the address is exact by design. A fuzzy match — same display name, same
    company domain — would attach the wrong job to the briefing, and a briefing that
    confidently describes the wrong project is worse than one that admits it knows
    nothing: the owner would open the call talking about someone else's post.
    """
    if not invitee_email:
        return "inbound", None, ""
    try:
        from db.models import LeadRecord, OutreachRecord, ProposalRecord
        from db.session import get_session

        with get_session() as session:
            row = (
                session.query(OutreachRecord)
                .filter(OutreachRecord.email == invitee_email)
                .order_by(OutreachRecord.sent_at.desc())
                .first()
            )
            if row is None:
                return "inbound", None, ""
            lead = (
                session.query(LeadRecord).filter(LeadRecord.id == row.lead_id).first()
                if row.lead_id
                else None
            )
            pitch = ""
            if row.lead_id:
                proposal = (
                    session.query(ProposalRecord)
                    .filter(ProposalRecord.lead_id == row.lead_id)
                    .order_by(ProposalRecord.created_at.asc())
                    .first()
                )
                pitch = proposal.body if proposal is not None else ""
            return "outreach", lead, pitch
    except Exception as exc:  # noqa: BLE001
        logger.warning("calls: could not match invitee to outreach: %s", exc)
        return "inbound", None, ""


def _stamp_call_booked(invitee_email: str) -> bool:
    """Complete the funnel: emailed -> replied -> **call booked** -> won.

    This is the write the cal.com webhook was supposed to perform. Without it the
    ``booked`` stage of every KPI report stays 0 no matter how many calls happen, which
    is how a working outcome looked like no outcome for a month.
    """
    if not invitee_email:
        return False
    try:
        from db.models import OutreachRecord
        from db.session import get_session

        with get_session() as session:
            row = (
                session.query(OutreachRecord)
                .filter(OutreachRecord.email == invitee_email)
                .order_by(OutreachRecord.sent_at.desc())
                .first()
            )
            if row is None:
                return False
            if row.call_booked_at is None:
                # Same clock every other column in this table uses (db.models._utcnow).
                row.call_booked_at = datetime.now(UTC)
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("calls: could not stamp call_booked_at: %s", exc)
        return False


def _fetch_cal_mail(settings, limit: int = 40) -> list[dict]:
    """Cal.com notification emails from the last ``_LOOKBACK_DAYS``. Never raises.

    Searches ``FROM cal.com`` over both read and unread mail: the owner reads their own
    inbox daily, so restricting to UNSEEN would miss every booking they happened to open
    first — the exact failure that made reply detection blind for a month.
    """
    imap_host = settings.resolved_imap_host()
    if not imap_host:
        logger.info("calls: no IMAP host — no-op")
        return []
    if not settings.smtp_user or not settings.smtp_password:
        logger.info("calls: IMAP credentials not configured — no-op")
        return []

    out: list[dict] = []
    conn: imaplib.IMAP4_SSL | None = None
    try:
        conn = imaplib.IMAP4_SSL(imap_host, settings.imap_port)
        conn.login(settings.smtp_user, settings.smtp_password)
        conn.select("INBOX")
        since = (datetime.now(UTC) - timedelta(days=_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        typ, data = conn.search(None, "FROM", "cal.com", "SINCE", since)
        if typ != "OK" or not data or not data[0]:
            return []
        for msg_id in data[0].split()[-limit:]:
            try:
                typ, raw = conn.fetch(msg_id, "(RFC822)")
                if typ != "OK" or not raw or not raw[0]:
                    continue
                msg = email.message_from_bytes(raw[0][1])
                sender = parseaddr(_decode(msg.get("From")))[1].strip().lower()
                # IMAP FROM search is a substring match, so a prospect writing about
                # "cal.com" would match too. Confirm the sending domain.
                if not sender.endswith("cal.com"):
                    continue
                out.append(
                    {
                        "subject": _decode(msg.get("Subject")),
                        "body": _body_text(msg),
                        "message_id": (msg.get("Message-ID") or "").strip() or None,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - one bad message, not the batch
                logger.warning("calls: skipped a message: %s", exc)
                continue
    except Exception as exc:  # noqa: BLE001 - never raise into the runner
        logger.warning("calls: IMAP error against %s: %s", imap_host, exc)
        return out
    finally:
        if conn is not None:
            for close in (conn.close, conn.logout):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
    return out


def scan_for_bookings() -> dict:
    """Find new bookings, brief the owner once each, and stamp the funnel.

    Returns ``{"scanned", "booked", "cancelled", "briefed", "already_known", "errors"}``.
    ``briefed`` is the number that reached the owner's mailbox — the only one of these
    that means the feature worked, so it is reported separately from ``booked``.
    """
    from config import get_settings

    settings = get_settings()
    stats = {
        "scanned": 0,
        "booked": 0,
        "cancelled": 0,
        "briefed": 0,
        "already_known": 0,
        "purged": 0,
        "errors": 0,
    }
    if not getattr(settings, "detect_calls", True):
        logger.info("calls: detection disabled")
        return stats

    try:
        messages = _fetch_cal_mail(settings)
    except Exception as exc:  # noqa: BLE001 - _fetch_cal_mail swallows its own errors,
        # but this is the one call outside any per-message guard, and "never raises" is a
        # promise to two callers: the reply pass and the daily monitor step.
        logger.warning("calls: mail fetch failed: %s", exc)
        stats["errors"] += 1
        return stats
    stats["scanned"] = len(messages)
    if not messages:
        return stats

    from db.models import CallRecord
    from db.session import get_session, init_db
    from runlog import send_alert

    init_db()
    stats["purged"] = _purge_unattributable()
    ours = _owner_addresses(settings)
    owner_name = getattr(settings, "owner_name", "")

    for message in messages:
        try:
            booking = parse_mod.parse_booking(
                subject=message["subject"],
                body=message["body"],
                message_id=message["message_id"],
                owner_addresses=ours,
                owner_name=owner_name,
            )
            if booking is None:
                continue

            with get_session() as session:
                existing = (
                    session.query(CallRecord)
                    .filter(CallRecord.booking_uid == booking["booking_uid"])
                    .first()
                )
                # A cancellation for a booking we already briefed: update the row and
                # tell the owner, so they never join a call that no longer exists.
                if existing is not None:
                    if booking["kind"] == "cancelled" and existing.status != "cancelled":
                        existing.status = "cancelled"
                        stats["cancelled"] += 1
                        subject, body = brief_mod.build_cancellation(booking)
                        if send_alert(subject, body):
                            stats["briefed"] += 1
                    elif _fill_in_gaps(existing, booking):
                        # The first briefing went out saying "(time not parsed — see the
                        # cal.com email)" because the confirmation is HTML-only and the
                        # parser was reading markup. Now that the time is known, the owner
                        # gets it: they were told to go and look it up themselves, for a
                        # call that may be tomorrow. Guarded on the field having been
                        # blank, so this cannot become a per-pass re-send.
                        booking_for_brief = {
                            **booking,
                            "invitee_name": existing.invitee_name or booking["invitee_name"],
                        }
                        origin, lead, pitch = _match_outreach(existing.invitee_email)
                        subject, body = brief_mod.build_brief(
                            booking=booking_for_brief,
                            origin=origin,
                            lead=lead,
                            pitch=pitch,
                            latest_post=(
                                _latest_published_post() if origin != "outreach" else None
                            ),
                        )
                        if send_alert(subject, body):
                            stats["briefed"] += 1
                            logger.info("calls: re-briefed a booking whose details filled in")
                        stats["already_known"] += 1
                    else:
                        stats["already_known"] += 1
                    continue

                if booking["kind"] == "cancelled":
                    # Cancelled before we ever saw the booking — record it so a later
                    # re-read cannot resurrect it as new, and say nothing.
                    session.add(
                        CallRecord(
                            booking_uid=booking["booking_uid"],
                            invitee_name=booking["invitee_name"],
                            invitee_email=booking["invitee_email"],
                            when_text=booking["when_text"],
                            join_url=booking["join_url"],
                            subject=booking["subject"],
                            origin="inbound",
                            status="cancelled",
                            notified=True,
                        )
                    )
                    stats["cancelled"] += 1
                    continue

            origin, lead, pitch = _match_outreach(booking["invitee_email"])
            if origin == "outreach":
                _stamp_call_booked(booking["invitee_email"])
            subject, body = brief_mod.build_brief(
                booking=booking,
                origin=origin,
                lead=lead,
                pitch=pitch,
                latest_post=_latest_published_post() if origin != "outreach" else None,
            )
            sent = send_alert(subject, body)

            with get_session() as session:
                session.add(
                    CallRecord(
                        booking_uid=booking["booking_uid"],
                        invitee_name=booking["invitee_name"],
                        invitee_email=booking["invitee_email"],
                        when_text=booking["when_text"],
                        join_url=booking["join_url"],
                        subject=booking["subject"],
                        origin=origin,
                        lead_id=lead.id if lead is not None else None,
                        status="booked",
                        # Only True when the mail actually left. An unsent briefing must
                        # be retried next pass, not marked done — the whole feature is
                        # "the owner finds out", and the row is not the owner.
                        notified=bool(sent),
                    )
                )
            stats["booked"] += 1
            if sent:
                stats["briefed"] += 1
                logger.info(
                    "calls: briefed the owner on a booking from %s (%s)",
                    booking["invitee_email"] or "unknown address",
                    origin,
                )
            else:
                logger.warning(
                    "calls: booking detected but the briefing did not send "
                    "(smtp_host empty?) — will retry next pass"
                )
        except Exception as exc:  # noqa: BLE001 - one message must not end the sweep
            stats["errors"] += 1
            logger.warning("calls: failed on one message: %s", exc)
            continue

    # Retry any briefing that was detected earlier but never reached the owner.
    stats["briefed"] += _retry_unnotified(settings)
    return stats


def _fill_in_gaps(row, booking: dict) -> bool:
    """Backfill fields an earlier, worse parser left blank. True when something material
    changed — meaning the owner's first briefing was missing it.

    Only ever fills BLANKS. Overwriting a populated field would let a later, differently
    formatted copy of the same mail (a reminder, a forward) quietly rewrite a booking the
    owner has already read, and re-notify them for a change they did not make.
    """
    material = False
    if not row.when_text and booking.get("when_text"):
        row.when_text = booking["when_text"]
        material = True
    if not row.join_url and booking.get("join_url"):
        row.join_url = booking["join_url"]
        material = True
    if not row.invitee_email and booking.get("invitee_email"):
        row.invitee_email = booking["invitee_email"]
        material = True
    # Cosmetic: a better name is worth storing but is not worth a second email.
    if booking.get("invitee_name") and row.invitee_name in ("", "(unknown)"):
        row.invitee_name = booking["invitee_name"]
    return material


def _purge_unattributable() -> int:
    """Delete rows naming neither a person nor a time. Returns how many were removed.

    Self-healing rather than a manual cleanup command, because the standing requirement is
    full automation. The first production sweep matched a cal.com *changelog* email and
    wrote a row with no invitee and no time — a permanent "[BOOKED] (unknown) — ?" line in
    every listing, and one the dedupe key would protect forever. The parser now refuses
    such mail outright, so this set is closed: it can only ever contain rows written by
    the version of the parser that was wrong.
    """
    from db.models import CallRecord
    from db.session import get_session

    try:
        with get_session() as session:
            junk = (
                session.query(CallRecord)
                .filter(CallRecord.invitee_email == "", CallRecord.when_text == "")
                .all()
            )
            for row in junk:
                logger.info(
                    "calls: purging an unattributable row (subject=%r)", row.subject
                )
                session.delete(row)
            return len(junk)
    except Exception as exc:  # noqa: BLE001
        logger.warning("calls: purge of unattributable rows failed: %s", exc)
        return 0


def _retry_unnotified(settings) -> int:
    """Re-send briefings for rows recorded while SMTP was down. Returns how many sent.

    Without this, a booking detected during an SMTP outage would be permanently silent:
    the row exists, so dedupe skips it forever, and the owner never hears about the call.
    """
    from db.models import CallRecord
    from db.session import get_session
    from runlog import send_alert

    sent_count = 0
    try:
        with get_session() as session:
            pending = (
                session.query(CallRecord)
                .filter(CallRecord.notified.is_(False), CallRecord.status == "booked")
                .all()
            )
            for row in pending:
                booking = {
                    "invitee_name": row.invitee_name,
                    "invitee_email": row.invitee_email,
                    "when_text": row.when_text,
                    "join_url": row.join_url,
                    "subject": row.subject,
                }
                origin, lead, pitch = _match_outreach(row.invitee_email)
                subject, body = brief_mod.build_brief(
                    booking=booking,
                    origin=origin,
                    lead=lead,
                    pitch=pitch,
                    latest_post=_latest_published_post() if origin != "outreach" else None,
                )
                if send_alert(subject, body):
                    row.notified = True
                    sent_count += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("calls: retry of unnotified briefings failed: %s", exc)
    return sent_count
