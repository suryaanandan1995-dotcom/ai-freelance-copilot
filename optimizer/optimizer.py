"""Self-optimizing outreach STRATEGY tuner.

What it does (and only this):
  * Rotates the pitch variant, then the subject style, through fixed, code-defined
    lists, and keeps a bounded fit threshold.
  * Measures the observed reply rate from ``OutreachRecord`` rows.
  * Promotes a new strategy version when there is enough data, and AUTO-REVERTS a
    trial whose reply rate dropped materially below its predecessor's baseline.

What it will NEVER do (hard boundaries — these are safety invariants, not tunables):
  * It never edits Python source code.
  * The search space is exactly ``PITCH_VARIANTS`` x ``SUBJECT_STYLES`` plus a
    fit threshold clamped to [70, 90]. It writes no other field on the strategy.
  * It never flips a send/auto-submit/opt-out/cap/pricing flag — it does not touch
    ``Settings`` at all; it only reads ``outreach_min_fit`` for the default.
  * ``active_strategy()`` is fully offline-safe: if the DB or table is missing it
    returns ``DEFAULT_STRATEGY`` and never raises, so the pitch never breaks.

Deterministic by construction: the next candidate is a pure rotation of the
current variant indices, so the whole thing is offline-testable without an LLM.
"""
from __future__ import annotations

import logging

from config import get_settings

logger = logging.getLogger(__name__)

# The ONLY safe search space. The optimizer must never invent values outside
# these lists; anything else would be an unbounded, unreviewed strategy change.
PITCH_VARIANTS = ["direct", "problem-first", "proof-first"]
SUBJECT_STYLES = ["plain", "question", "benefit"]

# Bounded fit-threshold window. Below 70 we'd email weak-fit leads (spammy /
# reputation risk); above 90 we'd email almost nobody. The optimizer is not
# allowed to leave this window.
_FIT_MIN = 70
_FIT_MAX = 90


def _default_strategy() -> dict:
    settings = get_settings()
    fit = int(getattr(settings, "outreach_min_fit", 80) or 80)
    return {
        "pitch_variant": PITCH_VARIANTS[0],
        "subject_style": SUBJECT_STYLES[0],
        "fit_threshold": fit,
    }


# Module-level default so callers can reference ``optimizer.DEFAULT_STRATEGY``.
DEFAULT_STRATEGY = _default_strategy()


def _clamp_fit(value: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = DEFAULT_STRATEGY["fit_threshold"]
    return max(_FIT_MIN, min(_FIT_MAX, v))


def _sanitize(params: dict | None) -> dict:
    """Coerce a stored params dict back into the safe search space."""
    params = params or {}
    pitch = params.get("pitch_variant")
    if pitch not in PITCH_VARIANTS:
        pitch = PITCH_VARIANTS[0]
    style = params.get("subject_style")
    if style not in SUBJECT_STYLES:
        style = SUBJECT_STYLES[0]
    return {
        "pitch_variant": pitch,
        "subject_style": style,
        "fit_threshold": _clamp_fit(params.get("fit_threshold", DEFAULT_STRATEGY["fit_threshold"])),
    }


def active_strategy() -> dict:
    """Return the active strategy's params, or ``DEFAULT_STRATEGY`` offline-safely.

    Wrapped in try/except so a missing DB / missing ``strategies`` table (e.g. the
    pitch drafting a message on a fresh machine) never raises. Does NOT create rows.
    """
    try:
        from db.models import StrategyRecord
        from db.session import get_session

        with get_session() as session:
            row = (
                session.query(StrategyRecord)
                .filter(StrategyRecord.active.is_(True))
                .order_by(StrategyRecord.version.desc())
                .first()
            )
            if row is None:
                return dict(DEFAULT_STRATEGY)
            return _sanitize(row.params)
    except Exception as exc:  # DB/table unavailable — stay safe and offline
        logger.debug("active_strategy: falling back to DEFAULT (%s)", exc)
        return dict(DEFAULT_STRATEGY)


def _reply_stats() -> tuple[float, int]:
    """Return ``(reply_rate, sent_count)`` from ``OutreachRecord``."""
    from db.models import OutreachRecord
    from db.session import get_session

    with get_session() as session:
        sent = session.query(OutreachRecord).filter(OutreachRecord.status == "sent").count()
        replied = session.query(OutreachRecord).filter(OutreachRecord.replied.is_(True)).count()
    rate = replied / sent if sent else 0.0
    return rate, sent


def _next_params(current: dict) -> tuple[dict, str]:
    """Deterministically rotate to the next candidate strategy.

    Rotate the pitch variant first; when it wraps back to index 0, also advance
    the subject style. The fit threshold is carried through unchanged (clamped).
    Returns ``(new_params, human_note_fragment)``.
    """
    current = _sanitize(current)
    p_idx = PITCH_VARIANTS.index(current["pitch_variant"])
    s_idx = SUBJECT_STYLES.index(current["subject_style"])

    next_p = (p_idx + 1) % len(PITCH_VARIANTS)
    next_s = s_idx
    if next_p == 0:  # pitch wrapped — advance the subject style
        next_s = (s_idx + 1) % len(SUBJECT_STYLES)

    new = {
        "pitch_variant": PITCH_VARIANTS[next_p],
        "subject_style": SUBJECT_STYLES[next_s],
        "fit_threshold": _clamp_fit(current["fit_threshold"]),
    }
    changes = []
    if new["pitch_variant"] != current["pitch_variant"]:
        changes.append(f"pitch {current['pitch_variant']}->{new['pitch_variant']}")
    if new["subject_style"] != current["subject_style"]:
        changes.append(f"subject {current['subject_style']}->{new['subject_style']}")
    what = ", ".join(changes) or "no-op"
    return new, what


def run_optimizer() -> dict:
    """Run one optimization step. Returns a small JSON-able result dict.

    Actions: ``disabled`` (gated off), ``insufficient_data``, ``tune``, ``revert``.
    """
    settings = get_settings()
    if not settings.self_optimize:
        return {"action": "disabled", "reply_rate": 0.0, "samples": 0}

    from db.models import StrategyRecord
    from db.session import get_session, init_db

    init_db()

    rate, samples = _reply_stats()
    min_samples = int(getattr(settings, "optimize_min_samples", 20) or 20)
    if samples < min_samples:
        return {"action": "insufficient_data", "samples": samples, "reply_rate": rate}

    revert_drop = float(getattr(settings, "optimize_revert_drop", 0.05) or 0.05)

    with get_session() as session:
        strategies = session.query(StrategyRecord).order_by(StrategyRecord.version.desc()).all()
        active = next((s for s in strategies if s.active), None)

        # No strategy row yet: create the current baseline as the active row.
        if active is None:
            base_params = _sanitize(strategies[0].params if strategies else DEFAULT_STRATEGY)
            base_version = (strategies[0].version if strategies else 0)
            active = StrategyRecord(
                version=base_version + 1,
                params=base_params,
                active=True,
                baseline_reply_rate=rate,
                note="seeded baseline",
            )
            session.add(active)
            session.flush()
            strategies = [active, *strategies]

        # A trial is any active row that has at least one older predecessor.
        priors = [s for s in strategies if s.id != active.id and s.version < active.version]
        is_trial = len(priors) >= 1

        # --- REVERT: a trial whose reply rate fell materially below its baseline.
        if is_trial and rate < (active.baseline_reply_rate - revert_drop):
            prior = max(priors, key=lambda s: s.version)  # most-recent predecessor
            active.active = False
            active.note = (
                f"reverted: reply_rate {rate:.3f} < baseline "
                f"{active.baseline_reply_rate:.3f} - {revert_drop:.3f}"
            )
            prior.active = True
            reverted_to = _sanitize(prior.params)
            return {
                "action": "revert",
                "strategy": reverted_to,
                "reverted_to_version": prior.version,
                "reply_rate": rate,
                "samples": samples,
            }

        # --- TUNE: promote the next deterministic candidate as the new active row.
        new_params, what = _next_params(active.params)
        new_row = StrategyRecord(
            version=active.version + 1,
            params=new_params,
            active=True,
            baseline_reply_rate=rate,
            note=f"tuned {what}",
        )
        active.active = False
        session.add(new_row)
        return {
            "action": "tune",
            "strategy": new_params,
            "reply_rate": rate,
            "samples": samples,
        }
