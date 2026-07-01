"""Public remote-job board adapter (niche DevOps category feeds).

READ-ONLY: fetches public listings and filters them to DevOps / cloud / SRE /
security / Kubernetes / Terraform roles. Rather than scanning each board's whole
firehose, it targets niche category feeds where available:

* RemoteOK  -> full JSON API, filtered per-job by title/tags/description.
* WeWorkRemotely -> the DevOps/SysAdmin category RSS.
* Remotive  -> the ``category=devops`` REST API.

Network failures are tolerated per-feed — the adapter returns whatever it
managed to collect (possibly nothing).
"""
from __future__ import annotations

import hashlib
import logging

import feedparser
import httpx

from core.schemas import Lead
from sources._keywords import extract_tags, matches_keywords
from sources.base import LeadSource, dedupe

logger = logging.getLogger(__name__)

REMOTEOK_API = "https://remoteok.com/api"
WWR_RSS = "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss"
REMOTIVE_API = "https://remotive.com/api/remote-jobs?category=devops"
USER_AGENT = "ai-freelance-copilot/1.0 (+https://github.com) read-only lead scanner"
TIMEOUT = 10.0


class RemoteBoardsSource(LeadSource):
    name = "remote_boards"

    def __init__(
        self,
        remoteok_url: str = REMOTEOK_API,
        wwr_rss_url: str = WWR_RSS,
        remotive_url: str = REMOTIVE_API,
    ) -> None:
        self.remoteok_url = remoteok_url
        self.wwr_rss_url = wwr_rss_url
        self.remotive_url = remotive_url

    # --- RemoteOK (JSON) ---------------------------------------------------
    def _fetch_remoteok(self, limit: int) -> list[Lead]:
        leads: list[Lead] = []
        try:
            resp = httpx.get(
                self.remoteok_url,
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("remote_boards: RemoteOK fetch failed: %s", exc)
            return leads

        if not isinstance(data, list):
            return leads

        for item in data:
            if len(leads) >= limit:
                break
            if not isinstance(item, dict) or "id" not in item:
                # RemoteOK's first element is a legal/metadata blob.
                continue
            title = item.get("position") or item.get("title") or ""
            company = item.get("company") or None
            desc = item.get("description") or ""
            tag_list = item.get("tags") or []
            if not isinstance(tag_list, list):
                tag_list = []
            tag_blob = " ".join(map(str, tag_list))
            if not matches_keywords(title, desc, tag_blob):
                continue
            tags = extract_tags(title, desc, tag_blob)
            leads.append(
                Lead(
                    source=self.name,
                    external_id=f"remoteok:{item.get('id')}",
                    title=str(title).strip(),
                    description=str(desc),
                    url=item.get("url") or item.get("apply_url") or "",
                    company=company,
                    tags=tags,
                    posted_at=item.get("date"),
                    raw=item,
                )
            )
        return leads

    # --- WeWorkRemotely (DevOps/SysAdmin category RSS) ---------------------
    def _fetch_wwr(self, limit: int) -> list[Lead]:
        leads: list[Lead] = []
        try:
            parsed = feedparser.parse(self.wwr_rss_url)
        except Exception as exc:  # pragma: no cover
            logger.warning("remote_boards: WWR parse failed: %s", exc)
            return leads
        for entry in getattr(parsed, "entries", []) or []:
            if len(leads) >= limit:
                break

            def get(k, d=None, _e=entry):
                return _e.get(k, d) if hasattr(_e, "get") else getattr(_e, k, d)

            title = get("title", "") or ""
            summary = get("summary", "") or get("description", "") or ""
            # This is already a DevOps category feed, but keep the filter so a
            # mislabeled entry can't leak through.
            if not matches_keywords(title, summary):
                continue
            link = get("link", "") or ""
            external_id = get("id", "") or (
                hashlib.sha1(link.encode("utf-8")).hexdigest() if link else ""
            )
            if not external_id:
                continue
            leads.append(
                Lead(
                    source=self.name,
                    external_id=f"wwr:{external_id}",
                    title=title.strip(),
                    description=summary,
                    url=link,
                    company=get("author", None) or None,
                    posted_at=get("published", None) or get("updated", None),
                    tags=extract_tags(title, summary),
                    raw=dict(entry) if hasattr(entry, "keys") else {},
                )
            )
        return leads

    # --- Remotive (DevOps category JSON) ----------------------------------
    def _fetch_remotive(self, limit: int) -> list[Lead]:
        leads: list[Lead] = []
        try:
            resp = httpx.get(
                self.remotive_url,
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("remote_boards: Remotive fetch failed: %s", exc)
            return leads

        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        if not isinstance(jobs, list):
            return leads

        for job in jobs:
            if len(leads) >= limit:
                break
            if not isinstance(job, dict):
                continue
            job_id = job.get("id")
            if job_id is None:
                continue
            title = job.get("title") or ""
            company = job.get("company_name") or None
            desc = job.get("description") or ""
            tag_list = job.get("tags") or []
            if not isinstance(tag_list, list):
                tag_list = []
            tag_blob = " ".join(map(str, tag_list))
            # This is the DevOps category feed, but keep the filter as a guard.
            if not matches_keywords(title, desc, tag_blob):
                continue
            leads.append(
                Lead(
                    source=self.name,
                    external_id=f"remotive:{job_id}",
                    title=str(title).strip(),
                    description=str(desc),
                    url=job.get("url") or "",
                    company=company,
                    tags=extract_tags(title, desc, tag_blob),
                    posted_at=job.get("publication_date"),
                    raw=job,
                )
            )
        return leads

    def fetch(self, limit: int = 50) -> list[Lead]:
        leads: list[Lead] = []
        leads.extend(self._fetch_remoteok(limit))
        remaining = limit - len(leads)
        if remaining > 0:
            leads.extend(self._fetch_wwr(remaining))
        remaining = limit - len(leads)
        if remaining > 0:
            leads.extend(self._fetch_remotive(remaining))
        return dedupe(leads)[:limit]
