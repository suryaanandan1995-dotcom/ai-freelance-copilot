"""Reddit r/forhire (and r/jobbit) "[Hiring]" adapter.

Both subreddits carry two kinds of posts, flagged in the title/flair:

* ``[Hiring]``   — a client looking to hire someone (these are the leads).
* ``[For Hire]`` — a freelancer advertising availability (NOT a lead).
* ``[Task]``     — a one-off micro task (NOT a lead).

We only want the ``[Hiring]`` side, because those are actual clients — and they
frequently include a direct contact email in the post body, which makes them
high-value for auto-email outreach. Kept posts must also carry a genuine
DevSecOps keyword.

Uses Reddit's PUBLIC ``/new.json`` endpoints (no auth). Reddit rejects requests
without a descriptive User-Agent (429), so one is always set. Network/parse
failures are tolerated per-endpoint — returns [] or partial, never raises.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

import httpx

from core.schemas import Lead
from sources._keywords import extract_tags, matches_keywords
from sources.base import LeadSource

logger = logging.getLogger(__name__)

REDDIT_ENDPOINTS = (
    "https://www.reddit.com/r/forhire/new.json?limit=100",
    "https://www.reddit.com/r/jobbit/new.json?limit=100",
)
# Reddit requires a descriptive, non-generic User-Agent or it 429s.
USER_AGENT = "ai-freelance-copilot/1.0 (personal lead reader)"
TIMEOUT = 10.0
REDDIT_BASE = "https://www.reddit.com"

# "[Hiring]" appears in the title and/or the link_flair_text.
_HIRING_RE = re.compile(r"\[\s*hiring\s*\]", re.IGNORECASE)
# Freelancer-availability / micro-task markers we must exclude.
_EXCLUDE_RE = re.compile(r"\[\s*for\s*hire\s*\]|\[\s*task\s*\]", re.IGNORECASE)


def _iso_from_utc(created_utc: object) -> str | None:
    """Convert a Reddit ``created_utc`` epoch float to an ISO8601 string."""
    try:
        ts = float(created_utc)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def _is_hiring(data: dict) -> bool:
    """True if the post is a client hiring (``[Hiring]``), not offering."""
    title = str(data.get("title") or "")
    flair = str(data.get("link_flair_text") or "")
    blob = f"{title} {flair}"
    if _EXCLUDE_RE.search(blob):
        return False
    return bool(_HIRING_RE.search(blob))


class RedditForHireSource(LeadSource):
    name = "reddit_forhire"

    def __init__(self, endpoints: tuple[str, ...] = REDDIT_ENDPOINTS) -> None:
        self.endpoints = endpoints

    def _fetch_endpoint(self, url: str, limit: int) -> list[Lead]:
        leads: list[Lead] = []
        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning("reddit_forhire: fetch failed for %s: %s", url, exc)
            return leads

        children = (
            payload.get("data", {}).get("children", [])
            if isinstance(payload, dict)
            else []
        )
        if not isinstance(children, list):
            return leads

        for child in children:
            if len(leads) >= limit:
                break
            if not isinstance(child, dict):
                continue
            data = child.get("data")
            if not isinstance(data, dict):
                continue
            try:
                lead = self._post_to_lead(data)
            except Exception as exc:
                logger.warning("reddit_forhire: bad post: %s", exc)
                continue
            if lead is not None:
                leads.append(lead)
        return leads

    def _post_to_lead(self, data: dict) -> Lead | None:
        if not _is_hiring(data):
            return None
        post_id = data.get("id")
        if not post_id:
            return None
        title = str(data.get("title") or "")
        selftext = data.get("selftext") or ""
        if not matches_keywords(title, selftext):
            return None
        permalink = data.get("permalink") or ""
        return Lead(
            source=self.name,
            external_id=str(post_id),
            title=title,
            description=str(selftext),
            url=f"{REDDIT_BASE}{permalink}" if permalink else "",
            company=data.get("author") or None,
            posted_at=_iso_from_utc(data.get("created_utc")),
            tags=extract_tags(title, selftext),
            raw=data,
        )

    def fetch(self, limit: int = 50) -> list[Lead]:
        leads: list[Lead] = []
        seen: set[str] = set()
        for url in self.endpoints:
            if len(leads) >= limit:
                break
            for lead in self._fetch_endpoint(url, limit - len(leads)):
                if lead.external_id in seen:
                    continue
                seen.add(lead.external_id)
                leads.append(lead)
                if len(leads) >= limit:
                    break
        return leads[:limit]
