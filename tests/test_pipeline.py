"""Offline tests for the pipeline integration core (no API key, no network).

Each test gets its own isolated SQLite database by rebinding ``db.session``'s
engine + sessionmaker to a fresh on-disk temp DB and recreating the schema.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
from agents.llm import FakeChat
from core.schemas import Lead
from db.models import Base, LeadRecord, LeadStatus, ProposalRecord


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Rebind the shared engine/SessionLocal to an isolated temp SQLite file."""
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    yield engine


class FakeRetriever:
    def retrieve(self, query, k=3):
        return [
            {
                "text": "multi-cloud-k8s-terraform cut infra cost 40%.",
                "source": "multi-cloud-k8s-terraform",
                "kind": "win",
                "score": 0.9,
            }
        ]


def _lead(i: int) -> Lead:
    return Lead(
        source="upwork_rss",
        external_id=f"job-{i}",
        title=f"Kubernetes + DevSecOps hardening #{i}",
        description="Secure our EKS clusters and CI/CD with Terraform.",
        company="Acme Corp",
        budget="$90/hr",
        tags=["kubernetes", "devsecops"],
    )


class FakeSource:
    """In-memory source returning a fixed list of leads."""

    name = "upwork_rss"

    def __init__(self, leads):
        self._leads = leads

    def fetch(self, limit: int = 50):
        return list(self._leads[:limit])


def _route_structured(messages):
    system = ""
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else None
        if role == "system":
            system = m.get("content", "")
            break
    if "qualifier" in system:
        return {
            "lead": _lead(0).model_dump(),
            "fit_score": 90,
            "reasons": ["strong k8s + devsecops fit"],
            "matched_projects": ["multi-cloud-k8s-terraform"],
        }
    return {
        "summary": "Acme runs EKS and wants CI/CD hardening.",
        "tech_stack": ["EKS", "Terraform"],
        "pain_points": ["insecure pipelines"],
        "contacts": [],
    }


def _high_fit_chat() -> FakeChat:
    body = (
        "Hi Acme, I have hardened Kubernetes platforms and CI/CD pipelines for "
        "years. On multi-cloud-k8s-terraform I cut infrastructure cost 40% and "
        "lifted deploy frequency 75% with security gates kept green. I can audit "
        "your EKS clusters, add policy-as-code guardrails, and wire signed "
        "supply-chain checks into your pipeline, shipping in small reviewable "
        "increments so you stay in control. Glad to share a concrete plan on a "
        "short call: https://cal.com/surya-devsecops/15min"
    )
    return FakeChat(responses=[body, body, body, body, body], structured=_route_structured)


def test_run_pipeline_persists_queued_leads_and_drafts(temp_db):
    from pipeline import run_pipeline

    sources = [FakeSource([_lead(1), _lead(2)])]
    stats = run_pipeline(
        sources=sources, retriever=FakeRetriever(), chat=_high_fit_chat()
    )

    assert stats["fetched"] == 2
    assert stats["new"] == 2
    assert stats["queued"] == 2
    assert stats["skipped"] == 0
    assert "cost_usd" in stats
    assert stats["budget_exhausted"] is False

    with dbsession.get_session() as session:
        leads = session.query(LeadRecord).all()
        assert len(leads) == 2
        assert all(lead.status == LeadStatus.drafted for lead in leads)
        assert all(lead.fit_score == 90 for lead in leads)
        proposals = session.query(ProposalRecord).all()
        assert len(proposals) == 2
        assert all("multi-cloud-k8s-terraform" in p.cited_projects for p in proposals)


def test_dedupe_skips_already_present(temp_db):
    from pipeline import run_pipeline

    # Pre-load one of the leads.
    with dbsession.get_session() as session:
        session.add(
            LeadRecord(
                source="upwork_rss",
                external_id="job-1",
                title="already here",
                status=LeadStatus.drafted,
            )
        )

    sources = [FakeSource([_lead(1), _lead(2)])]
    stats = run_pipeline(
        sources=sources, retriever=FakeRetriever(), chat=_high_fit_chat()
    )

    assert stats["fetched"] == 2
    assert stats["skipped"] == 1
    assert stats["new"] == 1
    assert stats["queued"] == 1


def test_max_proposals_per_day_cap(temp_db, monkeypatch):
    import config
    from pipeline import run_pipeline

    real_get = config.get_settings

    def capped():
        s = real_get()
        s.max_proposals_per_day = 1
        return s

    monkeypatch.setattr("pipeline.get_settings", capped)

    sources = [FakeSource([_lead(1), _lead(2), _lead(3)])]
    stats = run_pipeline(
        sources=sources, retriever=FakeRetriever(), chat=_high_fit_chat()
    )

    # First lead queues, then the per-day cap stops further queuing.
    assert stats["queued"] == 1
    with dbsession.get_session() as session:
        assert session.query(ProposalRecord).count() == 1


def test_stats_include_cost_usd(temp_db):
    from pipeline import run_pipeline

    stats = run_pipeline(
        sources=[FakeSource([_lead(1)])],
        retriever=FakeRetriever(),
        chat=_high_fit_chat(),
    )
    assert "cost_usd" in stats
    assert isinstance(stats["cost_usd"], float)


def test_over_budget_tracker_stops_cleanly(temp_db, monkeypatch):
    """A pre-exhausted budget makes the first metered call raise -> clean stop."""
    import pipeline as pipeline_mod
    from costs import CostTracker
    from pipeline import run_pipeline

    over = CostTracker(budget_usd=2.0)
    over.record("claude-opus-4-8", 0, 1_000_000)  # $25 spent, over the $2 cap
    assert over.would_exceed()

    monkeypatch.setattr(pipeline_mod, "CostTracker", lambda budget_usd=None: over)

    stats = run_pipeline(
        sources=[FakeSource([_lead(1), _lead(2)])],
        retriever=FakeRetriever(),
        chat=_high_fit_chat(),
    )

    assert stats["budget_exhausted"] is True
    assert stats["queued"] == 0
    with dbsession.get_session() as session:
        assert session.query(LeadRecord).count() == 0


def test_pipeline_stats_and_top_queued(temp_db):
    from pipeline import pipeline_stats, run_pipeline, top_queued

    run_pipeline(
        sources=[FakeSource([_lead(1), _lead(2)])],
        retriever=FakeRetriever(),
        chat=_high_fit_chat(),
    )
    stats = pipeline_stats()
    assert stats["total_leads"] == 2
    assert stats["by_status"]["drafted"] == 2

    top = top_queued(n=5)
    assert len(top) == 2
    assert top[0]["fit_score"] == 90
