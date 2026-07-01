"""Orchestrate one auto-reply pass.

For each unread reply from a known prospect: record the inbound message, honour
the suppression list, enforce the per-thread cap, draft a response with the
guardrailed model, and either suppress the address (on opt-out) or send + record
the outbound reply. Each message is wrapped so one failure can't stop the loop.

The whole thing is a NO-OP (returns zero counters) unless
``settings.auto_reply`` is True.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import func

from config import get_settings
from db.models import OutreachRecord, ReplyRecord
from db.session import get_session
from outreach.suppression import SUPPRESSION_PATH, is_suppressed

logger = logging.getLogger(__name__)


def _mark_replied(email: str) -> None:
    """Flag the matching OutreachRecord as replied so follow-ups stop for it."""
    addr = (email or "").strip().lower()
    if not addr:
        return
    try:
        with get_session() as session:
            rec = (
                session.query(OutreachRecord)
                .filter(func.lower(OutreachRecord.email) == addr)
                .first()
            )
            if rec is not None and not rec.replied:
                rec.replied = True
    except Exception as exc:  # marking replied must never break the pass
        logger.warning("run_reply_pass: could not mark %s replied: %s", addr, exc)


def _record_inbound(email: str, subject: str, body: str, message_id: str | None) -> None:
    with get_session() as session:
        session.add(
            ReplyRecord(
                email=email,
                direction="in",
                subject=subject or "",
                snippet=(body or "")[:500],
                message_id=message_id,
            )
        )


def _record_outbound(email: str, subject: str, body: str) -> None:
    with get_session() as session:
        session.add(
            ReplyRecord(
                email=email,
                direction="out",
                subject=subject or "",
                snippet=(body or "")[:500],
                message_id=None,
            )
        )


def _outbound_count(email: str) -> int:
    with get_session() as session:
        return (
            session.query(ReplyRecord)
            .filter(ReplyRecord.email == email, ReplyRecord.direction == "out")
            .count()
        )


def _suppress(email: str) -> None:
    """Append a lowercased address to the suppression file (opt-out)."""
    addr = (email or "").strip().lower()
    if not addr:
        return
    path = Path(SUPPRESSION_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        already = is_suppressed(addr, path)
        if not already:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(addr + "\n")
    except OSError as exc:
        logger.warning("runner: could not append %s to suppression list: %s", addr, exc)


def run_reply_pass(limit: int = 20, chat=None) -> dict:
    """Run one auto-reply pass. Returns per-outcome counters."""
    stats = {"inbound": 0, "replied": 0, "suppressed": 0, "skipped": 0, "capped": 0}

    settings = get_settings()
    if not settings.auto_reply:
        logger.info("run_reply_pass: auto_reply disabled — no-op")
        return stats

    # Imported here so tests can monkeypatch reply.inbox.fetch_replies cleanly and
    # so the module imports without live IMAP.
    from reply.inbox import fetch_replies
    from reply.respond import classify_and_draft
    from reply.sender import send_reply

    try:
        replies = fetch_replies(limit=limit)
    except Exception as exc:  # fetch_replies shouldn't raise, but be defensive
        logger.warning("run_reply_pass: fetch_replies failed: %s", exc)
        return stats

    for reply in replies:
        try:
            email = (reply.get("from_email") or "").strip().lower()
            if not email:
                stats["skipped"] += 1
                continue
            subject = reply.get("subject") or ""
            body = reply.get("body") or ""
            in_reply_to = reply.get("message_id")
            references = reply.get("references")

            stats["inbound"] += 1
            _record_inbound(email, subject, body, in_reply_to)
            # A reply from a contacted address stops any pending follow-ups.
            _mark_replied(email)

            if is_suppressed(email):
                stats["skipped"] += 1
                continue

            if _outbound_count(email) >= settings.max_replies_per_thread:
                stats["capped"] += 1
                continue

            res = classify_and_draft(email, body, chat=chat)
            action = res.get("action", "reply")

            if action == "suppress":
                _suppress(email)
                ack_body = (res.get("body") or "").strip()
                if ack_body:
                    send_reply(
                        email,
                        res.get("subject") or f"Re: {subject}",
                        ack_body,
                        in_reply_to=in_reply_to,
                        references=references,
                    )
                stats["suppressed"] += 1
                continue

            if action == "reply":
                sent = send_reply(
                    email,
                    res.get("subject") or f"Re: {subject}",
                    res.get("body") or "",
                    in_reply_to=in_reply_to,
                    references=references,
                )
                if sent:
                    _record_outbound(email, res.get("subject") or subject, res.get("body") or "")
                    stats["replied"] += 1
                else:
                    stats["skipped"] += 1
                continue

            # action == "skip" or anything unexpected
            stats["skipped"] += 1
        except Exception as exc:  # one bad reply shouldn't stop the pass
            logger.warning("run_reply_pass: error handling a reply: %s", exc)
            stats["skipped"] += 1
            continue

    return stats
