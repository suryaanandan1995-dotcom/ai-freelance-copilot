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
from sources.base import LeadSource

logger = logging.getLogger(__name__)

ALGOLIA_BASE = "https://hn.algolia.com/api/v1"
HN_ITEM_URL = "https://news.ycombinator.com/item?id="
USER_AGENT = "ai-freelance-copilot/1.0 (+https://github.com) read-only lead scanner"
TIMEOUT = 10.0

_TAG_RE = re.compile(r"<[^>]+>")

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


def _strip_html(text: str) -> str:
    return _TAG_RE.sub(" ", text or "").replace("&amp;", "&").strip()


def _first_line(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return (line[:117] + "...") if len(line) > 120 else line


def _detect_company(text: str) -> str | None:
    """Best-effort company name: leading token before a ``|`` delimiter."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    if "|" in first:
        candidate = first.split("|", 1)[0].strip()
        if 0 < len(candidate) <= 60:
            return candidate
    return None


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

    def _find_story_id(self, client: httpx.Client) -> str | None:
        try:
            resp = client.get(
                f"{self.base_url}/search_by_date",
                params={
                    "query": "Ask HN: Freelancer? Seeking Freelancer?",
                    "tags": "story,author_whoishiring",
                    "hitsPerPage": 1,
                },
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
        except Exception as exc:
            logger.warning("hn_freelancer: story search failed: %s", exc)
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
            return None

    def _comment_to_lead(self, comment: dict) -> Lead | None:
        text = _strip_html(comment.get("text") or "")
        if not text or not _is_seeking_freelancer(text):
            return None
        object_id = comment.get("objectID") or comment.get("id")
        if object_id is None:
            return None
        object_id = str(object_id)
        return Lead(
            source=self.name,
            external_id=object_id,
            title=_first_line(text) or "HN seeking-freelancer post",
            description=text,
            url=f"{HN_ITEM_URL}{object_id}",
            company=_detect_company(text) or comment.get("author"),
            posted_at=comment.get("created_at"),
            tags=extract_tags(text),
            raw=comment,
        )

    def fetch(self, limit: int = 50) -> list[Lead]:
        leads: list[Lead] = []
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
