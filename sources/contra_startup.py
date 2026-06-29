"""Startup / freelance-platform feed adapter (Wellfound / Contra style).

These platforms expose listings inconsistently and change often, so this
adapter is intentionally feed-agnostic: give it any list of RSS or Atom feed
URLs and it parses each with :mod:`feedparser` (READ-ONLY) and maps entries to
:class:`~core.schemas.Lead`.

Configuration
-------------
Feed URLs come from (in priority order):

1. the ``feeds`` constructor argument, or
2. the ``COPILOT_STARTUP_FEEDS`` environment variable (comma-separated), or
3. a single sensible public default (:data:`DEFAULT_FEED`).

Example::

    export COPILOT_STARTUP_FEEDS="https://example-startup-board.com/jobs.rss,https://another/feed.xml"
"""
from __future__ import annotations

import hashlib
import logging
import os

import feedparser

from core.schemas import Lead
from sources._keywords import extract_tags
from sources.base import LeadSource

logger = logging.getLogger(__name__)

#: A public, stable default feed (Remotive's software-dev RSS) so the adapter
#: works out of the box even before the user configures their own boards.
DEFAULT_FEED = "https://remotive.com/remote-jobs/feed/software-dev"


def _env_feeds() -> list[str]:
    raw = os.environ.get("COPILOT_STARTUP_FEEDS", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


class ContraStartupSource(LeadSource):
    name = "contra_startup"

    def __init__(self, feeds: list[str] | None = None) -> None:
        if feeds is not None:
            self.feeds = feeds
        else:
            env = _env_feeds()
            self.feeds = env if env else [DEFAULT_FEED]

    def _entry_to_lead(self, entry: object) -> Lead | None:
        get = entry.get if hasattr(entry, "get") else lambda k, d=None: getattr(entry, k, d)
        link = get("link", "") or ""
        external_id = get("id", "") or get("guid", "") or ""
        if not external_id:
            if not link:
                return None
            external_id = hashlib.sha1(link.encode("utf-8")).hexdigest()
        title = get("title", "") or ""
        summary = get("summary", "") or get("description", "") or ""
        author = get("author", None)
        return Lead(
            source=self.name,
            external_id=str(external_id),
            title=title.strip(),
            description=summary,
            url=link,
            company=author,
            posted_at=get("published", None) or get("updated", None),
            tags=extract_tags(title, summary),
            raw=dict(entry) if hasattr(entry, "keys") else {},
        )

    def fetch(self, limit: int = 50) -> list[Lead]:
        leads: list[Lead] = []
        for feed_url in self.feeds:
            if len(leads) >= limit:
                break
            try:
                parsed = feedparser.parse(feed_url)
            except Exception as exc:  # pragma: no cover
                logger.warning("contra_startup: parse failed for %s: %s", feed_url, exc)
                continue
            for entry in getattr(parsed, "entries", []) or []:
                if len(leads) >= limit:
                    break
                try:
                    lead = self._entry_to_lead(entry)
                except Exception as exc:
                    logger.warning("contra_startup: bad entry in %s: %s", feed_url, exc)
                    continue
                if lead is not None:
                    leads.append(lead)
        return leads[:limit]
