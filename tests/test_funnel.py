"""Offline tests for funnel-stall detection (monitor/funnel.py).

These encode the incident these checks exist for: 24 consecutive production runs
reported ``success`` while sending 1 email in a month. Every assertion below would
have failed the run at the time, which is the whole point — a green run that emails
nobody must not be reported as healthy.

No network, no API key: each test seeds ``RunRecord`` rows in an isolated SQLite DB.
"""
from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
import monitor.funnel as funnel
from db.models import Base, RunRecord


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'funnel.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    yield engine


def _seed(runs: list[dict], workflow: str = "outreach") -> None:
    """Insert runs oldest-first; ``created_at`` is spaced so ordering is stable."""
    base = _dt.datetime(2026, 7, 1, 6, 0, 0)
    with dbsession.get_session() as session:
        for i, stats in enumerate(runs):
            session.add(
                RunRecord(
                    workflow=workflow,
                    ok=True,
                    cost_usd=0.35,
                    stats=stats,
                    created_at=base + _dt.timedelta(days=i),
                )
            )


def _settings(monkeypatch, **overrides):
    """Pin the funnel thresholds regardless of the developer's .env."""
    import config

    real = config.get_settings

    def s():
        cfg = real()
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg

    monkeypatch.setattr(config, "get_settings", s)


# --------------------------------------------------------------------------- #
# outreach_flow — consecutive zero-send runs
# --------------------------------------------------------------------------- #
def test_no_runs_is_not_a_failure(temp_db):
    """A brand-new install has no history; that is not a stall."""
    for check in funnel.funnel_checks():
        assert check["ok"] is True, check


def test_zero_send_streak_at_threshold_fails(temp_db, monkeypatch):
    _settings(monkeypatch, alert_after_zero_email_runs=3)
    _seed([{"emailed": 2}, {"emailed": 0}, {"emailed": 0}, {"emailed": 0}])

    result = funnel.check_outreach_stalled()
    assert result["ok"] is False
    assert "3 consecutive runs sent 0 emails" in result["detail"]


def test_zero_send_streak_below_threshold_passes(temp_db, monkeypatch):
    _settings(monkeypatch, alert_after_zero_email_runs=3)
    _seed([{"emailed": 1}, {"emailed": 0}, {"emailed": 0}])

    assert funnel.check_outreach_stalled()["ok"] is True


def test_recent_send_resets_the_streak(temp_db, monkeypatch):
    """A long drought followed by one send is healthy — the streak is *leading*."""
    _settings(monkeypatch, alert_after_zero_email_runs=3)
    _seed([{"emailed": 0}] * 10 + [{"emailed": 1}])

    assert funnel.check_outreach_stalled()["ok"] is True


def test_the_real_month_would_have_failed(temp_db, monkeypatch):
    """Regression guard using the actual July 2026 shape: 1 email in 24 runs.

    The single send landed early, so the tail is a long zero streak.
    """
    _settings(monkeypatch, alert_after_zero_email_runs=3, alert_after_zero_queue_runs=5)
    runs = [{"emailed": 1, "queued": 1, "contactable": 1}]
    runs += [{"emailed": 0, "queued": 0, "contactable": 0}] * 23
    _seed(runs)

    results = {c["name"]: c for c in funnel.funnel_checks()}
    assert results["outreach_flow"]["ok"] is False
    assert results["queue_flow"]["ok"] is False
    assert results["contactable_supply"]["ok"] is False


# --------------------------------------------------------------------------- #
# queue_flow — nothing clearing the fit bar
# --------------------------------------------------------------------------- #
def test_zero_queue_streak_fails(temp_db, monkeypatch):
    _settings(monkeypatch, alert_after_zero_queue_runs=5)
    _seed([{"queued": 0}] * 5)

    result = funnel.check_queue_stalled()
    assert result["ok"] is False
    assert "min_fit_score" in result["detail"]  # points at the actual lever


# --------------------------------------------------------------------------- #
# contactable_supply — the leading indicator
# --------------------------------------------------------------------------- #
def test_contactable_below_floor_fails(temp_db, monkeypatch):
    _settings(monkeypatch, min_contactable_per_run=1)
    _seed([{"emailed": 1, "contactable": 4}, {"emailed": 1, "contactable": 0}])

    result = funnel.check_contactable_supply()
    assert result["ok"] is False
    assert "sourcing problem" in result["detail"]


def test_contactable_metric_absent_is_tolerated(temp_db, monkeypatch):
    """Runs recorded before the metric existed must not be reported as broken."""
    _settings(monkeypatch, min_contactable_per_run=1)
    _seed([{"emailed": 1, "queued": 1}])

    result = funnel.check_contactable_supply()
    assert result["ok"] is True
    assert "not recorded" in result["detail"]


def test_only_the_latest_run_decides_contactable(temp_db, monkeypatch):
    """Recovery is immediate: yesterday's zero must not fail today's healthy run."""
    _settings(monkeypatch, min_contactable_per_run=1)
    _seed([{"contactable": 0}, {"contactable": 6}])

    assert funnel.check_contactable_supply()["ok"] is True


# --------------------------------------------------------------------------- #
# scoping
# --------------------------------------------------------------------------- #
def test_linkedin_runs_do_not_count_as_outreach(temp_db, monkeypatch):
    """The linkedin/monitor workflows never email prospects.

    Counting them would make the streak meaningless: they'd contribute a zero every
    single day and permanently pin the check to "failed".
    """
    _settings(monkeypatch, alert_after_zero_email_runs=3)
    _seed([{"emailed": 0}] * 10, workflow="linkedin")

    assert funnel.check_outreach_stalled()["ok"] is True


def test_failed_runs_are_excluded_from_the_streak(temp_db, monkeypatch):
    """A crashed run already alerts via the failure path; it isn't a *stall*."""
    _settings(monkeypatch, alert_after_zero_email_runs=3)
    base = _dt.datetime(2026, 7, 1, 6, 0, 0)
    with dbsession.get_session() as session:
        session.add(
            RunRecord(
                workflow="outreach", ok=True, stats={"emailed": 3}, created_at=base
            )
        )
        for i in range(1, 6):
            session.add(
                RunRecord(
                    workflow="outreach",
                    ok=False,
                    stats={},
                    error="boom",
                    created_at=base + _dt.timedelta(days=i),
                )
            )

    assert funnel.check_outreach_stalled()["ok"] is True


def test_non_numeric_stats_do_not_raise(temp_db, monkeypatch):
    """Stats come from JSON: a string or None must degrade, never crash the monitor."""
    _settings(monkeypatch, alert_after_zero_email_runs=2)
    _seed([{"emailed": None}, {"emailed": "0"}])

    result = funnel.check_outreach_stalled()
    assert result["ok"] is False  # both parse as zero


def test_doctor_includes_the_funnel_checks(temp_db, monkeypatch):
    """The checks must be reachable through the healthcheck the cron actually runs."""
    import monitor.doctor as doctor

    names = {c["name"] for c in doctor.run_healthcheck()["checks"]}
    assert {"outreach_flow", "queue_flow", "contactable_supply"} <= names
