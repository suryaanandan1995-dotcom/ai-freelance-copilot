"""Offline tests for dashboard HTTP Basic auth (FastAPI TestClient).

Reuses the temp-file SQLite wiring pattern from ``tests/test_dashboard.py``. The
``require_auth`` dependency reads ``get_settings()`` at request time, so each test
monkeypatches ``interfaces.dashboard.get_settings`` to toggle the password.
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import db.session as session_mod
    from db.models import Base, LeadRecord, LeadStatus, OutreachRecord

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

    with SessionLocal() as s:
        s.add(
            LeadRecord(
                source="upwork",
                external_id="ext-1",
                title="Harden our Kubernetes cluster",
                description="Need OPA + Trivy + mTLS.",
                url="https://example.com/job/1",
                fit_score=88,
                status=LeadStatus.drafted,
            )
        )
        s.add(
            OutreachRecord(
                email="prospect@acme.com",
                subject="Hardening your CI/CD",
                status="sent",
                replied=False,
                followups_sent=0,
            )
        )
        s.commit()

    from interfaces.dashboard import app

    with TestClient(app) as c:
        yield c


def _patch_settings(monkeypatch, *, user="admin", password="", cal_secret=""):
    """Point interfaces.dashboard.get_settings at a fake settings object."""
    import interfaces.dashboard as dash

    fake = type(
        "S",
        (),
        {
            "dashboard_user": user,
            "dashboard_password": password,
            "cal_webhook_secret": cal_secret,
        },
    )()
    monkeypatch.setattr(dash, "get_settings", lambda: fake)


def _basic_header(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _booking_payload(email):
    return {
        "triggerEvent": "BOOKING_CREATED",
        "payload": {
            "attendees": [{"email": email, "name": "A Prospect"}],
            "responses": {"email": {"value": email}},
        },
    }


def test_blank_password_disables_auth(client, monkeypatch):
    _patch_settings(monkeypatch, password="")
    r = client.get("/")
    assert r.status_code == 200
    assert "Harden our Kubernetes cluster" in r.text


def test_password_set_without_creds_returns_401(client, monkeypatch):
    _patch_settings(monkeypatch, user="admin", password="hunter2")
    r = client.get("/")
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate") == "Basic"


def test_password_set_with_correct_creds_returns_200(client, monkeypatch):
    _patch_settings(monkeypatch, user="admin", password="hunter2")
    r = client.get("/", headers=_basic_header("admin", "hunter2"))
    assert r.status_code == 200
    assert "Harden our Kubernetes cluster" in r.text


def test_wrong_password_returns_401(client, monkeypatch):
    _patch_settings(monkeypatch, user="admin", password="hunter2")
    r = client.get("/", headers=_basic_header("admin", "wrong"))
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate") == "Basic"


def test_wrong_username_returns_401(client, monkeypatch):
    _patch_settings(monkeypatch, user="admin", password="hunter2")
    r = client.get("/", headers=_basic_header("intruder", "hunter2"))
    assert r.status_code == 401


def test_open_endpoints_work_without_creds_when_password_set(client, monkeypatch):
    # Even with auth enabled, /healthz and POST /webhooks/cal stay open.
    _patch_settings(monkeypatch, user="admin", password="hunter2", cal_secret="")

    h = client.get("/healthz")
    assert h.status_code == 200
    assert h.json() == {"status": "ok"}

    w = client.post("/webhooks/cal", json=_booking_payload("prospect@acme.com"))
    assert w.status_code == 200
    assert w.json() == {"ok": True, "matched": 1}


def test_metrics_open_without_creds_when_password_set(client, monkeypatch):
    _patch_settings(monkeypatch, user="admin", password="hunter2")
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.text
