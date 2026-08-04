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
from typing import Any


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
