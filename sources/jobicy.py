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


class JobicySource(LeadSource):
    name = "jobicy"

    def __init__(self, endpoints: tuple[str, ...] = JOBICY_ENDPOINTS) -> None:
        self.endpoints = endpoints

    def _fetch_endpoint(self, url: str, limit: int) -> list[Lead]:
        leads: list[Lead] = []
        try:
            resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning("jobicy: fetch failed for %s: %s", url, exc)
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
            company=job.get("companyName") or None,
            posted_at=job.get("pubDate") or None,
            tags=extract_tags(title, desc),
            raw=job,
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
