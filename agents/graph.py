"""LangGraph orchestrator wiring the agent pipeline together.

Flow:

    qualify ──(fit_score >= min_fit_score)──> research ──> write ──> review ──> END
        └──────(below threshold)──────────────────────────────────────────────> END

State is the serialized :class:`core.state.CopilotState`. Each node returns a
partial dict update. LangGraph does *not* merge mutations made inside edge
functions, so ``disposition`` is computed inside the ``review`` node (and a small
``drop`` node for the low-fit branch) and the conditional edge only *reads* it.
"""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph

from config import get_settings
from core.schemas import (
    CompanyResearch,
    Lead,
    ProposalDraft,
    ScoredLead,
)
from core.state import CopilotState

from .compliance import review
from .proposal_writer import write_proposal
from .qualifier import qualify
from .researcher import research as research_lead


def build_graph(retriever: Any = None, chat: Any = None) -> Any:
    """Compile and return the pipeline graph.

    ``retriever`` / ``chat`` are captured in the node closures so tests can inject
    a fake retriever and a ``FakeChat`` for a fully offline run.
    """
    settings = get_settings()

    def qualify_node(state: CopilotState) -> dict[str, Any]:
        lead = Lead(**state["lead"])
        scored = qualify(lead, retriever=retriever, chat=chat)
        return {"scored": scored.model_dump()}

    def route_after_qualify(state: CopilotState) -> str:
        scored = state.get("scored") or {}
        if int(scored.get("fit_score", 0)) < settings.min_fit_score:
            return "drop"
        # ``draft_allowed=False`` means "score this, but never pay to draft it" — the
        # caller already knows the lead has no deliverable contact. It is set here
        # rather than by skipping the lead entirely because qualification is the cheap
        # Sonnet call and drafting is the Opus one: a source whose leads are all
        # uncontactable was previously never scored at all, so the funnel reported
        # "unreachable: scoring them is wasted spend" about a source it had refused to
        # score. Absent key means allowed, so every existing caller is unaffected.
        if state.get("draft_allowed", True) is False:
            return "drop"
        return "research"

    def research_node(state: CopilotState) -> dict[str, Any]:
        lead = Lead(**state["lead"])
        enrichment = research_lead(lead, chat=chat)
        return {"research": enrichment.model_dump()}

    def write_node(state: CopilotState) -> dict[str, Any]:
        scored = ScoredLead(**state["scored"])
        enrichment = CompanyResearch(**(state.get("research") or {}))
        draft = write_proposal(scored, enrichment, retriever=retriever, chat=chat)
        return {"proposal": draft.model_dump()}

    def review_node(state: CopilotState) -> dict[str, Any]:
        draft = ProposalDraft(**state["proposal"])
        verdict = review(draft)
        # Compute disposition INSIDE the node (edges can't merge mutations).
        disposition = "queue" if verdict.approved else "drop"
        return {"verdict": verdict.model_dump(), "disposition": disposition}

    def drop_node(state: CopilotState) -> dict[str, Any]:
        return {"disposition": "drop"}

    graph = StateGraph(CopilotState)
    graph.add_node("qualify", qualify_node)
    graph.add_node("research", research_node)
    graph.add_node("write", write_node)
    graph.add_node("review", review_node)
    graph.add_node("drop", drop_node)

    graph.set_entry_point("qualify")
    graph.add_conditional_edges(
        "qualify",
        route_after_qualify,
        {"research": "research", "drop": "drop"},
    )
    graph.add_edge("research", "write")
    graph.add_edge("write", "review")
    graph.add_edge("review", END)
    graph.add_edge("drop", END)

    return graph.compile()


def run_lead(
    lead: Lead,
    retriever: Any = None,
    chat: Any = None,
    draft_allowed: bool = True,
) -> CopilotState:
    """Run a single ``lead`` end-to-end and return the final state dict.

    ``draft_allowed=False`` runs qualification and stops there, whatever the score:
    the lead is scored (cheap, and the only way the funnel report can tell an
    off-ICP source from one it never looked at) but never researched or drafted
    (expensive, and pointless for a lead nobody can be emailed about).
    """
    app = build_graph(retriever=retriever, chat=chat)
    initial: CopilotState = {
        "lead": lead.model_dump(),
        "draft_allowed": draft_allowed,
        "errors": [],
    }
    return app.invoke(initial)
