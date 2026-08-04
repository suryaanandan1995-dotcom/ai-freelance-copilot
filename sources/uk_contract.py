"""UK day-rate contract adapter (Adzuna official API).

Why this source exists
----------------------
The copilot's original sources were all *freelance-gig* boards, and measurement
over 24 production runs showed that market is empty for this ICP: Upwork's RSS is
gone (HTTP 410), r/forhire blocks datacenter IPs, and the HN freelancer thread
carries ~0-1 relevant posts a month. Meanwhile the UK *contract* market is large
and liquid — ~5,600 live contract vacancies citing DevOps at a £525/day median
(£550 in London), and LLM-citing contract vacancies grew +247% year-on-year.

So this adapter targets **day-rate contract roles**, which is where the money
actually is for a senior London-based DevSecOps/AI-infra contractor.

Why Adzuna specifically
-----------------------
Every keyless UK board (Reed, CWJobs, CV-Library, Jobserve, Technojobs,
findajob.dwp.gov.uk) is behind Cloudflare or blocks datacenter IPs outright —
verified 2026-08-03. Adzuna publishes an official, free-tier JSON API that
aggregates most of those same boards, supports ``contract_only``, and returns
salary bounds. It needs a (free) app id + key.

Configuration
-------------
Register at https://developer.adzuna.com and set either in ``.env`` or the
environment (both work — they are read through ``Settings``)::

    COPILOT_ADZUNA_APP_ID=...
    COPILOT_ADZUNA_APP_KEY=...

**Unconfigured behaviour is deliberate:** :meth:`fetch` logs a clear one-line
warning naming the missing variables and returns []. It does NOT fail silently —
silent zero-yield sources are exactly how this pipeline spent a month reporting
"success" while sending nothing.

Read-only: fetches public listings only, never applies or submits.
"""
from __future__ import annotations

import logging
import re

import httpx

from config import get_settings
from core.schemas import Lead
from sources._keywords import extract_tags, matches_keywords
from sources.base import LeadSource

logger = logging.getLogger(__name__)

API_BASE = "https://api.adzuna.com/v1/api/jobs/gb/search/1"
USER_AGENT = "ai-freelance-copilot/1.0 (personal lead reader)"
TIMEOUT = 15.0

#: Searches to run, aimed at the segment the market data says is growing.
#: Each becomes one ``what_or`` query against contract-only listings.
DEFAULT_QUERIES: tuple[str, ...] = (
    "devops kubernetes platform engineer",
    "site reliability engineer sre terraform",
    "llm ai infrastructure mlops rag",
    # AI agent engineering — the newest and fastest-growing slice.
    "ai agent langgraph agentic engineer",
    # Forward-deployed / solutions engineering: customer-facing delivery of an AI
    # product inside the client's stack. Frequently contract or contract-to-hire,
    # and it rewards exactly the "build in someone else's environment" skill set
    # that contract DevSecOps work already builds.
    "forward deployed solutions engineer ai",
)

#: Where to search. "London" first — it carries the day-rate premium (£550 vs
#: £500 outside London) and the largest contract volume.
DEFAULT_LOCATIONS: tuple[str, ...] = ("London", "UK")

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _TAG_RE.sub(" ", text or "").strip()


def _credentials() -> tuple[str, str]:
    """Read the Adzuna app id + key.

    Via ``Settings``, not ``os.environ``: pydantic-settings loads ``.env`` into the
    settings object and never into the process environment, so reading os.environ
    directly here meant keys set in ``.env`` were silently ignored and the source
    reported itself DISABLED — "you never configured it" rather than "your config is
    being ignored". ``os.environ`` still works, because pydantic reads it too.
    """
    settings = get_settings()
    return (
        (settings.adzuna_app_id or "").strip(),
        (settings.adzuna_app_key or "").strip(),
    )


def _format_budget(job: dict) -> str | None:
    """Render Adzuna's salary bounds as a human day/annum string.

    Adzuna normalises contract pay to an *annualised* figure, so a £550/day
    contract surfaces as ~£143k. We keep the raw annualised numbers rather than
    guessing a day rate from them — a wrong rate in a proposal is worse than none.
    """
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if not lo and not hi:
        return None
    if lo and hi and lo != hi:
        return f"£{int(lo):,}-£{int(hi):,} (annualised)"
    single = lo or hi
    return f"£{int(single):,} (annualised)"


class UKContractSource(LeadSource):
    name = "uk_contract"

    def __init__(
        self,
        queries: tuple[str, ...] = DEFAULT_QUERIES,
        locations: tuple[str, ...] = DEFAULT_LOCATIONS,
        base_url: str = API_BASE,
        max_days_old: int = 14,
    ) -> None:
        self.queries = queries
        self.locations = locations
        self.base_url = base_url
        self.max_days_old = max_days_old

    def _job_to_lead(self, job: dict) -> Lead | None:
        job_id = job.get("id")
        title = str(job.get("title") or "")
        desc = _strip_html(str(job.get("description") or ""))
        if not job_id or not title:
            return None
        # Keyword gate: Adzuna's relevance ranking is loose, so a "devops" query
        # still returns e.g. sales roles at devops companies.
        if not matches_keywords(title, desc):
            return None
        company = ((job.get("company") or {}) or {}).get("display_name")
        location = ((job.get("location") or {}) or {}).get("display_name")
        return Lead(
            source=self.name,
            external_id=str(job_id),
            title=f"{company}: {title}" if company else title,
            description=desc,
            url=str(job.get("redirect_url") or ""),
            company=company or None,
            budget=_format_budget(job),
            posted_at=job.get("created") or None,
            tags=extract_tags(title, desc) + ([location] if location else []),
            raw=job,
        )

    def _fetch_slice(
        self, client: httpx.Client, query: str, where: str, limit: int
    ) -> list[Lead]:
        app_id, app_key = _credentials()
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "what_or": query,
            "where": where,
            "results_per_page": min(50, max(1, limit)),
            "contract_only": 1,  # day-rate contracts only, not permanent roles
            "max_days_old": self.max_days_old,
            "sort_by": "date",
            "content-type": "application/json",
        }
        try:
            resp = client.get(self.base_url, params=params)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning("uk_contract: fetch failed for %r/%r: %s", query, where, exc)
            return []

        results = payload.get("results", []) if isinstance(payload, dict) else []
        leads: list[Lead] = []
        for job in results if isinstance(results, list) else []:
            if len(leads) >= limit:
                break
            if not isinstance(job, dict):
                continue
            try:
                lead = self._job_to_lead(job)
            except Exception as exc:
                logger.warning("uk_contract: bad job: %s", exc)
                continue
            if lead is not None:
                leads.append(lead)
        return leads

    def fetch(self, limit: int = 50) -> list[Lead]:
        app_id, app_key = _credentials()
        if not app_id or not app_key:
            logger.warning(
                "uk_contract: DISABLED — set COPILOT_ADZUNA_APP_ID and "
                "COPILOT_ADZUNA_APP_KEY (free key: https://developer.adzuna.com). "
                "This is the primary UK day-rate contract source; without it the "
                "pipeline only sees salaried/remote-board listings."
            )
            return []

        leads: list[Lead] = []
        seen: set[str] = set()
        try:
            with httpx.Client(
                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
            ) as client:
                for where in self.locations:
                    for query in self.queries:
                        if len(leads) >= limit:
                            return leads[:limit]
                        for lead in self._fetch_slice(
                            client, query, where, limit - len(leads)
                        ):
                            if lead.external_id in seen:
                                continue
                            seen.add(lead.external_id)
                            leads.append(lead)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("uk_contract: client error: %s", exc)
        return leads[:limit]
