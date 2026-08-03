"""Qualifier agent: score a lead's fit against the user's skills (cheap model).

Scores 0-100 fit for the user's DevSecOps / Kubernetes / AI-infra / forward-deployed
profile and maps the opportunity to the portfolio repos that prove it. Uses the cheap
(sonnet) model via structured output.

Two defects fixed here on 2026-08-03
------------------------------------
1. ``PORTFOLIO_PROJECTS`` was a hand-written list of 4 names, one of which
   (``devsecops-pipeline-templates``) **did not exist**, while 8 real repos —
   including every AI-infra one the market now pays a premium for — were absent.
   Since ``matched_projects`` is filtered against this list, the qualifier could
   not cite the strongest evidence it had. The list is now derived from the RAG
   knowledge base, so adding a repo to the portfolio makes it citable with no code
   change.
2. The skills string omitted AI agents and forward-deployed/solutions engineering,
   so leads in those segments scored low on the very axis they should score highest.
"""
from __future__ import annotations

import logging
from typing import Any

from config import get_settings
from core.schemas import Lead, ScoredLead

from .llm import get_chat

logger = logging.getLogger(__name__)

#: Fallback list, used only if the RAG store can't be read. Kept deliberately short
#: and verified-to-exist; the real list comes from :func:`portfolio_projects`.
_FALLBACK_PROJECTS = [
    "agentic-sre-platform",
    "rag-platform-k8s",
    "llm-inference-k8s",
    "llm-guardrails-gateway",
    "mcp-devops-server",
    "ai-devops-agent",
    "multi-cloud-k8s-terraform",
    "devsecops-cicd-pipeline",
    "gitops-multi-env",
    "cloud-cost-optimiser",
    "devops-automation-toolkit",
    "keycloak-nginx-k8s",
    "ai-freelance-copilot",
]

#: Non-repo metadata sources in the KB (achievements, won-project notes) that must
#: not be offered to the model as citable "repos".
_NON_REPO_SOURCES = ("achievements",)


def portfolio_projects() -> list[str]:
    """Repo names the qualifier may cite, read from the RAG knowledge base.

    Derived rather than hard-coded so the list cannot drift out of sync with the
    portfolio again. Falls back to :data:`_FALLBACK_PROJECTS` if the store is
    missing or unreadable.
    """
    try:
        import json

        settings = get_settings()
        with open(settings.rag_store_path, encoding="utf-8") as fh:
            store = json.load(fh)
        names: list[str] = []
        for doc in store.get("docs", []) or []:
            source = ((doc or {}).get("metadata") or {}).get("source") or ""
            if not source or source in _NON_REPO_SOURCES or ":" in source:
                continue  # ":" filters synthetic sources like "won:ext-1"
            if source not in names:
                names.append(source)
        if names:
            return names
    except Exception as exc:  # noqa: BLE001 - never let this break qualification
        logger.warning("qualifier: could not derive projects from RAG store: %s", exc)
    return list(_FALLBACK_PROJECTS)


#: Evaluated once at import for prompt construction; ``qualify`` re-reads it so a
#: KB rebuild takes effect without a restart.
PORTFOLIO_PROJECTS = portfolio_projects()

SKILLS = (
    "DevSecOps, Kubernetes, Terraform, multi-cloud infrastructure, CI/CD security, "
    "platform engineering, LLM/AI infrastructure (RAG, inference serving, guardrails, "
    "MCP), AI agent engineering (LangGraph multi-agent systems), and "
    "forward-deployed / solutions engineering (deploying and integrating a product "
    "inside a customer's environment)"
)


def _system_prompt(projects: list[str]) -> str:
    return (
        "You are a freelance-opportunity qualifier for an engineer whose skills are: "
        f"{SKILLS}. Score how well a lead fits these skills from 0 (no fit) to 100 "
        "(perfect fit).\n"
        "Scoring guidance:\n"
        "- Score HIGH for contract/freelance day-rate work, and for AI-infra, AI "
        "agent, and forward-deployed/solutions-engineering roles — these are the "
        "target segments even when the title is unusual.\n"
        "- Score LOW for roles that only mention this tech in passing (a frontend "
        "job whose stack list happens to include Kubernetes), for non-engineering "
        "'agent' roles (customer service, insurance, estate agents), and for roles "
        "requiring on-site presence outside the UK.\n"
        "Give short concrete reasons and list which of the user's portfolio repos "
        f"prove the fit. Valid repos: {', '.join(projects)}. "
        "Only list repos that are genuinely relevant."
    )


_SYSTEM = _system_prompt(PORTFOLIO_PROJECTS)


def qualify(lead: Lead, retriever: Any = None, chat: Any = None) -> ScoredLead:
    """Score ``lead`` and return a :class:`ScoredLead`.

    ``retriever`` is accepted for interface symmetry (unused here). ``chat`` lets
    tests inject a ``FakeChat`` so no API key is needed.
    """
    settings = get_settings()
    model = get_chat(settings.model_sonnet, chat=chat)
    structured = model.with_structured_output(ScoredLead)

    projects = portfolio_projects()
    text = (
        f"Title: {lead.title}\n"
        f"Company: {lead.company or 'unknown'}\n"
        f"Budget: {lead.budget or 'unknown'}\n"
        f"Tags: {', '.join(lead.tags)}\n"
        f"Description:\n{lead.description}"
    )
    messages = [
        {"role": "system", "content": _system_prompt(projects)},
        {"role": "user", "content": text},
    ]
    result: ScoredLead = structured.invoke(messages)

    # The model returns the scoring fields; ensure the lead is the one we passed.
    return ScoredLead(
        lead=lead,
        fit_score=max(0, min(100, int(result.fit_score))),
        reasons=result.reasons,
        matched_projects=[p for p in result.matched_projects if p in projects],
    )
