"""Offline tests for the lead-source adapters.

All network access is monkeypatched: feedparser.parse, httpx.get, and the
HN Algolia client are replaced with small inline fixtures. No real HTTP.
"""
from __future__ import annotations

import feedparser
import httpx

from core.schemas import Lead
from sources import registry
from sources.base import LeadSource
from sources.contra_startup import ContraStartupSource
from sources.hn_hiring import HNWhoIsHiringSource
from sources.remote_boards import RemoteBoardsSource
from sources.upwork_rss import UpworkRSSSource


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------
class FakeFeed:
    """Mimic feedparser's result object (has .entries)."""

    def __init__(self, entries):
        self.entries = entries


def _rss_entry(**kw):
    base = {
        "id": "",
        "link": "",
        "title": "",
        "summary": "",
        "published": None,
        "author": None,
    }
    base.update(kw)
    return base


class FakeResponse:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._json


# --------------------------------------------------------------------------
# 1. Upwork RSS mapping
# --------------------------------------------------------------------------
def test_upwork_maps_entry_to_lead(monkeypatch):
    entries = [
        _rss_entry(
            id="upwork-123",
            link="https://upwork.com/jobs/123",
            title="Senior DevOps Engineer (Kubernetes/Terraform)",
            summary="Need help with AWS and CI/CD pipelines.",
            published="2026-06-28T10:00:00Z",
        )
    ]
    monkeypatch.setattr(feedparser, "parse", lambda url: FakeFeed(entries))

    src = UpworkRSSSource(feeds=["https://example/feed.rss"])
    leads = src.fetch(limit=10)

    assert len(leads) == 1
    lead = leads[0]
    assert isinstance(lead, Lead)
    assert lead.source == "upwork_rss"
    assert lead.external_id == "upwork-123"
    assert lead.url == "https://upwork.com/jobs/123"
    assert lead.posted_at == "2026-06-28T10:00:00Z"
    # tags derived from title/summary keywords
    assert "kubernetes" in lead.tags
    assert "terraform" in lead.tags
    assert "aws" in lead.tags


def test_upwork_external_id_falls_back_to_link_hash(monkeypatch):
    entries = [_rss_entry(id="", link="https://upwork.com/jobs/no-id", title="DevOps")]
    monkeypatch.setattr(feedparser, "parse", lambda url: FakeFeed(entries))
    src = UpworkRSSSource(feeds=["https://example/feed.rss"])
    leads = src.fetch()
    assert len(leads) == 1
    # hashed, not empty, and stable
    assert leads[0].external_id and leads[0].external_id != ""


def test_upwork_returns_empty_on_network_error(monkeypatch):
    def boom(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(feedparser, "parse", boom)
    src = UpworkRSSSource(feeds=["https://example/feed.rss"])
    assert src.fetch() == []


# --------------------------------------------------------------------------
# 2. Remote boards: RemoteOK JSON + keyword filtering
# --------------------------------------------------------------------------
def test_remote_boards_filters_by_keyword(monkeypatch):
    remoteok_payload = [
        {"legal": "metadata blob, no id here"},  # must be skipped
        {
            "id": "1",
            "position": "Senior Kubernetes Platform Engineer",
            "company": "CloudCo",
            "description": "Terraform + AWS",
            "tags": ["devops", "kubernetes"],
            "url": "https://remoteok.com/jobs/1",
            "date": "2026-06-20",
        },
        {
            "id": "2",
            "position": "Marketing Copywriter",
            "company": "AdCo",
            "description": "Write blog posts",
            "tags": ["marketing"],
            "url": "https://remoteok.com/jobs/2",
        },
    ]

    def fake_get(url, headers=None, timeout=None):
        return FakeResponse(remoteok_payload)

    monkeypatch.setattr(httpx, "get", fake_get)
    # avoid WWR network: empty feed
    monkeypatch.setattr(feedparser, "parse", lambda url: FakeFeed([]))

    src = RemoteBoardsSource()
    leads = src.fetch(limit=10)

    assert len(leads) == 1  # only the kubernetes role passes the filter
    lead = leads[0]
    assert lead.source == "remote_boards"
    assert lead.external_id == "remoteok:1"
    assert lead.company == "CloudCo"
    assert "kubernetes" in lead.tags


def test_remote_boards_returns_empty_on_network_error(monkeypatch):
    def boom(url, headers=None, timeout=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(feedparser, "parse", lambda url: FakeFeed([]))
    src = RemoteBoardsSource()
    assert src.fetch() == []


# --------------------------------------------------------------------------
# 3. Contra / startup feeds
# --------------------------------------------------------------------------
def test_contra_startup_maps_default_feed(monkeypatch):
    entries = [
        _rss_entry(
            id="job-9",
            link="https://startup.example/jobs/9",
            title="Cloud SRE",
            summary="On-call SRE for a cloud platform.",
            author="StartupX",
            published="2026-06-25",
        )
    ]
    monkeypatch.setattr(feedparser, "parse", lambda url: FakeFeed(entries))
    src = ContraStartupSource()  # uses DEFAULT_FEED
    assert src.feeds  # has a sensible default
    leads = src.fetch(limit=5)
    assert len(leads) == 1
    assert leads[0].source == "contra_startup"
    assert leads[0].company == "StartupX"
    assert "sre" in leads[0].tags


def test_contra_startup_returns_empty_on_error(monkeypatch):
    def boom(url):
        raise ValueError("bad feed")

    monkeypatch.setattr(feedparser, "parse", boom)
    src = ContraStartupSource(feeds=["https://x/feed"])
    assert src.fetch() == []


# --------------------------------------------------------------------------
# 4. HN who-is-hiring
# --------------------------------------------------------------------------
class FakeHNClient:
    """Stand-in for httpx.Client used by HNWhoIsHiringSource."""

    def __init__(self, search_json, item_json, *, fail=False):
        self._search = search_json
        self._item = item_json
        self._fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        if self._fail:
            raise httpx.ConnectError("down")
        if url.endswith("/search_by_date"):
            return FakeResponse(self._search)
        return FakeResponse(self._item)


def _install_hn_client(monkeypatch, search_json, item_json, fail=False):
    def factory(*a, **kw):
        return FakeHNClient(search_json, item_json, fail=fail)

    monkeypatch.setattr(httpx, "Client", factory)


def test_hn_hiring_maps_matching_comments(monkeypatch):
    search = {"hits": [{"objectID": "5000"}]}
    item = {
        "id": 5000,
        "children": [
            {
                "objectID": "5001",
                "author": "acme",
                "created_at": "2026-06-01T00:00:00Z",
                "text": "Acme | Remote | DevOps engineer | Kubernetes &amp; AWS<p>great team</p>",
            },
            {
                "objectID": "5002",
                "author": "bakery",
                "text": "Local bakery looking for a <b>pastry chef</b>, on-site only",
            },
        ],
    }
    _install_hn_client(monkeypatch, search, item)

    src = HNWhoIsHiringSource()
    leads = src.fetch(limit=10)

    assert len(leads) == 1  # bakery filtered out (no keyword)
    lead = leads[0]
    assert lead.source == "hn_hiring"
    assert lead.external_id == "5001"
    assert lead.url == "https://news.ycombinator.com/item?id=5001"
    assert "<p>" not in lead.description  # html stripped
    assert "devops" in lead.tags


def test_hn_hiring_returns_empty_on_error(monkeypatch):
    _install_hn_client(monkeypatch, {}, {}, fail=True)
    src = HNWhoIsHiringSource()
    assert src.fetch() == []


# --------------------------------------------------------------------------
# 5. Registry
# --------------------------------------------------------------------------
def test_get_default_sources_returns_four_sources():
    sources = registry.get_default_sources()
    assert len(sources) == 4
    assert all(isinstance(s, LeadSource) for s in sources)
    names = {s.name for s in sources}
    assert names == {"upwork_rss", "remote_boards", "contra_startup", "hn_hiring"}


def test_fetch_all_dedupes_across_sources():
    dup = Lead(source="upwork_rss", external_id="A", title="x")

    class StubA(LeadSource):
        name = "upwork_rss"

        def fetch(self, limit=50):
            return [dup, Lead(source="upwork_rss", external_id="A", title="dup")]

    class StubB(LeadSource):
        name = "remote_boards"

        def fetch(self, limit=50):
            return [Lead(source="remote_boards", external_id="B", title="y")]

    class StubBoom(LeadSource):
        name = "boom"

        def fetch(self, limit=50):
            raise RuntimeError("should be swallowed by fetch_all")

    leads = registry.fetch_all([StubA(), StubB(), StubBoom()])
    keys = {lead.dedupe_key for lead in leads}
    assert keys == {"upwork_rss:A", "remote_boards:B"}
    assert len(leads) == 2  # the duplicate "A" collapsed to one
