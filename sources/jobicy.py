"""Jobicy remote-jobs adapter (public JSON API, no auth).

Jobicy (jobicy.com) publishes remote jobs through a free JSON API. We pull a few
DevOps/engineering slices and keep only listings that carry a genuine DevSecOps
keyword, widening the top of the funnel beyond RemoteOK/Remotive/WWR.

Read-only. Tolerant of network/parse failures per endpoint — returns [] (or a
partial list), never raises.
"""
from __future__ import annotations

import logging
import re

import httpx

from core.schemas import Lead
from sources._keywords import extract_tags, matches_keywords
from sources.base import LeadSource

logger = logging.getLogger(__name__)

# Public JSON API. `tag` is a free-text search; we query a few relevant slices.
JOBICY_ENDPOINTS = (
    "https://jobicy.com/api/v2/remote-jobs?count=50&tag=devops",
    "https://jobicy.com/api/v2/remote-jobs?count=50&tag=kubernetes",
    "https://jobicy.com/api/v2/remote-jobs?count=50&tag=platform+engineer",
)
USER_AGENT = "ai-freelance-copilot/1.0 (personal lead reader)"
TIMEOUT = 10.0

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _TAG_RE.sub(" ", text or "").strip()


#: Currency code -> symbol, for the codes Jobicy actually emits. Unknown codes fall
#: back to the code itself ("PLN 12,000"), which is ugly but never wrong; guessing a
#: symbol would put a different currency's number in a proposal.
_CURRENCY_SYMBOL: dict[str, str] = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "C$", "AUD": "A$"}

#: How Jobicy spells ``salaryPeriod``, mapped to the unit printed in the prompt. The
#: period is never inferred: an annual figure rendered as a day rate is how a proposal
#: quotes £143,000 a day, and the qualifier prompt explicitly rewards day-rate work,
#: so the unit is the part it reads.
_PERIOD_LABEL: dict[str, str] = {
    "hourly": "/hour",
    "hour": "/hour",
    "daily": "/day",
    "day": "/day",
    "weekly": "/week",
    "week": "/week",
    "monthly": "/month",
    "month": "/month",
    "yearly": "/year",
    "year": "/year",
    "annual": "/year",
    "annually": "/year",
}


def _format_budget(job: dict) -> str | None:
    """Render Jobicy's salary bounds, or None when the payload states no pay.

    Live payloads carry ``salaryMin``/``salaryMax``/``salaryCurrency``/
    ``salaryPeriod`` and the adapter never set ``Lead.budget``, so every Jobicy
    lead reached the scorer as ``Budget: unknown`` — on a prompt whose scoring
    guidance explicitly rewards contract/day-rate pay. Same rule as
    ``contract_jobs._format_budget``: the stated numbers are passed through and
    nothing is converted or annualised, because a wrong rate in a proposal is
    worse than none. An unstated period is left unlabelled rather than assumed.
    """
    def _num(value: object) -> int | None:
        try:
            out = int(float(str(value)))
        except (TypeError, ValueError):
            return None
        return out if out > 0 else None

    lo, hi = _num(job.get("salaryMin")), _num(job.get("salaryMax"))
    if lo is None and hi is None:
        return None
    code = str(job.get("salaryCurrency") or "").strip().upper()
    sym = _CURRENCY_SYMBOL.get(code, f"{code} " if code else "")
    period = _PERIOD_LABEL.get(str(job.get("salaryPeriod") or "").strip().lower(), "")
    if lo is not None and hi is not None and lo != hi:
        return f"{sym}{lo:,}-{sym}{hi:,}{period}"
    single = lo if lo is not None else hi
    return f"{sym}{single:,}{period}"


class JobicySource(LeadSource):
    name = "jobicy"

    def __init__(self, endpoints: tuple[str, ...] = JOBICY_ENDPOINTS) -> None:
        self.endpoints = endpoints
        #: Last transport/HTTP failure, or None if every endpoint succeeded. Read by
        #: the funnel report so "the API rejected us" is not reported as "no jobs
        #: matched": a swallowed 500 and a genuinely empty market both returned []
        #: and both surfaced as ``dead: fetched nothing``, which names the wrong
        #: lever — one needs a code fix, the other needs different queries.
        self.last_error: str | None = None

    def _fetch_endpoint(self, url: str, limit: int) -> list[Lead]:
        leads: list[Lead] = []
        try:
            resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning("jobicy: fetch failed for %s: %s", url, exc)
            self.last_error = f"{type(exc).__name__}: {exc}"
            return leads

        jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        if not isinstance(jobs, list):
            return leads

        for job in jobs:
            if len(leads) >= limit:
                break
            if not isinstance(job, dict):
                continue
            try:
                lead = self._job_to_lead(job)
            except Exception as exc:
                logger.warning("jobicy: bad job: %s", exc)
                continue
            if lead is not None:
                leads.append(lead)
        return leads

    def _job_to_lead(self, job: dict) -> Lead | None:
        job_id = job.get("id")
        title = str(job.get("jobTitle") or "")
        desc = _strip_html(str(job.get("jobDescription") or ""))
        if not job_id or not title:
            return None
        if not matches_keywords(title, desc):
            return None
        return Lead(
            source=self.name,
            external_id=str(job_id),
            title=title,
            description=desc,
            url=str(job.get("url") or ""),
            budget=_format_budget(job),
            company=job.get("companyName") or None,
            posted_at=job.get("pubDate") or None,
            tags=extract_tags(title, desc),
            raw=job,
        )

    def fetch(self, limit: int = 50) -> list[Lead]:
        self.last_error = None  # per-fetch, so a fixed source stops reporting stale errors
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
