"""Autonomous self-optimizer for the outreach STRATEGY.

The optimizer tunes only the message strategy (which pitch/subject variant and a
bounded fit threshold), measures the resulting reply rate, and auto-reverts a
change that hurts. It NEVER edits Python source and NEVER touches any safety
invariant (auto-submit stays off, opt-out/caps/pricing->call are all off-limits).
"""
from __future__ import annotations

from optimizer.optimizer import (
    DEFAULT_STRATEGY,
    PITCH_VARIANTS,
    SUBJECT_STYLES,
    active_strategy,
    run_optimizer,
)

__all__ = [
    "DEFAULT_STRATEGY",
    "PITCH_VARIANTS",
    "SUBJECT_STYLES",
    "active_strategy",
    "run_optimizer",
]
