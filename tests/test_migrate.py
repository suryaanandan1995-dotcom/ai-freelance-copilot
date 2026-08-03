"""Tests for the lightweight auto-migration in db.session._ensure_columns."""
from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

import db.session as dbsession


def test_ensure_columns_adds_missing_on_existing_table(tmp_path):
    # Simulate an OLD `outreach` table created before the funnel columns existed.
    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}", future=True)
    with eng.begin() as c:
        c.execute(
            text(
                "CREATE TABLE outreach ("
                "id INTEGER PRIMARY KEY, email VARCHAR, status VARCHAR, sent_at DATETIME)"
            )
        )

    added = dbsession._ensure_columns(eng)

    cols = {col["name"] for col in inspect(eng).get_columns("outreach")}
    for missing in ("replied", "followups_sent", "last_contact_at", "call_booked_at"):
        assert missing in cols, f"{missing} was not added"
    assert "outreach.replied" in added


def test_ensure_columns_noop_on_fresh_schema(tmp_path):
    # A DB created from the current models has nothing to add.
    from db.models import Base

    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    Base.metadata.create_all(eng)
    assert dbsession._ensure_columns(eng) == []


# --------------------------------------------------------------------------- #
# verification: "added nothing" and "complete" are different states
# --------------------------------------------------------------------------- #
def test_missing_columns_reports_a_drifted_table(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'drift.db'}", future=True)
    with eng.begin() as c:
        c.execute(
            text(
                "CREATE TABLE outreach ("
                "id INTEGER PRIMARY KEY, email VARCHAR, status VARCHAR, sent_at DATETIME)"
            )
        )

    missing = dbsession.missing_columns(eng)
    assert "outreach.replied" in missing
    assert "outreach.call_booked_at" in missing


def test_missing_columns_is_empty_on_a_complete_schema(tmp_path):
    from db.models import Base

    eng = create_engine(f"sqlite:///{tmp_path / 'complete.db'}", future=True)
    Base.metadata.create_all(eng)
    assert dbsession.missing_columns(eng) == []


def test_missing_columns_does_not_raise_on_an_unusable_bind():
    """It feeds a health check; an inspection failure must degrade, not explode."""
    eng = create_engine("sqlite:///:memory:", future=True)
    eng.dispose()
    assert isinstance(dbsession.missing_columns(eng), list)


def test_doctor_reports_not_ok_when_a_heal_could_not_land(tmp_path, monkeypatch):
    """The whole point of verifying: ``_ensure_columns`` swallows failed ALTERs, so a
    heal that never lands used to be reported as ``schema: ok — up to date``."""
    from monitor.doctor import _check_schema

    # init_db + _ensure_columns succeed at the call level but change nothing...
    monkeypatch.setattr(dbsession, "init_db", lambda: None)
    monkeypatch.setattr(dbsession, "_ensure_columns", lambda bind: [])
    # ...while the DB still lacks a column.
    monkeypatch.setattr(dbsession, "missing_columns", lambda bind: ["outreach.replied"])

    check, fixed = _check_schema()
    assert check["ok"] is False
    assert "outreach.replied" in check["detail"]
    assert fixed == []


def test_doctor_reports_ok_when_the_schema_verifies_clean(monkeypatch):
    from monitor.doctor import _check_schema

    monkeypatch.setattr(dbsession, "init_db", lambda: None)
    monkeypatch.setattr(dbsession, "_ensure_columns", lambda bind: ["outreach.replied"])
    monkeypatch.setattr(dbsession, "missing_columns", lambda bind: [])

    check, fixed = _check_schema()
    assert check["ok"] is True
    assert "healed 1 column" in check["detail"]
    assert fixed == ["outreach.replied"]
