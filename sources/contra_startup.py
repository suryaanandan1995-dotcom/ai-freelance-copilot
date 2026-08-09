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
from sources._keywords import extract_tags, matches_keywords
from sources._text import company_from_title, strip_html
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
        #: Last feed-parse failure, or None if every feed parsed. Read by the funnel
        #: report so "the feed is gone" is not reported as "no jobs matched": a
        #: swallowed error and a genuinely empty market both returned [] and both
        #: surfaced as ``dead: fetched nothing``, which names the wrong lever — one
        #: needs a code fix, the other needs different feeds.
        self.last_error: str | None = None
        #: Entries read across all feeds, before ``matches_keywords``. See
        #: ``LeadSource.scanned``: it separates an empty feed from a rejecting filter.
        #: Especially load-bearing here, because the NoDesk feed is scoped to
        #: "engineering" rather than to infra, so this adapter's normal state is
        #: reading many entries and keeping few.
        self.scanned: int | None = None

    def _entry_to_lead(self, entry: object) -> Lead | None:
        get = entry.get if hasattr(entry, "get") else lambda k, d=None: getattr(entry, k, d)
        link = get("link", "") or ""
        external_id = get("id", "") or get("guid", "") or ""
        if not external_id:
            if not link:
                return None
            external_id = hashlib.sha1(link.encode("utf-8")).hexdigest()
        title = (get("title", "") or "").strip()
        summary = strip_html(get("summary", "") or get("description", "") or "")
        # Keyword gate, as every sibling adapter has (working_nomads.py, jobicy.py,
        # contract_jobs.py). This adapter had none, and its NoDesk feed is scoped to
        # "engineering", not to DevOps: live leads on 2026-08-07 included "Senior
        # React Native Developer" and "Software engineer at Sticker Mule", each
        # costing a Claude call to score 28 and be discarded. The gate reads title and
        # description together, like the siblings, so a genuine infra role that names
        # its stack only in the body still passes.
        if not matches_keywords(title, summary):
            return None
        return Lead(
            source=self.name,
            external_id=str(external_id),
            title=title,
            description=summary,
            url=link,
            # NoDesk entries carry no ``author``, so 9 of 10 live leads reached the
            # scorer as ``Company: unknown`` while the name sat in the title
            # ("Software engineer at Sticker Mule").
            company=get("author", None) or company_from_title(title),
            posted_at=get("published", None) or get("updated", None),
            tags=extract_tags(title, summary),
            raw=dict(entry) if hasattr(entry, "keys") else {},
        )

    def fetch(self, limit: int = 50) -> list[Lead]:
        self.last_error = None  # per-fetch, so a fixed source stops reporting stale errors
        self.scanned = None  # stays None if every feed errors; promoted below per feed
        leads: list[Lead] = []
        for feed_url in self.feeds:
            if len(leads) >= limit:
                break
            try:
                parsed = feedparser.parse(feed_url)
            except Exception as exc:
                logger.warning("contra_startup: parse failed for %s: %s", feed_url, exc)
                self.last_error = f"{type(exc).__name__}: {exc}"
                continue
            # feedparser does not raise on an HTTP error — it returns an empty feed
            # with ``status``/``bozo`` set, so a 500 or a moved feed looked exactly
            # like "this board has no infra jobs this week" in the funnel report.
            status = getattr(parsed, "status", None)
            if isinstance(status, int) and status >= 400:
                logger.warning("contra_startup: HTTP %s for %s", status, feed_url)
                self.last_error = f"HTTP {status}: {feed_url}"
                continue
            entries = getattr(parsed, "entries", []) or []
            # Accumulated past the error ``continue``s above, so a feed that 500s
            # contributes nothing rather than resetting a healthy feed's count.
            self.scanned = (self.scanned or 0) + len(entries)
            for entry in entries:
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
