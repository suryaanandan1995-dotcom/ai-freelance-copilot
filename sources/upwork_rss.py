"""Upwork saved-search RSS adapter.

Upwork lets you save a search and subscribe to it as an RSS feed. This adapter
parses one or more of those feeds (READ-ONLY) and maps each entry to a
:class:`~core.schemas.Lead`. It never logs into or submits anything to Upwork.

Configuration
-------------
Feed URLs come from (in priority order):

1. the ``feeds`` constructor argument, or
2. the ``COPILOT_UPWORK_FEEDS`` environment variable (comma-separated).

Example::

    export COPILOT_UPWORK_FEEDS="https://www.upwork.com/ab/feed/jobs/rss?q=devops&sort=recency,https://www.upwork.com/ab/feed/jobs/rss?q=kubernetes"
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


def _env_feeds() -> list[str]:
    raw = os.environ.get("COPILOT_UPWORK_FEEDS", "")
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
