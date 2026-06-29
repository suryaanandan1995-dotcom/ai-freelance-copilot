"""Prometheus metrics for the copilot.

Importing this module is side-effect-free beyond registering collectors on the
default registry. All instrumentation helpers degrade to no-ops if
``prometheus_client`` is not installed, so the rest of the app never hard-depends
on it.
"""
from __future__ import annotations

try:  # pragma: no cover - exercised indirectly
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

    _ENABLED = True
except Exception:  # prometheus_client missing
    _ENABLED = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


if _ENABLED:
    leads_fetched_total = Counter("copilot_leads_fetched_total", "Leads fetched from all sources")
    leads_qualified_total = Counter(
        "copilot_leads_qualified_total", "Leads that passed the fit-score gate"
    )
    proposals_drafted_total = Counter(
        "copilot_proposals_drafted_total", "Proposals drafted and queued"
    )
    proposals_won_total = Counter("copilot_proposals_won_total", "Leads marked won")
    proposals_lost_total = Counter("copilot_proposals_lost_total", "Leads marked lost")
    claude_cost_usd = Counter("copilot_claude_cost_usd_total", "Cumulative Claude API spend (USD)")
    fit_score = Histogram(
        "copilot_fit_score", "Distribution of lead fit scores", buckets=[10, 30, 50, 70, 85, 95, 100]
    )
    proposal_quality = Histogram(
        "copilot_proposal_quality", "Compliance quality scores", buckets=[10, 30, 50, 70, 85, 95, 100]
    )
    rag_retrieval_seconds = Histogram(
        "copilot_rag_retrieval_seconds", "RAG retrieval latency (seconds)"
    )


def _noop(*_a, **_k) -> None:
    return None


class _NoopMetric:
    inc = _noop
    observe = _noop

    def labels(self, *_a, **_k):  # noqa: D401
        return self


def _m(name: str):
    """Return the named metric, or a no-op stand-in when prometheus is absent."""
    return globals().get(name, _NoopMetric()) if _ENABLED else _NoopMetric()


def inc(name: str, amount: float = 1.0) -> None:
    try:
        _m(name).inc(amount)
    except Exception:
        pass


def observe(name: str, value: float) -> None:
    try:
        _m(name).observe(value)
    except Exception:
        pass


def render() -> tuple[bytes, str]:
    """Return (body, content_type) for a /metrics endpoint."""
    if not _ENABLED:
        return b"# prometheus_client not installed\n", CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST


def enabled() -> bool:
    return _ENABLED
