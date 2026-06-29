"""Offline unit tests for the individual agents (no API key, no network)."""
from __future__ import annotations

from agents.compliance import review
from agents.followup import draft_followup
from agents.llm import FakeChat
from agents.proposal_writer import QUANTIFIED_WINS, write_proposal
from agents.qualifier import qualify
from agents.researcher import research
from core.schemas import (
    CompanyResearch,
    Lead,
    ProposalDraft,
    ScoredLead,
)


class FakeRetriever:
    """Minimal retriever: ``.retrieve(q, k)`` -> list of {text, source, score}."""

    def __init__(self, chunks=None):
        self._chunks = chunks or [
            {
                "text": "Cut multi-cloud spend 40% with reusable Terraform modules.",
                "source": "multi-cloud-k8s-terraform",
                "score": 0.91,
            },
            {
                "text": "Shipped an LLM guardrails gateway blocking unsafe prompts.",
                "source": "llm-guardrails-gateway",
                "score": 0.88,
            },
        ]

    def retrieve(self, query, k=3):
        return self._chunks[:k]


def _lead(**kw):
    base = dict(
        source="upwork_rss",
        external_id="job-123",
        title="Kubernetes platform + DevSecOps pipeline hardening",
        description="Need help securing our EKS clusters and CI/CD with Terraform.",
        company="Acme Corp",
        budget="$90/hr",
        tags=["kubernetes", "devsecops", "terraform"],
    )
    base.update(kw)
    return Lead(**base)


def test_qualifier_scores_and_matches_projects():
    chat = FakeChat(
        structured={
            "lead": _lead().model_dump(),
            "fit_score": 88,
            "reasons": ["strong k8s + devsecops overlap"],
            "matched_projects": ["multi-cloud-k8s-terraform", "not-a-real-repo"],
        }
    )
    scored = qualify(_lead(), chat=chat)
    assert isinstance(scored, ScoredLead)
    assert scored.fit_score == 88
    assert scored.reasons
    # Unknown repo names are filtered out, valid ones kept.
    assert scored.matched_projects == ["multi-cloud-k8s-terraform"]


def test_researcher_returns_enrichment():
    chat = FakeChat(
        structured={
            "summary": "Acme runs EKS and wants CI/CD security hardening.",
            "tech_stack": ["EKS", "Terraform", "GitHub Actions"],
            "pain_points": ["insecure pipelines", "cluster drift"],
            "contacts": ["Jane (CTO)"],
        }
    )
    enrichment = research(_lead(), chat=chat)
    assert isinstance(enrichment, CompanyResearch)
    assert "EKS" in enrichment.tech_stack
    assert enrichment.pain_points


def test_proposal_writer_cites_a_project():
    scored = ScoredLead(
        lead=_lead(),
        fit_score=88,
        reasons=["k8s + devsecops"],
        matched_projects=["multi-cloud-k8s-terraform"],
    )
    enrichment = CompanyResearch(
        summary="Acme runs EKS.",
        tech_stack=["EKS", "Terraform"],
        pain_points=["insecure pipelines"],
    )
    body = (
        "Hi Acme — I have hardened EKS clusters and CI/CD before. My "
        "multi-cloud-k8s-terraform project cut infra cost 40% and gave 50% faster "
        "deploys. I'd love to help secure your pipelines. Happy to chat: "
        "https://cal.com/surya-devsecops/15min"
    )
    chat = FakeChat(responses=[body])
    draft = write_proposal(scored, enrichment, retriever=FakeRetriever(), chat=chat)
    assert isinstance(draft, ProposalDraft)
    # RAG step cited a real portfolio project that appears in the body.
    assert "multi-cloud-k8s-terraform" in draft.cited_projects
    assert "multi-cloud-k8s-terraform" in draft.body
    assert any(win.split()[0] in draft.body for win in QUANTIFIED_WINS)


def _good_draft():
    body = (
        "Hi Acme, I have spent years hardening Kubernetes platforms and CI/CD "
        "pipelines. On multi-cloud-k8s-terraform I cut infrastructure cost 40% "
        "and lifted deploy frequency 75% while keeping security gates green. I can "
        "audit your EKS setup, add policy-as-code guardrails, and wire signed "
        "supply-chain checks into your pipeline. I work transparently and ship in "
        "small reviewable increments so you stay in control the whole time. If "
        "useful, I'm happy to walk through a concrete plan on a short call at your "
        "convenience — no pressure either way."
    )
    return ProposalDraft(
        lead_external_id="job-123",
        title="Proposal: Kubernetes platform hardening",
        body=body,
        suggested_rate="$90/hr",
        cited_projects=["multi-cloud-k8s-terraform"],
    )


def test_compliance_approves_a_good_draft():
    verdict = review(_good_draft())
    assert verdict.approved is True
    assert verdict.issues == []
    assert verdict.quality_score >= 90


def test_compliance_rejects_spam():
    spam = ProposalDraft(
        lead_external_id="job-999",
        title="Proposal: anything",
        body="Dear Sir/Madam, I can do this easily for the cheapest price. Buy now!",
        suggested_rate="$5/hr",
        cited_projects=[],  # no project cited -> generic
    )
    verdict = review(spam)
    assert verdict.approved is False
    assert any("generic" in i for i in verdict.issues)
    assert any("forbidden" in i for i in verdict.issues)


def test_compliance_detects_duplicate():
    draft = _good_draft()
    key = f"{draft.lead_external_id}:{draft.title}".lower()
    verdict = review(draft, existing_keys={key})
    assert verdict.is_duplicate is True
    assert verdict.approved is False


def test_followup_is_short_and_nonempty():
    chat = FakeChat(responses=["Hi Acme, just circling back on the EKS work — "
                               "no rush, happy to help whenever it's useful."])
    msg = draft_followup(_lead(), days_since=5, chat=chat)
    assert isinstance(msg, str) and msg.strip()
