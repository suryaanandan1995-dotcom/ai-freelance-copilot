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
        #: Last transport/HTTP failure, or None if every board succeeded. Read by the
        #: funnel report so "the API rejected us" is not reported as "no jobs matched":
        #: a swallowed 500 and a genuinely empty market both returned [] and both
        #: surfaced as ``dead: fetched nothing``, which names the wrong lever — one
        #: needs a code fix, the other needs different queries.
        #:
        #: This source is three independent boards behind one name, so the message is
        #: PREFIXED with the board that failed and failures accumulate rather than
        #: overwrite. A bare last-writer-wins string would report "RemoteOK is down"
        #: when RemoteOK was fine and Remotive was down, and would hide two outages
        #: behind the third. Two of three failing is also not the same event as all
        #: three: the count is what says whether to look at the network or the code.
        self.last_error: str | None = None
        self._errors: list[str] = []
        #: Listings read across all three boards, before ``matches_keywords``. See
        #: ``LeadSource.scanned``. Accumulated for the same reason ``last_error`` is:
        #: one board's number standing in for three would misreport which lever to
        #: pull. RemoteOK is an all-categories feed, so a large scanned count with few
        #: leads is this board working correctly, not a dead source.
        self.scanned: int | None = None

    def _note_scanned(self, n: int) -> None:
        """Add a board's candidate count, promoting None -> 0 on first real payload."""
        self.scanned = (self.scanned or 0) + n

    def _note_error(self, board: str, exc: Exception) -> None:
        self._note_failure(board, f"{type(exc).__name__}: {exc}")

    def _note_failure(self, board: str, detail: str) -> None:
        """Record a failure that is not an exception (e.g. an HTTP status on a feed)."""
        self._errors.append(f"{board}: {detail}")
        self.last_error = "; ".join(self._errors)

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
            self._note_error("RemoteOK", exc)
            return leads

        if not isinstance(data, list):
            return leads

        # RemoteOK's first element is a legal blob, not a job; excluded so the count
        # reports candidates considered rather than array length.
        self._note_scanned(sum(1 for i in data if isinstance(i, dict) and "id" in i))

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
            self._note_error("WeWorkRemotely", exc)
            return leads
        # feedparser does NOT raise on an HTTP error — it returns an empty feed with
        # ``status``/``bozo`` set. So a WWR outage set no last_error at all and reported
        # as "this board has no infra jobs this week": precisely the bug last_error
        # exists to end, still live in the one board that reads RSS. contra_startup
        # already guards this; this adapter was missed.
        status = getattr(parsed, "status", None)
        if isinstance(status, int) and status >= 400:
            logger.warning("remote_boards: WWR HTTP %s for %s", status, self.wwr_rss_url)
            self._note_failure("WeWorkRemotely", f"HTTP {status}")
            return leads
        wwr_entries = getattr(parsed, "entries", []) or []
        self._note_scanned(len(wwr_entries))
        for entry in wwr_entries:
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
            self._note_error("Remotive", exc)
            return leads

        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        if not isinstance(jobs, list):
            return leads

        self._note_scanned(sum(1 for j in jobs if isinstance(j, dict)))

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
        # Per-fetch, so a board that recovers stops reporting a stale error. Without
        # this reset the source is permanently "broken" after one bad run, which is
        # the mirror of the bug last_error was added to fix.
        self.last_error = None
        self._errors = []
        # None, not 0: if all three boards fail there is no candidate count to report,
        # and 0 would assert three empty boards.
        self.scanned = None
        leads: list[Lead] = []
        leads.extend(self._fetch_remoteok(limit))
        remaining = limit - len(leads)
        if remaining > 0:
            leads.extend(self._fetch_wwr(remaining))
        remaining = limit - len(leads)
        if remaining > 0:
            leads.extend(self._fetch_remotive(remaining))
        return dedupe(leads)[:limit]
