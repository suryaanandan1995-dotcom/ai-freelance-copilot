"""Offline tests for the Reddit r/forhire "[Hiring]" adapter.

``httpx.get`` is monkeypatched with a small inline Reddit-style JSON fixture --
no real HTTP. Asserts that a ``[Hiring]`` DevSecOps post with an email becomes a
Lead (with the email recoverable for auto-outreach), while ``[For Hire]`` and
off-topic posts are excluded, and network errors yield [].
"""
from __future__ import annotations

import httpx

from core.schemas import Lead
from outreach.extract import find_contact_email
from sources.reddit_forhire import RedditForHireSource
from sources.registry import get_default_sources


class FakeResponse:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._json


def _child(**data):
    return {"kind": "t3", "data": data}


# A realistic listing: one [Hiring] DevSecOps post with a contact email, one
# [For Hire] post, and one [Hiring] but off-topic (no DevSecOps keyword) post.
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


def _install(monkeypatch, payload, *, fail=False):
    def fake_get(url, headers=None, timeout=None):
        if fail:
            raise httpx.ConnectError("down")
        # Assert the descriptive User-Agent is sent (Reddit requires it).
        assert headers and "ai-freelance-copilot" in headers.get("User-Agent", "")
        return FakeResponse(payload)

    monkeypatch.setattr(httpx, "get", fake_get)


def test_keeps_hiring_devsecops_post(monkeypatch):
    _install(monkeypatch, LISTING)
    # Single endpoint so the fixture isn't served twice.
    src = RedditForHireSource(endpoints=("https://www.reddit.com/r/forhire/new.json",))
    leads = src.fetch(limit=10)

    assert len(leads) == 1
    lead = leads[0]
    assert isinstance(lead, Lead)
    assert lead.source == "reddit_forhire"
    assert lead.external_id == "abc123"
    assert lead.url == "https://www.reddit.com/r/forhire/comments/abc123/hiring_k8s/"
    assert lead.company == "acmeclient"
    assert "kubernetes" in lead.tags
    assert lead.posted_at is not None and lead.posted_at.startswith("2024-")


def test_hiring_email_recoverable_for_outreach(monkeypatch):
    _install(monkeypatch, LISTING)
    src = RedditForHireSource(endpoints=("https://www.reddit.com/r/forhire/new.json",))
    lead = src.fetch(limit=10)[0]
    assert find_contact_email(lead) == "hiring@acmecloud.io"


def test_excludes_for_hire_and_offtopic(monkeypatch):
    _install(monkeypatch, LISTING)
    src = RedditForHireSource(endpoints=("https://www.reddit.com/r/forhire/new.json",))
    ids = {lead.external_id for lead in src.fetch(limit=10)}
    # [For Hire] freelancer post and off-topic bakery [Hiring] post excluded.
    assert "def456" not in ids
    assert "ghi789" not in ids


def test_network_error_returns_empty(monkeypatch):
    _install(monkeypatch, LISTING, fail=True)
    assert RedditForHireSource().fetch() == []


def test_dedupes_across_endpoints(monkeypatch):
    _install(monkeypatch, LISTING)
    # Both default endpoints serve the same fixture; the [Hiring] post must
    # appear only once.
    src = RedditForHireSource()
    leads = src.fetch(limit=10)
    ids = [lead.external_id for lead in leads]
    assert ids == ["abc123"]


def test_respects_limit(monkeypatch):
    _install(monkeypatch, LISTING)
    src = RedditForHireSource(endpoints=("https://www.reddit.com/r/forhire/new.json",))
    assert len(src.fetch(limit=0)) == 0


def test_registry_includes_reddit_forhire():
    names = {s.name for s in get_default_sources()}
    assert "reddit_forhire" in names
