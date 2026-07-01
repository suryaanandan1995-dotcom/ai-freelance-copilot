"""Offline tests for run recording + failure alerting (no network, no SMTP).

DB-touching tests rebind ``db.session`` to a fresh temp SQLite file (same pattern
as the other suites). ``smtplib.SMTP`` is monkeypatched with a fake so alerts are
captured instead of sent.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
import runlog
from db.models import Base, RunRecord


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'runs.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    yield engine


class _FakeSMTP:
    """Captures send_message calls; mimics the smtplib.SMTP context manager."""

    sent: list = []

    def __init__(self, host, port):
        self.host = host
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        _FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp_on(monkeypatch):
    """Set an SMTP host and capture sends via a fake smtplib.SMTP."""
    import config

    real = config.get_settings

    def s():
        cfg = real()
        cfg.smtp_host = "smtp.example.com"
        cfg.smtp_user = ""
        cfg.alert_email = "owner@example.com"
        return cfg

    monkeypatch.setattr(runlog, "get_settings", s)
    _FakeSMTP.sent = []
    monkeypatch.setattr(runlog.smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


# --------------------------------------------------------------------------- #
# 1. success -> ok RunRecord with cost_usd, stats returned
# --------------------------------------------------------------------------- #
def test_record_run_success_writes_ok_record(temp_db):
    stats = runlog.record_run(
        "outreach", lambda: {"sent": 3, "cost_usd": 0.42}
    )
    assert stats == {"sent": 3, "cost_usd": 0.42}

    with dbsession.get_session() as session:
        rec = session.query(RunRecord).one()
        assert rec.workflow == "outreach"
        assert rec.ok is True
        assert rec.cost_usd == pytest.approx(0.42)
        assert rec.stats == {"sent": 3, "cost_usd": 0.42}
        assert rec.error is None


# --------------------------------------------------------------------------- #
# 2. missing cost_usd defaults to 0.0
# --------------------------------------------------------------------------- #
def test_record_run_success_default_cost(temp_db):
    runlog.record_run("followup", lambda: {"sent": 0})
    with dbsession.get_session() as session:
        rec = session.query(RunRecord).one()
        assert rec.cost_usd == 0.0


# --------------------------------------------------------------------------- #
# 3. failure -> ok=False RunRecord w/ error, alert sent, exception re-raised
# --------------------------------------------------------------------------- #
def test_record_run_failure_records_alerts_and_reraises(temp_db, smtp_on):
    def boom():
        raise RuntimeError("pipeline exploded")

    with pytest.raises(RuntimeError, match="pipeline exploded"):
        runlog.record_run("reply", boom)

    with dbsession.get_session() as session:
        rec = session.query(RunRecord).one()
        assert rec.workflow == "reply"
        assert rec.ok is False
        assert "pipeline exploded" in (rec.error or "")
        assert rec.stats == {}

    # An alert email was dispatched to the owner.
    assert len(smtp_on.sent) == 1
    assert smtp_on.sent[0]["To"] == "owner@example.com"
    assert "reply" in smtp_on.sent[0]["Subject"]


# --------------------------------------------------------------------------- #
# 4. send_alert no-op when smtp_host empty
# --------------------------------------------------------------------------- #
def test_send_alert_noop_when_smtp_host_empty(monkeypatch):
    import config

    real = config.get_settings

    def s():
        cfg = real()
        cfg.smtp_host = ""
        return cfg

    monkeypatch.setattr(runlog, "get_settings", s)
    assert runlog.send_alert("subject", "body") is False


# --------------------------------------------------------------------------- #
# 5. send_alert actually "sends" when smtp_host is set
# --------------------------------------------------------------------------- #
def test_send_alert_sends_when_configured(smtp_on):
    assert runlog.send_alert("[Copilot] test", "body text") is True
    assert len(smtp_on.sent) == 1
    assert smtp_on.sent[0]["Subject"] == "[Copilot] test"


# --------------------------------------------------------------------------- #
# 6. send_alert never raises even if the transport blows up
# --------------------------------------------------------------------------- #
def test_send_alert_never_raises(monkeypatch):
    import config

    real = config.get_settings

    def s():
        cfg = real()
        cfg.smtp_host = "smtp.example.com"
        return cfg

    monkeypatch.setattr(runlog, "get_settings", s)

    def boom(host, port):
        raise OSError("connection refused")

    monkeypatch.setattr(runlog.smtplib, "SMTP", boom)
    assert runlog.send_alert("s", "b") is False
