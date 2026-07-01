"""Offline tests for the approval dashboard (FastAPI TestClient).

Uses a temp-file SQLite engine wired into ``db.session`` so the real session
contextmanager (and the get_db dependency) operate on throwaway data.
"""
from __future__ import annotations

import datetime as _dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import db.session as session_mod
    from db.models import (
        Base,
        LeadRecord,
        LeadStatus,
        OutreachRecord,
        ProposalRecord,
        ProposalStatus,
        ReplyRecord,
        RunRecord,
    )

    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}, future=True
    )
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(session_mod, "engine", engine)
    monkeypatch.setattr(session_mod, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)

    # seed a drafted lead + a draft proposal
    with SessionLocal() as s:
        lead = LeadRecord(
            source="upwork",
            external_id="ext-1",
            title="Harden our Kubernetes cluster",
            description="Need OPA + Trivy + mTLS.",
            url="https://example.com/job/1",
            company="Acme Corp",
            budget="$5k",
            tags=["kubernetes", "security"],
            fit_score=88,
            status=LeadStatus.drafted,
        )
        s.add(lead)
        s.flush()
        s.add(
            ProposalRecord(
                lead_id=lead.id,
                body="Original draft body for Acme.",
                suggested_rate="$90/hr",
                cited_projects=["multi-cloud-k8s-terraform"],
                status=ProposalStatus.draft,
            )
        )
        # mission-control data: outreach, a reply thread, and a run
        s.add(
            OutreachRecord(
                email="dana@example.com",
                subject="Cutting your cloud bill",
                status="sent",
                replied=True,
                followups_sent=2,
            )
        )
        s.add(
            RunRecord(
                workflow="outreach",
                ok=False,
                cost_usd=0.13,
                stats={"emailed": 3},
                error="SMTP auth failed",
            )
        )
        s.add_all(
            [
                ReplyRecord(
                    email="dana@example.com",
                    direction="out",
                    subject="Cutting your cloud bill",
                    snippet="Hi Dana, noticed your stack ...",
                ),
                ReplyRecord(
                    email="dana@example.com",
                    direction="in",
                    subject="Re: Cutting your cloud bill",
                    snippet="Yes, we overspend on egress.",
                ),
            ]
        )
        s.commit()
        lead_id = lead.id

    from interfaces.dashboard import app

    with TestClient(app) as c:
        c.lead_id = lead_id
        yield c


def _read_lead(client):
    import db.session as session_mod
    from db.models import LeadRecord

    with session_mod.SessionLocal() as s:
        lead = s.get(LeadRecord, client.lead_id)
        return lead.status, lead.proposals[0].status, lead.proposals[0].body, lead.proposals[0].submitted_at


def test_inbox_shows_lead(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Harden our Kubernetes cluster" in r.text


def test_lead_detail_shows_proposal_and_tos_note(client):
    r = client.get(f"/lead/{client.lead_id}")
    assert r.status_code == 200
    assert "Original draft body for Acme." in r.text
    assert "auto-submitting violates platform ToS" in r.text


def test_approve_transitions_status(client):
    from db.models import LeadStatus, ProposalStatus

    r = client.post(f"/lead/{client.lead_id}/approve", follow_redirects=False)
    assert r.status_code == 303
    status, pstatus, _, _ = _read_lead(client)
    assert status == LeadStatus.approved
    assert pstatus == ProposalStatus.approved


def test_save_proposal_edits_body(client):
    r = client.post(
        f"/lead/{client.lead_id}/proposal",
        data={"body": "Edited body v2", "suggested_rate": "$110/hr"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    _, _, body, _ = _read_lead(client)
    assert body == "Edited body v2"


def test_mark_submitted(client):
    from db.models import LeadStatus, ProposalStatus

    r = client.post(f"/lead/{client.lead_id}/submitted", follow_redirects=False)
    assert r.status_code == 303
    status, pstatus, _, submitted_at = _read_lead(client)
    assert status == LeadStatus.submitted
    assert pstatus == ProposalStatus.submitted
    assert isinstance(submitted_at, _dt.datetime)


def test_reject(client):
    from db.models import LeadStatus

    client.post(f"/lead/{client.lead_id}/reject", follow_redirects=False)
    status, _, _, _ = _read_lead(client)
    assert status == LeadStatus.rejected


def test_won_runs_learning_loop(client):
    from db.models import LeadStatus

    r = client.post(f"/lead/{client.lead_id}/won", follow_redirects=False)
    assert r.status_code == 303
    status, _, _, _ = _read_lead(client)
    assert status == LeadStatus.won


def test_pipeline_page(client):
    r = client.get("/pipeline")
    assert r.status_code == 200
    assert "Pipeline" in r.text


def test_content_page(client):
    r = client.get("/content")
    assert r.status_code == 200
    assert "Inbound Content Engine" in r.text


def test_outreach_page(client):
    r = client.get("/outreach")
    assert r.status_code == 200
    assert "dana@example.com" in r.text
    assert "Cutting your cloud bill" in r.text


def test_conversations_page(client):
    r = client.get("/conversations")
    assert r.status_code == 200
    assert "dana@example.com" in r.text
    assert "Yes, we overspend on egress." in r.text


def test_analytics_page(client):
    r = client.get("/analytics")
    assert r.status_code == 200
    assert "Analytics" in r.text
    assert "Reply rate" in r.text


def test_runs_page(client):
    r = client.get("/runs")
    assert r.status_code == 200
    assert "SMTP auth failed" in r.text
    assert "outreach" in r.text


def test_metrics_and_healthz(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.text  # non-empty text body

    h = client.get("/healthz")
    assert h.status_code == 200
    assert h.json() == {"status": "ok"}
