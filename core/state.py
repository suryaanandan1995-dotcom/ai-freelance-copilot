"""LangGraph orchestration state.

The graph mutates a single typed dict as it flows:
Scout -> Qualifier -> Researcher -> Proposal Writer -> Compliance -> queue.

Nodes return partial updates; the disposition string drives conditional edges
(LangGraph does not merge mutations made inside edge functions, so disposition
is computed in a node and only *read* by the edge).
"""
from __future__ import annotations

from typing import Any, TypedDict


class CopilotState(TypedDict, total=False):
    lead: dict[str, Any]          # serialized core.schemas.Lead
    scored: dict[str, Any] | None  # serialized ScoredLead
    research: dict[str, Any] | None  # serialized CompanyResearch
    proposal: dict[str, Any] | None  # serialized ProposalDraft
    verdict: dict[str, Any] | None   # serialized ComplianceVerdict
    disposition: str  # one of: "queue" | "drop" | "needs_research"
    errors: list[str]
