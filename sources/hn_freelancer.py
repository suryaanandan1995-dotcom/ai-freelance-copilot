"""Hacker News "Ask HN: Freelancer? Seeking Freelancer?" adapter.

Every month HN runs a "Freelancer? Seeking Freelancer?" thread (posted by the
``whoishiring`` bot) with two kinds of top-level comments:

* SEEKING WORK      — freelancers advertising their availability.
* SEEKING FREELANCER — clients/companies looking to hire a freelancer.

We only want the **SEEKING FREELANCER** side (the clients), because those posts
are the actual leads — and they very often include a direct contact email,
which makes them high-value for auto-email outreach.

Uses the public HN Algolia API (READ-ONLY) to find the latest such story, fetch
its comments, and keep only client-side comments that also carry a genuine
DevSecOps keyword. Network/parse failures are tolerated — returns [] or partial.
"""
from __future__ import annotations

import logging
import re

import httpx

from core.schemas import Lead
from sources._keywords import extract_tags, matches_keywords
from sources._text import detect_company, first_line, strip_html
from sources.base import LeadSource

logger = logging.getLogger(__name__)

ALGOLIA_BASE = "https://hn.algolia.com/api/v1"
HN_ITEM_URL = "https://news.ycombinator.com/item?id="
USER_AGENT = "ai-freelance-copilot/1.0 (+https://github.com) read-only lead scanner"
TIMEOUT = 10.0

# Phrases that clearly mark the "client is hiring a freelancer" side.
_SEEKING_FREELANCER_RE = re.compile(
    r"seeking\s+freelancer|seeking\s+contractor|"
    r"looking\s+to\s+hire|we(?:'re|\s+are)\s+(?:hiring|looking)|"
    r"hiring\s+a?\s*(?:freelancer|contractor|engineer|consultant)",
)

# Phrases that clearly mark the "freelancer is available" (SEEKING WORK) side,
# which we must exclude even if they mention keywords.
_SEEKING_WORK_RE = re.compile(
    r"seeking\s+work|available\s+for\s+(?:work|hire|projects|freelance)|"
    r"looking\s+for\s+(?:work|freelance|contract\s+work|remote\s+work)|"
    r"i(?:'m|\s+am)\s+(?:a\s+)?(?:freelance|available|open\s+to)",
)


# NOTE: ``strip_html``, ``first_line`` and ``detect_company`` now come from
# ``sources._text``, shared with hn_hiring — the two adapters read the same threads
# with the same conventions, and keeping private copies is how the entity bug
# survived here as a hand-rolled ``.replace("&amp;", "&")``: it decoded 14 of the
# 480 entities measured across 50 live HN leads and left ``&#x27;`` and ``&#x2F;``
# in the text the model reads.


def _is_seeking_freelancer(text: str) -> bool:
    """True if the comment is a client hiring a freelancer with a relevant skill."""
    low = text.lower()
    if _SEEKING_WORK_RE.search(low):
        return False
    if not matches_keywords(text):
        return False
    if _SEEKING_FREELANCER_RE.search(low):
        return True
    return False


class HNFreelancerSource(LeadSource):
    name = "hn_freelancer"

    def __init__(self, base_url: str = ALGOLIA_BASE) -> None:
        self.base_url = base_url.rstrip("/")
        #: Last transport/HTTP failure, or None if every request succeeded. Read by
        #: the funnel report so "Algolia rejected us" is not reported as "no jobs
        #: matched": a swallowed 500 and a genuinely quiet thread both returned [] and
        #: both surfaced as ``dead: fetched nothing``, which names the wrong lever —
        #: one needs a code fix, the other needs different queries.
        self.last_error: str | None = None

    def _find_story_id(self, client: httpx.Client) -> str | None:
        try:
            resp = client.get(
                f"{self.base_url}/search_by_date",
                params={
                    "query": "Freelancer? Seeking freelancer?",
                    # NOTE: do NOT pin author here. The ``whoishiring`` bot stopped
                    # posting the freelancer thread after Oct 2025 (it is now posted
                    # by a community member), so ``author_whoishiring`` silently
                    # resolved to a stale 2025 story — which quietly turned this
                    # source into a duplicate of hn_hiring on year-old salaried posts.
                    "tags": "story",
                    "restrictSearchableAttributes": "title",
                    "hitsPerPage": 1,
                },
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
        except Exception as exc:
            logger.warning("hn_freelancer: story search failed: %s", exc)
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        if not hits:
            return None
        return str(hits[0].get("objectID") or "") or None

    def _fetch_story(self, client: httpx.Client, story_id: str) -> dict | None:
        try:
            resp = client.get(f"{self.base_url}/items/{story_id}")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("hn_freelancer: item fetch failed: %s", exc)
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    def _comment_to_lead(self, comment: dict) -> Lead | None:
        text = strip_html(comment.get("text") or "")
        if not text or not _is_seeking_freelancer(text):
            return None
        object_id = comment.get("objectID") or comment.get("id")
        if object_id is None:
            return None
        object_id = str(object_id)
        return Lead(
            source=self.name,
            external_id=object_id,
            title=first_line(text) or "HN seeking-freelancer post",
            description=text,
            url=f"{HN_ITEM_URL}{object_id}",
            company=detect_company(text) or comment.get("author"),
            posted_at=comment.get("created_at"),
            tags=extract_tags(text),
            raw=comment,
        )

    def fetch(self, limit: int = 50) -> list[Lead]:
        leads: list[Lead] = []
        # Per-fetch, so a source that recovers stops reporting a stale error. Without
        # the reset one bad run marks the feed ``broken`` forever, which is the mirror
        # of the bug last_error exists to fix.
        self.last_error = None
        try:
            with httpx.Client(
                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
            ) as client:
                story_id = self._find_story_id(client)
                if not story_id:
                    return leads
                story = self._fetch_story(client, story_id)
        except Exception as exc:  # pragma: no cover
            logger.warning("hn_freelancer: client error: %s", exc)
            self.last_error = f"{type(exc).__name__}: {exc}"
            return leads

        if not story:
            return leads

        for comment in story.get("children", []) or []:
            if len(leads) >= limit:
                break
            if not isinstance(comment, dict):
                continue
            try:
                lead = self._comment_to_lead(comment)
            except Exception as exc:
                logger.warning("hn_freelancer: bad comment: %s", exc)
                continue
            if lead is not None:
                leads.append(lead)
        return leads[:limit]
