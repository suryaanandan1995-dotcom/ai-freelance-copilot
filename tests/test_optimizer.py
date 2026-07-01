"""Offline tests for the autonomous self-optimizer (no API key, no network).

Each DB-touching test gets its own isolated SQLite database by rebinding
``db.session``'s engine + sessionmaker to a fresh temp file (same pattern as
``test_outreach``). ``get_settings`` is patched per test to toggle the gate /
sample thresholds without mutating the process environment.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import db.session as dbsession
from db.models import Base, OutreachRecord, StrategyRecord
from optimizer.optimizer import (
    PITCH_VARIANTS,
    SUBJECT_STYLES,
    active_strategy,
    run_optimizer,
)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    yield engine


def _patch_settings(monkeypatch, **overrides):
    """Patch get_settings everywhere the optimizer reads it (config + module)."""
    real = config.get_settings

    def fake():
        cfg = real()
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg

    monkeypatch.setattr(config, "get_settings", fake)
    monkeypatch.setattr("optimizer.optimizer.get_settings", fake)
    return fake


def _seed_outreach(sent: int, replied: int) -> None:
    """Insert ``sent`` 'sent' rows, of which ``replied`` have replied==True."""
    with dbsession.get_session() as session:
        for i in range(sent):
            session.add(
                OutreachRecord(
                    email=f"lead-{i}@example.com",
                    status="sent",
                    replied=(i < replied),
                )
            )


# --------------------------------------------------------------------------- #
# gate + data thresholds
# --------------------------------------------------------------------------- #
def test_disabled_when_self_optimize_off(temp_db, monkeypatch):
    _patch_settings(monkeypatch, self_optimize=False)
    result = run_optimizer()
    assert result["action"] == "disabled"


def test_insufficient_data_below_min_samples(temp_db, monkeypatch):
    _patch_settings(monkeypatch, self_optimize=True, optimize_min_samples=20)
    _seed_outreach(sent=5, replied=1)
    result = run_optimizer()
    assert result["action"] == "insufficient_data"
    assert result["samples"] == 5
    assert result["reply_rate"] == pytest.approx(1 / 5)


# --------------------------------------------------------------------------- #
# active_strategy offline safety
# --------------------------------------------------------------------------- #
def test_active_strategy_returns_default_without_table(tmp_path, monkeypatch):
    # A fresh engine with NO tables created — active_strategy must not raise.
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)

    strat = active_strategy()
    assert strat["pitch_variant"] in PITCH_VARIANTS
    assert strat["subject_style"] in SUBJECT_STYLES
    assert 70 <= strat["fit_threshold"] <= 90


def test_active_strategy_returns_default_when_no_row(temp_db):
    # Table exists but is empty -> DEFAULT, and no row is created.
    strat = active_strategy()
    assert strat["pitch_variant"] == PITCH_VARIANTS[0]
    assert strat["subject_style"] == SUBJECT_STYLES[0]
    with dbsession.get_session() as session:
        assert session.query(StrategyRecord).count() == 0


# --------------------------------------------------------------------------- #
# tune
# --------------------------------------------------------------------------- #
def test_tune_creates_active_strategy_with_current_baseline(temp_db, monkeypatch):
    _patch_settings(monkeypatch, self_optimize=True, optimize_min_samples=20)
    _seed_outreach(sent=40, replied=10)  # rate = 0.25
    result = run_optimizer()

    assert result["action"] == "tune"
    assert result["reply_rate"] == pytest.approx(0.25)
    assert result["samples"] == 40

    with dbsession.get_session() as session:
        active = (
            session.query(StrategyRecord).filter(StrategyRecord.active.is_(True)).one()
        )
        assert active.baseline_reply_rate == pytest.approx(0.25)
        assert active.params["pitch_variant"] in PITCH_VARIANTS
        assert active.params["subject_style"] in SUBJECT_STYLES
        assert 70 <= active.params["fit_threshold"] <= 90
        # active_strategy() should now reflect the freshly-tuned row.
        assert active_strategy()["pitch_variant"] == active.params["pitch_variant"]


def test_tuned_params_stay_in_allowed_search_space(temp_db, monkeypatch):
    _patch_settings(monkeypatch, self_optimize=True, optimize_min_samples=20)
    _seed_outreach(sent=30, replied=6)

    # Run several tuning steps; every produced strategy must stay in-bounds.
    for _ in range(6):
        result = run_optimizer()
        assert result["action"] == "tune"
        strat = result["strategy"]
        assert strat["pitch_variant"] in PITCH_VARIANTS
        assert strat["subject_style"] in SUBJECT_STYLES
        assert 70 <= strat["fit_threshold"] <= 90


def test_tune_is_deterministic_rotation(temp_db, monkeypatch):
    _patch_settings(monkeypatch, self_optimize=True, optimize_min_samples=20)
    _seed_outreach(sent=25, replied=5)

    first = run_optimizer()["strategy"]
    # From the DEFAULT ("direct"/"plain"), the first rotation advances the pitch.
    assert first["pitch_variant"] == PITCH_VARIANTS[1]
    assert first["subject_style"] == SUBJECT_STYLES[0]


# --------------------------------------------------------------------------- #
# revert
# --------------------------------------------------------------------------- #
def test_revert_when_trial_reply_rate_drops(temp_db, monkeypatch):
    _patch_settings(
        monkeypatch,
        self_optimize=True,
        optimize_min_samples=20,
        optimize_revert_drop=0.05,
    )
    # A prior (baseline) strategy and an active trial with a high baseline the
    # current low reply rate clearly fails to beat.
    with dbsession.get_session() as session:
        session.add(
            StrategyRecord(
                version=1,
                params={
                    "pitch_variant": "direct",
                    "subject_style": "plain",
                    "fit_threshold": 80,
                },
                active=False,
                baseline_reply_rate=0.30,
                note="baseline",
            )
        )
        session.add(
            StrategyRecord(
                version=2,
                params={
                    "pitch_variant": "problem-first",
                    "subject_style": "plain",
                    "fit_threshold": 80,
                },
                active=True,
                baseline_reply_rate=0.30,
                note="trial",
            )
        )
    # Current observed rate = 0.10, well below 0.30 - 0.05.
    _seed_outreach(sent=40, replied=4)

    result = run_optimizer()
    assert result["action"] == "revert"
    assert result["reverted_to_version"] == 1

    with dbsession.get_session() as session:
        active = (
            session.query(StrategyRecord).filter(StrategyRecord.active.is_(True)).one()
        )
        assert active.version == 1  # the prior strategy is active again
        assert active.params["pitch_variant"] == "direct"


def test_no_revert_when_rate_holds_then_tunes(temp_db, monkeypatch):
    _patch_settings(
        monkeypatch,
        self_optimize=True,
        optimize_min_samples=20,
        optimize_revert_drop=0.05,
    )
    with dbsession.get_session() as session:
        session.add(
            StrategyRecord(
                version=1,
                params={
                    "pitch_variant": "direct",
                    "subject_style": "plain",
                    "fit_threshold": 80,
                },
                active=False,
                baseline_reply_rate=0.20,
                note="baseline",
            )
        )
        session.add(
            StrategyRecord(
                version=2,
                params={
                    "pitch_variant": "problem-first",
                    "subject_style": "plain",
                    "fit_threshold": 80,
                },
                active=True,
                baseline_reply_rate=0.20,
                note="trial",
            )
        )
    # Current rate = 0.22, which holds vs the 0.20 baseline -> tune, not revert.
    _seed_outreach(sent=50, replied=11)

    result = run_optimizer()
    assert result["action"] == "tune"
    with dbsession.get_session() as session:
        active = (
            session.query(StrategyRecord).filter(StrategyRecord.active.is_(True)).one()
        )
        assert active.version == 3  # a new trial was promoted
