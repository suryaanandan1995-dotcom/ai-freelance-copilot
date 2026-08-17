"""Offline tests for the Reddit r/forhire "[Hiring]" adapter.

Every test is hermetic: an ``httpx.MockTransport`` is injected into the adapter and
serves both the OAuth token endpoint and the listing endpoints from an inline
Reddit-shaped fixture. No socket is opened.

Two behaviours are covered:

* Filtering — a ``[Hiring]`` DevSecOps post with an email becomes a Lead (with the
  email recoverable for auto-outreach), while ``[For Hire]``, ``[Task]`` and
  off-topic posts are excluded, and network errors yield [].
* App-only OAuth — the token is fetched once per run and reused across both
  subreddits, reads go to oauth.reddit.com with a Bearer header, a 401 buys exactly
  one refresh (never a loop), a refused token endpoint degrades to no leads, and an
  unconfigured install still uses the old public ``.json`` URLs.
"""
from __future__ import annotations

import base64

import httpx
import pytest

from core.schemas import Lead
from outreach.extract import find_contact_email
from sources.reddit_forhire import RedditForHireSource
from sources.registry import get_default_sources

FORHIRE = "https://www.reddit.com/r/forhire/new.json?limit=100"
JOBBIT = "https://www.reddit.com/r/jobbit/new.json?limit=100"


def _child(**data):
    return {"kind": "t3", "data": data}


# A realistic listing: one [Hiring] DevSecOps post with a contact email, one
# [For Hire] post, one [Task] micro-job, and one [Hiring] but off-topic post.
LISTING = {
    "kind": "Listing",
    "data": {
        "children": [
            _child(
                id="abc123",
                title="[Hiring] Freelance Kubernetes + Terraform engineer",
                link_flair_text="Hiring",
                selftext=(
                    "We need help hardening our EKS cluster and CI/CD. "
                    "Remote, long term. Email us at hiring@acmecloud.io"
                ),
                permalink="/r/forhire/comments/abc123/hiring_k8s/",
                author="acmeclient",
                created_utc=1719763200.0,
            ),
            _child(
                id="def456",
                title="[For Hire] Senior DevOps / SRE, AWS + Kubernetes",
                link_flair_text="For Hire",
                selftext="Available for contracts. Reach me at me@dev.io",
                permalink="/r/forhire/comments/def456/forhire_devops/",
                author="somefreelancer",
                created_utc=1719763300.0,
            ),
            _child(
                # A micro task: carries every DevSecOps keyword and is still not a
                # lead. $40 of Terraform is not a contract, and the exclusion has to
                # survive the authenticated path, not just the public one.
                id="jkl012",
                title="[Task] Fix one broken Terraform state file, $40",
                link_flair_text="Task",
                selftext="Quick job on my AWS EKS setup. paypal only, dm me",
                permalink="/r/forhire/comments/jkl012/task_terraform/",
                author="taskposter",
                created_utc=1719763350.0,
            ),
            _child(
                id="ghi789",
                title="[Hiring] Logo designer for my bakery",
                link_flair_text="Hiring",
                selftext="Need a nice logo, will pay. contact@bakery.example",
                permalink="/r/forhire/comments/ghi789/hiring_logo/",
                author="bakerclient",
                created_utc=1719763400.0,
            ),
        ]
    },
}

#: Candidate posts per subreddit response, i.e. what ``scanned`` must report per
#: listing read. Derived, so adding a fixture post can't silently break the count.
CHILDREN = len(LISTING["data"]["children"])


class RedditServer:
    """Canned reddit.com + oauth.reddit.com, recording every request it serves.

    Attributes are mutable mid-test so a run can be made to succeed and then fail
    (that is how the ``scanned`` reset is proved).
    """

    def __init__(
        self,
        *,
        listing=LISTING,
        token_status: int = 200,
        listing_status: int = 200,
        unauthorized: int = 0,
        connect_error: bool = False,
    ) -> None:
        self.listing = listing
        self.token_status = token_status
        self.listing_status = listing_status
        #: How many listing reads to answer with 401 before serving normally.
        self.unauthorized = unauthorized
        self.connect_error = connect_error
        self.token_requests: list[httpx.Request] = []
        self.listing_requests: list[httpx.Request] = []

    @property
    def token_calls(self) -> int:
        return len(self.token_requests)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        # Reddit 429s a generic User-Agent, on the token call as much as the reads.
        assert "ai-freelance-copilot" in request.headers.get("user-agent", "")

        if request.url.path == "/api/v1/access_token":
            self.token_requests.append(request)
            if self.token_status >= 400:
                return httpx.Response(self.token_status, json={"error": "server_error"})
            return httpx.Response(
                200,
                json={
                    # Numbered, so a test can tell a reused token from a refreshed one.
                    "access_token": f"tok-{self.token_calls}",
                    "expires_in": 86400,
                    "token_type": "bearer",
                },
            )

        if self.connect_error:
            raise httpx.ConnectError("down")
        self.listing_requests.append(request)
        if self.unauthorized > 0:
            self.unauthorized -= 1
            return httpx.Response(401, json={"message": "Unauthorized", "error": 401})
        if self.listing_status >= 400:
            return httpx.Response(self.listing_status, json={})
        return httpx.Response(200, json=self.listing)


def _source(server: RedditServer, endpoints=None) -> RedditForHireSource:
    kwargs = {"transport": httpx.MockTransport(server)}
    if endpoints is not None:
        kwargs["endpoints"] = endpoints
    return RedditForHireSource(**kwargs)


def _with_credentials(monkeypatch) -> None:
    monkeypatch.setenv("COPILOT_REDDIT_CLIENT_ID", "test-app-id")
    monkeypatch.setenv("COPILOT_REDDIT_CLIENT_SECRET", "test-app-secret")


def _without_credentials(monkeypatch) -> None:
    # Pinned rather than assumed: conftest does not clear these, so a contributor's
    # .env would otherwise decide which code path the "unauthenticated" tests take.
    monkeypatch.setenv("COPILOT_REDDIT_CLIENT_ID", "")
    monkeypatch.setenv("COPILOT_REDDIT_CLIENT_SECRET", "")


@pytest.fixture(autouse=True)
def _no_credentials_by_default(monkeypatch):
    """Default every test to the unauthenticated path; auth tests opt in."""
    _without_credentials(monkeypatch)


# --------------------------------------------------------------------------
# Filtering (unauthenticated path)
# --------------------------------------------------------------------------
def test_keeps_hiring_devsecops_post():
    server = RedditServer()
    # Single endpoint so the fixture isn't served twice.
    leads = _source(server, endpoints=(FORHIRE,)).fetch(limit=10)

    assert len(leads) == 1
    lead = leads[0]
    assert isinstance(lead, Lead)
    assert lead.source == "reddit_forhire"
    assert lead.external_id == "abc123"
    assert lead.url == "https://www.reddit.com/r/forhire/comments/abc123/hiring_k8s/"
    assert lead.company == "acmeclient"
    assert "kubernetes" in lead.tags
    assert lead.posted_at is not None and lead.posted_at.startswith("2024-")


def test_hiring_email_recoverable_for_outreach():
    lead = _source(RedditServer(), endpoints=(FORHIRE,)).fetch(limit=10)[0]
    assert find_contact_email(lead) == "hiring@acmecloud.io"


def test_excludes_for_hire_task_and_offtopic():
    ids = {
        lead.external_id
        for lead in _source(RedditServer(), endpoints=(FORHIRE,)).fetch(limit=10)
    }
    # [For Hire] freelancer post, [Task] micro-job and off-topic bakery [Hiring].
    assert "def456" not in ids
    assert "jkl012" not in ids
    assert "ghi789" not in ids


def test_network_error_returns_empty():
    assert _source(RedditServer(connect_error=True)).fetch() == []


def test_dedupes_across_endpoints():
    # Both default endpoints serve the same fixture; the [Hiring] post must appear
    # only once.
    leads = _source(RedditServer()).fetch(limit=10)
    assert [lead.external_id for lead in leads] == ["abc123"]


def test_respects_limit():
    assert len(_source(RedditServer(), endpoints=(FORHIRE,)).fetch(limit=0)) == 0


def test_without_credentials_uses_the_public_json_urls():
    """No creds must change nothing about today's behaviour — 403 path included.

    The fallback is the path measured at 403 on 48/48 production fetches, so it is
    only ever taken by an install that has not created the free "script" app. It must
    not ask for a token it has no credentials for, and it must not send a Bearer.
    """
    server = RedditServer()
    _source(server).fetch(limit=10)

    assert server.token_calls == 0
    assert [str(r.url) for r in server.listing_requests] == [FORHIRE, JOBBIT]
    assert all("authorization" not in r.headers for r in server.listing_requests)


def test_public_json_403_yields_no_leads_and_no_scanned_claim():
    """The 2026-08-03 production reality, pinned: 403 is not "an empty subreddit"."""
    server = RedditServer(listing_status=403)
    source = _source(server)

    assert source.fetch(limit=10) == []
    assert source.scanned is None


# --------------------------------------------------------------------------
# App-only OAuth
# --------------------------------------------------------------------------
def test_token_is_requested_once_and_reused_across_both_subreddits(monkeypatch):
    """One token per run, not one per endpoint.

    An app-only token lives ~24h and both subreddits are read in the same run, so a
    token call per endpoint is pure duplicate traffic against Reddit's rate limiter.
    """
    _with_credentials(monkeypatch)
    server = RedditServer()
    leads = _source(server).fetch(limit=10)

    assert server.token_calls == 1
    assert len(server.listing_requests) == 2  # r/forhire + r/jobbit
    bearers = {r.headers.get("authorization") for r in server.listing_requests}
    assert bearers == {"Bearer tok-1"}
    assert [lead.external_id for lead in leads] == ["abc123"]


def test_token_request_is_basic_auth_client_credentials(monkeypatch):
    _with_credentials(monkeypatch)
    server = RedditServer()
    _source(server, endpoints=(FORHIRE,)).fetch(limit=10)

    request = server.token_requests[0]
    assert str(request.url) == "https://www.reddit.com/api/v1/access_token"
    assert request.method == "POST"
    scheme, _, encoded = request.headers["authorization"].partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode() == "test-app-id:test-app-secret"
    assert b"grant_type=client_credentials" in request.content


def test_authenticated_reads_use_the_oauth_host_and_bearer(monkeypatch):
    """oauth.reddit.com, no ``.json`` suffix, same JSON shape parsed."""
    _with_credentials(monkeypatch)
    server = RedditServer()
    leads = _source(server).fetch(limit=10)

    urls = [str(r.url) for r in server.listing_requests]
    assert urls == [
        "https://oauth.reddit.com/r/forhire/new?limit=100",
        "https://oauth.reddit.com/r/jobbit/new?limit=100",
    ]
    assert all(".json" not in url for url in urls)
    # The lead's URL is still the www permalink: it gets shown to a human and mailed.
    assert leads[0].url.startswith("https://www.reddit.com/r/forhire/comments/")


def test_expired_token_triggers_exactly_one_refresh(monkeypatch):
    _with_credentials(monkeypatch)
    server = RedditServer(unauthorized=1)  # first read 401s, then all is well
    source = _source(server)
    leads = source.fetch(limit=10)

    assert server.token_calls == 2  # initial + one refresh
    # r/forhire (401), r/forhire retried, r/jobbit.
    assert len(server.listing_requests) == 3
    assert server.listing_requests[-1].headers["authorization"] == "Bearer tok-2"
    assert [lead.external_id for lead in leads] == ["abc123"]
    assert source.scanned == CHILDREN * 2


def test_persistent_401_refreshes_once_and_does_not_loop(monkeypatch):
    """A wrong credential must fail once, not once per endpoint per attempt.

    A second 401 after a fresh token means the credentials are bad, not stale, so a
    retry cannot help: retrying a broken auth N times is one bug billed N times. The
    refresh latch is per run, which is why the second subreddit does not buy its own.
    """
    _with_credentials(monkeypatch)
    server = RedditServer(unauthorized=99)
    source = _source(server)

    assert source.fetch(limit=10) == []
    assert server.token_calls == 2  # initial + exactly one refresh, ever
    # r/forhire, its single retry, r/jobbit — and then it stops.
    assert len(server.listing_requests) == 3
    assert source.scanned is None


def test_token_endpoint_failure_returns_no_leads_without_raising(monkeypatch):
    """A 500 from the token endpoint degrades; it does not raise or fetch.

    The unauthenticated fallback is NOT taken here: it measured 403 on 48/48, so
    spending two refused fetches would only relabel a credential problem as "no
    hiring posts this week".
    """
    _with_credentials(monkeypatch)
    server = RedditServer(token_status=500)
    source = _source(server)

    assert source.fetch(limit=10) == []
    assert server.token_calls == 1
    assert server.listing_requests == []
    assert source.scanned is None


def test_token_response_without_access_token_returns_no_leads(monkeypatch):
    _with_credentials(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/access_token"
        return httpx.Response(200, json={"expires_in": 86400})

    source = RedditForHireSource(transport=httpx.MockTransport(handler))
    assert source.fetch(limit=10) == []
    assert source.scanned is None


def test_excludes_for_hire_and_task_on_the_authenticated_path(monkeypatch):
    """The side filters are shared, and this pins that they stay shared.

    The whole point of authenticating is that these listings finally arrive, so a
    filter that only ran on the dead public path would be worse than useless.
    """
    _with_credentials(monkeypatch)
    source = _source(RedditServer())
    ids = {lead.external_id for lead in source.fetch(limit=50)}

    assert ids == {"abc123"}
    assert source.scanned == CHILDREN * 2  # every excluded post was seen and rejected


# --------------------------------------------------------------------------
# scanned: "we read an empty listing" vs "we never got a payload"
# --------------------------------------------------------------------------
def test_scanned_is_none_when_every_subreddit_fails(monkeypatch):
    _with_credentials(monkeypatch)
    source = _source(RedditServer(connect_error=True))

    assert source.fetch(limit=10) == []
    # None, not 0: 0 claims two empty listings, which is what the 403 era reported
    # to the funnel digest and it named the wrong lever.
    assert source.scanned is None


def test_scanned_accumulates_across_subreddits(monkeypatch):
    _with_credentials(monkeypatch)
    source = _source(RedditServer())
    source.fetch(limit=10)
    assert source.scanned == CHILDREN * 2

    single = _source(RedditServer(), endpoints=(FORHIRE,))
    single.fetch(limit=10)
    assert single.scanned == CHILDREN


def test_scanned_is_zero_for_a_genuinely_empty_listing(monkeypatch):
    _with_credentials(monkeypatch)
    empty = {"kind": "Listing", "data": {"children": []}}
    source = _source(RedditServer(listing=empty), endpoints=(FORHIRE,))

    assert source.fetch(limit=10) == []
    assert source.scanned == 0  # a payload arrived and it was empty — a real 0


def test_scanned_resets_between_fetches(monkeypatch):
    """A run that reaches nothing must not inherit the previous run's count."""
    _with_credentials(monkeypatch)
    server = RedditServer()
    source = _source(server)

    source.fetch(limit=10)
    assert source.scanned == CHILDREN * 2

    server.connect_error = True
    assert source.fetch(limit=10) == []
    assert source.scanned is None


# --------------------------------------------------------------------------
# Registry wiring
# --------------------------------------------------------------------------
def test_registry_enables_reddit_forhire_only_with_oauth_credentials(monkeypatch):
    """Conditional, not permanent: OAuth credentials are the whole switch.

    Measured 2026-08-03: 403 Blocked on 48/48 production fetches — Reddit blocks
    unauthenticated ``.json`` from datacenter IPs, which is where CI runs, so keeping
    it in the registry cost a fetch every run for zero leads. A free "script" app at
    reddit.com/prefs/apps supplies the two values that turn it back on.
    """
    _without_credentials(monkeypatch)
    assert "reddit_forhire" not in {s.name for s in get_default_sources()}

    _with_credentials(monkeypatch)
    names = [s.name for s in get_default_sources()]
    assert "reddit_forhire" in names
    # Second, behind hn_hiring: this is the highest-yield *contactable* source we
    # have (client + contract + address in the body), and per_source ordering decides
    # who fills max_leads_per_run.
    assert names[:2] == ["hn_hiring", "reddit_forhire"]


def test_registry_ignores_a_half_configured_reddit_app(monkeypatch):
    """One of the two values is not a credential — it would 401 every run."""
    monkeypatch.setenv("COPILOT_REDDIT_CLIENT_ID", "test-app-id")
    monkeypatch.setenv("COPILOT_REDDIT_CLIENT_SECRET", "")
    assert "reddit_forhire" not in {s.name for s in get_default_sources()}

    monkeypatch.setenv("COPILOT_REDDIT_CLIENT_ID", "   ")
    monkeypatch.setenv("COPILOT_REDDIT_CLIENT_SECRET", "test-app-secret")
    assert "reddit_forhire" not in {s.name for s in get_default_sources()}


# --- last_error: the two failures here are both fixable in two minutes ---------
# This adapter reported `scanned` but no `last_error`, so a missing app and a refused
# token both reached the funnel report as "dead: fetched nothing" — advice to retire the
# one source that carries contract work WITH a contact address. Same wrong-lever defect
# `last_error` was added to remote_boards/contra_startup to end.


def test_an_unconfigured_app_names_the_two_env_vars(monkeypatch):
    _without_credentials(monkeypatch)
    src = RedditForHireSource(
        transport=httpx.MockTransport(lambda r: httpx.Response(403))
    )
    src.fetch()

    assert src.last_error is not None
    assert "COPILOT_REDDIT_CLIENT_ID" in src.last_error
    assert "reddit.com/prefs/apps" in src.last_error
    # And it must not claim to have read an empty listing.
    assert src.scanned is None


def test_a_refused_token_is_reported_as_a_credential_fault(monkeypatch):
    _with_credentials(monkeypatch)
    src = RedditForHireSource(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(401, json={"error": "invalid_grant"})
        )
    )
    assert src.fetch() == []

    assert src.last_error is not None
    assert "token refused" in src.last_error
    assert "script" in src.last_error
    assert src.scanned is None


def test_the_first_failure_is_kept_not_the_last(monkeypatch):
    """A token refusal makes every later read fail too, so reporting the LAST error
    would describe the symptom furthest from the fix."""
    _without_credentials(monkeypatch)
    src = RedditForHireSource(
        transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    src.fetch()

    assert src.last_error is not None
    assert src.last_error.startswith("unauthenticated:")


def test_a_recovered_source_stops_reporting_a_stale_error(monkeypatch):
    """Cleared per fetch, for the same reason `scanned` is: a stale error describes the
    previous run and would keep a working source labelled broken."""
    _without_credentials(monkeypatch)
    src = RedditForHireSource(
        transport=httpx.MockTransport(lambda r: httpx.Response(403))
    )
    src.fetch()
    assert src.last_error is not None

    _with_credentials(monkeypatch)

    def ok(request: httpx.Request) -> httpx.Response:
        if "access_token" in str(request.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json={"data": {"children": []}})

    src2 = RedditForHireSource(transport=httpx.MockTransport(ok))
    src2.fetch()
    assert src2.last_error is None
    # A payload DID arrive and it was empty: 0 is a real measurement here.
    assert src2.scanned == 0


def test_a_single_subreddit_failure_names_which_one(monkeypatch):
    """A 403 on r/forhire while r/jobbit answers is a different problem from both
    refusing, and only the detail says which."""
    _with_credentials(monkeypatch)

    def partial(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "access_token" in url:
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if "forhire" in url:
            return httpx.Response(500)
        return httpx.Response(200, json={"data": {"children": []}})

    src = RedditForHireSource(transport=httpx.MockTransport(partial))
    src.fetch()

    assert src.last_error is not None
    assert "forhire" in src.last_error
    # The healthy subreddit still delivered a payload, so scanned is a real 0.
    assert src.scanned == 0
