"""Offline tests for the follow-up subsystem (no API key, no network, no SMTP).

Each DB-touching test gets an isolated SQLite database by rebinding
``db.session``'s engine + sessionmaker to a fresh temp file (same pattern as
``test_reply.py`` / ``test_outreach.py``). ``send_outreach`` is monkeypatched so
nothing leaves the box, and a ``FakeChat`` supplies a deterministic draft.
"""
from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
from agents.llm import FakeChat
from db.models import Base, OutreachRecord

LEAD_EMAIL = "founder@acme.com"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'followup.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    yield engine


@pytest.fixture
def temp_suppress(tmp_path, monkeypatch):
    """Point the suppression list at a temp file for the follow-up subsystem."""
    supp = tmp_path / "suppressed.txt"
    import outreach.suppression as suppression

    monkeypatch.setattr(suppression, "SUPPRESSION_PATH", supp)
    return supp


@pytest.fixture
def auto_email_on(monkeypatch):
    """Flip auto_email True + SMTP set wherever the follow-up runner reads settings."""
    import config

    real = config.get_settings

    def s():
        cfg = real()
        cfg.auto_email = True
        cfg.smtp_host = "smtp.example.com"
        cfg.smtp_user = "me@example.com"
        cfg.smtp_password = "app-pw"
        cfg.max_followups = 2
        cfg.followup_after_days = 3
        cfg.max_emails_per_day = 8
        return cfg

    monkeypatch.setattr("followup.runner.get_settings", s, raising=False)
    return s


def _patch_send_true(monkeypatch):
    """Make send_outreach succeed without SMTP; capture the calls."""
    calls: list[dict] = []

    def fake_send(to, subject, body):
        calls.append({"to": to, "subject": subject, "body": body})
        return True

    monkeypatch.setattr("outreach.sender.send_outreach", fake_send)
    return calls


def _seed(email: str, *, days_ago: float, replied: bool = False, followups: int = 0,
          status: str = "sent", subject: str = "Quick question about your API work") -> None:
    contacted = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=days_ago)
    with dbsession.get_session() as session:
        session.add(
            OutreachRecord(
                email=email,
                subject=subject,
                status=status,
                sent_at=contacted,
                replied=replied,
                followups_sent=followups,
                last_contact_at=contacted,
            )
        )


# --------------------------------------------------------------------------- #
# 1. master gate off -> no-op
# --------------------------------------------------------------------------- #
def test_run_followups_noop_when_auto_email_off(temp_db, monkeypatch):
    import config
    import followup.runner as runner

    real = config.get_settings

    def off():
        cfg = real()
        cfg.auto_email = False
        return cfg

    monkeypatch.setattr(runner, "get_settings", off)
    # send_outreach must never be called when gated off.
    monkeypatch.setattr(
        "outreach.sender.send_outreach",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not send")),
    )

    stats = runner.run_followups()
    assert stats == {"candidates": 0, "sent": 0, "capped": 0, "skipped": 0}


# --------------------------------------------------------------------------- #
# 2. a due, unreplied record -> a follow-up is sent + counters advance
# --------------------------------------------------------------------------- #
def test_due_record_gets_followup(temp_db, temp_suppress, auto_email_on, monkeypatch):
    import followup.runner as runner

    _seed(LEAD_EMAIL, days_ago=5)  # older than followup_after_days (3)
    calls = _patch_send_true(monkeypatch)

    stats = runner.run_followups(chat=FakeChat(responses=["Just circling back — Surya"]))

    assert stats["candidates"] == 1
    assert stats["sent"] == 1
    assert len(calls) == 1
    assert calls[0]["to"] == LEAD_EMAIL
    assert calls[0]["subject"].lower().startswith("re:")

    with dbsession.get_session() as session:
        rec = session.query(OutreachRecord).filter_by(email=LEAD_EMAIL).one()
        assert rec.followups_sent == 1
        # SQLite hands datetimes back naive; normalize before comparing.
        last = rec.last_contact_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=_dt.UTC)
        assert last >= _dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=5)


# --------------------------------------------------------------------------- #
# 3. subject that already starts with "Re:" is preserved (not doubled)
# --------------------------------------------------------------------------- #
def test_reply_subject_not_doubled(temp_db, temp_suppress, auto_email_on, monkeypatch):
    import followup.runner as runner

    _seed(LEAD_EMAIL, days_ago=5, subject="Re: your data pipeline")
    calls = _patch_send_true(monkeypatch)

    runner.run_followups(chat=FakeChat(responses=["nudge"]))

    assert calls[0]["subject"] == "Re: your data pipeline"


# --------------------------------------------------------------------------- #
# 4. a replied record is skipped
# --------------------------------------------------------------------------- #
def test_replied_record_skipped(temp_db, temp_suppress, auto_email_on, monkeypatch):
    import followup.runner as runner

    _seed(LEAD_EMAIL, days_ago=5, replied=True)
    calls = _patch_send_true(monkeypatch)

    stats = runner.run_followups(chat=FakeChat(responses=["nudge"]))

    assert stats["candidates"] == 0
    assert stats["sent"] == 0
    assert calls == []


# --------------------------------------------------------------------------- #
# 5. the max_followups cap stops the sequence
# --------------------------------------------------------------------------- #
def test_max_followups_cap(temp_db, temp_suppress, auto_email_on, monkeypatch):
    import followup.runner as runner

    # Already at the cap (max_followups=2) -> not a candidate.
    _seed(LEAD_EMAIL, days_ago=5, followups=2)
    calls = _patch_send_true(monkeypatch)

    stats = runner.run_followups(chat=FakeChat(responses=["nudge"]))

    assert stats["candidates"] == 0
    assert stats["sent"] == 0
    assert calls == []


# --------------------------------------------------------------------------- #
# 6. the daily cap is respected -> excess candidates are "capped"
# --------------------------------------------------------------------------- #
def test_daily_cap_respected(temp_db, temp_suppress, monkeypatch):
    import config
    import followup.runner as runner

    real = config.get_settings

    def s():
        cfg = real()
        cfg.auto_email = True
        cfg.smtp_host = "smtp.example.com"
        cfg.max_followups = 2
        cfg.followup_after_days = 3
        cfg.max_emails_per_day = 2  # small cap
        return cfg

    monkeypatch.setattr(runner, "get_settings", s)

    for i in range(3):
        _seed(f"lead{i}@acme.com", days_ago=5)
    calls = _patch_send_true(monkeypatch)

    stats = runner.run_followups(chat=FakeChat(responses=["nudge"]))

    assert stats["candidates"] == 3
    assert stats["sent"] == 2
    assert stats["capped"] == 1
    assert len(calls) == 2


# --------------------------------------------------------------------------- #
# 7. a suppressed email is skipped
# --------------------------------------------------------------------------- #
def test_suppressed_email_skipped(temp_db, temp_suppress, auto_email_on, monkeypatch):
    import followup.runner as runner

    _seed(LEAD_EMAIL, days_ago=5)
    temp_suppress.write_text(LEAD_EMAIL + "\n", encoding="utf-8")
    calls = _patch_send_true(monkeypatch)

    stats = runner.run_followups(chat=FakeChat(responses=["nudge"]))

    assert stats["candidates"] == 1
    assert stats["sent"] == 0
    assert stats["skipped"] == 1
    assert calls == []


# --------------------------------------------------------------------------- #
# 8. a record contacted too recently is not yet due
# --------------------------------------------------------------------------- #
def test_recent_contact_not_due(temp_db, temp_suppress, auto_email_on, monkeypatch):
    import followup.runner as runner

    _seed(LEAD_EMAIL, days_ago=1)  # within followup_after_days (3)
    calls = _patch_send_true(monkeypatch)

    stats = runner.run_followups(chat=FakeChat(responses=["nudge"]))

    assert stats["candidates"] == 0
    assert calls == []
