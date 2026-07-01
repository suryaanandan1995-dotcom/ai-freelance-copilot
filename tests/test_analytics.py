"""Offline tests for the analytics helpers.

Wires a throwaway temp-file SQLite engine into ``db.session`` (same pattern as
test_dashboard) so the pure ``analytics`` functions read seeded rows.
"""
from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    import db.session as session_mod
    from db.models import (
        Base,
        LeadRecord,
        LeadStatus,
        OutreachRecord,
        ReplyRecord,
        RunRecord,
    )

    db_path = tmp_path / "analytics.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}, future=True
    )
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(session_mod, "engine", engine)
    monkeypatch.setattr(session_mod, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)

    now = _dt.datetime.now(_dt.UTC)
    with SessionLocal() as s:
        s.add_all(
            [
                OutreachRecord(
                    email="alice@example.com",
                    subject="Quick idea for your API",
                    status="sent",
                    sent_at=now,
                    replied=True,
                    followups_sent=1,
                    last_contact_at=now,
                ),
                OutreachRecord(
                    email="bob@example.com",
                    subject="Scaling your infra",
                    status="sent",
                    sent_at=now,
                    replied=False,
                    followups_sent=0,
                    last_contact_at=now,
                ),
                OutreachRecord(
                    email="carol@example.com",
                    subject="",
                    status="suppressed",
                    sent_at=now,
                ),
            ]
        )
        s.add_all(
            [
                LeadRecord(
                    source="hn",
                    external_id="won-1",
                    title="Won deal",
                    status=LeadStatus.won,
                ),
                LeadRecord(
                    source="hn",
                    external_id="lost-1",
                    title="Lost deal",
                    status=LeadStatus.lost,
                ),
                LeadRecord(
                    source="hn",
                    external_id="wip-1",
                    title="In progress deal",
                    status=LeadStatus.drafted,
                ),
            ]
        )
        s.add(
            RunRecord(
                workflow="outreach",
                ok=True,
                cost_usd=0.42,
                stats={"emailed": 2, "skipped": 1},
                created_at=now,
            )
        )
        s.add(
            RunRecord(
                workflow="reply",
                ok=False,
                cost_usd=0.08,
                stats={},
                error="IMAP timeout",
                created_at=now,
            )
        )
        s.add_all(
            [
                ReplyRecord(
                    email="alice@example.com",
                    direction="out",
                    subject="Quick idea for your API",
                    snippet="Hi Alice, ...",
                    created_at=now - _dt.timedelta(minutes=10),
                ),
                ReplyRecord(
                    email="alice@example.com",
                    direction="in",
                    subject="Re: Quick idea for your API",
                    snippet="Sounds interesting, tell me more.",
                    created_at=now - _dt.timedelta(minutes=5),
                ),
            ]
        )
        s.commit()

    return SessionLocal


def test_funnel_stats(seeded):
    import analytics

    stats = analytics.funnel_stats()
    assert stats["emailed"] == 2
    assert stats["replied"] == 1
    assert stats["reply_rate"] == 0.5
    assert stats["won"] == 1
    assert stats["lost"] == 1
    assert stats["in_progress"] == 1
    assert stats["suppressed"] == 1
    assert stats["emails_today"] == 2
    assert stats["total_cost_usd"] == pytest.approx(0.5)


def test_recent_runs(seeded):
    import analytics

    runs = analytics.recent_runs()
    assert len(runs) == 2
    workflows = {r["workflow"] for r in runs}
    assert workflows == {"outreach", "reply"}
    fail = next(r for r in runs if not r["ok"])
    assert fail["error"] == "IMAP timeout"


def test_outreach_list(seeded):
    import analytics

    rows = analytics.outreach_list()
    assert len(rows) == 3
    alice = next(r for r in rows if r["email"] == "alice@example.com")
    assert alice["replied"] is True
    assert alice["followups_sent"] == 1


def test_conversations_grouping(seeded):
    import analytics

    threads = analytics.conversations()
    assert len(threads) == 1
    thread = threads[0]
    assert thread["email"] == "alice@example.com"
    assert [m["direction"] for m in thread["messages"]] == ["out", "in"]
    assert thread["messages"][1]["snippet"] == "Sounds interesting, tell me more."
