"""Post topics, weighted toward the segments that actually buy.

Why this is Python and not a bash array
---------------------------------------
The rotation used to live inline in ``.github/workflows/linkedin.yml``, which meant
it could not be tested, could not be reused by the dashboard or CLI, and drifted out
of sync with what the pipeline was targeting. Fourteen posts went out over the month
on DevOps-only angles while the *demand* had moved.

Targeting rationale (ITJobsWatch UK contract data, 6 months to 2026-08-03)
--------------------------------------------------------------------------
* Kubernetes: £535/day median, rank **down 6** places YoY — flat.
* LLM: £550/day median, vacancies **+247%** YoY (295 -> 1,024), p75 £650, p90 £784.

Inbound content should therefore lead with AI-infra and agent work, keep DevSecOps as
the credibility base (it is what the buyer searches for), and include forward-deployed
/ solutions-engineering angles, which are a distinct and well-paid hiring track at AI
companies. Weights encode that mix rather than treating all topics as equal.
"""
from __future__ import annotations

#: (topic, segment, weight). Weight = how many slots the topic occupies in the
#: rotation, so a weight-3 topic appears three times as often as a weight-1 one.
TOPICS: tuple[tuple[str, str, int], ...] = (
    # --- AI infrastructure: the growth segment, highest weight -----------------
    ("autoscaling GPU inference on Kubernetes with vLLM", "ai-infra", 3),
    ("running RAG in production: retrieval quality, not just embeddings", "ai-infra", 3),
    ("prompt-injection and PII guardrails for production LLM apps", "ai-infra", 3),
    ("what LLM serving costs really come from, and how to cut them", "ai-infra", 3),
    ("shipping AI/LLM workloads securely on Kubernetes", "ai-infra", 2),
    ("evaluating an LLM feature before you ship it: evals as CI gates", "ai-infra", 2),
    ("MCP servers: giving an agent safe access to your infrastructure", "ai-infra", 2),
    # --- Agent engineering ----------------------------------------------------
    ("multi-agent systems with LangGraph: where they help and where they don't", "agents", 3),
    ("an agent that triages Kubernetes incidents before you wake up", "agents", 3),
    ("giving an AI agent production access without giving it the keys", "agents", 2),
    ("why most agent demos fail in production: state, retries, and cost caps", "agents", 2),
    # --- Forward-deployed / solutions engineering ------------------------------
    ("what forward-deployed engineering actually looks like day to day", "fde", 2),
    ("deploying an AI product inside a customer's cloud, on their terms", "fde", 2),
    ("the first two weeks of an integration: how to de-risk a rollout", "fde", 1),
    # --- DevSecOps base: credibility and search surface -----------------------
    ("Kubernetes security hardening and policy-as-code", "devsecops", 2),
    ("building a DevSecOps CI/CD pipeline with fail-closed vulnerability gates", "devsecops", 2),
    ("cutting cloud cost without slowing delivery", "devsecops", 2),
    ("GitOps multi-environment promotion with ArgoCD", "devsecops", 1),
    ("Terraform multi-cloud platform engineering done right", "devsecops", 1),
    ("shift-left security: catching CVEs before they reach prod", "devsecops", 1),
    ("secrets management and zero-trust access with Keycloak + mTLS", "devsecops", 1),
    ("observability that matters: SLOs, tracing, and cost signals", "devsecops", 1),
    ("platform engineering: golden paths that make the secure way the easy way", "devsecops", 1),
)


def rotation() -> list[str]:
    """Topics expanded by weight, in declaration order."""
    out: list[str] = []
    for topic, _segment, weight in TOPICS:
        out.extend([topic] * max(1, weight))
    return out


def topic_for_day(day_of_year: int) -> str:
    """Deterministic topic for a given day-of-year.

    Deterministic (not random) so a re-run of the same day's workflow produces the
    same post rather than a second, different one — the publish path is capped at one
    post/day and a duplicate would be silently dropped.
    """
    slots = rotation()
    return slots[int(day_of_year) % len(slots)]


def segments() -> dict[str, int]:
    """Weighted slot count per segment — the actual content mix being published."""
    counts: dict[str, int] = {}
    for _topic, segment, weight in TOPICS:
        counts[segment] = counts.get(segment, 0) + max(1, weight)
    return counts
