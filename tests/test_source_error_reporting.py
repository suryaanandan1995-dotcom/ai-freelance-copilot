"""Every adapter must say *why* it came back empty.

``pipeline.py:343-346`` reads ``source.last_error`` to distinguish the ``broken``
verdict from ``dead``, and only ``contract_jobs`` ever set it. So a 500 from
working_nomads, jobicy or contra_startup reported as ``dead: fetched nothing`` —
the verdict that says "your queries found no market" about a problem that needs a
code or endpoint fix. That is the exact failure the ``broken`` verdict was
introduced to end, and it survived in three of the four adapters that can hit it.

Grouped by invariant rather than by adapter, because the invariant is the point:
returning [] is a contract (an adapter must never abort a run), so the *reason* is
the only thing that can tell the two cases apart. jobicy's version of these tests
lives beside its other tests in ``test_jobicy.py``.
"""
from __future__ import annotations

import feedparser
import httpx

from sources.contra_startup import ContraStartupSource
from sources.remote_boards import RemoteBoardsSource
from sources.working_nomads import WorkingNomadsSource


class FakeResponse:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._json


class FakeFeed:
    def __init__(self, entries, status=200):
        self.entries = entries
        self.status = status


_JOB = {
    "url": "https://www.workingnomads.com/jobs/devops-1",
    "title": "Remote DevOps / Kubernetes Engineer",
    "description": "Own our Terraform + EKS pipeline.",
    "company_name": "Globex",
}


def test_working_nomads_records_an_http_failure(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse({}, status=500))
    src = WorkingNomadsSource()
    assert src.fetch() == []
    assert src.last_error and "HTTPStatusError" in src.last_error


def test_working_nomads_records_a_transport_failure(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "get", boom)
    src = WorkingNomadsSource()
    assert src.fetch() == []
    assert src.last_error and "ConnectError" in src.last_error


def test_working_nomads_records_a_payload_that_is_not_a_job_list(monkeypatch):
    """An error document where a list belongs is a broken endpoint, not an empty market."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse({"error": "nope"}))
    src = WorkingNomadsSource()
    assert src.fetch() == []
    assert src.last_error and "dict" in src.last_error


def test_working_nomads_last_error_is_per_fetch(monkeypatch):
    """A recovered source must stop reporting broken, or the verdict gets ignored."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse({}, status=500))
    src = WorkingNomadsSource()
    src.fetch()
    assert src.last_error

    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse([_JOB]))
    assert src.fetch()
    assert src.last_error is None


def test_contra_startup_records_a_parse_failure(monkeypatch):
    def boom(url):
        raise ValueError("bad feed")

    monkeypatch.setattr(feedparser, "parse", boom)
    src = ContraStartupSource(feeds=["https://x/feed"])
    assert src.fetch() == []
    assert src.last_error and "ValueError" in src.last_error


def test_contra_startup_records_an_http_status_feedparser_swallowed(monkeypatch):
    """feedparser does not raise on HTTP errors — it returns an empty feed with a
    ``status``. A moved or 500ing board therefore looked identical to a quiet week."""
    monkeypatch.setattr(feedparser, "parse", lambda url: FakeFeed([], status=500))
    src = ContraStartupSource(feeds=["https://x/feed"])
    assert src.fetch() == []
    assert src.last_error and "500" in src.last_error


def test_contra_startup_last_error_is_per_fetch(monkeypatch):
    monkeypatch.setattr(feedparser, "parse", lambda url: FakeFeed([], status=500))
    src = ContraStartupSource(feeds=["https://x/feed"])
    src.fetch()
    assert src.last_error

    entries = [{"id": "1", "link": "https://x/1", "title": "SRE", "summary": "Kubernetes"}]
    monkeypatch.setattr(feedparser, "parse", lambda url: FakeFeed(entries))
    assert src.fetch()
    assert src.last_error is None


def test_a_healthy_empty_fetch_is_not_reported_as_broken(monkeypatch):
    """The other half of the gate: "no infra jobs this week" must stay ``dead``.

    A diagnostic that fires on both outcomes distinguishes nothing, which is how the
    real 400 in contract_jobs survived a month of runs.
    """
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse([]))
    nomads = WorkingNomadsSource()
    assert nomads.fetch() == []
    assert nomads.last_error is None

    monkeypatch.setattr(feedparser, "parse", lambda url: FakeFeed([]))
    contra = ContraStartupSource(feeds=["https://x/feed"])
    assert contra.fetch() == []
    assert contra.last_error is None


# --------------------------------------------------------------------------- #
# remote_boards: three boards behind one name
# --------------------------------------------------------------------------- #
def test_remote_boards_names_which_board_failed(monkeypatch):
    """One ``last_error`` string for three independent feeds needs attribution.

    ``remote_boards`` fetches RemoteOK, WeWorkRemotely and Remotive under a single
    source name, so a bare last-writer-wins message would report "RemoteOK is down"
    when RemoteOK was fine and Remotive was down — sending you to debug a healthy
    board. That is the same wrong-lever failure ``broken`` exists to prevent, one
    level down.
    """
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused"))
    )
    monkeypatch.setattr(feedparser, "parse", lambda url: FakeFeed([]))

    src = RemoteBoardsSource()
    assert src.fetch() == []
    # Both JSON boards failed; WWR returned empty-but-healthy and must not appear.
    assert src.last_error
    assert "RemoteOK" in src.last_error
    assert "Remotive" in src.last_error
    assert "WeWorkRemotely" not in src.last_error


def test_remote_boards_does_not_hide_one_outage_behind_another(monkeypatch):
    """Failures accumulate. Two boards down is not the same event as one."""
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused"))
    )
    monkeypatch.setattr(feedparser, "parse", lambda url: FakeFeed([]))

    src = RemoteBoardsSource()
    src.fetch()
    assert src.last_error.count("ConnectError") == 2


def test_remote_boards_last_error_is_per_fetch(monkeypatch):
    """A recovered board must stop reporting a stale error, or it is `broken` forever."""
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused"))
    )
    monkeypatch.setattr(feedparser, "parse", lambda url: FakeFeed([]))
    src = RemoteBoardsSource()
    src.fetch()
    assert src.last_error

    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse([]))
    assert src.fetch() == []
    assert src.last_error is None


# --------------------------------------------------------------------------- #
# the two HN sources: the highest-volume feed had no diagnostic at all
# --------------------------------------------------------------------------- #
#
# ``hn_hiring`` supplies more leads than any other adapter, and it had zero
# occurrences of ``last_error`` — so an Algolia outage or a rate-limit reported as
# ``dead: fetched nothing``, the verdict that means "your queries found no market".
# Both HN adapters swallow at three levels (story search, item fetch, and the client
# context itself) and each one returns empty on its own, so each needs recording:
# a diagnostic that covers two of three failure paths still lies on the third.


class _FailingHNClient:
    """Algolia client that fails at a chosen stage. ``stage`` is 'search' or 'item'."""

    def __init__(self, stage: str, exc: Exception):
        self._stage = stage
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        is_search = url.endswith("/search_by_date")
        if (self._stage == "search") is is_search:
            raise self._exc
        return FakeResponse({"hits": [{"objectID": "7000"}]})


class _HealthyHNClient:
    """Reaches a real (empty) thread: nothing matched, nothing broke."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        if url.endswith("/search_by_date"):
            return FakeResponse({"hits": [{"objectID": "7000"}]})
        return FakeResponse({"id": 7000, "children": []})


def _hn_sources():
    from sources.hn_freelancer import HNFreelancerSource
    from sources.hn_hiring import HNWhoIsHiringSource

    return [HNWhoIsHiringSource(), HNFreelancerSource()]


def test_hn_sources_record_a_failed_story_search(monkeypatch):
    monkeypatch.setattr(
        httpx, "Client", lambda *a, **k: _FailingHNClient("search", httpx.ConnectError("down"))
    )
    for src in _hn_sources():
        assert src.fetch() == []
        assert src.last_error and "ConnectError" in src.last_error, src.name


def test_hn_sources_record_a_failed_item_fetch(monkeypatch):
    """The thread was found and then could not be read — a different fault, same [].

    Worth its own test because ``_find_story_ids`` succeeding makes the source look
    reachable; only the item call reveals the outage.
    """
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **k: _FailingHNClient("item", httpx.HTTPStatusError("500", request=None, response=None)),
    )
    for src in _hn_sources():
        assert src.fetch() == []
        assert src.last_error and "HTTPStatusError" in src.last_error, src.name


def test_hn_sources_record_a_client_construction_failure(monkeypatch):
    """The outermost handler — e.g. no DNS, or a bad proxy env var.

    It wraps the whole ``with httpx.Client(...)`` block, so neither inner handler
    ever runs and neither would have set the message.
    """

    def boom(*a, **k):
        raise httpx.UnsupportedProtocol("bad proxy")

    monkeypatch.setattr(httpx, "Client", boom)
    for src in _hn_sources():
        assert src.fetch() == []
        assert src.last_error and "UnsupportedProtocol" in src.last_error, src.name


def test_hn_sources_last_error_is_per_fetch(monkeypatch):
    monkeypatch.setattr(
        httpx, "Client", lambda *a, **k: _FailingHNClient("search", httpx.ConnectError("down"))
    )
    sources = _hn_sources()
    for src in sources:
        src.fetch()
        assert src.last_error

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _HealthyHNClient())
    for src in sources:
        # A quiet thread: empty, but healthy — and the stale error must be gone.
        assert src.fetch() == []
        assert src.last_error is None, src.name
