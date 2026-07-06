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
