"""Offline tests for the win/loss learning loop (no API key, no network)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
from db.models import Base, LeadRecord, LeadStatus, ProposalRecord, ProposalStatus
from rag.store import InMemoryVectorStore


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    yield engine


def _seed_lead(body: str = "A winning proposal body that closed the deal.") -> int:
    with dbsession.get_session() as session:
        lead = LeadRecord(
            source="upwork_rss",
            external_id="job-win-1",
            title="K8s hardening",
            status=LeadStatus.drafted,
            fit_score=88,
        )
        lead.proposals.append(ProposalRecord(body=body, status=ProposalStatus.draft))
        session.add(lead)
        session.flush()
        return lead.id


def test_record_outcome_won_flips_status_and_grows_kb(temp_db, tmp_path, monkeypatch):
    import rag.learning as learning

    kb_path = str(tmp_path / "kb.json")
    # Point the KB append at a temp file via the settings store path.
    import config

    real_get = config.get_settings

    def patched():
        s = real_get()
        s.rag_store_path = kb_path
        return s

    monkeypatch.setattr(learning, "get_settings", patched)

    lead_id = _seed_lead("Hardened the EKS cluster and cut deploy time 50%.")
    ok = learning.record_outcome(lead_id, won=True)
    assert ok is True

    with dbsession.get_session() as session:
        lead = session.get(LeadRecord, lead_id)
        assert lead.status == LeadStatus.won
        assert all(p.outcome_at is not None for p in lead.proposals)

    # KB gained a kind="win" doc.
    store = InMemoryVectorStore.from_file(kb_path)
    assert len(store) == 1
    doc = store.docs[0]
    assert doc["metadata"]["kind"] == "win"
    assert "EKS" in doc["text"]


def test_record_outcome_lost_no_kb_append(temp_db, tmp_path, monkeypatch):
    import rag.learning as learning

    kb_path = str(tmp_path / "kb.json")
    import config

    real_get = config.get_settings

    def patched():
        s = real_get()
        s.rag_store_path = kb_path
        return s

    monkeypatch.setattr(learning, "get_settings", patched)

    lead_id = _seed_lead()
    ok = learning.record_outcome(lead_id, won=False)
    assert ok is True

    with dbsession.get_session() as session:
        lead = session.get(LeadRecord, lead_id)
        assert lead.status == LeadStatus.lost

    import os

    assert not os.path.exists(kb_path)  # no win -> no KB file written


def test_record_outcome_missing_lead_returns_false(temp_db):
    import rag.learning as learning

    assert learning.record_outcome(99999, won=True) is False
