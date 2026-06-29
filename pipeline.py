"""Pipeline orchestration: discover -> qualify -> research -> draft -> queue.

This is the integration core. It fans out to every lead source, runs each fresh
lead through the LangGraph agent pipeline under a hard per-run Claude-spend cap,
and persists *drafted* proposals for a human to review and submit. It NEVER
submits anything to any platform — that is a deliberate ToS-safety decision.

Everything here runs fully offline when a ``FakeChat`` + fake retriever +
in-memory sources are injected (see the test suite).
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from config import get_settings
from costs import BudgetExhausted, CostTracker
from db.models import LeadRecord, LeadStatus, ProposalRecord, ProposalStatus
from db.session import get_session, init_db
from observability import metrics

logger = logging.getLogger(__name__)


def _today_start() -> _dt.datetime:
    now = _dt.datetime.now(_dt.UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _proposals_today(session: Any) -> int:
    """Count proposals created since UTC midnight (anti-spam day cap)."""
    return (
        session.query(ProposalRecord)
        .filter(ProposalRecord.created_at >= _today_start())
        .count()
    )


def run_pipeline(
    limit: int | None = None,
    sources: list | None = None,
    retriever: Any = None,
    chat: Any = None,
    notify: bool = False,
) -> dict:
    """Run one end-to-end pipeline pass and return run statistics.

    Returns ``{fetched, new, queued, dropped, skipped, cost_usd, budget_exhausted}``.
    """
    settings = get_settings()
    init_db()

    tracker = CostTracker(budget_usd=settings.max_usd_per_run)
    # Install the tracker so the metered LLM wrapper meters + budget-gates every call.
    from agents.llm import set_cost_tracker

    set_cost_tracker(tracker)

    fetched = new = queued = dropped = skipped = 0
    budget_exhausted = False
    queued_leads: list[dict] = []

    try:
        from agents.graph import run_lead
        from sources.registry import fetch_all, get_default_sources

        srcs = sources if sources is not None else get_default_sources()
        per_source = max(1, (limit or settings.max_leads_per_run))
        leads = fetch_all(srcs, per_source_limit=per_source)

        cap = limit if limit is not None else settings.max_leads_per_run
        if cap is not None and len(leads) > cap:
            leads = leads[:cap]

        fetched = len(leads)
        metrics.inc("leads_fetched_total", fetched)

        for lead in leads:
            # Dedupe against the DB by (source, external_id).
            with get_session() as session:
                exists = (
                    session.query(LeadRecord.id)
                    .filter(
                        LeadRecord.source == lead.source,
                        LeadRecord.external_id == lead.external_id,
                    )
                    .first()
                )
            if exists is not None:
                skipped += 1
                continue

            new += 1
            try:
                state = run_lead(lead, retriever=retriever, chat=chat)
            except BudgetExhausted:
                budget_exhausted = True
                logger.warning("budget exhausted at $%.4f — stopping run", tracker.usd())
                break

            scored = state.get("scored") or {}
            fit_score = int(scored.get("fit_score", 0))
            if scored:
                metrics.observe("fit_score", fit_score)
                if fit_score >= settings.min_fit_score:
                    metrics.inc("leads_qualified_total")

            if state.get("disposition") != "queue":
                dropped += 1
                continue

            # Enforce the per-day proposal cap before persisting another draft.
            with get_session() as session:
                if _proposals_today(session) >= settings.max_proposals_per_day:
                    logger.info("max_proposals_per_day reached — stop queuing")
                    dropped += 1
                    break

            proposal = state.get("proposal") or {}
            verdict = state.get("verdict") or {}

            with get_session() as session:
                record = LeadRecord(
                    source=lead.source,
                    external_id=lead.external_id,
                    title=lead.title,
                    description=lead.description or "",
                    url=lead.url or "",
                    company=lead.company,
                    budget=lead.budget,
                    tags=list(lead.tags or []),
                    posted_at=lead.posted_at,
                    fit_score=fit_score,
                    status=LeadStatus.drafted,
                )
                record.proposals.append(
                    ProposalRecord(
                        body=proposal.get("body", ""),
                        suggested_rate=proposal.get("suggested_rate", "") or "",
                        cited_projects=list(proposal.get("cited_projects", []) or []),
                        status=ProposalStatus.draft,
                    )
                )
                session.add(record)
                session.flush()
                lead_db_id = record.id

            queued += 1
            metrics.inc("proposals_drafted_total")
            metrics.observe("proposal_quality", int(verdict.get("quality_score", 0)))
            queued_leads.append(
                {"id": lead_db_id, "title": lead.title, "fit_score": fit_score}
            )
    finally:
        set_cost_tracker(None)

    stats = {
        "fetched": fetched,
        "new": new,
        "queued": queued,
        "dropped": dropped,
        "skipped": skipped,
        "cost_usd": tracker.usd(),
        "budget_exhausted": budget_exhausted,
    }

    if notify:
        try:
            from interfaces.notify import send_digest

            top = sorted(queued_leads, key=lambda d: d["fit_score"], reverse=True)
            send_digest(stats, top)
        except Exception as exc:  # notification must never break a run
            logger.warning("digest notification failed: %s", exc)

    return stats


def pipeline_stats() -> dict:
    """Lead counts grouped by status (used by the MCP server, dashboard, CLI)."""
    init_db()
    counts: dict[str, int] = {status.value: 0 for status in LeadStatus}
    total = 0
    proposals = 0
    with get_session() as session:
        for lead in session.query(LeadRecord).all():
            counts[lead.status.value] = counts.get(lead.status.value, 0) + 1
            total += 1
        proposals = session.query(ProposalRecord).count()
    return {"total_leads": total, "total_proposals": proposals, "by_status": counts}


def top_queued(n: int = 5) -> list[dict]:
    """Return the highest-fit drafted leads awaiting human review."""
    init_db()
    out: list[dict] = []
    with get_session() as session:
        rows = (
            session.query(LeadRecord)
            .filter(LeadRecord.status == LeadStatus.drafted)
            .order_by(LeadRecord.fit_score.desc(), LeadRecord.id.desc())
            .limit(max(0, n))
            .all()
        )
        for lead in rows:
            out.append(
                {
                    "id": lead.id,
                    "title": lead.title,
                    "fit_score": lead.fit_score,
                    "company": lead.company,
                    "url": lead.url,
                    "source": lead.source,
                }
            )
    return out
