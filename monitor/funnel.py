"""Funnel-stall detection: catch a system that is "succeeding" at doing nothing.

The gap this closes
-------------------
Over 24 consecutive production runs (2026-07-03 .. 2026-08-03) every workflow
reported ``success`` while the funnel delivered:

    1,200 leads fetched -> 828 new -> 25 queued -> 1 email -> 0 replies

Nothing alerted, because "sent 0 emails" was never an error condition. Only a
*raised exception* counted as failure, so a month passed with $8.55 spent and no
outreach. Uptime was perfect; output was zero.

The lesson generalises: a pipeline whose success signal is "the process exited 0"
cannot tell you it has stopped producing. So these checks assert on **output**,
reading the persisted ``RunRecord.stats`` history rather than exit codes:

* consecutive runs with ``emailed == 0``   -> outreach is not reaching anyone
* consecutive runs with ``queued == 0``    -> nothing is clearing the fit bar
* ``contactable`` below the floor          -> the top of the funnel is broken

Each returns a check dict shaped like the other ``monitor.doctor`` checks so they
compose into the same report and the same alert email.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Workflows whose stats are meaningful for funnel health. The "linkedin" and
#: "monitor" workflows legitimately never email prospects, so counting them would
#: dilute the signal into uselessness.
_FUNNEL_WORKFLOWS = ("outreach",)


def _recent_stats(limit: int = 10) -> list[dict]:
    """Most-recent-first stats dicts for successful funnel runs."""
    from db.models import RunRecord
    from db.session import get_session

    with get_session() as session:
        rows = (
            session.query(RunRecord)
            .filter(
                RunRecord.workflow.in_(_FUNNEL_WORKFLOWS),
                RunRecord.ok.is_(True),
            )
            .order_by(RunRecord.created_at.desc())
            .limit(max(1, limit))
            .all()
        )
        return [dict(r.stats or {}) for r in rows]


def _leading_zero_streak(stats: list[dict], key: str) -> int:
    """How many of the most recent runs had ``stats[key] == 0``, consecutively."""
    streak = 0
    for s in stats:
        try:
            value = int(s.get(key, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value == 0:
            streak += 1
        else:
            break
    return streak


def check_outreach_stalled() -> dict:
    """Fail when N consecutive runs sent zero emails."""
    from config import get_settings

    threshold = get_settings().alert_after_zero_email_runs
    try:
        stats = _recent_stats(limit=max(threshold * 2, 10))
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "outreach_flow",
            "ok": False,
            "detail": f"could not read run history: {exc}",
        }

    if not stats:
        return {"name": "outreach_flow", "ok": True, "detail": "no runs yet"}

    streak = _leading_zero_streak(stats, "emailed")
    if streak >= threshold:
        return {
            "name": "outreach_flow",
            "ok": False,
            "detail": (
                f"{streak} consecutive runs sent 0 emails (threshold {threshold}). "
                "The pipeline is running but reaching nobody — check contactable "
                "lead volume and the emailed_skipped reasons before tuning prompts."
            ),
        }
    return {
        "name": "outreach_flow",
        "ok": True,
        "detail": f"last send within {streak + 1} run(s)",
    }


def check_queue_stalled() -> dict:
    """Fail when N consecutive runs queued zero proposals."""
    from config import get_settings

    threshold = get_settings().alert_after_zero_queue_runs
    try:
        stats = _recent_stats(limit=max(threshold * 2, 10))
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "queue_flow",
            "ok": False,
            "detail": f"could not read run history: {exc}",
        }

    if not stats:
        return {"name": "queue_flow", "ok": True, "detail": "no runs yet"}

    streak = _leading_zero_streak(stats, "queued")
    if streak >= threshold:
        return {
            "name": "queue_flow",
            "ok": False,
            "detail": (
                f"{streak} consecutive runs queued 0 proposals (threshold "
                f"{threshold}). Either min_fit_score is too high for the current "
                "lead mix, or the sources are returning off-ICP listings."
            ),
        }
    return {
        "name": "queue_flow",
        "ok": True,
        "detail": f"last queue within {streak + 1} run(s)",
    }


def check_contactable_supply() -> dict:
    """Fail when the newest run found fewer contactable leads than the floor.

    This is the leading indicator: proposals cannot be sent to leads that expose no
    address, so a collapse here shows up ~a day before the send count drops.
    """
    from config import get_settings

    floor = get_settings().min_contactable_per_run
    try:
        stats = _recent_stats(limit=1)
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "contactable_supply",
            "ok": False,
            "detail": f"could not read run history: {exc}",
        }

    if not stats:
        return {"name": "contactable_supply", "ok": True, "detail": "no runs yet"}

    latest = stats[0]
    if "contactable" not in latest:
        # Runs recorded before this metric existed — not a failure.
        return {
            "name": "contactable_supply",
            "ok": True,
            "detail": "metric not recorded for the latest run",
        }

    try:
        count = int(latest.get("contactable", 0) or 0)
    except (TypeError, ValueError):
        count = 0

    if count < floor:
        return {
            "name": "contactable_supply",
            "ok": False,
            "detail": (
                f"only {count} contactable lead(s) in the latest run (floor {floor}). "
                "Sources are returning listings with no reachable address — this is a "
                "sourcing problem, not a prompt problem."
            ),
        }
    return {
        "name": "contactable_supply",
        "ok": True,
        "detail": f"{count} contactable lead(s) in the latest run",
    }


def funnel_checks() -> list[dict]:
    """All funnel-health checks, shaped like ``monitor.doctor`` checks."""
    return [
        check_contactable_supply(),
        check_queue_stalled(),
        check_outreach_stalled(),
    ]
