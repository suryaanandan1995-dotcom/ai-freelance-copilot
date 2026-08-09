"""Lead-source adapter interface.

Every source (Upwork RSS, remote boards, Contra/startup feeds, HN "who is
hiring") implements `LeadSource`. Sources are READ-ONLY: they only fetch public
opportunity listings. They never submit anything to any platform.
"""
from __future__ import annotations

import abc

from core.schemas import Lead


class LeadSource(abc.ABC):
    #: short, stable adapter name, also stored on each Lead.source
    name: str = "base"

    #: How many candidate listings the last ``fetch`` actually looked at, BEFORE the
    #: adapter's own keyword/side filters, or ``None`` if the adapter doesn't count.
    #:
    #: ``fetch`` returning ``[]`` has two opposite causes and the funnel report could
    #: not tell them apart: the feed handed us nothing (retire it, or change the
    #: queries), or the feed handed us plenty and our filters rejected all of it (the
    #: filters are the lever). Both surfaced as ``dead: fetched nothing``, which the
    #: digest sorts under "retire what never produces" — so the second case advises
    #: deleting a working source. Diagnosing hn_freelancer by hand cost an afternoon
    #: to establish what this integer states outright: it reached the August thread
    #: and read all 13 top-level comments, every one of them a freelancer advertising
    #: availability rather than a client hiring.
    #:
    #: Same shape as ``last_error`` one level down: distinguish a silent upstream from
    #: a silent filter, because they need opposite fixes.
    scanned: int | None = None

    @abc.abstractmethod
    def fetch(self, limit: int = 50) -> list[Lead]:
        """Return up to `limit` freshly discovered leads. Must not raise on
        empty/unreachable feeds — return [] and log instead."""
        raise NotImplementedError


def dedupe(leads: list[Lead]) -> list[Lead]:
    """Drop duplicates by Lead.dedupe_key, preserving order."""
    seen: set[str] = set()
    out: list[Lead] = []
    for lead in leads:
        if lead.dedupe_key in seen:
            continue
        seen.add(lead.dedupe_key)
        out.append(lead)
    return out
