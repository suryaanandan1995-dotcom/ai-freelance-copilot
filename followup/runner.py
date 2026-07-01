"""Orchestrate one follow-up pass.

Finds cold-email recipients who were contacted, never replied, and are due for
another polite nudge, then drafts + sends a follow-up for each. Shares outreach's
master gate (``settings.auto_email``), daily send cap, SMTP transport, and
suppression list, so nothing sends under the safe default config.

Selection criteria for a candidate OutreachRecord:
  * ``status == "sent"`` (only successfully contacted addresses)
  * ``replied is False`` (a reply stops the sequence)
  * ``followups_sent < settings.max_followups`` (bounded number of touches)
  * ``last_contact_at <= now - followup_after_days`` (enough silence)
  * the email is not on the suppression list

Each candidate is wrapped so one failure (bad lead, send error) can't stop the
loop, and the daily cap counts follow-ups already sent today.
"""
from __future__ import annotations

import datetime as _dt
import logging

from config import get_settings

logger = logging.getLogger(__name__)


def _today_start() -> _dt.datetime:
    now = _dt.datetime.now(_dt.UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _lead_from_record(lead_rec, outreach):
    """Build a ``core.schemas.Lead`` from the outreach's LeadRecord, or synthesize
    a minimal one from the outreach subject when there is no linked lead."""
    from core.schemas import Lead

    if lead_rec is not None:
        return Lead(
            source=lead_rec.source or "outreach",
            external_id=str(lead_rec.external_id or lead_rec.id),
            title=lead_rec.title or (outreach.subject or "our earlier conversation"),
            description=lead_rec.description or "",
            company=lead_rec.company,
            url=lead_rec.url or "",
            tags=list(lead_rec.tags or []),
        )
    subject = outreach.subject or "our earlier conversation"
    return Lead(
        source="outreach",
        external_id=outreach.email,
        title=subject,
    )


def run_followups(chat=None) -> dict:
    """Run one follow-up pass. Returns per-outcome counters.

    NO-OP (all zeros) unless ``settings.auto_email`` is True.
    """
    stats = {"candidates": 0, "sent": 0, "capped": 0, "skipped": 0}

    settings = get_settings()
    if not settings.auto_email:
        logger.info("run_followups: auto_email disabled — no-op")
        return stats

    # Imported here so tests can monkeypatch cleanly and so the module imports
    # without a configured DB / SMTP.
    from agents.followup import draft_followup
    from db.models import LeadRecord, OutreachRecord
    from db.session import get_session, init_db
    from outreach.sender import send_outreach
    from outreach.suppression import is_suppressed

    init_db()

    now = _dt.datetime.now(_dt.UTC)
    cutoff = now - _dt.timedelta(days=settings.followup_after_days)

    # How many follow-ups have already gone out today (respect the daily cap).
    # A follow-up bumps ``last_contact_at`` to ``now`` and increments
    # ``followups_sent``, so records touched today with >=1 follow-up are today's.
    with get_session() as session:
        sent_today = (
            session.query(OutreachRecord)
            .filter(
                OutreachRecord.last_contact_at >= _today_start(),
                OutreachRecord.followups_sent > 0,
            )
            .count()
        )

    daily_cap = settings.max_emails_per_day
    remaining = max(0, daily_cap - sent_today)

    with get_session() as session:
        candidates = (
            session.query(OutreachRecord)
            .filter(
                OutreachRecord.status == "sent",
                OutreachRecord.replied.is_(False),
                OutreachRecord.followups_sent < settings.max_followups,
                OutreachRecord.last_contact_at <= cutoff,
            )
            .order_by(OutreachRecord.last_contact_at.asc())
            .all()
        )
        # Detach the data we need so we can work outside the read session.
        pending = [
            {
                "id": rec.id,
                "email": rec.email,
                "subject": rec.subject or "",
                "lead_id": rec.lead_id,
                "last_contact_at": rec.last_contact_at,
            }
            for rec in candidates
        ]

    stats["candidates"] = len(pending)

    for item in pending:
        try:
            email = (item["email"] or "").strip().lower()
            if not email or is_suppressed(email):
                stats["skipped"] += 1
                continue

            if remaining <= 0:
                stats["capped"] += 1
                continue

            # Build the Lead for the drafting agent.
            with get_session() as session:
                lead_rec = None
                if item["lead_id"] is not None:
                    lead_rec = session.get(LeadRecord, item["lead_id"])
                # Re-fetch the outreach so we edit a live, attached row.
                outreach = session.get(OutreachRecord, item["id"])
                if outreach is None:
                    stats["skipped"] += 1
                    continue

                lead = _lead_from_record(lead_rec, outreach)
                last = outreach.last_contact_at
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=_dt.UTC)
                days_since = (now - last).days if last is not None else 0

                body = draft_followup(lead, days_since, chat)
                subject = outreach.subject or ""
                if not subject.lower().startswith("re:"):
                    subject = f"Re: {subject}" if subject else "Re: following up"

                ok = send_outreach(email, subject, body)
                if ok:
                    outreach.followups_sent += 1
                    outreach.last_contact_at = now
                    stats["sent"] += 1
                    remaining -= 1
                else:
                    stats["skipped"] += 1
        except Exception as exc:  # one bad candidate shouldn't stop the pass
            logger.warning("run_followups: error following up %s: %s", item.get("email"), exc)
            stats["skipped"] += 1
            continue

    return stats
