"""Working Nomads adapter (public JSON API, no auth).

Working Nomads (workingnomads.com) exposes its live remote-job list as a single
JSON array. We fetch it and keep only DevSecOps-relevant listings. The job URL is
used as the stable external id (the feed has no numeric id).

Read-only. Tolerant of network/parse failures — returns [] , never raises.
"""
from __future__ import annotations

import logging
import re

import httpx

from core.schemas import Lead
from sources._keywords import extract_tags, matches_keywords
from sources.base import LeadSource

logger = logging.getLogger(__name__)

WORKING_NOMADS_ENDPOINT = "https://www.workingnomads.com/api/exposed_jobs/"
USER_AGENT = "ai-freelance-copilot/1.0 (personal lead reader)"
TIMEOUT = 12.0

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _TAG_RE.sub(" ", text or "").strip()


class WorkingNomadsSource(LeadSource):
    name = "working_nomads"

    def __init__(self, endpoint: str = WORKING_NOMADS_ENDPOINT) -> None:
        self.endpoint = endpoint

    def _job_to_lead(self, job: dict) -> Lead | None:
        url = str(job.get("url") or "")
        title = str(job.get("title") or "")
        desc = _strip_html(str(job.get("description") or ""))
        if not url or not title:
            return None
        if not matches_keywords(title, desc):
            return None
        return Lead(
            source=self.name,
            external_id=url,  # stable; the feed has no numeric id
            title=title,
            description=desc,
            url=url,
            company=job.get("company_name") or None,
            posted_at=job.get("pub_date") or None,
            tags=extract_tags(title, desc),
            raw=job,
        )

    def fetch(self, limit: int = 50) -> list[Lead]:
        try:
            resp = httpx.get(
                self.endpoint, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
            )
            resp.raise_for_status()
            jobs = resp.json()
        except Exception as exc:
            logger.warning("working_nomads: fetch failed: %s", exc)
            return []

        if not isinstance(jobs, list):
            return []

        leads: list[Lead] = []
        seen: set[str] = set()
        for job in jobs:
            if len(leads) >= limit:
                break
            if not isinstance(job, dict):
                continue
            try:
                lead = self._job_to_lead(job)
            except Exception as exc:
                logger.warning("working_nomads: bad job: %s", exc)
                continue
            if lead is not None and lead.external_id not in seen:
                seen.add(lead.external_id)
                leads.append(lead)
        return leads[:limit]
