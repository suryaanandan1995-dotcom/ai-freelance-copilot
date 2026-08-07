"""Offline tests for the HN "Freelancer? Seeking Freelancer?" adapter.

The HN Algolia client is monkeypatched with a small inline fixture. No real
HTTP. Asserts that a SEEKING FREELANCER (client-hiring) comment with an email
becomes a Lead, while a SEEKING WORK (freelancer-availability) comment does not.
"""
from __future__ import annotations

import httpx

from core.schemas import Lead
from outreach.extract import find_contact_email
from sources.hn_freelancer import HNFreelancerSource


class FakeResponse:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._json


class FakeHNClient:
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


def _install(monkeypatch, search_json, item_json, fail=False):
    def factory(*a, **kw):
        return FakeHNClient(search_json, item_json, fail=fail)

    monkeypatch.setattr(httpx, "Client", factory)


def test_hn_freelancer_keeps_seeking_freelancer_with_email(monkeypatch):
    search = {"hits": [{"objectID": "9000"}]}
    item = {
        "id": 9000,
        "children": [
            # SEEKING FREELANCER (client hiring) + DevSecOps keyword + email.
            {
                "objectID": "9001",
                "author": "acmeco",
                "created_at": "2026-06-01T00:00:00Z",
                "text": (
                    "SEEKING FREELANCER | Acme Corp | Remote<p>We are looking to hire "
                    "a Kubernetes + Terraform contractor. Email jobs [at] acmecorp "
                    "[dot] com</p>"
                ),
            },
            # SEEKING WORK (freelancer available) — must be excluded even though
            # it mentions relevant keywords.
            {
                "objectID": "9002",
                "author": "devdan",
                "text": (
                    "SEEKING WORK | Remote<p>I'm a freelance DevOps/Kubernetes "
                    "engineer available for projects. reach me at dan@dev.io</p>"
                ),
            },
            # Client hiring but no relevant skill — excluded.
            {
                "objectID": "9003",
                "author": "bakery",
                "text": "SEEKING FREELANCER | We are hiring a pastry chef, on-site.",
            },
        ],
    }
    _install(monkeypatch, search, item)

    src = HNFreelancerSource()
    leads = src.fetch(limit=10)

    assert len(leads) == 1
    lead = leads[0]
    assert isinstance(lead, Lead)
    assert lead.source == "hn_freelancer"
    assert lead.external_id == "9001"
    assert lead.url == "https://news.ycombinator.com/item?id=9001"
    assert "<p>" not in lead.description  # html stripped
    # NOT "SEEKING FREELANCER": that segment is the thread side, not a company, and
    # sending it as ``Company:`` is the same defect as sending an HN username.
    assert lead.company == "Acme Corp"
    assert "kubernetes" in lead.tags
    # the obfuscated email is recoverable for auto-outreach
    assert find_contact_email(lead) == "jobs@acmecorp.com"


def test_hn_freelancer_excludes_availability_comment(monkeypatch):
    search = {"hits": [{"objectID": "9100"}]}
    item = {
        "id": 9100,
        "children": [
            {
                "objectID": "9101",
                "author": "freelancer",
                "text": (
                    "SEEKING WORK<p>Available for work. Senior SRE, AWS + "
                    "Kubernetes. hire@me.dev</p>"
                ),
            },
        ],
    }
    _install(monkeypatch, search, item)

    src = HNFreelancerSource()
    assert src.fetch(limit=10) == []


def test_hn_freelancer_returns_empty_on_error(monkeypatch):
    _install(monkeypatch, {}, {}, fail=True)
    assert HNFreelancerSource().fetch() == []


def test_html_entities_are_decoded_not_just_ampersands(monkeypatch):
    """The old hand-rolled fix decoded ``&amp;`` only — 14 of 480 live entities.

    HN serves comment text escaped, so the qualifier prompt was reading
    ``We&#x27;re hiring`` and ``https:&#x2F;&#x2F;acme.com``. It is one source of
    corruption in a five-field prompt, and it affected 48 of 50 live leads.
    """
    search = {"hits": [{"objectID": "9200"}]}
    item = {
        "id": 9200,
        "children": [
            {
                "objectID": "9201",
                "author": "acme",
                "text": (
                    "Acme &amp; Co | Remote<p>We&#x27;re looking to hire a Kubernetes "
                    "contractor. Docs: https:&#x2F;&#x2F;acme.com&#x2F;jobs &quot;now&quot; "
                    "— mail jobs@acme.com</p>"
                ),
            },
        ],
    }
    _install(monkeypatch, search, item)

    lead = HNFreelancerSource().fetch(limit=5)[0]
    assert "&#x2F;" not in lead.description
    assert "&#x27;" not in lead.description
    assert "&quot;" not in lead.description
    assert "&amp;" not in lead.description
    assert "We're looking to hire" in lead.description
    assert "https://acme.com/jobs" in lead.description
    assert lead.company == "Acme & Co"


def test_commenter_typed_brackets_survive_stripping(monkeypatch):
    """Unescape AFTER stripping tags, or the strip eats the commenter's own text.

    HN escapes angle brackets a human typed (``&lt;first&gt;@corp.com`` is a common
    way to write an address pattern) while emitting real ``<p>`` markup of its own.
    Unescaping first turns the human's brackets into something ``<[^>]+>`` deletes,
    taking the rest of the line — including the address — with it.
    """
    search = {"hits": [{"objectID": "9300"}]}
    item = {
        "id": 9300,
        "children": [
            {
                "objectID": "9301",
                "author": "acme",
                "text": (
                    "Acme | Remote<p>We are hiring an SRE. Mail "
                    "&lt;yourname&gt;.devops@acme.com to apply.</p>"
                ),
            },
        ],
    }
    _install(monkeypatch, search, item)

    lead = HNFreelancerSource().fetch(limit=5)[0]
    assert "<yourname>.devops@acme.com" in lead.description
    assert "to apply" in lead.description  # the tail was not swallowed
    assert "<p>" not in lead.description  # HN's real markup still went
