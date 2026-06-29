"""Hacker News "Ask HN: Who is hiring?" adapter.

Uses the public HN Algolia API (READ-ONLY) to:

1. find the most recent "Ask HN: Who is hiring?" story, then
2. fetch that story's comments, and
3. turn each comment mentioning the copilot's keywords (remote, devops,
   kubernetes, cloud, sre, platform, security, ...) into a
   :class:`~core.schemas.Lead`.

Network/parse failures are tolerated — the adapter returns [] or a partial list.
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


def _strip_html(text: str) -> str:
    return _TAG_RE.sub(" ", text or "").strip()


def _first_line(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return (line[:117] + "...") if len(line) > 120 else line


class HNWhoIsHiringSource(LeadSource):
    name = "hn_hiring"

    def __init__(self, base_url: str = ALGOLIA_BASE) -> None:
        self.base_url = base_url.rstrip("/")

    def _find_story_id(self, client: httpx.Client) -> str | None:
        try:
            resp = client.get(
                f"{self.base_url}/search_by_date",
                params={
                    "query": "Ask HN: Who is hiring?",
                    "tags": "story,author_whoishiring",
                    "hitsPerPage": 1,
                },
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
        except Exception as exc:
            logger.warning("hn_hiring: story search failed: %s", exc)
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
            logger.warning("hn_hiring: item fetch failed: %s", exc)
            return None

    def _comment_to_lead(self, comment: dict) -> Lead | None:
        text = _strip_html(comment.get("text") or "")
        if not text or not matches_keywords(text):
            return None
        object_id = comment.get("objectID") or comment.get("id")
        if object_id is None:
            return None
        object_id = str(object_id)
        return Lead(
            source=self.name,
            external_id=object_id,
            title=_first_line(text) or "HN who-is-hiring post",
            description=text,
            url=f"{HN_ITEM_URL}{object_id}",
            company=comment.get("author"),
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
            logger.warning("hn_hiring: client error: %s", exc)
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
                logger.warning("hn_hiring: bad comment: %s", exc)
                continue
            if lead is not None:
                leads.append(lead)
        return leads[:limit]
