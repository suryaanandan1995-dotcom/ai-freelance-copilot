"""MCP server exposing the freelance copilot to any MCP-capable AI client.

Runs a FastMCP stdio server with a small, well-typed toolset so an agent (Claude
Desktop, an IDE, a custom client) can drive the copilot: trigger a discovery run,
read the review queue, inspect a draft, fetch stats, and record that a HUMAN
submitted a proposal on the platform.

There is deliberately NO tool that submits to any freelance platform — the only
"submit" tool here just records, in this CRM, that a human already did so.
"""
from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

import analytics
from db.models import LeadRecord, LeadStatus, ProposalStatus
from db.session import get_session, init_db
from pipeline import pipeline_stats as _pipeline_stats
from pipeline import run_pipeline, top_queued

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai-freelance-copilot.mcp")

mcp = FastMCP("ai-freelance-copilot")


@mcp.tool()
def fetch_leads(limit: int = 25) -> dict:
    """Run one discovery pass: fetch, qualify, research, and DRAFT proposals.

    Returns run statistics. Never submits anything to any platform.
    """
    return run_pipeline(limit=limit)


@mcp.tool()
def list_queue() -> list[dict]:
    """List the highest-fit drafted proposals awaiting human review."""
    return top_queued(n=10)


@mcp.tool()
def get_proposal(lead_id: int) -> dict:
    """Fetch the latest draft proposal for a lead (body, rate, cited projects)."""
    init_db()
    with get_session() as session:
        lead = session.get(LeadRecord, lead_id)
        if lead is None:
            return {"error": f"lead {lead_id} not found"}
        proposal = lead.proposals[-1] if lead.proposals else None
        return {
            "lead_id": lead.id,
            "title": lead.title,
            "url": lead.url,
            "fit_score": lead.fit_score,
            "status": lead.status.value,
            "proposal": None
            if proposal is None
            else {
                "body": proposal.body,
                "suggested_rate": proposal.suggested_rate,
                "cited_projects": proposal.cited_projects,
                "status": proposal.status.value,
            },
        }


@mcp.tool()
def mark_submitted(lead_id: int) -> dict:
    """Record that a HUMAN submitted this proposal on the platform.

    Sets the lead + its latest proposal to ``submitted`` and stamps the time.
    Does NOT contact any external platform.
    """
    import datetime as _dt

    init_db()
    with get_session() as session:
        lead = session.get(LeadRecord, lead_id)
        if lead is None:
            return {"error": f"lead {lead_id} not found"}
        lead.status = LeadStatus.submitted
        now = _dt.datetime.now(_dt.UTC)
        for proposal in lead.proposals:
            proposal.status = ProposalStatus.submitted
            proposal.submitted_at = now
        return {"lead_id": lead_id, "status": LeadStatus.submitted.value}


@mcp.tool()
def pipeline_stats() -> dict:
    """Return lead/proposal counts grouped by status."""
    return _pipeline_stats()


# --- read-only analytics (so an agent can inspect its own state) ---------------
@mcp.tool()
def funnel_stats() -> dict:
    """Return the outreach funnel: emailed, replied, reply_rate, won/lost/in_progress,
    suppressed, total Claude cost (USD), and emails sent today."""
    init_db()
    return analytics.funnel_stats()


@mcp.tool()
def list_outreach(limit: int = 50) -> list[dict]:
    """List recently sent cold emails (email, subject, status, replied, follow-ups)."""
    init_db()
    return analytics.outreach_list(limit=limit)


@mcp.tool()
def list_conversations() -> list[dict]:
    """List reply threads grouped by prospect email, messages in chronological order."""
    init_db()
    return analytics.conversations()


@mcp.tool()
def run_history(limit: int = 20) -> list[dict]:
    """List recent workflow runs (workflow, ok, cost_usd, stats, error, created_at)."""
    init_db()
    return analytics.recent_runs(limit=limit)


@mcp.tool()
def current_strategy() -> dict | None:
    """Return the active self-optimizer strategy the system is running now
    (version, params like pitch_variant/subject_style/fit_threshold,
    baseline_reply_rate, note), or None if none has been recorded yet."""
    init_db()
    return analytics.current_strategy()


@mcp.tool()
def strategy_history(limit: int = 20) -> list[dict]:
    """List recent self-optimizer strategy versions, newest first
    (version, params, active, baseline_reply_rate, note, created_at)."""
    init_db()
    return analytics.strategy_history(limit=limit)


if __name__ == "__main__":
    mcp.run()
