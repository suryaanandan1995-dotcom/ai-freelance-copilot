"""Qualifier agent: score a lead's fit against the user's skills (cheap model).

Scores 0-100 fit for the user's DevSecOps / Kubernetes / AI-infra profile and
maps the opportunity to the portfolio repos that prove it. Uses the cheap
(sonnet) model via structured output.
"""
from __future__ import annotations

from typing import Any

from config import get_settings
from core.schemas import Lead, ScoredLead

from .llm import get_chat

# Portfolio repos the qualifier can cite as proof of fit.
PORTFOLIO_PROJECTS = [
    "llm-guardrails-gateway",
    "multi-cloud-k8s-terraform",
    "devsecops-pipeline-templates",
    "ai-freelance-copilot",
]

SKILLS = (
    "DevSecOps, Kubernetes, Terraform, multi-cloud infrastructure, CI/CD security, "
    "LLM/AI infrastructure, guardrails and platform engineering"
)

_SYSTEM = (
    "You are a freelance-opportunity qualifier for an engineer whose skills are: "
    f"{SKILLS}. Score how well a lead fits these skills from 0 (no fit) to 100 "
    "(perfect fit). Give short concrete reasons and list which of the user's "
    f"portfolio repos prove the fit. Valid repos: {', '.join(PORTFOLIO_PROJECTS)}. "
    "Only list repos that are genuinely relevant."
)


def qualify(lead: Lead, retriever: Any = None, chat: Any = None) -> ScoredLead:
    """Score ``lead`` and return a :class:`ScoredLead`.

    ``retriever`` is accepted for interface symmetry (unused here). ``chat`` lets
    tests inject a ``FakeChat`` so no API key is needed.
    """
    settings = get_settings()
    model = get_chat(settings.model_sonnet, chat=chat)
    structured = model.with_structured_output(ScoredLead)

    text = (
        f"Title: {lead.title}\n"
        f"Company: {lead.company or 'unknown'}\n"
        f"Budget: {lead.budget or 'unknown'}\n"
        f"Tags: {', '.join(lead.tags)}\n"
        f"Description:\n{lead.description}"
    )
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": text},
    ]
    result: ScoredLead = structured.invoke(messages)

    # The model returns the scoring fields; ensure the lead is the one we passed.
    return ScoredLead(
        lead=lead,
        fit_score=max(0, min(100, int(result.fit_score))),
        reasons=result.reasons,
        matched_projects=[p for p in result.matched_projects if p in PORTFOLIO_PROJECTS],
    )
