"""Offline tests for the Claude cost tracker + per-run budget guardrail."""
from __future__ import annotations

import pytest

from costs import PRICING, BudgetExhausted, CostTracker


def test_pricing_table_opus_and_sonnet():
    assert PRICING["claude-opus-4-8"] == (5.0, 25.0)
    assert PRICING["claude-sonnet-4-6"] == (3.0, 15.0)


def test_usd_accumulation_opus_math():
    tracker = CostTracker()
    # 1M input @ $5 + 1M output @ $25 = $30.
    tracker.record("claude-opus-4-8", 1_000_000, 1_000_000)
    assert tracker.usd() == pytest.approx(30.0)
    # Add 0.5M input @ $5 = +$2.50.
    tracker.record("claude-opus-4-8", 500_000, 0)
    assert tracker.usd() == pytest.approx(32.5)
    assert tracker.calls == 2


def test_unknown_model_falls_back_to_opus_pricing():
    tracker = CostTracker()
    tracker.record("some-future-model", 1_000_000, 0)
    assert tracker.usd() == pytest.approx(5.0)


def test_would_exceed_and_check_raises():
    tracker = CostTracker(budget_usd=2.0)
    assert tracker.would_exceed() is False
    tracker.check()  # under budget -> no raise

    # 1M output @ $25 = $25, well over the $2 cap.
    tracker.record("claude-opus-4-8", 0, 1_000_000)
    assert tracker.would_exceed() is True
    with pytest.raises(BudgetExhausted):
        tracker.check()


def test_no_budget_never_exceeds():
    tracker = CostTracker(budget_usd=None)
    tracker.record("claude-opus-4-8", 0, 10_000_000)
    assert tracker.would_exceed() is False
    tracker.check()  # no raise
