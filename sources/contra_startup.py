"""Startup / freelance-platform feed adapter (Wellfound / Contra style).

These platforms expose listings inconsistently and change often, so this
adapter is intentionally feed-agnostic: give it any list of RSS or Atom feed
URLs and it parses each with :mod:`feedparser` (READ-ONLY) and maps entries to
:class:`~core.schemas.Lead`.

Configuration
-------------
Feed URLs come from (in priority order):

1. the ``feeds`` constructor argument, or
2. ``COPILOT_STARTUP_FEEDS`` (comma-separated) — read through ``Settings``, so it
   works in ``.env`` as well as the environment, or
3. a single sensible public default (:data:`DEFAULT_FEED`).

Example (in ``.env`` or exported)::

    COPILOT_STARTUP_FEEDS="https://example-startup-board.com/jobs.rss,https://another/feed.xml"
"""
from __future__ import annotations

import hashlib
import logging

import feedparser

from core.schemas import Lead
from sources._keywords import extract_tags
from sources.base import LeadSource, dedupe

logger = logging.getLogger(__name__)

#: Public, stable default feeds so the adapter works out of the box before the
#: user configures their own boards. These are deliberately *category-scoped* to
#: the DevOps/infra niche — the previous generic ``software-dev`` default mostly
#: returned front-end/mobile roles that every keyword filter then discarded, so
#: this source contributed nothing while still costing a fetch each run.
DEFAULT_FEEDS = (
    "https://remotive.com/remote-jobs/feed/devops",
    "https://nodesk.co/remote-jobs/engineering/index.xml",
)
#: Back-compat alias — some callers/tests import the single-feed name.
DEFAULT_FEED = DEFAULT_FEEDS[0]


def _env_feeds() -> list[str]:
    """Configured feed overrides, via ``Settings`` so ``.env`` is honoured.

    Reading ``os.environ`` directly meant a value set in ``.env`` — where every other
    setting in this project lives — was silently ignored and the module defaults used
    instead.
    """
    from config import get_settings

    raw = get_settings().startup_feeds or ""
    return [u.strip() for u in raw.split(",") if u.strip()]


class ContraStartupSource(LeadSource):
    name = "contra_startup"

    def __init__(self, feeds: list[str] | None = None) -> None:
        if feeds is not None:
            self.feeds = feeds
        else:
            env = _env_feeds()
            self.feeds = env if env else list(DEFAULT_FEEDS)

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
        # Now that several feeds are read, the same posting can arrive twice
        # (aggregators syndicate each other). Dedupe here rather than relying on
        # the registry, so ``limit`` counts distinct leads.
        return dedupe(leads)[:limit]
