"""Day-rate contract adapter — UK onsite plus remote in the US, EU and ANZ (Adzuna).

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

Why more than the UK
--------------------
Remote contract work is not geographically bounded, and the volume outside the UK is
larger than inside it: the same DevOps/platform query returns 16,223 remote contract
vacancies in the US against 3,687 in the UK (measured 2026-08-05). Ten country
endpoints are queried — see :data:`DEFAULT_REGIONS` — with the UK searched onsite *and*
remote and every other market **remote-only**, because a role requiring relocation is
not a lead and paying an LLM to qualify one is waste.

This source was called ``uk_contract`` until it stopped being UK-only. It was renamed
rather than quietly widened: a source whose name misdescribes its scope is how this
pipeline lost a month.

Why Adzuna specifically
-----------------------
Every keyless UK board (Reed, CWJobs, CV-Library, Jobserve, Technojobs,
findajob.dwp.gov.uk) is behind Cloudflare or blocks datacenter IPs outright —
verified 2026-08-03. Adzuna publishes an official, free-tier JSON API that
aggregates most of those same boards, filters to ``contract`` roles, spans 16
countries on one key, and returns salary bounds. It needs a (free) app id + key.

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
import time
from typing import NamedTuple

import httpx

from config import get_settings
from core.schemas import Lead
from sources._keywords import excluded_title, extract_tags, matches_keywords
from sources.base import LeadSource

logger = logging.getLogger(__name__)

#: ``{country}`` is an Adzuna country code. The endpoint is per-country — there is no
#: global search — so covering four regions means N times the requests, not one query
#: with a country filter.
API_TEMPLATE = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"
USER_AGENT = "ai-freelance-copilot/1.0 (personal lead reader)"
TIMEOUT = 15.0

#: Adzuna's phrase filter for remote work. ``where=remote`` returns **0 results in
#: every country** (measured 2026-08-05) even though it looks like the obvious
#: parameter; ``what_phrase=remote`` intersects correctly with ``what_or`` (22,896 UK
#: hits vs 166,826 for the trades alone). A filter that silently returns 0 is the same
#: trap as ``contract_only`` returning 400 — it just fails quietly instead of loudly.
REMOTE_PHRASE = "remote"

#: Retry budget per request slice. Adzuna's free tier emits sporadic 503s; without a
#: retry those slices are lost *and* the run reports the source ``broken``, which
#: teaches you to ignore the one verdict that means "there is a bug".
RETRIES = 2
RETRY_BACKOFF = 0.5  # seconds, doubled per attempt

#: Statuses worth retrying. Deliberately excludes 400/401/403/404: those mean the
#: request or the credentials are wrong and will fail identically forever, so retrying
#: only delays the report that would get them fixed.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

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


class Region(NamedTuple):
    """One Adzuna country endpoint, and how to search it.

    ``locations`` is per-region because a city name only means anything inside its own
    country: passing "London" to the US endpoint is not an error, it just quietly
    matches London, Ohio.

    ``remote_only`` marks the markets we are not physically in — a Sydney-onsite
    contract is not a lead for a London-based contractor. Those regions carry
    ``locations = ("",)``: the *endpoint* already restricts the country and
    ``what_phrase=remote`` already restricts to remote, so adding a ``where`` on top is
    not just redundant, it is destructive — ``where=Remote`` on the US endpoint returns
    **0** of 4,816 matches, and ``where=Germany`` on the German endpoint likewise
    returns 0 of 104. Measured 2026-08-05: the first live multi-region fetch returned
    nothing at all from the US, the single largest market.
    """

    country: str
    label: str
    locations: tuple[str, ...]
    remote_only: bool


#: The four markets we send proposals into. Counts are live contract vacancies
#: matching the DevOps/platform query, measured 2026-08-05.
#:
#: UK is onsite-or-remote (we are in London); everything else is **remote-only**,
#: because a lead requiring relocation wastes the qualifier's budget and the
#: prospect's time. Ordered by remote contract volume: us 16,223 · gb 3,687 ·
#: au 784 · de 449 · nl 235 · fr 100 · nz 83 · be 33 · at 23 · ch 22.
#:
#: Not included: ie/se/no/dk/fi all return **HTTP 404** — Adzuna has no endpoint for
#: them, so listing them would produce a `broken` verdict every run for a country
#: that simply does not exist in this API. es/mx/pl return 0 contract roles.
#: Remote regions pass no ``where`` at all — see :class:`Region` for why adding one
#: silently zeroes the result set.
DEFAULT_REGIONS: tuple[Region, ...] = (
    Region("gb", "UK", ("London", "UK"), remote_only=False),
    Region("us", "US", ("",), remote_only=True),
    Region("au", "Australia", ("",), remote_only=True),
    Region("de", "Germany", ("",), remote_only=True),
    Region("nl", "Netherlands", ("",), remote_only=True),
    Region("fr", "France", ("",), remote_only=True),
    Region("nz", "New Zealand", ("",), remote_only=True),
    Region("ch", "Switzerland", ("",), remote_only=True),
    Region("at", "Austria", ("",), remote_only=True),
    Region("be", "Belgium", ("",), remote_only=True),
)

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


#: Currency symbol per country endpoint. Adzuna returns salary figures in the local
#: currency with no unit of its own, so the symbol has to come from the endpoint we
#: asked. No conversion is attempted — an approximate rate in a proposal is a wrong
#: number, and a wrong number is worse than no number.
CURRENCY: dict[str, str] = {
    "gb": "£",
    "us": "$",
    "au": "A$",
    "nz": "NZ$",
    "ch": "CHF ",
    "de": "€",
    "nl": "€",
    "fr": "€",
    "at": "€",
    "be": "€",
}


def _format_budget(job: dict, region: Region = DEFAULT_REGIONS[0]) -> str | None:
    """Render Adzuna's salary bounds as a human day/annum string.

    Adzuna normalises contract pay to an *annualised* figure, so a £550/day
    contract surfaces as ~£143k. We keep the raw annualised numbers rather than
    guessing a day rate from them — a wrong rate in a proposal is worse than none.
    """
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if not lo and not hi:
        return None
    # Currency follows the country endpoint. Rendering a US salary with a "£" would put
    # a wrong number in a proposal — worse than omitting the budget, which is why the
    # annualised figure is never converted to a guessed day rate either.
    sym = CURRENCY.get(region.country, "")
    if lo and hi and lo != hi:
        return f"{sym}{int(lo):,}-{sym}{int(hi):,} (annualised)"
    single = lo or hi
    return f"{sym}{int(single):,} (annualised)"


class ContractJobsSource(LeadSource):
    #: NOT ``uk_contract``. The source was renamed when it stopped being UK-only: a
    #: name that lies about scope is how this pipeline lost a month, and the rename was
    #: free because the invalid-filter bug meant no lead ever carried the old name.
    name = "contract_jobs"

    def __init__(
        self,
        queries: tuple[str, ...] = DEFAULT_QUERIES,
        regions: tuple[Region, ...] = DEFAULT_REGIONS,
        base_url: str | None = None,
        max_days_old: int = 14,
        locations: tuple[str, ...] | None = None,
    ) -> None:
        self.queries = queries
        self.regions = regions
        #: Set only by tests pointing at a stub server; None means derive the URL from
        #: each region's country code.
        self.base_url = base_url
        self.max_days_old = max_days_old
        # ``locations=`` is the pre-multi-region signature: it meant "search these UK
        # locations". Honour it as exactly that — UK only — rather than layering it
        # over ten countries, so a caller that passes two locations still gets two
        # requests per query and not twenty.
        if locations is not None:
            self.regions = (Region("gb", "UK", locations, remote_only=False),)
        #: Last transport/HTTP failure, or None if every request succeeded. Read by
        #: the funnel report so "the API rejected us" is not reported as "no jobs
        #: matched": a swallowed 400 and a genuinely empty market both returned []
        #: and both surfaced as ``dead: fetched nothing``, which names the wrong
        #: lever — one needs a code fix, the other needs different queries.
        self.last_error: str | None = None
        #: Listings read across every (region, location, query) slice, before
        #: ``matches_keywords`` and ``excluded_title``. See ``LeadSource.scanned``.
        #: Adzuna's relevance ranking is loose — a "devops" query returns sales roles at
        #: devops companies — so this source is expected to scan far more than it keeps,
        #: and that is not the same event as the API rejecting the request.
        self.scanned: int | None = None

    def _job_to_lead(self, job: dict, region: Region = DEFAULT_REGIONS[0]) -> Lead | None:
        job_id = job.get("id")
        title = str(job.get("title") or "")
        desc = _strip_html(str(job.get("description") or ""))
        if not job_id or not title:
            return None
        # Keyword gate: Adzuna's relevance ranking is loose, so a "devops" query
        # still returns e.g. sales roles at devops companies.
        if not matches_keywords(title, desc):
            return None
        # ...and the keyword gate alone is not enough, because it searches the whole
        # description: "Marketing Manager" qualified on the word "agentic" in its body.
        # The title is what decides whether this is an engineering role at all.
        excluded = excluded_title(title)
        if excluded:
            logger.debug(
                "contract_jobs: dropping %r — excluded title word %r", title, excluded
            )
            return None
        company = ((job.get("company") or {}) or {}).get("display_name")
        location = ((job.get("location") or {}) or {}).get("display_name")
        return Lead(
            source=self.name,
            # Namespaced by country: Adzuna ids are unique per *country* endpoint, not
            # globally, so a bare id lets a German role collide with a British one and
            # be dropped as a duplicate — silently, since dedupe logs nothing.
            external_id=f"{region.country}:{job_id}",
            title=f"{company}: {title}" if company else title,
            description=desc,
            url=str(job.get("redirect_url") or ""),
            company=company or None,
            budget=_format_budget(job, region),
            posted_at=job.get("created") or None,
            # The region label is a tag so the qualifier and the proposal writer can see
            # which market a lead is in; "remote, Germany" changes how you pitch.
            tags=extract_tags(title, desc)
            + ([location] if location else [])
            + [region.label],
            raw=job,
        )

    def _url_for(self, region: Region) -> str:
        return self.base_url or API_TEMPLATE.format(country=region.country)

    def _get(
        self,
        client: httpx.Client,
        url: str,
        params: dict,
        region: Region,
        query: str,
        where: str,
    ) -> dict | list | None:
        """GET with bounded retry on *transient* failures. None means give up.

        Adzuna returns sporadic 503s under the free tier — a live fetch on 2026-08-05
        got three in a row on UK queries while every other country succeeded. Without
        retry those slices are simply lost, and worse, they set ``last_error`` and the
        run reports the source ``broken`` when nothing is wrong with it. A diagnostic
        that cries wolf gets ignored, which is how the real 400 survived a month.

        Only transient statuses are retried. A 400 or 404 means the request itself is
        wrong (invalid filter, no such country) and will fail identically forever —
        retrying it wastes the run's time and delays the report that would fix it.
        """
        last: Exception | None = None
        for attempt in range(RETRIES + 1):
            try:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                last = exc
                if exc.response is None or exc.response.status_code not in RETRY_STATUS:
                    break
            except (httpx.TransportError, ValueError) as exc:
                # TransportError covers timeouts/connection resets; ValueError covers a
                # truncated body that fails to parse as JSON. Both are worth one retry.
                last = exc
            except Exception as exc:  # pragma: no cover - defensive
                last = exc
                break
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF * (2**attempt))

        logger.warning(
            "contract_jobs: fetch failed for %s %r/%r after %d attempt(s): %s",
            region.country, query, where, RETRIES + 1, last,
        )
        self.last_error = f"{type(last).__name__}: {last}"
        return None

    def _fetch_slice(
        self,
        client: httpx.Client,
        query: str,
        where: str,
        limit: int,
        region: Region = DEFAULT_REGIONS[0],
    ) -> list[Lead]:
        app_id, app_key = _credentials()
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "what_or": query,
            "where": where,
            "results_per_page": min(50, max(1, limit)),
            # Day-rate contracts only, not permanent roles. The parameter is
            # ``contract``, NOT ``contract_only``: Adzuna answers an unknown filter
            # name with a 400 HTML error page, so every single request this source
            # ever made failed. The unit test asserting ``contract_only == 1`` passed
            # anyway, because the mocked client accepts any parameter name a real
            # server would reject — a test that pinned the bug it existed to prevent.
            "contract": 1,
            "max_days_old": self.max_days_old,
            "sort_by": "date",
            "content-type": "application/json",
        }
        if region.remote_only:
            # Outside the UK only remote work is a real lead. See REMOTE_PHRASE for why
            # this is not ``where=remote``.
            params["what_phrase"] = REMOTE_PHRASE
        if not where:
            # Omit rather than send blank: the endpoint already scopes the country and
            # any ``where`` on top of the remote phrase zeroes the result set.
            params.pop("where", None)
        payload = self._get(client, self._url_for(region), params, region, query, where)
        if payload is None:
            return []

        results = payload.get("results", []) if isinstance(payload, dict) else []
        rows = results if isinstance(results, list) else []
        # Accumulated per slice, and only past the ``payload is None`` return above, so
        # rejected requests stay attributable to last_error rather than looking like an
        # empty market.
        self.scanned = (self.scanned or 0) + sum(1 for j in rows if isinstance(j, dict))
        leads: list[Lead] = []
        for job in rows:
            if len(leads) >= limit:
                break
            if not isinstance(job, dict):
                continue
            try:
                lead = self._job_to_lead(job, region)
            except Exception as exc:
                logger.warning("contract_jobs: bad job: %s", exc)
                continue
            if lead is not None:
                leads.append(lead)
        return leads

    def _slices(self) -> list[tuple[Region, str, str]]:
        """Every (region, location, query) to request, ordered round-robin by region.

        Order is the whole point. Nesting ``for region: for query:`` and stopping at
        ``limit`` would spend the entire budget on the first region — with 10 regions
        and a limit of 50, the UK's five queries return 50 leads and the other nine
        countries are never requested at all, every run, invisibly. That is the same
        prefix-vs-sample bug the run cap had (see ``_interleave_by_source`` in
        pipeline.py); it is cheaper to not write it twice than to diagnose it twice.

        Interleaving by region means the limit *samples* the four markets. UK slices
        still come first within each round, because that is the market we can also work
        onsite in.
        """
        per_region = [
            [(region, where, query) for where in region.locations for query in self.queries]
            for region in self.regions
        ]
        out: list[tuple[Region, str, str]] = []
        for i in range(max((len(s) for s in per_region), default=0)):
            for slices in per_region:
                if i < len(slices):
                    out.append(slices[i])
        return out

    def fetch(self, limit: int = 50) -> list[Lead]:
        self.last_error = None  # per-fetch, so a fixed source stops reporting stale errors
        # Stays None when unconfigured or when every request fails: 0 would report an
        # empty market for a source that never got to ask.
        self.scanned = None
        app_id, app_key = _credentials()
        if not app_id or not app_key:
            self.last_error = "not configured: no Adzuna app id/key"
            logger.warning(
                "contract_jobs: DISABLED — set COPILOT_ADZUNA_APP_ID and "
                "COPILOT_ADZUNA_APP_KEY (free key: https://developer.adzuna.com). "
                "This is the primary day-rate contract source; without it the "
                "pipeline only sees salaried/remote-board listings."
            )
            return []

        leads: list[Lead] = []
        seen: set[str] = set()
        try:
            with httpx.Client(
                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
            ) as client:
                for region, where, query in self._slices():
                    if len(leads) >= limit:
                        return leads[:limit]
                    for lead in self._fetch_slice(
                        client, query, where, limit - len(leads), region
                    ):
                        # Adzuna ids are only unique *within* a country, so the key is
                        # (country, id) — a bare id would silently drop a German role
                        # that happened to share a number with a British one.
                        if lead.external_id in seen:
                            continue
                        seen.add(lead.external_id)
                        leads.append(lead)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("contract_jobs: client error: %s", exc)
            self.last_error = f"{type(exc).__name__}: {exc}"
        return leads[:limit]
