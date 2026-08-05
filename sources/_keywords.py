"""Shared keyword matching used by several source adapters.

The copilot targets DevSecOps / cloud / SRE / platform work **and** the AI-infra
segment (LLM serving, RAG, agents, MCP, LLMOps). Adapters use
:func:`matches_keywords` to filter listings down to those with a *genuine*
signal (not merely a generic word like "remote" or "cloud" appearing anywhere)
and :func:`extract_tags` to derive coarse tags.

Why AI-infra keywords carry weight here
---------------------------------------
Market data (ITJobsWatch, 6 months to 2026-08-03) says the two segments are
moving in opposite directions:

* Kubernetes contract roles: £535/day median, **rank fell 6 places** YoY.
* LLM contract roles: £550/day median, vacancies **+247% YoY** (295 -> 1,024),
  rank up 143 places, 75th percentile £650 and 90th £784.

The original keyword list contained no LLM/RAG/agent/MCP terms at all, so the
pipeline systematically under-scored the growing, better-paying segment — the one
this portfolio is actually differentiated in — while over-scoring the flat one.

Matching is word-boundary-ish so that short terms don't match inside unrelated
words (e.g. "sre" must not match inside "stores", "aws" not inside "flaws").
"""
from __future__ import annotations

import re

# Lowercased role/skill keywords that constitute a genuine relevant signal.
# Multi-word phrases are matched as phrases; short tokens are matched on word
# boundaries. Order here is also the order tags are emitted in.
KEYWORDS: tuple[str, ...] = (
    # --- AI infrastructure / LLMOps (the growth segment) ---
    "llm",
    "llmops",
    "large language model",
    "generative ai",
    "genai",
    "rag",
    "retrieval-augmented",
    "retrieval augmented",
    "vector database",
    "agentic",
    "ai agent",
    "multi-agent",
    "mcp server",
    "model context protocol",
    "ai infrastructure",
    "ai platform",
    "mlops",
    "inference",
    "vllm",
    "fine-tuning",
    "ai security",
    "guardrails",
    "prompt engineering",
    "langchain",
    "langgraph",
    "agent orchestration",
    "tool calling",
    "evals",
    # --- Forward-Deployed Engineering / solutions (customer-facing delivery) ---
    # A distinct, well-paid title at AI companies (Palantir originated it; OpenAI,
    # Anthropic, Scale and most AI startups now hire it): an engineer who deploys
    # and integrates the product inside the customer's environment. It maps to the
    # same skill set as contract delivery work — build against someone else's stack,
    # under their constraints, on a deadline — and it is frequently contract or
    # contract-to-hire, so it belongs in this pipeline's ICP.
    "forward deployed",
    "forward-deployed",
    "fde",
    "solutions engineer",
    "solutions architect",
    "implementation engineer",
    "deployment engineer",
    "customer engineer",
    "integration engineer",
    "professional services engineer",
    # --- DevSecOps / platform / cloud (the base segment) ---
    "kubernetes",
    "k8s",
    "devsecops",
    "devops",
    "dev ops",
    "sre",
    "site reliability",
    "platform engineer",
    "platform engineering",
    "infrastructure engineer",
    "cloud engineer",
    "cloud security",
    "security engineer",
    "terraform",
    "ci/cd",
    "cicd",
    "aws",
    "gcp",
    "azure",
    "eks",
    "gke",
    "aks",
    "docker",
    "helm",
    "argocd",
    "argo cd",
    "istio",
    "ansible",
    "observability",
    "prometheus",
)

#: Keywords that are *only* meaningful in an engineering context. Bare "agent"
#: and "inference" are excluded from KEYWORDS entirely because live sampling
#: showed them matching "Customer Service Agent", "Helpdesk Support Agent" and
#: "Insurance Agent" — the exact false-positive class that wastes LLM scoring
#: spend. Phrases like "ai agent"/"agentic" are safe and cover the real signal.
#:
#: Titles matching these are strong-signal: an LLM/platform term in the *title*
#: is far more predictive than the same term buried in boilerplate.
STRONG_TITLE_KEYWORDS: frozenset[str] = frozenset(
    {
        "llm",
        "llmops",
        "generative ai",
        "genai",
        "rag",
        "agentic",
        "ai agent",
        "multi-agent",
        "ai infrastructure",
        "ai platform",
        "mlops",
        "mcp server",
        "langgraph",
        # FDE / solutions titles. "solutions architect" is included but note it is
        # the weakest of these — it is also used for pre-sales roles with no build
        # component, so the qualifier agent still has to judge the body.
        "forward deployed",
        "forward-deployed",
        "fde",
        "solutions engineer",
        "solutions architect",
        "implementation engineer",
        "deployment engineer",
        "customer engineer",
        "integration engineer",
        "kubernetes",
        "k8s",
        "devops",
        "devsecops",
        "sre",
        "site reliability",
        "platform engineer",
        "platform engineering",
        "infrastructure engineer",
        "cloud engineer",
        "terraform",
    }
)


def _compile(kw: str) -> re.Pattern[str]:
    """Word-boundary-ish matcher for a keyword.

    Uses lookaround on alphanumerics so that keywords containing non-word
    characters (``ci/cd``) still match, while short tokens (``sre``, ``aws``)
    only match as standalone words.
    """
    return re.compile(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])")


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kw, _compile(kw)) for kw in KEYWORDS
)


def matches_keywords(*texts: str | None) -> bool:
    """True only if a genuinely DevSecOps-relevant term appears in the texts."""
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return False
    return any(pat.search(blob) for _, pat in _PATTERNS)


#: Job-title words that mean "not an engineering role", however good the tech
#: keywords in the body look.
#:
#: The gate above searches the title *and description together*, which is deliberate —
#: a genuine role often names its stack only in the body. But it means one tech word
#: anywhere in a long description passes the whole listing, and job descriptions are
#: full of them: a live Adzuna fetch on 2026-08-05 qualified "Marketing Manager"
#: (its body mentions "agentic"), "Account-Based Marketing Mgr" ("AWS" — at AWS),
#: "Strategic Sourcing Principal" ("Azure") and four Project Manager roles. Those
#: cost a Claude call each to score and are unqualifiable by construction.
#:
#: Titles are the right place for this. A description mentioning "sales" is normal
#: (every product has customers); a *title* containing it is decisive.
EXCLUDED_TITLE_WORDS: tuple[str, ...] = (
    "sales",
    "account executive",
    "account manager",
    "account-based",
    "business development",
    "marketing",
    "recruiter",
    "recruitment consultant",
    "talent acquisition",
    "procurement",
    "sourcing principal",
    "buyer",
    "project manager",
    "programme manager",
    "program manager",
    "scrum master",
    "delivery manager",
    "product manager",
    "product owner",
    "business analyst",
    "annotator",
    "data annotation",
    "teacher",
    "lecturer",
    "nurse",
    "driver",
)

_EXCLUDED_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    _compile(w) for w in EXCLUDED_TITLE_WORDS
)


def excluded_title(title: str | None) -> str | None:
    """The excluded word found in ``title``, or None if the title is acceptable.

    Returns the *word* rather than a bool so callers can log why a listing was
    dropped: a filter that silently discards is how you end up unable to tell an
    over-strict gate from an empty market.
    """
    if not title:
        return None
    low = title.lower()
    for word, pat in zip(EXCLUDED_TITLE_WORDS, _EXCLUDED_PATTERNS, strict=True):
        if pat.search(low):
            return word
    return None


def extract_tags(*texts: str | None) -> list[str]:
    """Return the subset of KEYWORDS found in the texts (deduped, ordered)."""
    blob = " ".join(t for t in texts if t).lower()
    out: list[str] = []
    if not blob:
        return out
    for kw, pat in _PATTERNS:
        if pat.search(blob) and kw not in out:
            out.append(kw)
    return out


def title_signal(title: str | None) -> list[str]:
    """Strong-signal keywords present in a listing *title*.

    Used to prioritise leads before spending LLM budget on them: a title hit is
    much more predictive of fit than a description hit, because descriptions carry
    boilerplate tech stacks ("we use Kubernetes") for roles that are not the job.
    """
    if not title:
        return []
    low = title.lower()
    return [
        kw
        for kw, pat in _PATTERNS
        if kw in STRONG_TITLE_KEYWORDS and pat.search(low)
    ]


def is_ai_infra(*texts: str | None) -> bool:
    """True if the texts carry an AI-infrastructure signal (the growth segment)."""
    ai_terms = {
        "llm",
        "llmops",
        "large language model",
        "generative ai",
        "genai",
        "rag",
        "retrieval-augmented",
        "retrieval augmented",
        "vector database",
        "agentic",
        "ai agent",
        "multi-agent",
        "mcp server",
        "model context protocol",
        "ai infrastructure",
        "ai platform",
        "mlops",
        "vllm",
        "ai security",
        "langchain",
        "langgraph",
        "agent orchestration",
        "prompt engineering",
    }
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return False
    return any(
        pat.search(blob) for kw, pat in _PATTERNS if kw in ai_terms
    )


#: Forward-Deployed / solutions-engineering titles. Kept separate from
#: :data:`is_ai_infra` because the two segments need different pitches: FDE work is
#: sold on customer-facing delivery ("I ship inside your stack, with your team"),
#: AI-infra on systems depth ("I run LLM serving at scale").
_FDE_TERMS: frozenset[str] = frozenset(
    {
        "forward deployed",
        "forward-deployed",
        "fde",
        "solutions engineer",
        "solutions architect",
        "implementation engineer",
        "deployment engineer",
        "customer engineer",
        "integration engineer",
        "professional services engineer",
    }
)


def is_fde(*texts: str | None) -> bool:
    """True if the texts describe a forward-deployed / solutions-engineering role."""
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return False
    return any(pat.search(blob) for kw, pat in _PATTERNS if kw in _FDE_TERMS)
