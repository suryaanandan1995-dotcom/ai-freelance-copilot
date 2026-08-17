"""Reddit r/forhire (and r/jobbit) "[Hiring]" adapter.

Both subreddits carry two kinds of posts, flagged in the title/flair:

* ``[Hiring]``   — a client looking to hire someone (these are the leads).
* ``[For Hire]`` — a freelancer advertising availability (NOT a lead).
* ``[Task]``     — a one-off micro task (NOT a lead).

We only want the ``[Hiring]`` side, because those are actual clients — and they
frequently include a direct contact email in the post body, which makes them
high-value for auto-email outreach. Kept posts must also carry a genuine
DevSecOps keyword.

Why this source is worth authenticating for
-------------------------------------------
Measured over 6 production runs (2026-08-10..17): 269 leads cleared the fit bar
and 196 carried an email address, but only **7 were both**. 181 of those 196
addresses came from ``hn_hiring`` — full-time employment posts, median fit 28 —
while the 231 qualified leads from the job boards carry no address at all. The
funnel's blocker is not scoring and not volume, it is that overlap. r/forhire
``[Hiring]`` posts sit exactly in it: a *client*, posting *contract* work, with a
*contact address in the body*.

Auth: app-only OAuth ("client credentials")
-------------------------------------------
The adapter was disabled in the registry on 2026-08-03 because unauthenticated
``www.reddit.com/*.json`` returned **403 on 48/48 fetches** — Reddit blocks
datacenter IPs, which is where this runs. App-only OAuth fixes that and costs
nothing (a free "script" app at reddit.com/prefs/apps → ``COPILOT_REDDIT_CLIENT_ID``
/ ``COPILOT_REDDIT_CLIENT_SECRET``).

Two rules the auth path follows, both of them lessons paid for elsewhere:

* **One token per run, not per endpoint.** An app-only token lives ~24h and both
  subreddits are read in the same run, so a token per endpoint doubles the token
  traffic for nothing and invites Reddit's own rate limiter.
* **One refresh per run on a 401, then stop.** A 401 after a successful token
  means expired/revoked; a *second* 401 means the credentials are wrong, and
  retrying a wrong credential N times is one bug billed N times.

The unauthenticated path is kept as a fallback for an unconfigured install, so
nothing changes without configuration — but it logs that it is the path measured
at 403 48/48, because a silent fallback to a known-refused endpoint reads as
"Reddit has no hiring posts this week".

Reddit rejects requests without a descriptive User-Agent (429), so one is always
set, on the token call as well as the reads. Network/parse failures are tolerated
per-endpoint — returns [] or partial, never raises.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime

import httpx

from config import get_settings
from core.schemas import Lead
from sources._keywords import extract_tags, matches_keywords
from sources.base import LeadSource

logger = logging.getLogger(__name__)

#: Subreddits read, in order. Kept as the unauthenticated URLs for back-compat:
#: the authenticated host is derived from them (see :func:`_subreddit_of`), so
#: there is one endpoint list rather than two that can drift apart.
REDDIT_ENDPOINTS = (
    "https://www.reddit.com/r/forhire/new.json?limit=100",
    "https://www.reddit.com/r/jobbit/new.json?limit=100",
)
# Reddit requires a descriptive, non-generic User-Agent or it 429s.
USER_AGENT = "ai-freelance-copilot/1.0 (personal lead reader)"
TIMEOUT = 10.0
REDDIT_BASE = "https://www.reddit.com"
#: The token endpoint lives on www, not on the oauth host.
TOKEN_URL = f"{REDDIT_BASE}/api/v1/access_token"
#: Authenticated reads. Note there is no ``.json`` suffix here — the oauth host
#: serves JSON by default — but the payload shape is byte-identical to the public
#: listing, so the parsing below is shared.
OAUTH_BASE = "https://oauth.reddit.com"
#: Reddit's listing maximum. Asking for less just means fewer candidates per run.
LISTING_LIMIT = 100
#: Seconds shaved off ``expires_in`` before the cached token is considered stale.
#: An app-only token lasts ~86400s, so this is free insurance against a token that
#: expires between the two subreddit reads.
TOKEN_LEEWAY = 60.0

# "[Hiring]" appears in the title and/or the link_flair_text.
_HIRING_RE = re.compile(r"\[\s*hiring\s*\]", re.IGNORECASE)
# Freelancer-availability / micro-task markers we must exclude.
_EXCLUDE_RE = re.compile(r"\[\s*for\s*hire\s*\]|\[\s*task\s*\]", re.IGNORECASE)
# Pulls "forhire" out of ".../r/forhire/new.json?limit=100" so the authenticated
# URL can be built from the same endpoint tuple.
_SUBREDDIT_RE = re.compile(r"/r/([A-Za-z0-9_]+)/")


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


def _subreddit_of(url: str) -> str | None:
    """The subreddit name in a listing URL, or None if it isn't a subreddit URL."""
    match = _SUBREDDIT_RE.search(url or "")
    return match.group(1) if match else None


class RedditForHireSource(LeadSource):
    name = "reddit_forhire"

    def __init__(
        self,
        endpoints: tuple[str, ...] = REDDIT_ENDPOINTS,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.endpoints = endpoints
        #: Injection seam for tests (``httpx.MockTransport``); None in production.
        self._transport = transport
        #: App-only token cached IN-PROCESS with its expiry, so one run's two
        #: subreddit reads share a single token call.
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        #: Latched per fetch: at most one token refresh per run on a 401. Without the
        #: latch each endpoint would refresh on its own and a wrong credential would
        #: cost 2 token calls + 4 reads instead of failing once.
        self._refreshed = False
        #: Posts read across both subreddits, before the ``[Hiring]``/keyword filters.
        #: See ``LeadSource.scanned``. Stays None on every failure path: 0 asserts "we
        #: read an empty listing", and that is precisely the lie the 403 era told the
        #: funnel report — a source that never got a payload must not claim one.
        self.scanned: int | None = None
        #: Why the last fetch produced nothing, or None. Every other adapter reports this
        #: and this one did not, which matters most here: the two ways this source fails
        #: are a missing app and a refused token, both fixed in one place in about two
        #: minutes — and without ``last_error`` the funnel report calls either one
        #: "dead: fetched nothing" and advises retiring the feed. That is the exact
        #: wrong-lever defect ``last_error`` was added elsewhere to end, and it is how
        #: this adapter spent a month looking like an empty market rather than a 403.
        self.last_error: str | None = None

    def _note_error(self, detail: str) -> None:
        """Record a failure reason, keeping the first (the cause, not its consequence).

        A token refusal makes every later read fail too; overwriting would leave the
        report describing the symptom furthest from the fix.
        """
        if self.last_error is None:
            self.last_error = detail

    # --- credentials / token ----------------------------------------------
    def _credentials(self) -> tuple[str, str]:
        """Read the Reddit app id + secret.

        Via ``Settings``, not ``os.environ``: pydantic-settings loads ``.env`` into
        the settings object and never into the process environment, so reading
        os.environ here would silently ignore keys set in ``.env`` and report the
        source unconfigured — "you never set it" rather than "your config is being
        ignored".
        """
        settings = get_settings()
        return (
            (settings.reddit_client_id or "").strip(),
            (settings.reddit_client_secret or "").strip(),
        )

    def _token_for(self, client: httpx.Client) -> str | None:
        """Cached app-only bearer token, fetching one if the cache is cold/stale.

        Never raises: a refused token endpoint logs a warning and returns None, which
        the caller turns into an empty run.
        """
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        client_id, client_secret = self._credentials()
        if not (client_id and client_secret):
            return None
        try:
            resp = client.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                # HTTP Basic: the app id is the user, the secret is the password.
                auth=(client_id, client_secret),
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning("reddit_forhire: app-only token request failed: %s", exc)
            return None
        token = ""
        expires_in = 0.0
        if isinstance(payload, dict):
            token = str(payload.get("access_token") or "")
            try:
                expires_in = float(payload.get("expires_in") or 0.0)
            except (TypeError, ValueError):
                expires_in = 0.0
        if not token:
            logger.warning("reddit_forhire: token response carried no access_token")
            return None
        self._token = token
        # max(..., 0) so a short/absent expires_in degrades to "use once" rather than
        # to a cached token that is already stale on arrival.
        self._token_expires_at = time.monotonic() + max(expires_in - TOKEN_LEEWAY, 0.0)
        logger.info(
            "reddit_forhire: app-only token acquired, valid ~%ds", int(expires_in)
        )
        return token

    # --- HTTP -------------------------------------------------------------
    def _client(self) -> httpx.Client:
        return httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
            transport=self._transport,
        )

    def _authed_url(self, endpoint: str) -> str:
        """The oauth.reddit.com equivalent of a public listing URL."""
        subreddit = _subreddit_of(endpoint)
        if not subreddit:
            # Unparseable endpoint (a test double, say): send it as given rather than
            # guessing a subreddit. A wrong URL with a valid Bearer is a 404 we'd have
            # to debug; the original URL at least fails the way it always did.
            logger.warning("reddit_forhire: no subreddit in %s", endpoint)
            return endpoint
        return f"{OAUTH_BASE}/r/{subreddit}/new?limit={LISTING_LIMIT}"

    def _read_listing(
        self, client: httpx.Client, endpoint: str, *, authed: bool
    ) -> object | None:
        """Fetch one listing's JSON payload, or None on any failure.

        On the authenticated path a 401 buys exactly one token refresh per *run*
        (see ``self._refreshed``) and one retry. Anything else is logged and skipped:
        one refused subreddit must not lose the other one.
        """
        url = endpoint
        headers: dict[str, str] = {}
        if authed:
            token = self._token_for(client)
            if not token:
                return None
            url = self._authed_url(endpoint)
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = client.get(url, headers=headers)
            if resp.status_code == 401 and authed and not self._refreshed:
                # An app-only token lives ~24h, so a 401 means expired or revoked.
                # Latch first: the retry below must not be able to refresh again.
                self._refreshed = True
                self._token = None
                self._token_expires_at = 0.0
                logger.info(
                    "reddit_forhire: 401 on %s, refreshing the app-only token once", url
                )
                if not self._token_for(client):
                    return None
                return self._read_listing(client, endpoint, authed=True)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            # Named per subreddit: a 403 on r/forhire while r/jobbit answers is a
            # different problem from both refusing, and only the detail says which.
            sub = _subreddit_of(endpoint) or url
            self._note_error(f"{sub}: {type(exc).__name__}: {exc}")
            logger.warning("reddit_forhire: fetch failed for %s: %s", url, exc)
            return None

    def _fetch_endpoint(
        self, client: httpx.Client, endpoint: str, limit: int, *, authed: bool
    ) -> list[Lead]:
        leads: list[Lead] = []
        payload = self._read_listing(client, endpoint, authed=authed)
        if payload is None:
            return leads

        children = (
            payload.get("data", {}).get("children", [])
            if isinstance(payload, dict)
            else []
        )
        if not isinstance(children, list):
            return leads

        # Candidates this subreddit handed us, before the [Hiring]/keyword filters.
        # Accumulated (not overwritten) so one subreddit's number can't stand in for
        # both, and promoting None -> 0 only here means "we got a payload".
        self.scanned = (self.scanned or 0) + sum(
            1
            for child in children
            if isinstance(child, dict) and isinstance(child.get("data"), dict)
        )

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
            # Always the www permalink, even when the listing came from the oauth
            # host: this URL is shown to a human and mailed out.
            url=f"{REDDIT_BASE}{permalink}" if permalink else "",
            company=data.get("author") or None,
            posted_at=_iso_from_utc(data.get("created_utc")),
            tags=extract_tags(title, selftext),
            raw=data,
        )

    def fetch(self, limit: int = 50) -> list[Lead]:
        leads: list[Lead] = []
        seen: set[str] = set()
        # Per-fetch resets. scanned stays None unless a payload actually arrives; the
        # refresh latch is per run, so a token that expires tomorrow can still be
        # refreshed tomorrow.
        self.scanned = None
        self._refreshed = False
        # Cleared per fetch for the same reason ``scanned`` is: a stale error describes
        # the previous run, and a source that recovered must stop reporting broken.
        self.last_error = None

        client_id, client_secret = self._credentials()
        authed = bool(client_id and client_secret)
        if not authed:
            self._note_error(
                "unauthenticated: set COPILOT_REDDIT_CLIENT_ID and "
                "COPILOT_REDDIT_CLIENT_SECRET (free script app at reddit.com/prefs/apps)"
                " — the public .json path measured 403 on 48/48 fetches"
            )
            logger.warning(
                "reddit_forhire: no COPILOT_REDDIT_CLIENT_ID/COPILOT_REDDIT_CLIENT_SECRET"
                " — falling back to the unauthenticated .json path, which measured 403 on"
                " 48/48 production fetches (Reddit blocks datacenter IPs). A free"
                " 'script' app at reddit.com/prefs/apps unblocks this source."
            )

        try:
            with self._client() as client:
                if authed and self._token_for(client) is None:
                    # Credentials are configured but the token endpoint refused. Do NOT
                    # fall through to the public .json URLs: that path is measured at
                    # 403 48/48, so it would buy one guaranteed refusal per subreddit
                    # and report the credential problem as "no hiring posts".
                    self._note_error(
                        "app-only OAuth token refused — check the client id/secret and"
                        " that the app type is 'script' at reddit.com/prefs/apps"
                    )
                    logger.warning(
                        "reddit_forhire: no app-only token this run; returning no leads"
                        " rather than spending a fetch on the 403 path"
                    )
                    return []
                for endpoint in self.endpoints:
                    if len(leads) >= limit:
                        break
                    for lead in self._fetch_endpoint(
                        client, endpoint, limit - len(leads), authed=authed
                    ):
                        if lead.external_id in seen:
                            continue
                        seen.add(lead.external_id)
                        leads.append(lead)
                        if len(leads) >= limit:
                            break
        except Exception as exc:  # pragma: no cover - client construction only
            self._note_error(f"{type(exc).__name__}: {exc}")
            logger.warning("reddit_forhire: client error: %s", exc)
        return leads[:limit]
