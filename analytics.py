"""Read-only analytics over the CRM tables.

Pure functions that summarise the outreach funnel, run history, sent-email list,
and reply conversations. They open their own DB sessions (via ``get_session``) so
the same helpers can back the dashboard UI, the MCP read tools, and the tests
without any web/agent plumbing. Nothing here mutates state.
"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import func

from db.models import LeadRecord, LeadStatus, OutreachRecord, ReplyRecord, RunRecord
from db.session import get_session


def _today_start() -> _dt.datetime:
    now = _dt.datetime.now(_dt.UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def funnel_stats() -> dict:
    """Top-of-funnel to closed-won counts + spend, as JSON-serialisable dict.

    ``reply_rate`` is replied/emailed (0.0 when nothing has been emailed yet).
    """
    with get_session() as session:
        emailed = (
            session.query(OutreachRecord)
            .filter(OutreachRecord.status == "sent")
            .count()
        )
        replied = (
            session.query(OutreachRecord)
            .filter(OutreachRecord.replied.is_(True))
            .count()
        )
        # Fall back to distinct inbound conversation partners if the flag was
        # never set (e.g. replies logged without back-updating outreach).
        if replied == 0:
            replied = (
                session.query(func.count(func.distinct(ReplyRecord.email)))
                .filter(ReplyRecord.direction == "in")
                .scalar()
                or 0
            )

        def _leads(status: LeadStatus) -> int:
            return (
                session.query(LeadRecord)
                .filter(LeadRecord.status == status)
                .count()
            )

        won = _leads(LeadStatus.won)
        lost = _leads(LeadStatus.lost)
        in_progress = (
            session.query(LeadRecord)
            .filter(
                LeadRecord.status.in_(
                    [
                        LeadStatus.new,
                        LeadStatus.qualified,
                        LeadStatus.drafted,
                        LeadStatus.approved,
                        LeadStatus.submitted,
                    ]
                )
            )
            .count()
        )
        suppressed = (
            session.query(OutreachRecord)
            .filter(OutreachRecord.status == "suppressed")
            .count()
        )
        calls_booked = (
            session.query(OutreachRecord)
            .filter(OutreachRecord.call_booked_at.isnot(None))
            .count()
        )
        total_cost_usd = (
            session.query(func.coalesce(func.sum(RunRecord.cost_usd), 0.0)).scalar()
            or 0.0
        )
        emails_today = (
            session.query(OutreachRecord)
            .filter(
                OutreachRecord.status == "sent",
                OutreachRecord.sent_at >= _today_start(),
            )
            .count()
        )
        reply_rate = (replied / emailed) if emailed else 0.0
        return {
            "emailed": emailed,
            "replied": replied,
            "reply_rate": round(reply_rate, 4),
            "calls_booked": calls_booked,
            "in_progress": in_progress,
            "won": won,
            "lost": lost,
            "suppressed": suppressed,
            "total_cost_usd": round(float(total_cost_usd), 4),
            "emails_today": emails_today,
        }


def recent_runs(limit: int = 20) -> list[dict]:
    """Most recent workflow runs (newest first), as plain dicts."""
    with get_session() as session:
        rows = (
            session.query(RunRecord)
            .order_by(RunRecord.created_at.desc(), RunRecord.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "workflow": r.workflow,
                "ok": bool(r.ok),
                "cost_usd": round(float(r.cost_usd or 0.0), 4),
                "stats": r.stats or {},
                "error": r.error,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


def outreach_list(limit: int = 100) -> list[dict]:
    """Sent/attempted cold emails (newest first), as plain dicts."""
    with get_session() as session:
        rows = (
            session.query(OutreachRecord)
            .order_by(OutreachRecord.sent_at.desc(), OutreachRecord.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": o.id,
                "email": o.email,
                "subject": o.subject,
                "status": o.status,
                "replied": bool(o.replied),
                "followups_sent": int(o.followups_sent or 0),
                "sent_at": o.sent_at.isoformat() if o.sent_at else None,
                "last_contact_at": o.last_contact_at.isoformat()
                if o.last_contact_at
                else None,
                "lead_id": o.lead_id,
            }
            for o in rows
        ]


def conversations() -> list[dict]:
    """Group reply messages by prospect email into ordered threads.

    Returns ``[{email, messages: [{direction, subject, snippet, created_at}]}]``
    with prospects ordered by their most recent message (newest thread first)
    and messages within a thread in chronological order.
    """
    with get_session() as session:
        rows = (
            session.query(ReplyRecord)
            .order_by(ReplyRecord.created_at.asc(), ReplyRecord.id.asc())
            .all()
        )
        threads: dict[str, dict] = {}
        latest: dict[str, _dt.datetime] = {}
        for r in rows:
            thread = threads.setdefault(r.email, {"email": r.email, "messages": []})
            thread["messages"].append(
                {
                    "direction": r.direction,
                    "subject": r.subject,
                    "snippet": r.snippet,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
            )
            if r.created_at is not None:
                latest[r.email] = r.created_at
        return sorted(
            threads.values(),
            key=lambda t: latest.get(t["email"], _dt.datetime.min),
            reverse=True,
        )
