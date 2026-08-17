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


def _actionable_count(latest: dict) -> int | None:
    """Leads in a run that were BOTH over the fit bar and reachable, or None.

    ``None`` when the run predates the fields this needs — a missing input must report
    that it measured nothing rather than return 0, which is a real measurement meaning
    "the intersection was empty" and would fail the check on old rows.
    """
    fit = latest.get("fit") or {}
    passed = fit.get("passed")
    if passed is None:
        # Pre-``fit`` runs: ``queued`` is the closest real proxy, since an uncontactable
        # lead is drafted with ``draft_allowed=False`` and so never reaches the queue.
        queued = latest.get("queued")
        return None if queued is None else max(0, int(queued or 0))
    unreachable = latest.get("apply_yourself")
    if unreachable is None:
        return None
    return max(0, int(passed or 0) - len(unreachable))


def check_contactable_supply() -> dict:
    """Fail when the newest run produced too few leads that are BOTH good and reachable.

    Measures the intersection, not ``contactable``, and that change is the whole point of
    the check. Over the six runs of 2026-08-10..17 this check reported ``[ok] 29
    contactable lead(s)`` on Friday 08-14 — on the third consecutive run that emailed
    nobody, in a ten-day window that sent 6 emails against 269 qualified leads. Both
    facts were true: there were 29 addresses, and none of them belonged to a lead worth
    writing to. 181 of the window's 196 addresses came from ``hn_hiring`` (full-time
    employment posts, median fit 28) while the qualified leads came from job boards that
    publish no address at all, so raw contactability could sit at 28-47 per run forever
    while sends stayed at zero.

    A floor of 1 on a number that never drops is a check that cannot fail for the reason
    it exists — the same shape as ``passed`` being read as success. The intersection is
    the number that actually predicts a send: it was 1, 4, 0, 0, 0, 2 across the window,
    against 6 emails. ``contactable`` is still reported in the detail, because the gap
    between the two IS the diagnosis and a reader who sees only the intersection cannot
    tell a sourcing collapse from a routing one.
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

    actionable = _actionable_count(latest)
    if actionable is None:
        return {
            "name": "contactable_supply",
            "ok": True,
            "detail": (
                f"{count} contactable lead(s) in the latest run; "
                "qualified-and-reachable not recorded for this run"
            ),
        }
    if actionable < floor:
        return {
            "name": "contactable_supply",
            "ok": False,
            "detail": (
                f"{count} contactable lead(s) but only {actionable} that also cleared the "
                f"fit bar (floor {floor}). The addresses and the good leads are coming "
                "from different sources, so the send count cannot rise by widening "
                "queries or lowering the bar — see the run's Bottleneck line for which "
                "sources sit on each side."
            ),
        }
    return {
        "name": "contactable_supply",
        "ok": True,
        "detail": (
            f"{actionable} qualified + reachable lead(s) of {count} contactable "
            "in the latest run"
        ),
    }


def check_reply_detection_alive() -> dict:
    """Fail when mail has gone out but the inbox has apparently never been read.

    The checks above watch the OUTBOUND half of the loop. Nothing watched the inbound
    half, and it fails in a way that is invisible by construction:
    :func:`reply.inbox.fetch_replies` swallows every IMAP error and returns ``[]``, so
    a wrong host, an expired app password, or a provider that simply is not the one
    ``imap_host`` points at all produce the identical, cheerful ``inbound: 0`` with a
    zero exit code. ``imap_host`` defaults to ``imap.gmail.com`` while the credentials
    come from the *SMTP* settings, so pointing SMTP at any non-Gmail provider silently
    decouples reading from sending.

    Why that is worse than it sounds: a reply is what STOPS the follow-up sequence
    (``replied is False`` is a selection criterion in ``followup.runner``). If replies
    are never detected, nobody is ever marked replied, so the system keeps nudging
    people who already answered — the rudest possible failure, and it looks like
    "no one is interested" rather than like a bug.

    Deliberately conservative: zero inbound is entirely normal at low volume, so this
    only fires once enough mail has been sent that *never once* seeing a reply is
    better explained by a broken reader than by a quiet market. It reports what it
    cannot distinguish rather than asserting a cause.
    """
    from config import get_settings

    settings = get_settings()
    threshold = getattr(settings, "alert_after_sent_without_inbound", 25)
    try:
        sent = sum(int(s.get("emailed", 0) or 0) for s in _recent_stats(limit=60))
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "reply_detection",
            "ok": False,
            "detail": f"could not read run history: {exc}",
        }

    if sent < threshold:
        return {
            "name": "reply_detection",
            "ok": True,
            "detail": (
                f"{sent} email(s) sent so far; below the {threshold} needed to judge "
                "whether silence is real"
            ),
        }

    try:
        from db.models import ReplyRecord
        from db.session import get_session, init_db

        # A missing replies table means "no data yet", not "the reader is broken".
        # Without this the check reported ok=False on a fresh database, which is the
        # over-alerting mirror: a check that fails before it can possibly know
        # anything teaches you to ignore it.
        init_db()
        with get_session() as session:
            inbound = (
                session.query(ReplyRecord)
                .filter(ReplyRecord.direction == "in")
                .count()
            )
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "reply_detection",
            "ok": False,
            "detail": f"could not read reply history: {exc}",
        }

    if inbound == 0:
        try:
            imap = settings.resolved_imap_host() or "(unset)"
        except Exception:  # noqa: BLE001 - a settings shim in tests may lack the method
            imap = getattr(settings, "imap_host", "") or "(unset)"
        return {
            "name": "reply_detection",
            "ok": False,
            "detail": (
                f"{sent} emails sent, 0 inbound messages ever recorded. Either nobody "
                f"has replied, or the IMAP reader is failing silently — fetch_replies "
                f"swallows IMAP errors and returns [] either way. Verify imap_host "
                f"({imap}) matches the SMTP provider the credentials belong to, since "
                "the reader logs in with smtp_user/smtp_password. Until an inbound "
                "message is seen, nobody is marked replied and follow-ups will keep "
                "nudging prospects who already answered."
            ),
        }
    return {
        "name": "reply_detection",
        "ok": True,
        "detail": f"{inbound} inbound message(s) recorded across {sent} sent",
    }


def check_discovery_productive() -> dict:
    """Fail when contact discovery has looked up leads for days and found nothing.

    Discovery is the intended fix for the largest loss in the funnel — 262 of the 269
    qualified leads over 2026-08-10..17 had no address to send to — and it fails silently
    by construction. It fetches other people's websites, so any of a blocked user-agent,
    a robots.txt change, a world that has moved entirely onto hosted ATS domains, or a
    bug in the extraction path produces the same thing: ``discovered: 0``, a run that
    still exits 0, and an ``outreach_flow`` alert three days later pointing at sends.

    The check is ATTEMPTS-gated. A run with zero attempts is not evidence of anything —
    it means nothing qualified without an address, which is the good case — so it does
    not count toward the streak. That distinction is the whole reason ``discovery_attempts``
    is recorded next to ``discovered``: "found nothing" and "never looked" have opposite
    fixes, and one number cannot say which happened.
    """
    from config import get_settings

    settings = get_settings()
    if not getattr(settings, "discover_contacts", False):
        return {
            "name": "discovery",
            "ok": True,
            "detail": "contact discovery is disabled",
        }
    threshold = int(getattr(settings, "alert_after_zero_email_runs", 3) or 3)
    try:
        stats = _recent_stats(limit=max(threshold * 2, 10))
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "discovery",
            "ok": False,
            "detail": f"could not read run history: {exc}",
        }

    tried = 0
    found = 0
    runs_that_looked = 0
    for run in stats:
        try:
            attempts = int(run.get("discovery_attempts", 0) or 0)
            hits = int(run.get("discovered", 0) or 0)
        except (TypeError, ValueError):
            continue
        if attempts <= 0:
            continue
        runs_that_looked += 1
        tried += attempts
        found += hits

    if runs_that_looked < threshold:
        return {
            "name": "discovery",
            "ok": True,
            "detail": (
                f"only {runs_that_looked} run(s) have attempted discovery "
                f"({tried} lookup(s)) — too early to judge"
            ),
        }
    if found == 0:
        return {
            "name": "discovery",
            "ok": False,
            "detail": (
                f"{tried} lookup(s) across {runs_that_looked} runs found 0 addresses. "
                "Discovery is running and producing nothing: check that the fetches are "
                "not being refused (user-agent, robots.txt) before concluding companies "
                "publish no addresses."
            ),
        }
    return {
        "name": "discovery",
        "ok": True,
        "detail": f"{found} address(es) from {tried} lookup(s) across {runs_that_looked} runs",
    }


def funnel_checks() -> list[dict]:
    """All funnel-health checks, shaped like ``monitor.doctor`` checks."""
    return [
        check_contactable_supply(),
        check_queue_stalled(),
        check_outreach_stalled(),
        check_reply_detection_alive(),
        check_discovery_productive(),
    ]
