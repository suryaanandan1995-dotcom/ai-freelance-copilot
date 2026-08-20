"""Outcome KPIs: report what the system *achieved*, not that it ran.

The reporting defect
--------------------
For 24 consecutive runs the only signal reaching the owner was "workflow succeeded"
plus activity counts (fetched / new / dropped). Every one of those numbers was
healthy-looking. Meanwhile the outcome was: 1 email sent, 0 replies, 0 calls, 0
projects won, $8.55 spent. Uptime was being reported; results were not.

So this module computes the funnel in *outcome* terms, over a rolling window:

    contactable -> emailed -> replied -> call booked -> won

plus the two efficiency numbers that decide whether to keep spending: cost per reply
and cost per booked call. A conversion rate of 0 with a healthy top-of-funnel is a
targeting problem; a healthy reply rate with no bookings is a pitch problem. Reporting
the stage-by-stage numbers is what makes those distinguishable at a glance.

Read-only: no writes, no network. Safe to call from the dashboard, CLI, or digest.
"""
from __future__ import annotations

import datetime as _dt
import logging

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = 30


def _cutoff(days: int) -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=max(1, days))


def _rate(numerator: int, denominator: int) -> float | None:
    """Conversion rate as a percentage, or None when there is nothing to divide by.

    None is deliberate: reporting "0.0%" for a stage that had no input invites the
    wrong fix (rewriting a pitch nobody received). No denominator means no data.
    """
    if not denominator:
        return None
    return round(100.0 * numerator / denominator, 1)


def funnel(window_days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """Outcome funnel over the last ``window_days``.

    Every field degrades to 0/None rather than raising: this feeds a notification
    path, and a KPI query must never be the reason a run reports failure.
    """
    from db.models import CallRecord, LeadStatus, OutreachRecord, PostRecord, RunRecord
    from db.session import get_session

    since = _cutoff(window_days)
    out = {
        "window_days": window_days,
        "since": since.isoformat(timespec="seconds"),
    }

    try:
        with get_session() as session:
            sent = (
                session.query(OutreachRecord)
                .filter(
                    OutreachRecord.sent_at >= since,
                    OutreachRecord.status == "sent",
                )
                .all()
            )
            emailed = len(sent)
            replied = sum(1 for r in sent if r.replied)
            replied_booked = sum(1 for r in sent if r.call_booked_at is not None)
            followups = sum(int(r.followups_sent or 0) for r in sent)

            # Calls come from BOTH channels, and for a month this function could only see
            # one of them. ``call_booked_at`` is stamped only when the invitee is someone we
            # cold-emailed, so the first real booking this system ever produced — inbound,
            # from a LinkedIn post — left `calls_booked: 0` in every report while a briefing
            # about it sat in the owner's inbox. A metric that cannot rise for the channel
            # that actually converts is not a metric; worse, ``verdict`` below reads it and
            # was answering "rewrite the cold-email pitch" to a week that had booked a call.
            call_rows = (
                session.query(CallRecord)
                .filter(CallRecord.created_at >= since, CallRecord.status == "booked")
                .all()
            )
            inbound_calls = [c for c in call_rows if c.origin != "outreach"]
            # Union, not sum: an outreach booking is normally evidenced twice — the stamp on
            # the send AND the CallRecord the inbox sweep wrote — and counting both would
            # double it. Deduped on the address, which is the same key both paths key on.
            outreach_booked_addresses = {
                (r.email or "").lower() for r in sent if r.call_booked_at is not None
            } | {
                (c.invitee_email or "").lower()
                for c in call_rows
                if c.origin == "outreach" and c.invitee_email
            }
            outreach_calls = len({a for a in outreach_booked_addresses if a})
            booked = outreach_calls + len(inbound_calls)

            # The denominator for inbound. Nothing reported posts against bookings, so the
            # channel with the only result was also the one with no numbers attached.
            posts_published = (
                session.query(PostRecord)
                .filter(PostRecord.created_at >= since, PostRecord.status == "published")
                .count()
            )

            runs = (
                session.query(RunRecord)
                .filter(RunRecord.created_at >= since)
                .all()
            )
            cost = sum(float(r.cost_usd or 0.0) for r in runs)
            contactable = 0
            for r in runs:
                try:
                    contactable += int((r.stats or {}).get("contactable", 0) or 0)
                except (TypeError, ValueError):
                    continue

            # Won/lost is human-marked on the lead, not derivable from email state.
            from db.models import LeadRecord

            won = (
                session.query(LeadRecord)
                .filter(
                    LeadRecord.status == LeadStatus.won,
                    LeadRecord.created_at >= since,
                )
                .count()
            )
    except Exception as exc:  # noqa: BLE001 - KPIs must never break a run
        logger.warning("kpi: could not compute funnel: %s", exc)
        out["error"] = str(exc)
        return out

    out.update(
        {
            "contactable": contactable,
            "emailed": emailed,
            "followups": followups,
            "replied": replied,
            "calls_booked": booked,
            "calls_from_outreach": outreach_calls,
            "calls_inbound": len(inbound_calls),
            "posts_published": posts_published,
            "won": won,
            "cost_usd": round(cost, 4),
            "reply_rate_pct": _rate(replied, emailed),
            # Reply→booking is an OUTREACH rate: an inbound booking has no reply behind it,
            # so mixing it in here would inflate the number that judges the reply handler
            # with calls the reply handler never touched.
            "booking_rate_pct": _rate(replied_booked, replied),
            "win_rate_pct": _rate(won, booked),
            "cost_per_reply_usd": round(cost / replied, 2) if replied else None,
            "cost_per_call_usd": round(cost / booked, 2) if booked else None,
        }
    )
    out["verdict"] = verdict(out)
    return out


def verdict(k: dict) -> str:
    """One line naming the current bottleneck stage and the lever that moves it.

    Ordered from the top of the funnel down, because fixing a downstream stage while
    an upstream one is empty produces no change — which is exactly what a month of
    prompt-tuning against an empty top-of-funnel would have achieved.
    """
    # "No contactable leads" only means the top of the funnel when nothing downstream
    # is moving either. ``contactable`` is read from RunRecord.stats, so it reads 0 for
    # any window whose runs predate that stat or that contains no runs at all — and
    # short-circuiting on it alone would answer "fix sourcing" to a window holding real
    # replies and booked calls, hiding the very outcomes this report exists to surface.
    if not (k.get("contactable") or k.get("replied") or k.get("calls_booked") or k.get("won")):
        return (
            "TOP OF FUNNEL EMPTY: no contactable leads. Fix sourcing/targeting — "
            "nothing downstream can improve while there is nobody to email."
        )
    if not k.get("emailed"):
        return (
            f"NOT SENDING: {k.get('contactable', 0)} contactable lead(s) but 0 emails. "
            "Check auto_email, SMTP config, the daily cap, and outreach_min_fit."
        )
    # Checked BEFORE the reply ladder, because the reply ladder assumes cold email is the
    # only channel and will happily answer "rewrite the pitch" to a week in which a call was
    # booked through the other one. Ranking a channel with a booked call below a channel with
    # zero replies is how a month gets spent tuning the losing half.
    if k.get("calls_inbound") and not k.get("calls_from_outreach"):
        posts = k.get("posts_published") or 0
        return (
            f"INBOUND IS THE CHANNEL THAT CONVERTS: {k['calls_inbound']} call(s) booked "
            f"from {posts} post(s), against {k.get('replied', 0)} reply and "
            f"{k.get('calls_from_outreach', 0)} calls from {k.get('emailed', 0)} cold "
            "email(s). Post more and post better before rewriting the cold-email pitch — "
            "the evidence says publishing works and emailing strangers does not."
        )
    if not k.get("replied"):
        return (
            f"NO REPLIES from {k['emailed']} email(s). At this volume that is not yet "
            "evidence the pitch is wrong — send more before rewriting it."
            if k["emailed"] < 20
            else f"NO REPLIES from {k['emailed']} emails. Volume is sufficient to "
            "conclude the targeting or the pitch is off; change one, measure, repeat."
        )
    if not k.get("calls_booked"):
        return (
            f"REPLIES BUT NO CALLS ({k['replied']} replies). The opener works and the "
            "close does not — the reply handler should be driving to the booking link."
        )
    if not k.get("won"):
        return (
            f"{k['calls_booked']} call(s) booked, none won yet. The machine is working; "
            "the remaining variable is the call itself."
        )
    return (
        f"WORKING: {k['won']} won from {k['calls_booked']} call(s) at "
        f"${k.get('cost_per_call_usd')}/call."
    )


def format_kpis(k: dict) -> str:
    """Plain-text KPI block for the digest email and CLI."""
    if k.get("error"):
        return f"KPIs unavailable: {k['error']}"

    def stage(label: str, value, rate_key: str | None = None) -> str:
        line = f"  {label:<16}{value}"
        if rate_key:
            rate = k.get(rate_key)
            line += f"   ({rate}%)" if rate is not None else "   (n/a)"
        return line

    lines = [
        f"OUTCOMES — last {k.get('window_days')} days",
        stage("contactable", k.get("contactable", 0)),
        stage("emailed", k.get("emailed", 0)),
        stage("replied", k.get("replied", 0), "reply_rate_pct"),
        stage("calls booked", k.get("calls_booked", 0)),
        # Split, because the total hides the only finding that matters: which channel
        # produced it. The rate belongs to the outreach half — inbound has no reply behind
        # it, so its denominator is posts published, not replies received.
        f"    {'from outreach':<14}{k.get('calls_from_outreach', 0)}"
        f"   ({k.get('booking_rate_pct')}% of replies)"
        if k.get("booking_rate_pct") is not None
        else f"    {'from outreach':<14}{k.get('calls_from_outreach', 0)}",
        f"    {'inbound':<14}{k.get('calls_inbound', 0)}"
        f"   (from {k.get('posts_published', 0)} post(s) published)",
        stage("won", k.get("won", 0), "win_rate_pct"),
        f"  {'spend':<16}${k.get('cost_usd', 0.0)}",
    ]
    if k.get("cost_per_reply_usd") is not None:
        lines.append(f"  {'per reply':<16}${k['cost_per_reply_usd']}")
    if k.get("cost_per_call_usd") is not None:
        lines.append(f"  {'per call':<16}${k['cost_per_call_usd']}")
    lines += ["", k.get("verdict", "")]
    return "\n".join(lines)
