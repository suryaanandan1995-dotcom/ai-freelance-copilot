"""Offline tests for the shared send counter and the warmup ramp.

Each test gets an isolated SQLite database by rebinding ``db.session``'s engine +
sessionmaker to a fresh temp file (same pattern as ``test_followup.py``).

The ramp exists to protect one unrecoverable asset: across 2026-08-10..17 the
pipeline sent 6 cold emails in ten days against a cap of 20, because only 7 leads
were both qualified and reachable. Contact discovery is built to remove that
constraint, so the first run after it works can present dozens of sendable leads
at once — and a mailbox averaging under 4 messages a day jumping to its ceiling is
the one failure in this project that fixing the code afterwards cannot undo.
"""
from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
from db.models import Base, OutreachRecord
from outreach.quota import (
    WARMUP_FLOOR,
    WARMUP_GROWTH,
    effective_cap,
    peak_daily_sends,
)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'quota.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    yield engine


def _cold(email: str, when: _dt.datetime, *, status: str = "sent") -> OutreachRecord:
    return OutreachRecord(
        email=email, subject="s", status=status, sent_at=when, last_contact_at=when
    )


def _followup(email: str, when: _dt.datetime, *, touches: int = 1) -> OutreachRecord:
    """A record whose LAST touch was a follow-up sent at ``when``.

    ``sent_at`` is deliberately outside every lookback window used here so the row
    contributes exactly one send to ``when``'s day and the cold-email column can't
    quietly inflate the peak.
    """
    return OutreachRecord(
        email=email,
        subject="s",
        status="sent",
        sent_at=when - _dt.timedelta(days=90),
        last_contact_at=when,
        followups_sent=touches,
    )


def _days_ago(n: int) -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=n)


# --------------------------------------------------------------------------- #
# peak_daily_sends
# --------------------------------------------------------------------------- #
def test_no_history_measures_no_peak(temp_db):
    """An empty table is 0 sends, not an error and not a guess."""
    with dbsession.get_session() as session:
        assert peak_daily_sends(session) == 0


def test_the_peak_is_the_busiest_single_day_not_the_total(temp_db):
    """3 + 1 + 1 across three days is a peak of 3.

    The distinction is the whole point: a mailbox that sent 5 messages over a week
    has not proven it can send 5 in an hour, and the receiving filters score the
    day, not the week.
    """
    with dbsession.get_session() as session:
        for i in range(3):
            session.add(_cold(f"a{i}@acme.com", _days_ago(5)))
        session.add(_cold("b@acme.com", _days_ago(4)))
        session.add(_cold("c@acme.com", _days_ago(3)))
        session.commit()
    with dbsession.get_session() as session:
        assert peak_daily_sends(session) == 3


def test_a_followup_wave_counts_as_proven_capacity(temp_db):
    """The 16-message day of 2026-08-14 was follow-ups, and it still happened.

    Counting only cold sends would forget it and throttle the mailbox below what it
    has demonstrably done without incident — the ramp would become a regression.
    """
    with dbsession.get_session() as session:
        for i in range(16):
            session.add(_followup(f"f{i}@acme.com", _days_ago(3)))
        session.commit()
    with dbsession.get_session() as session:
        assert peak_daily_sends(session) == 16


def test_history_older_than_the_window_is_not_capacity(temp_db):
    """A 40-send day last quarter says nothing about this mailbox today."""
    with dbsession.get_session() as session:
        for i in range(40):
            session.add(_cold(f"old{i}@acme.com", _days_ago(60)))
        session.commit()
    with dbsession.get_session() as session:
        assert peak_daily_sends(session) == 0


def test_a_failed_send_is_not_capacity(temp_db):
    """``status != "sent"`` never reached a recipient, so it proved nothing.

    Same rule ``emails_sent_today`` follows: an SMTP error must not buy headroom.
    """
    with dbsession.get_session() as session:
        for i in range(9):
            session.add(_cold(f"x{i}@acme.com", _days_ago(2), status="failed"))
        session.commit()
    with dbsession.get_session() as session:
        assert peak_daily_sends(session) == 0


def test_the_lookback_window_is_adjustable_and_bounded(temp_db):
    """A caller may narrow the window; ``0`` degrades to one day, never to zero days.

    ``max(1, ...)`` matters because a 0-day lookback would compare against a cutoff
    of midnight-today and read the mailbox as brand new every morning.
    """
    with dbsession.get_session() as session:
        session.add(_cold("y@acme.com", _days_ago(5)))
        session.commit()
    with dbsession.get_session() as session:
        assert peak_daily_sends(session, lookback_days=10) == 1
        assert peak_daily_sends(session, lookback_days=2) == 0
        assert peak_daily_sends(session, lookback_days=0) == 0


# --------------------------------------------------------------------------- #
# effective_cap
# --------------------------------------------------------------------------- #
def test_a_cold_mailbox_gets_the_warmup_floor(temp_db):
    """No history and a cap of 20 permits WARMUP_FLOOR today, not 20."""
    with dbsession.get_session() as session:
        assert effective_cap(session, 20) == WARMUP_FLOOR


def test_proven_volume_raises_the_ramp_above_the_floor(temp_db):
    """A peak of 16 permits int(16 * 1.5) = 24 — if the config allows that much."""
    with dbsession.get_session() as session:
        for i in range(16):
            session.add(_followup(f"f{i}@acme.com", _days_ago(3)))
        session.commit()
    with dbsession.get_session() as session:
        assert effective_cap(session, 40) == int(16 * WARMUP_GROWTH)


def test_the_configured_cap_is_still_the_ceiling(temp_db):
    """The ramp may only ever LOWER the limit.

    This is what makes raising ``max_emails_per_day`` safe: the new number becomes a
    target the ramp walks toward over a few days rather than tomorrow's volume. It
    is also why the ramp cannot resurrect the per-channel cap bug — it never adds
    headroom to a configured ceiling, from either send path.
    """
    with dbsession.get_session() as session:
        for i in range(16):
            session.add(_followup(f"f{i}@acme.com", _days_ago(3)))
        session.commit()
    with dbsession.get_session() as session:
        # Ramp says 24, config says 20 — config wins.
        assert effective_cap(session, 20) == 20


def test_a_disabled_cap_stays_disabled(temp_db):
    """0 (or negative) means "send nothing"; the floor must not turn that into 10."""
    with dbsession.get_session() as session:
        assert effective_cap(session, 0) == 0
        assert effective_cap(session, -5) == 0


def test_timestamps_read_back_naive_are_still_counted(temp_db):
    """The columns are plain ``DateTime``, so the DB returns them without a tzinfo.

    Written aware, read naive: comparing one to an aware cutoff raises TypeError, which
    ``effective_cap`` catches — so this bug presented as "the ramp does nothing" with
    every other test green. Asserted through the DB rather than by calling ``_as_utc``
    directly, because the defect lived in the round trip, not in the arithmetic.
    """
    with dbsession.get_session() as session:
        for i in range(4):
            session.add(_cold(f"n{i}@acme.com", _days_ago(1)))
        session.commit()
    with dbsession.get_session() as session:
        row = session.query(OutreachRecord).first()
        assert row.sent_at.tzinfo is None, "precondition: the column is tz-naive"
        assert peak_daily_sends(session) == 4
        assert effective_cap(session, 40) == WARMUP_FLOOR  # int(4*1.5)=6 < floor 10


def test_the_fallback_says_it_is_not_ramping(temp_db, caplog):
    """A permissive fallback that logs nothing is indistinguishable from a working guard.

    That is precisely how the naive-timestamp bug above stayed invisible, so the branch
    has to name itself in the run log.
    """

    class _Exploding:
        def query(self, *_a, **_k):
            raise RuntimeError("connection reset")

    with caplog.at_level("WARNING"):
        assert effective_cap(_Exploding(), 20) == 20
    assert "NO warmup ramp" in caplog.text
    assert "connection reset" in caplog.text


def test_an_unreadable_history_does_not_block_sending(temp_db):
    """A DB fault falls back to the configured cap instead of failing closed.

    Failing closed would silently stop all outreach on an error unrelated to
    sending, and a stopped pipeline is discovered days later; over-sending by one
    day's worth is recoverable. This is the cheaper of the two mistakes, but only
    because the configured cap still bounds it.
    """

    class _Exploding:
        def query(self, *_a, **_k):
            raise RuntimeError("connection reset")

    assert effective_cap(_Exploding(), 20) == 20


def test_both_send_paths_are_ramped_by_the_same_number(temp_db):
    """The cold path and the follow-up path must read one ceiling, not two.

    A ramp one channel can walk around is the per-channel cap bug again, and that
    one let a configured 20 send 40 messages from a single mailbox. Both call sites
    are asserted structurally because the two paths agree only as long as neither
    grows its own copy of the arithmetic — which is exactly how they diverged before.
    """
    import inspect

    import followup.runner as followup_mod
    import pipeline as pipeline_mod

    assert "effective_cap" in inspect.getsource(followup_mod.run_followups)
    assert "effective_cap" in inspect.getsource(pipeline_mod._maybe_email_lead)

    # And the number itself is one number: a 12-send peak ramps to 18, which the
    # cap of 20 permits, so both paths see 18 rather than one seeing the raw 20.
    with dbsession.get_session() as session:
        for i in range(12):
            session.add(_followup(f"f{i}@acme.com", _days_ago(2)))
        session.commit()
    with dbsession.get_session() as session:
        assert effective_cap(session, 20) == 18
