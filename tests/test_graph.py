"""Offline tests for the LangGraph orchestrator (no API key, no network)."""
from __future__ import annotations

from agents.graph import build_graph, run_lead
from agents.llm import FakeChat
from core.schemas import Lead


class FakeRetriever:
    def retrieve(self, query, k=3):
        return [
            {
                "text": "multi-cloud-k8s-terraform cut infra cost 40%.",
                "source": "multi-cloud-k8s-terraform",
                "score": 0.9,
            }
        ]


def _lead():
    return Lead(
        source="upwork_rss",
        external_id="job-123",
        title="Kubernetes + DevSecOps hardening",
        description="Secure our EKS clusters and CI/CD with Terraform.",
        company="Acme Corp",
        budget="$90/hr",
        tags=["kubernetes", "devsecops"],
    )


def _high_fit_chat():
    """FakeChat that scores high, enriches, and writes a clean proposal."""
    proposal_body = (
        "Hi Acme, I have hardened Kubernetes platforms and CI/CD pipelines for "
        "years. On multi-cloud-k8s-terraform I cut infrastructure cost 40% and "
        "lifted deploy frequency 75% with security gates kept green. I can audit "
        "your EKS clusters, add policy-as-code guardrails, and wire signed "
        "supply-chain checks into your pipeline, shipping in small reviewable "
        "increments so you stay in control. If it's useful, I'm glad to share a "
        "concrete plan on a short call — no pressure: "
        "https://cal.com/surya-devsecops/15min"
    )
    return FakeChat(
        # qualify (structured), research (structured), write (plain text)
        responses=[proposal_body],
        structured=_route_structured,
    )


def _route_structured(messages):
    """Return scoring vs research payload based on the system prompt content."""
    system = ""
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else None
        if role == "system":
            system = m.get("content", "")
            break
    if "qualifier" in system:
        return {
            "lead": _lead().model_dump(),
            "fit_score": 90,
            "reasons": ["strong k8s + devsecops fit"],
            "matched_projects": ["multi-cloud-k8s-terraform"],
        }
    # researcher
    return {
        "summary": "Acme runs EKS and wants CI/CD hardening.",
        "tech_stack": ["EKS", "Terraform"],
        "pain_points": ["insecure pipelines"],
        "contacts": [],
    }


def test_graph_builds():
    app = build_graph(retriever=FakeRetriever(), chat=_high_fit_chat())
    assert app is not None


def test_high_fit_lead_routes_to_queue():
    final = run_lead(_lead(), retriever=FakeRetriever(), chat=_high_fit_chat())
    assert final["disposition"] == "queue"
    assert final["scored"]["fit_score"] >= 70
    assert final["proposal"] is not None
    assert final["verdict"]["approved"] is True
    assert "multi-cloud-k8s-terraform" in final["proposal"]["cited_projects"]


def test_low_fit_lead_is_dropped():
    low_chat = FakeChat(
        structured={
            "lead": _lead().model_dump(),
            "fit_score": 20,
            "reasons": ["mostly frontend work, weak infra overlap"],
            "matched_projects": [],
        }
    )
    final = run_lead(_lead(), retriever=FakeRetriever(), chat=low_chat)
    assert final["disposition"] == "drop"
    # Dropped before the research/write/review nodes ran.
    assert final.get("proposal") is None
    assert final.get("verdict") is None
