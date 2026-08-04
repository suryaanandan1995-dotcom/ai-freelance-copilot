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


def _emails_today(session: Any) -> int:
    """Count every outbound email sent since UTC midnight (shared daily cap).

    Delegates to ``outreach.quota`` so cold emails and follow-ups are counted against
    one budget. Counting only this channel's sends made the cap per-channel, which is
    not what protects a sending domain.
    """
    from outreach.quota import emails_sent_today

    return emails_sent_today(session)


def _maybe_email_lead(
    *,
    lead: Any,
    scored_state: dict,
    research_state: dict,
    fit_score: int,
    lead_db_id: int | None,
    settings: Any,
    retriever: Any,
    chat: Any,
) -> str:
    """Attempt one cold email for a freshly queued lead.

    Returns ``"sent"`` on a successful send, otherwise a short skip-reason string
    (e.g. ``"no_email"``, ``"low_fit"``, ``"duplicate"``, ``"suppressed"``,
    ``"daily_cap"``, ``"send_failed"``). All guards (gate, fit, dedupe,
    suppression, daily cap) are enforced here. Never raises for control flow.
    """
    from db.models import OutreachRecord
    from outreach.extract import find_deliverable_email
    from outreach.pitch import draft_email
    from outreach.sender import send_outreach
    from outreach.suppression import is_suppressed

    email = find_deliverable_email(lead)
    if not email:
        return "no_email"
    if fit_score < settings.outreach_min_fit:
        return "low_fit"
    if is_suppressed(email):
        return "suppressed"

    with get_session() as session:
        already = (
            session.query(OutreachRecord.id)
            .filter(OutreachRecord.email == email)
            .first()
        )
        if already is not None:
            return "duplicate"
        if _emails_today(session) >= settings.max_emails_per_day:
            return "daily_cap"

    from core.schemas import CompanyResearch, ScoredLead

    scored = ScoredLead(**scored_state) if scored_state else None
    if scored is None:
        return "no_score"
    research = CompanyResearch(**research_state) if research_state else CompanyResearch()

    draft = draft_email(scored, research, retriever=retriever, chat=chat)
    sent = send_outreach(email, draft["subject"], draft["body"])

    with get_session() as session:
        session.add(
            OutreachRecord(
                lead_id=lead_db_id,
                email=email,
                subject=draft["subject"],
                status="sent" if sent else "failed",
            )
        )
    return "sent" if sent else "send_failed"


def _fit_summary(scores: list[int], threshold: int) -> dict:
    """Summarise the run's fit-score distribution and name the likely bottleneck.

    ``dropped: 34`` on its own is unactionable. The two causes need opposite fixes:

    * scores clustered just *below* the threshold  -> the threshold is too strict;
    * scores clustered far below it                -> the sources are off-ICP.

    ``near_miss`` (within 10 points of the threshold) is the number that decides
    which one it is, so it is computed here rather than left to be eyeballed.
    """
    if not scores:
        return {"n": 0, "bottleneck": "no leads scored"}

    ordered = sorted(scores)
    n = len(ordered)

    def pct(p: float) -> int:
        return ordered[min(n - 1, max(0, int(round(p * (n - 1)))))]

    passed = sum(1 for s in ordered if s >= threshold)
    near_miss = sum(1 for s in ordered if threshold - 10 <= s < threshold)

    if passed:
        bottleneck = "none — leads are clearing the bar"
    elif near_miss >= max(2, n // 5):
        bottleneck = (
            f"threshold: {near_miss}/{n} scored within 10 points of {threshold}. "
            "Lowering min_fit_score is likely to help more than changing sources."
        )
    else:
        bottleneck = (
            f"sources: 0/{n} cleared {threshold} and only {near_miss} came close. "
            "The lead mix is off-ICP; fix targeting, not the threshold."
        )

    return {
        "n": n,
        "threshold": threshold,
        "min": ordered[0],
        "p50": pct(0.5),
        "p90": pct(0.9),
        "max": ordered[-1],
        "passed": passed,
        "near_miss": near_miss,
        "bottleneck": bottleneck,
    }


def _per_source_summary(rows: dict[str, dict], threshold: int) -> dict:
    """Attribute the funnel to each source, so targeting can be fixed with evidence.

    ``_fit_summary`` can say "the lead mix is off-ICP" but not WHICH sources produced
    the off-ICP mix. With 7 sources enabled that leaves the actual fix — drop the dead
    ones, expand the productive one — as guesswork, and a run that fetches 46 leads and
    qualifies 0 looks equally like "all sources are mediocre" and "six are useless and
    one is good". Those need opposite responses.

    A source that yields leads but never a *contactable* one is a distinct failure from
    one that yields nothing: the first is costing LLM scoring spend every run for leads
    that can never be emailed, and it is invisible in the totals.
    """
    out: dict[str, dict] = {}
    for name, row in sorted(rows.items()):
        scores = sorted(row.get("scores") or [])
        entry = {
            "fetched": row.get("fetched", 0),
            "new": row.get("new", 0),
            "contactable": row.get("contactable", 0),
            "queued": row.get("queued", 0),
            "scored": len(scores),
        }
        if scores:
            entry["p50"] = scores[len(scores) // 2]
            entry["max"] = scores[-1]
            entry["passed"] = sum(1 for s in scores if s >= threshold)
        # Name the per-source verdict rather than leaving it to be inferred from
        # five numbers. Ordered most- to least-severe.
        if not entry["fetched"]:
            entry["verdict"] = "dead: fetched nothing"
        elif not entry["new"]:
            entry["verdict"] = "stale: every lead already seen"
        elif not entry["contactable"]:
            entry["verdict"] = (
                "unreachable: leads have no email, so scoring them is wasted spend"
            )
        elif not scores:
            entry["verdict"] = "unscored: pre-gated before reaching the model"
        elif not entry.get("passed"):
            entry["verdict"] = f"off-ICP: best score {entry['max']} < {threshold}"
        else:
            entry["verdict"] = f"productive: {entry['passed']} cleared {threshold}"
        out[name] = entry
    return out


def run_pipeline(
    limit: int | None = None,
    sources: list | None = None,
    retriever: Any = None,
    chat: Any = None,
    notify: bool = False,
    auto_email: bool = False,
) -> dict:
    """Run one end-to-end pipeline pass and return run statistics.

    Returns ``{fetched, new, queued, dropped, skipped, cost_usd, budget_exhausted,
    emailed, emailed_skipped}``.

    When ``auto_email`` is True, each freshly queued lead that exposes a public
    contact email AND clears ``outreach_min_fit`` is sent a single short cold
    intro email — deduped against ``OutreachRecord`` (never emailed twice),
    suppression-list aware, and capped at ``max_emails_per_day``. This is the
    ONLY auto-send path; Upwork/LinkedIn submission stays human-only. The actual
    send is itself gated by ``settings.auto_email`` + SMTP config in the sender,
    so passing ``auto_email=True`` here is safe by default.
    """
    settings = get_settings()
    init_db()

    tracker = CostTracker(budget_usd=settings.max_usd_per_run)
    # Install the tracker so the metered LLM wrapper meters + budget-gates every call.
    from agents.llm import set_cost_tracker

    set_cost_tracker(tracker)

    fetched = new = queued = dropped = skipped = 0
    emailed = 0
    emailed_skipped: dict[str, int] = {}
    budget_exhausted = False
    queued_leads: list[dict] = []
    # Contactability is measured for EVERY new lead, before any LLM spend, because
    # it is the funnel's real bottleneck: 18 of 25 drafted proposals were discarded
    # at the contact step after being fully researched and written at Opus prices.
    contactable = 0
    uncontactable_skipped = 0
    # Every fit score seen this run. Without this, "dropped: 34" is unactionable:
    # a run cannot tell you whether 34 leads scored 68 (threshold too strict) or 12
    # (sources off-ICP), which are opposite fixes. Recorded so min_fit_score can be
    # tuned from evidence instead of guessed.
    fit_scores: list[int] = []
    # Per-source funnel attribution. Keyed by lead.source; see _per_source_summary.
    by_source: dict[str, dict] = {}

    def _src(name: str) -> dict:
        return by_source.setdefault(
            name,
            {"fetched": 0, "new": 0, "contactable": 0, "queued": 0, "scores": []},
        )

    def _skip_email(reason: str) -> None:
        emailed_skipped[reason] = emailed_skipped.get(reason, 0) + 1

    try:
        from agents.graph import run_lead
        from sources.registry import fetch_all, get_default_sources

        srcs = sources if sources is not None else get_default_sources()
        per_source = max(1, (limit or settings.max_leads_per_run))

        # Seed a row for every ENABLED source, including ones that yield nothing.
        # Attribution built only from returned leads would omit a dead source entirely,
        # and an absent row reads as "not a problem" — the exact failure this reporting
        # exists to expose. A source is enabled, so it must appear in the report.
        for source in srcs:
            _src(getattr(source, "name", source.__class__.__name__))

        leads = fetch_all(srcs, per_source_limit=per_source)

        cap = limit if limit is not None else settings.max_leads_per_run
        if cap is not None and len(leads) > cap:
            leads = leads[:cap]

        fetched = len(leads)
        metrics.inc("leads_fetched_total", fetched)

        # Counted after the run cap (so the per-source figures sum to ``fetched``) but
        # before the seen-already filter, which keeps "returns only leads we've already
        # seen" distinguishable from "returns nothing at all".
        for lead in leads:
            _src(lead.source)["fetched"] += 1

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
            _src(lead.source)["new"] += 1

            # --- cheap gates BEFORE any LLM spend ---------------------------
            # Extracting a contact costs one regex pass + one cached DNS lookup;
            # researching and drafting costs Opus tokens. Doing them in that order
            # is the difference between paying to discover a lead is unsendable and
            # discovering it for free. Only applies when auto-emailing is the goal —
            # with auto_email off, drafts for human submission are still valuable.
            has_contact = False
            try:
                from outreach.extract import find_deliverable_email

                has_contact = find_deliverable_email(lead) is not None
            except Exception as exc:  # extraction must never break the loop
                logger.warning("contact pre-check failed for %s: %s", lead.url, exc)
            if has_contact:
                contactable += 1
                _src(lead.source)["contactable"] += 1
            elif auto_email and settings.require_contact_before_draft:
                uncontactable_skipped += 1
                _skip_email("no_email_pregate")
                continue

            try:
                state = run_lead(lead, retriever=retriever, chat=chat)
            except BudgetExhausted:
                budget_exhausted = True
                logger.warning("budget exhausted at $%.4f — stopping run", tracker.usd())
                break

            scored = state.get("scored") or {}
            fit_score = int(scored.get("fit_score", 0))
            if scored:
                fit_scores.append(fit_score)
                _src(lead.source)["scores"].append(fit_score)
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
            _src(lead.source)["queued"] += 1
            metrics.inc("proposals_drafted_total")
            metrics.observe("proposal_quality", int(verdict.get("quality_score", 0)))
            queued_leads.append(
                {"id": lead_db_id, "title": lead.title, "fit_score": fit_score}
            )

            # --- auto cold-email outreach (email-only; never platform submit) ---
            if auto_email:
                try:
                    result = _maybe_email_lead(
                        lead=lead,
                        scored_state=state.get("scored") or {},
                        research_state=state.get("research") or {},
                        fit_score=fit_score,
                        lead_db_id=lead_db_id,
                        settings=settings,
                        retriever=retriever,
                        chat=chat,
                    )
                    if result == "sent":
                        emailed += 1
                    else:
                        _skip_email(result)
                except Exception as exc:  # email must never break the lead loop
                    logger.warning("auto-email failed for lead %s: %s", lead_db_id, exc)
                    _skip_email("error")
    finally:
        set_cost_tracker(None)

    stats = {
        "fetched": fetched,
        "new": new,
        # `contactable` is the leading indicator for this whole system: no amount of
        # proposal quality matters if nothing is reachable. Surfaced as a top-level
        # stat (not buried in emailed_skipped) so the weekly review can act on it.
        "contactable": contactable,
        "uncontactable_skipped": uncontactable_skipped,
        "queued": queued,
        "dropped": dropped,
        "skipped": skipped,
        "emailed": emailed,
        "emailed_skipped": emailed_skipped,
        "cost_usd": tracker.usd(),
        "budget_exhausted": budget_exhausted,
    }
    stats["fit"] = _fit_summary(fit_scores, settings.min_fit_score)
    stats["by_source"] = _per_source_summary(by_source, settings.min_fit_score)

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
