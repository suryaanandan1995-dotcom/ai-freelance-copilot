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
