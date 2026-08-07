"""Hacker News "Ask HN: Who is hiring?" adapter.

Uses the public HN Algolia API (READ-ONLY) to:

1. find the most recent "Ask HN: Who is hiring?" stories, then
2. fetch those stories' comments, and
3. turn each comment mentioning the copilot's keywords (remote, devops,
   kubernetes, cloud, sre, platform, security, LLM, RAG, ...) into a
   :class:`~core.schemas.Lead`.

Why this is the *primary* source
--------------------------------
Measured on the live August 2026 thread: of 43 top-level comments, 26 carried a
relevant keyword and **6 published a direct hiring-manager email** — a ~23% raw /
46%-of-drafted contactable rate. Every other source in the registry surfaces ATS
"apply" links with no reachable address, which is precisely why 18 of 25 drafted
proposals died at the contact step. HN posts are the only high-volume place where
the person doing the hiring writes their own address in public.

Two things follow from that, both implemented below:

* **Ordering matters more than volume.** ``per_source_limit`` truncates, and the
  thread grows to ~600 comments over a month. Taking the first N in thread order
  discards contactable leads at random, so comments are *ranked* — an address
  first, then an AI-infra signal — before the limit is applied.
* **One thread is not enough early in the month.** A thread posted on the 1st has
  a handful of comments, so the previous month's thread (still actively read and
  its addresses still live) is included as a fallback.

Network/parse failures are tolerated — the adapter returns [] or a partial list.
"""
from __future__ import annotations

import logging
import re

import httpx

from core.schemas import Lead
from sources._keywords import extract_tags, is_ai_infra, matches_keywords
from sources._text import detect_company, detect_role, first_line, strip_html
from sources.base import LeadSource, dedupe

logger = logging.getLogger(__name__)

ALGOLIA_BASE = "https://hn.algolia.com/api/v1"
HN_ITEM_URL = "https://news.ycombinator.com/item?id="
USER_AGENT = "ai-freelance-copilot/1.0 (+https://github.com) read-only lead scanner"
TIMEOUT = 10.0

#: How many recent "Who is hiring?" threads to read. 2 = this month + last month,
#: so a run on the 1st of the month is not starved by an empty new thread.
DEFAULT_STORIES = 2

#: Deliberately permissive: this only *ranks* comments, it does not decide
#: deliverability (outreach.extract does that, with validation and a DNS check).
_EMAIL_HINT_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}"
    r"|[\[\(\{]\s*at\s*[\]\)\}]",  # "name [at] domain [dot] com" obfuscation
    re.IGNORECASE,
)


def _has_contact_hint(text: str) -> bool:
    return bool(_EMAIL_HINT_RE.search(text or ""))


def _priority(lead: Lead) -> tuple[int, int]:
    """Sort key (descending) deciding which leads survive ``limit``.

    An address is worth more than a perfect topic match, because a lead with no
    address cannot be actioned at all by the auto-email channel. Among contactable
    leads, AI-infra ones rank higher: that segment pays more (£550/day median vs
    £535 for Kubernetes) and is growing (+247% YoY vacancies).
    """
    text = lead.description or ""
    return (1 if _has_contact_hint(text) else 0, 1 if is_ai_infra(text) else 0)


def _title_for(text: str) -> str:
    """The Title field the qualifier prompt will read.

    HN who-is-hiring headers follow a pipe convention — ``Company | Role |
    Location | Type`` — but the old code sent ``first_line(text)`` truncated at
    117 characters, and 46 of 50 live titles on 2026-08-07 therefore ended in
    ``...`` with the role clipped off entirely::

        'Snout  https:&#x2F;&#x2F;snout.com&#x2F;  | Multiple Engineering +
         Product Roles | Remote US or Ontario, Canada | Ful...'

    ``Company — Role`` is what a human would call that post. Many comments ignore
    the convention, so an undetectable role falls back to the old first line
    rather than to a guess: a location in the Title field would score no better
    than a truncation, and would be harder to notice.
    """
    company, role = detect_company(text), detect_role(text)
    if company and role:
        return first_line(f"{company} — {role}")
    return first_line(role or text)


class HNWhoIsHiringSource(LeadSource):
    name = "hn_hiring"

    def __init__(
        self, base_url: str = ALGOLIA_BASE, stories: int = DEFAULT_STORIES
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.stories = max(1, stories)
        #: Last transport/HTTP failure, or None if every request succeeded. Read by
        #: the funnel report so "Algolia rejected us" is not reported as "no jobs
        #: matched": a swallowed 500 and a genuinely quiet thread both returned [] and
        #: both surfaced as ``dead: fetched nothing``, which names the wrong lever —
        #: one needs a code fix, the other needs different queries.
        self.last_error: str | None = None

    def _find_story_ids(self, client: httpx.Client) -> list[str]:
        """Newest-first story IDs for the most recent who-is-hiring threads."""
        try:
            resp = client.get(
                f"{self.base_url}/search_by_date",
                params={
                    "query": "Ask HN: Who is hiring?",
                    "tags": "story,author_whoishiring",
                    "hitsPerPage": self.stories,
                },
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
        except Exception as exc:
            logger.warning("hn_hiring: story search failed: %s", exc)
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        return [str(h.get("objectID")) for h in hits if h.get("objectID")]

    def _find_story_id(self, client: httpx.Client) -> str | None:
        """Back-compat single-story lookup."""
        ids = self._find_story_ids(client)
        return ids[0] if ids else None

    def _fetch_story(self, client: httpx.Client, story_id: str) -> dict | None:
        try:
            resp = client.get(f"{self.base_url}/items/{story_id}")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("hn_hiring: item fetch failed: %s", exc)
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    def _comment_to_lead(self, comment: dict) -> Lead | None:
        text = strip_html(comment.get("text") or "")
        if not text or not matches_keywords(text):
            return None
        object_id = comment.get("objectID") or comment.get("id")
        if object_id is None:
            return None
        object_id = str(object_id)
        return Lead(
            source=self.name,
            external_id=object_id,
            title=_title_for(text) or "HN who-is-hiring post",
            description=text,
            url=f"{HN_ITEM_URL}{object_id}",
            # The header's company, not the HN account that typed it. ``author``
            # made the prompt say ``Company: kcartmell`` for 50 of 50 live leads,
            # and the researcher agent then went looking for a company by that
            # name. It stays only as a last resort, because a lead with no company
            # at all is still worth scoring on its description.
            company=detect_company(text) or comment.get("author"),
            posted_at=comment.get("created_at"),
            tags=extract_tags(text),
            raw=comment,
        )

    def _walk(self, comment: dict, out: list[Lead]) -> None:
        """Collect leads from a comment and its replies.

        Replies matter: a poster often puts the role in the top comment and the
        address in a follow-up ("email me at ..."), and recruiters reply to their
        own posts with contact details.
        """
        if not isinstance(comment, dict):
            return
        try:
            lead = self._comment_to_lead(comment)
        except Exception as exc:
            logger.warning("hn_hiring: bad comment: %s", exc)
            lead = None
        if lead is not None:
            out.append(lead)
        for child in comment.get("children") or []:
            self._walk(child, out)

    def fetch(self, limit: int = 50) -> list[Lead]:
        candidates: list[Lead] = []
        # Per-fetch, so a source that recovers stops reporting a stale error. Without
        # the reset one bad run marks the feed ``broken`` forever, which is the mirror
        # of the bug last_error exists to fix.
        self.last_error = None
        try:
            with httpx.Client(
                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
            ) as client:
                story_ids = self._find_story_ids(client)
                if not story_ids:
                    return []
                stories = [self._fetch_story(client, sid) for sid in story_ids]
        except Exception as exc:  # pragma: no cover
            logger.warning("hn_hiring: client error: %s", exc)
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []

        for story in stories:
            if not story:
                continue
            for comment in story.get("children", []) or []:
                self._walk(comment, candidates)

        candidates = dedupe(candidates)
        # Rank BEFORE truncating. The old code took the first `limit` comments in
        # thread order, which threw away contactable leads at random once the
        # thread grew past the limit — the single biggest cause of "no_email".
        candidates.sort(key=_priority, reverse=True)
        if len(candidates) > limit:
            kept = candidates[:limit]
            logger.info(
                "hn_hiring: %d relevant comments, keeping top %d by contactability "
                "(%d of the kept have a contact hint)",
                len(candidates),
                limit,
                sum(1 for lead in kept if _has_contact_hint(lead.description or "")),
            )
            candidates = kept
        return candidates
