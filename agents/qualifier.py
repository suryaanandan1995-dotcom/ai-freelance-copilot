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
from core.schemas import FitVerdict, Lead, ScoredLead

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
    """Build the qualifier's system prompt.

    Three properties of this text are load-bearing, and each replaced something that
    measurably distorted the scores.

    **The scale is anchored.** It used to give the model only "0 (no fit) to 100
    (perfect fit)" and then a list of things to deduct for. An LLM handed an
    unanchored range and a pile of caveats regresses to the low-middle, which is what
    the funnel measured: p50 28, p90 58. The bands below name what each decade *is*,
    and state that 70 is the threshold at which a human writes to the lead — the
    number ``min_fit_score`` actually uses, which the model was never told.

    **Missing information is not a deduction.** The prompt asks the model to reward
    day-rate contract work, but only one of seven adapters populates ``budget``, so
    the user message says ``Budget: unknown`` for nearly every lead. That made the
    rubric asymmetric: every reason to deduct was checkable in the input and the main
    reason to award was not. Terse board listings were capped low for being terse.

    **Remote is the ICP, not the UK.** The old text said to score LOW for "on-site
    presence outside the UK" — written when the only source was a UK feed. Sourcing
    now spans ten country endpoints across the US, EU and ANZ, all of them queried
    with a remote filter, so that one clause penalised every lead from the nine new
    markets for being foreign. The deduction now applies to what actually disqualifies
    a role: a hard on-site requirement.
    """
    return (
        "You are a freelance-opportunity qualifier for an engineer whose skills are: "
        f"{SKILLS}. Score how well a lead fits these skills from 0 (no fit) to 100 "
        "(perfect fit).\n\n"
        "Anchor the scale. 70 is the action threshold: at or above 70 the engineer "
        "will personally write to this lead. Score 70+ when you would advise that, "
        "and do not withhold it from a lead that deserves it.\n"
        "  90-100  Dead centre. A contract/day-rate engagement, or an FDE/solutions "
        "role, whose core deliverable IS one of the skills above: running LLM "
        "inference or a RAG system in production, building an agent platform, "
        "hardening a Kubernetes/CI-CD platform, deploying an AI product inside a "
        "customer's stack.\n"
        "  75-89   Strong. The role's main axis is one of these skills, but something "
        "is off-axis: permanent rather than contract, a seniority or scope mismatch, "
        "or one named technology the engineer has not used. A 'Senior AI Engineer' "
        "building LLM/RAG features, or a 'Senior Platform/SRE' role on Kubernetes and "
        "Terraform, belongs HERE.\n"
        "  60-74   Real overlap, secondary. Adjacent engineering where these skills "
        "are about half the job.\n"
        "  40-59   Weak. Engineering, and the tech appears, but it is not what the "
        "role is for.\n"
        "  20-39   Mostly irrelevant, or a diffuse multi-role posting where no single "
        "listed role fits.\n"
        "  0-19    Not applicable: non-engineering, or a non-technical 'agent' role "
        "(customer service, insurance, estate agents).\n\n"
        "Further guidance:\n"
        "- Score HIGH for AI-infra, AI-agent, and forward-deployed/solutions-"
        "engineering roles — these are the target segments even when the title is "
        "unusual.\n"
        "- Missing information is NOT a negative. Most of these feeds publish no "
        "budget at all. When Budget is 'unknown', or the description omits scope, "
        "terms or duration, score the ROLE on the evidence present and do not deduct "
        "for the gap. 'Budget: unknown' is the normal case and must never cap a score.\n"
        "- Contract framing is a bonus, not a requirement. Day-rate or freelance "
        "framing adds to the score; permanent or full-time framing does NOT subtract, "
        "because these segments hire permanently and contract-to-hire, and the "
        "engineer's proof transfers either way.\n"
        "- Location: the engineer works REMOTELY and the pipeline sources remote "
        "roles worldwide. Deduct only when a role REQUIRES on-site attendance. "
        "'Remote', 'Remote US', 'hybrid', or an unstated location is not a deduction, "
        "and neither is the country.\n"
        "- Score the strongest SINGLE role in the posting. Some listings (Hacker News "
        "'who is hiring' comments) advertise several roles at once; judge the best-"
        "fitting one, not the average.\n"
        "- Still score LOW for roles that only mention this tech in passing — a "
        "frontend job whose stack list happens to include Kubernetes.\n\n"
        "Give short concrete reasons and list which of the user's portfolio repos "
        f"prove the fit. Valid repos: {', '.join(projects)}. "
        "Only list repos that are genuinely relevant."
    )


_SYSTEM = _system_prompt(PORTFOLIO_PROJECTS)


def _format_evidence(chunks: list[dict[str, Any]]) -> str:
    if not chunks:
        return "(none retrieved)"
    return "\n".join(f"- {c.get('text', '')} [{c.get('source', '')}]" for c in chunks)


def qualify(lead: Lead, retriever: Any = None, chat: Any = None) -> ScoredLead:
    """Score ``lead`` and return a :class:`ScoredLead`.

    ``chat`` lets tests inject a ``FakeChat`` so no API key is needed.

    ``retriever`` was previously accepted and then **discarded** — the docstring said
    "for interface symmetry (unused here)" while ``build_graph`` dutifully passed one
    in. So the model was asked to judge *fit* while holding only one side of the
    comparison: a bare list of repo *names*. It could not know that
    ``rag-platform-k8s`` is a production RAG-on-Kubernetes build, so a lead asking for
    exactly that got no lift. Both downstream agents that write prose already retrieve
    real chunks (``proposal_writer``, ``outreach.pitch``); the one agent whose entire
    job is comparison was the one working from names. Retrieval failure is non-fatal:
    a missing KB should score a lead conservatively, not crash the run.
    """
    settings = get_settings()
    model = get_chat(settings.model_sonnet, chat=chat)
    # FitVerdict, not ScoredLead: the model is asked for the three fields it decides and
    # nothing else. Asking for ``lead`` too — a field this function overwrites six lines
    # below — is what killed run 32339343714. See :class:`core.schemas.FitVerdict`.
    structured = model.with_structured_output(FitVerdict)

    projects = portfolio_projects()

    if retriever is None:
        try:
            from rag.retriever import get_retriever  # lazy: keeps module import-safe

            retriever = get_retriever()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("qualifier: no retriever available (%s)", exc)
    evidence: list[dict[str, Any]] = []
    if retriever is not None:
        try:
            query = f"{lead.title} {' '.join(lead.tags)}"
            evidence = retriever.retrieve(query, 3) or []
        except Exception as exc:  # retrieval must never fail a run
            logger.warning("qualifier: portfolio retrieval failed (%s)", exc)

    text = (
        f"Title: {lead.title}\n"
        f"Company: {lead.company or 'unknown'}\n"
        f"Budget: {lead.budget or 'unknown'}\n"
        f"Tags: {', '.join(lead.tags)}\n"
        f"Description:\n{lead.description}\n\n"
        "Portfolio evidence (what the engineer has actually built — judge fit "
        f"against this, not against the repo names alone):\n{_format_evidence(evidence)}"
    )
    messages = [
        {"role": "system", "content": _system_prompt(projects)},
        {"role": "user", "content": text},
    ]
    result: FitVerdict = structured.invoke(messages)

    # The model returns the scoring fields; the lead is the one we passed, never echoed.
    return ScoredLead(
        lead=lead,
        fit_score=max(0, min(100, int(result.fit_score))),
        reasons=result.reasons,
        matched_projects=[p for p in result.matched_projects if p in projects],
    )
