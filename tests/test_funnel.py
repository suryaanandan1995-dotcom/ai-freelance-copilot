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
# contactable_supply — the intersection, not the raw count
#
# Friday 2026-08-14 (monitor run 31785221158) printed "[ok] contactable_supply: 29
# contactable lead(s)" on the THIRD consecutive run that emailed nobody. Both halves
# were true: 29 addresses existed, and not one belonged to a lead worth writing to.
# A floor of 1 on a number that sat at 28-47 every single run is a check that cannot
# fail for the reason it exists.
# --------------------------------------------------------------------------- #
def test_addresses_without_qualified_leads_is_reported_as_a_failure(temp_db, monkeypatch):
    """Friday 08-14, reproduced: 29 contactable, 42 over the bar, all 42 unreachable."""
    _settings(monkeypatch, min_contactable_per_run=1)
    _seed([{
        "emailed": 0,
        "queued": 0,
        "contactable": 29,
        "fit": {"passed": 42},
        "apply_yourself": [{"fit_score": 80}] * 42,
    }])

    result = funnel.check_contactable_supply()
    assert result["ok"] is False
    # Both numbers, because the GAP between them is the diagnosis.
    assert "29 contactable" in result["detail"]
    assert "only 0" in result["detail"]
    # And the lever it must not send the reader towards.
    assert "different sources" in result["detail"]


def test_a_run_with_a_real_intersection_passes(temp_db, monkeypatch):
    """Tuesday 08-11: 47 contactable, 38 passed, 34 unreachable -> 4 actionable, 4 sent."""
    _settings(monkeypatch, min_contactable_per_run=1)
    _seed([{
        "emailed": 4,
        "queued": 4,
        "contactable": 47,
        "fit": {"passed": 38},
        "apply_yourself": [{"fit_score": 80}] * 34,
    }])

    result = funnel.check_contactable_supply()
    assert result["ok"] is True
    assert "4 qualified + reachable" in result["detail"]
    assert "47 contactable" in result["detail"]


def test_a_run_predating_the_fit_block_falls_back_to_queued(temp_db, monkeypatch):
    """``queued`` is the honest proxy: an uncontactable lead is drafted with
    draft_allowed=False and so can never reach the queue."""
    _settings(monkeypatch, min_contactable_per_run=1)
    _seed([{"contactable": 30, "queued": 0}])

    result = funnel.check_contactable_supply()
    assert result["ok"] is False
    assert "only 0" in result["detail"]


def test_a_run_recording_neither_fit_nor_queued_measures_nothing(temp_db, monkeypatch):
    """A missing input must say it measured nothing. Returning 0 would assert an empty
    intersection it never observed, and fail every historical row."""
    _settings(monkeypatch, min_contactable_per_run=1)
    _seed([{"contactable": 30}])

    result = funnel.check_contactable_supply()
    assert result["ok"] is True
    assert "not recorded" in result["detail"]


def test_the_raw_sourcing_collapse_still_reports_as_sourcing(temp_db, monkeypatch):
    """The two failures need opposite fixes, so they must not share one message: zero
    addresses is a sourcing problem, addresses-without-good-leads is a routing one."""
    _settings(monkeypatch, min_contactable_per_run=1)
    _seed([{"contactable": 0, "fit": {"passed": 40}, "apply_yourself": []}])

    result = funnel.check_contactable_supply()
    assert result["ok"] is False
    assert "sourcing problem" in result["detail"]
    assert "different sources" not in result["detail"]


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


# --------------------------------------------------------------------------- #
# discovery — running, and producing nothing
# --------------------------------------------------------------------------- #
def test_discovery_finding_nothing_across_runs_fails(temp_db, monkeypatch):
    """The failure mode discovery has: it fetches, it is refused, it reports zero.

    A blocked user-agent and a world with no published addresses produce the identical
    ``discovered: 0``, so the check names what it cannot distinguish instead of asserting
    a cause — but it must fire, because the alternative is an ``outreach_flow`` alert
    days later pointing at sends.
    """
    _settings(monkeypatch, discover_contacts=True, alert_after_zero_email_runs=3)
    _seed([{"discovery_attempts": 12, "discovered": 0} for _ in range(3)])

    result = funnel.check_discovery_productive()
    assert result["ok"] is False
    assert "36 lookup(s)" in result["detail"]  # the denominator, not a bare zero


def test_a_single_found_address_is_enough_to_pass(temp_db, monkeypatch):
    """The check watches for a dead mechanism, not for a low yield.

    Discovery hitting on 1 of 36 is a tuning question the digest already reports; it is
    not the thing that needs waking somebody up.
    """
    _settings(monkeypatch, discover_contacts=True, alert_after_zero_email_runs=3)
    _seed(
        [
            {"discovery_attempts": 12, "discovered": 0},
            {"discovery_attempts": 12, "discovered": 1},
            {"discovery_attempts": 12, "discovered": 0},
        ]
    )
    assert funnel.check_discovery_productive()["ok"] is True


def test_runs_that_never_looked_are_not_evidence(temp_db, monkeypatch):
    """``0 of 0`` and ``0 of 40`` need opposite fixes, so only the second counts.

    Zero attempts means every qualified lead already published an address — the good
    case. Counting it toward the streak would make the check fire hardest exactly when
    the funnel is healthiest, which is how a guard trains its reader to ignore it.
    """
    _settings(monkeypatch, discover_contacts=True, alert_after_zero_email_runs=2)
    _seed([{"discovery_attempts": 0, "discovered": 0} for _ in range(6)])

    result = funnel.check_discovery_productive()
    assert result["ok"] is True
    assert "0 run(s) have attempted discovery" in result["detail"]


def test_one_barren_run_is_too_early_to_judge(temp_db, monkeypatch):
    """Discovery only fires on qualified-uncontactable leads, so a run can legitimately
    attempt two lookups and find nothing. The streak is measured in runs that looked."""
    _settings(monkeypatch, discover_contacts=True, alert_after_zero_email_runs=3)
    _seed(
        [
            {"discovery_attempts": 0, "discovered": 0},
            {"discovery_attempts": 2, "discovered": 0},
        ]
    )
    result = funnel.check_discovery_productive()
    assert result["ok"] is True
    assert "1 run(s)" in result["detail"]


def test_discovery_disabled_is_not_a_failure(temp_db, monkeypatch):
    """A switched-off feature is a decision, not an outage — and the old runs in history
    would otherwise keep alerting about a mechanism nobody is running."""
    _settings(monkeypatch, discover_contacts=False, alert_after_zero_email_runs=1)
    _seed([{"discovery_attempts": 40, "discovered": 0} for _ in range(4)])

    result = funnel.check_discovery_productive()
    assert result["ok"] is True
    assert "disabled" in result["detail"]


def test_runs_predating_discovery_do_not_alert(temp_db, monkeypatch):
    """Rows recorded before these counters existed carry neither key. A missing input
    must read as "measured nothing", not as a measured zero."""
    _settings(monkeypatch, discover_contacts=True, alert_after_zero_email_runs=2)
    _seed([{"emailed": 1, "queued": 3} for _ in range(5)])

    assert funnel.check_discovery_productive()["ok"] is True


def test_non_numeric_discovery_stats_do_not_raise(temp_db, monkeypatch):
    """Stats are JSON from a past version of the code; the monitor must degrade."""
    _settings(monkeypatch, discover_contacts=True, alert_after_zero_email_runs=2)
    _seed([{"discovery_attempts": "lots", "discovered": None}, {"discovery_attempts": None}])

    assert funnel.check_discovery_productive()["ok"] is True


def test_doctor_includes_the_discovery_check(temp_db, monkeypatch):
    """Registered, not merely defined. The last four checks in this file were each written
    before the function that runs them knew they existed."""
    import monitor.doctor as doctor

    _settings(monkeypatch, discover_contacts=True)
    names = {c["name"] for c in doctor.run_healthcheck()["checks"]}
    assert "discovery" in names


# --------------------------------------------------------------------------- #
# lead_errors — the quiet partial failure the per-lead catch created
# --------------------------------------------------------------------------- #
def test_a_run_that_dropped_a_third_of_its_leads_is_a_failure(temp_db, monkeypatch):
    """Catching per-lead exceptions traded a loud total failure for a quiet partial one.
    This is the check that keeps the quiet one from reading as a clean run."""
    _seed([{"new": 175, "lead_errors": 60, "emailed": 1, "queued": 2}])

    result = funnel.check_lead_errors()
    assert result["ok"] is False
    assert "60 of 175" in result["detail"] and "34%" in result["detail"]


def test_one_bad_model_response_in_175_is_the_weather(temp_db, monkeypatch):
    """Ratio, not count: an occasional malformed response is not an incident, and a check
    that fires on it gets muted, taking the 34% case with it."""
    _seed([{"new": 175, "lead_errors": 1, "emailed": 1}])
    assert funnel.check_lead_errors()["ok"] is True


def test_no_lead_errors_passes_cleanly(temp_db, monkeypatch):
    _seed([{"new": 175, "lead_errors": 0}])
    result = funnel.check_lead_errors()
    assert result["ok"] is True
    assert "no leads failed" in result["detail"]


def test_errors_with_no_lead_total_are_still_reported(temp_db, monkeypatch):
    """A missing denominator must not silently become a passing rate."""
    _seed([{"lead_errors": 4}])
    result = funnel.check_lead_errors()
    assert result["ok"] is False
    assert "unrecorded" in result["detail"]


def test_runs_predating_the_lead_error_counter_do_not_alert(temp_db, monkeypatch):
    _seed([{"new": 175, "emailed": 2}])
    assert funnel.check_lead_errors()["ok"] is True


def test_doctor_includes_the_lead_error_check(temp_db, monkeypatch):
    import monitor.doctor as doctor

    names = {c["name"] for c in doctor.run_healthcheck()["checks"]}
    assert "lead_errors" in names
