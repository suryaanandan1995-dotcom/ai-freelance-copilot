"""Upwork saved-search RSS adapter.

Upwork lets you save a search and subscribe to it as an RSS feed. This adapter
parses one or more of those feeds (READ-ONLY) and maps each entry to a
:class:`~core.schemas.Lead`. It never logs into or submits anything to Upwork.

Configuration
-------------
Feed URLs come from (in priority order):

1. the ``feeds`` constructor argument, or
2. ``COPILOT_UPWORK_FEEDS`` (comma-separated) — read through ``Settings``, so it
   works in ``.env`` as well as the environment.

There is **no default**: with neither set this source yields nothing. That is why the
setting has to be read where the operator actually writes config.

Example (in ``.env`` or exported)::

    COPILOT_UPWORK_FEEDS="https://www.upwork.com/ab/feed/jobs/rss?q=devops&sort=recency,https://www.upwork.com/ab/feed/jobs/rss?q=kubernetes"
"""
from __future__ import annotations

import hashlib
import logging

import feedparser

from core.schemas import Lead
from sources._keywords import extract_tags
from sources.base import LeadSource

logger = logging.getLogger(__name__)


def _env_feeds() -> list[str]:
    """Configured feed URLs, via ``Settings`` so ``.env`` is honoured.

    This source has NO built-in default feed list, so it is entirely driven by this
    setting. Reading ``os.environ`` directly meant a value set in ``.env`` was ignored
    and the source ran with zero feeds — silently switched off while still reporting a
    successful, empty fetch.
    """
    from config import get_settings

    raw = get_settings().upwork_feeds or ""
    return [u.strip() for u in raw.split(",") if u.strip()]


class UpworkRSSSource(LeadSource):
    name = "upwork_rss"

    def __init__(self, feeds: list[str] | None = None) -> None:
        self.feeds = feeds if feeds is not None else _env_feeds()

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
        published = get("published", None) or get("updated", None)
        tags = extract_tags(title, summary)
        return Lead(
            source=self.name,
            external_id=str(external_id),
            title=title.strip(),
            description=summary,
            url=link,
            posted_at=published,
            tags=tags,
            raw=dict(entry) if hasattr(entry, "keys") else {},
        )

    def fetch(self, limit: int = 50) -> list[Lead]:
        leads: list[Lead] = []
        for feed_url in self.feeds:
            if len(leads) >= limit:
                break
            try:
                parsed = feedparser.parse(feed_url)
            except Exception as exc:  # pragma: no cover - feedparser rarely raises
                logger.warning("upwork_rss: failed to parse %s: %s", feed_url, exc)
                continue
            for entry in getattr(parsed, "entries", []) or []:
                if len(leads) >= limit:
                    break
                try:
                    lead = self._entry_to_lead(entry)
                except Exception as exc:
                    logger.warning("upwork_rss: bad entry in %s: %s", feed_url, exc)
                    continue
                if lead is not None:
                    leads.append(lead)
        return leads[:limit]
