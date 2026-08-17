"""One shared daily send counter for every outbound email channel.

``max_emails_per_day`` exists to protect deliverability: the sending domain's
reputation is a property of the mailbox, not of the code path that used it. It
therefore has to be counted once across every channel that sends.

It used to be enforced twice against two disjoint counters — the pipeline counted
cold emails (``sent_at`` today, ``status == "sent"``), and the follow-up runner
counted follow-ups (``last_contact_at`` today, ``followups_sent > 0``). Neither
could see the other's sends, so a configured cap of 20 permitted 40 messages a
day from one mailbox, and the ceiling silently rose with every channel added.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

logger = logging.getLogger(__name__)


def today_start() -> _dt.datetime:
    now = _dt.datetime.now(_dt.UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def emails_sent_today(session: Any) -> int:
    """Count every outbound email sent from this mailbox since UTC midnight.

    Cold emails and follow-ups are stored on the same table but recorded on
    different columns, so both are counted:

      * cold: ``sent_at`` today and ``status == "sent"``
      * follow-up: ``last_contact_at`` today and ``followups_sent > 0``

    A record cold-emailed AND followed up on the same day is counted twice. That
    needs ``followup_after_days == 0`` to happen at all, and double-counting errs
    toward sending less, which is the correct direction to be wrong in when the
    thing being protected is a domain reputation.
    """
    from db.models import OutreachRecord

    start = today_start()
    cold = (
        session.query(OutreachRecord)
        .filter(OutreachRecord.sent_at >= start, OutreachRecord.status == "sent")
        .count()
    )
    followups = (
        session.query(OutreachRecord)
        .filter(
            OutreachRecord.last_contact_at >= start,
            OutreachRecord.followups_sent > 0,
        )
        .count()
    )
    return cold + followups


def remaining_today(session: Any, cap: int) -> int:
    """How many more emails may be sent today, never negative."""
    return max(0, cap - emails_sent_today(session))


#: Daily volume allowed before any sending history exists. 10 rather than 1: the mailbox
#: has already sent 16 in a single day (2026-08-14, follow-up wave) without incident, so
#: a lower floor would throttle behaviour that is already proven and make the ramp a
#: regression rather than a guard.
WARMUP_FLOOR = 10
#: How much above the recent proven peak today may go. 1.5x is the conservative end of
#: the usual warmup advice; the point is that volume climbs in steps the receiving
#: filters have seen before.
WARMUP_GROWTH = 1.5
#: Window for "recent proven peak". Two weeks covers the follow-up cadence
#: (``followup_after_days`` 3, ``max_followups`` 2) so a legitimate wave is remembered
#: as capacity rather than forgotten between bursts.
WARMUP_LOOKBACK_DAYS = 14


def _as_utc(value: _dt.datetime | None) -> _dt.datetime | None:
    """Read a stored timestamp back as timezone-aware UTC.

    ``OutreachRecord.sent_at`` / ``last_contact_at`` are plain ``DateTime`` columns, so
    both SQLite and Postgres hand them back NAIVE even though they were written from
    ``datetime.now(UTC)``. Comparing one to an aware ``cutoff`` raises TypeError, and
    ``effective_cap`` catches every exception in order to never block a send — so the
    ramp would have quietly returned the unramped configured cap forever while looking
    like it worked. ``followup/runner.py`` normalises the same column for the same
    reason; this is that fix, not a new convention.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=_dt.UTC)


def peak_daily_sends(session: Any, lookback_days: int = WARMUP_LOOKBACK_DAYS) -> int:
    """The largest single-day send count in the recent past, across both channels.

    The busiest single DAY, not the window total: a mailbox that sent 5 messages over a
    week has not demonstrated it can send 5 in an hour, and reputation systems score the
    day. Both columns are counted for the same reason ``emails_sent_today`` counts both
    — the 16-message day of 2026-08-14 was a follow-up wave, and it still happened.
    """
    from db.models import OutreachRecord

    cutoff = today_start() - _dt.timedelta(days=max(1, lookback_days))
    per_day: dict[_dt.date, int] = {}
    rows = (
        session.query(OutreachRecord)
        .filter(
            (OutreachRecord.sent_at >= cutoff)
            | (OutreachRecord.last_contact_at >= cutoff)
        )
        .all()
    )
    for rec in rows:
        sent = _as_utc(rec.sent_at)
        if sent is not None and sent >= cutoff and rec.status == "sent":
            per_day[sent.date()] = per_day.get(sent.date(), 0) + 1
        last = _as_utc(rec.last_contact_at)
        if last is not None and last >= cutoff and (rec.followups_sent or 0) > 0:
            per_day[last.date()] = per_day.get(last.date(), 0) + 1
    return max(per_day.values(), default=0)


def effective_cap(session: Any, configured_cap: int) -> int:
    """Today's real send ceiling: the configured cap, ramped toward it.

    ``max_emails_per_day`` is a ceiling, not a schedule. That distinction did not matter
    while the binding constraint was upstream — across 2026-08-10..17 the pipeline had
    only 7 leads that were both qualified and reachable, so it sent 6 cold emails in ten
    days against a cap of 20 and the cap was decorative.

    ``outreach/discover.py`` removes that constraint deliberately: the same window had
    262 qualified leads with no address, and discovery exists to find addresses for them.
    So the first run after it works can present dozens of sendable leads at once, and a
    mailbox averaging under 4 messages a day would jump straight to its ceiling — then
    further, the moment the ceiling is raised to match the new supply. A step change in
    cold volume is one of the few things spam filters score directly, and unlike every
    other failure in this project it is not reversible by fixing the code: a burned
    sending reputation cannot be rebought, and it takes the follow-up sequence and the
    replies of every already-contacted prospect with it.

    So growth is tied to what the mailbox has already demonstrated, not to what config
    permits. This can only ever LOWER the limit — ``configured_cap`` remains the ceiling
    — which also means the owner raising the cap is safe: it becomes a target the ramp
    walks toward over a few days instead of tomorrow's volume.
    """
    if configured_cap <= 0:
        return 0
    try:
        peak = peak_daily_sends(session)
    except Exception as exc:  # noqa: BLE001 - a quota check must never break a send loop
        # Unknown history: fall back to the configured cap rather than blocking sends.
        # Failing closed here would silently stop all outreach on an unrelated DB error,
        # which is the more expensive of the two mistakes.
        #
        # LOGGED, not swallowed. This branch already hid a real bug once: naive
        # timestamps out of the DB raised TypeError on every call, so the ramp returned
        # the unramped cap while every test of the cap still passed. A permissive
        # fallback that says nothing is indistinguishable from a working guard.
        logger.warning(
            "effective_cap: could not measure send history (%s: %s) — falling back to"
            " the configured cap of %d with NO warmup ramp",
            type(exc).__name__,
            exc,
            configured_cap,
        )
        return configured_cap
    ramped = max(WARMUP_FLOOR, int(peak * WARMUP_GROWTH))
    return min(configured_cap, ramped)
