"""Offline tests for the health monitor ("doctor")."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
import monitor.doctor as doctor
from db.models import Base, RunRecord


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    yield engine


def _no_linkedin(monkeypatch):
    # Ensure the LinkedIn check is skipped (no token) unless a test sets one.
    from config import Settings

    monkeypatch.setattr(
        doctor,
        "run_healthcheck",
        doctor.run_healthcheck,  # keep real function
    )
    # patch get_settings used inside _check_linkedin_token
    import config

    base = Settings(linkedin_access_token="")
    monkeypatch.setattr(config, "get_settings", lambda: base)


def test_all_healthy_no_alert(temp_db, monkeypatch):
    _no_linkedin(monkeypatch)
    alerts = []
    import runlog

    monkeypatch.setattr(runlog, "send_alert", lambda subject, body: alerts.append(subject))

    result = doctor.run_healthcheck()
    assert result["ok"] is True
    assert result["issues"] == []
    assert alerts == []  # nothing to alert
    names = {c["name"] for c in result["checks"]}
    assert names == {"schema", "database", "recent_runs", "linkedin_token"}


def test_recent_failure_is_flagged_and_alerts(temp_db, monkeypatch):
    _no_linkedin(monkeypatch)
    alerts = []
    import runlog

    monkeypatch.setattr(runlog, "send_alert", lambda subject, body: alerts.append((subject, body)))

    # seed a failed run
    with dbsession.get_session() as s:
        s.add(RunRecord(workflow="optimize", ok=False, error="boom"))

    result = doctor.run_healthcheck()
    assert result["ok"] is False
    assert any("optimize" in i for i in result["issues"])
    assert alerts and "issue" in alerts[0][0].lower()


def test_linkedin_invalid_token_flagged(temp_db, monkeypatch):
    import config
    from config import Settings

    monkeypatch.setattr(config, "get_settings", lambda: Settings(linkedin_access_token="tok"))

    # make the LinkedIn client raise LinkedInError on whoami
    import linkedin.client as lc

    def boom_whoami(self):
        raise lc.LinkedInError("Invalid access token")

    monkeypatch.setattr(lc.LinkedInClient, "whoami", boom_whoami)

    alerts = []
    import runlog

    monkeypatch.setattr(runlog, "send_alert", lambda subject, body: alerts.append(subject))

    result = doctor.run_healthcheck()
    assert result["ok"] is False
    assert any("token" in i.lower() for i in result["issues"])


def test_format_report_readable():
    result = {
        "ok": False,
        "checks": [{"name": "database", "ok": False, "detail": "database unreachable: x"}],
        "auto_fixed": ["outreach.replied"],
        "issues": ["database unreachable: x"],
    }
    text = doctor.format_report(result)
    assert "ISSUES FOUND" in text
    assert "outreach.replied" in text
    assert "database unreachable" in text
