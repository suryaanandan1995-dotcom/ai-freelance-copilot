"""Offline tests for what hn_hiring actually hands the qualifier prompt.

``agents/qualifier.py`` renders exactly five fields — Title, Company, Budget, Tags,
Description — so these tests are written against those fields rather than against
the adapter's internals. Live measurement of 50 hn_hiring leads on 2026-08-07 found
all three of the defects pinned here: 48 of 50 descriptions carried raw HTML
entities, 46 of 50 titles were a 117-character truncation ending in ``...``, and
50 of 50 companies were an HN username. Production never scored above 78.

The HN Algolia client is monkeypatched; no real HTTP.
"""
from __future__ import annotations

import httpx

from sources.hn_hiring import HNWhoIsHiringSource

#: The real title measured on 2026-08-07, one of the 46 truncated ones. Kept verbatim
#: (entities, double spaces, inline URL and all) because every part of it is a thing
#: the parser has to survive.
SNOUT_TEXT = (
    "Snout  https:&#x2F;&#x2F;snout.com&#x2F;  | Multiple Engineering + Product Roles "
    "| Remote US or Ontario, Canada | Full-time<p>We&#x27;re a small team building "
    "computer vision for dogs. Stack is Kubernetes on AWS with Terraform. "
    "Mail careers@snout.com</p>"
)


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
    def __init__(self, search_json, item_json):
        self._search = search_json
        self._item = item_json

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        if url.endswith("/search_by_date"):
            return FakeResponse(self._search)
        return FakeResponse(self._item)


def _install(monkeypatch, children):
    search = {"hits": [{"objectID": "7000"}]}
    item = {"id": 7000, "children": children}
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: FakeHNClient(search, item))


def _one(monkeypatch, text, author="kcartmell"):
    _install(monkeypatch, [{"objectID": "7001", "author": author, "text": text}])
    leads = HNWhoIsHiringSource().fetch(limit=10)
    assert len(leads) == 1
    return leads[0]


# --------------------------------------------------------------------------
# A. HTML entities
# --------------------------------------------------------------------------
def test_entities_are_decoded_in_description_and_title(monkeypatch):
    """48 of 50 live leads reached the model as ``We&#x27;re hiring``."""
    lead = _one(monkeypatch, SNOUT_TEXT)
    for entity in ("&#x2F;", "&#x27;", "&quot;", "&amp;"):
        assert entity not in lead.description
        assert entity not in lead.title
    assert "We're a small team" in lead.description
    assert "https://snout.com/" in lead.description


# --------------------------------------------------------------------------
# B. Title is a job title, not a truncated first line
# --------------------------------------------------------------------------
def test_title_is_the_role_not_a_truncated_first_line(monkeypatch):
    """The measured Snout title clipped the role off at 117 chars and ended in '...'."""
    lead = _one(monkeypatch, SNOUT_TEXT)
    assert not lead.title.endswith("...")
    assert lead.title == "Snout — Multiple Engineering + Product Roles"
    # The location and employment-type segments are not the job title.
    assert "Ontario" not in lead.title
    assert "Full-time" not in lead.title


def test_title_falls_back_to_the_first_line_without_the_convention(monkeypatch):
    """Many comments ignore the pipe convention; those must still produce a Lead.

    Falling back is the point: a parser that only handles the convention would drop
    the non-conforming half of the thread, which is worse than an imperfect title.
    """
    text = (
        "<p>We are a stealth startup hiring a senior DevOps engineer to run our "
        "Kubernetes platform. Fully remote. Mail founders@stealth.io</p>"
    )
    lead = _one(monkeypatch, text)
    assert lead.title.startswith("We are a stealth startup hiring")
    assert "devops" in lead.tags


def test_a_pipe_deep_in_prose_is_not_read_as_a_header(monkeypatch):
    """``strip_html`` flattens the comment to one line, so 'the first line' is the
    whole post. A pipe 300 characters in is prose and must not define the fields."""
    text = (
        "<p>We are hiring a platform engineer. " + "Our stack is broad. " * 20 +
        "You will own Kubernetes | Terraform | AWS. Mail jobs@prose.io</p>"
    )
    lead = _one(monkeypatch, text, author="m00dy")
    assert lead.company == "m00dy"  # no header, so the author is the last resort
    assert "Terraform" not in lead.title


# --------------------------------------------------------------------------
# C. Company is a company, not an HN username
# --------------------------------------------------------------------------
def test_company_comes_from_the_header_not_the_hn_username(monkeypatch):
    """50 of 50 live leads said ``Company: <hn username>``; the researcher agent
    then went looking for a company by that name."""
    lead = _one(monkeypatch, SNOUT_TEXT, author="kcartmell")
    assert lead.company == "Snout"  # the inline URL is stripped out of the segment
    assert lead.company != "kcartmell"


def test_author_remains_the_last_resort_company(monkeypatch):
    """A lead with no detectable company is still worth scoring, so ``author`` stays
    as a fallback rather than the field being left empty."""
    text = "<p>Hiring an SRE for our AWS estate, remote UK. mail ops@nopipes.io</p>"
    assert _one(monkeypatch, text, author="hiringmgr").company == "hiringmgr"


def test_offtopic_comment_is_still_dropped(monkeypatch):
    """The keyword gate must still be able to reject: parsing titles better must not
    turn the adapter into a firehose."""
    _install(
        monkeypatch,
        [{"objectID": "7002", "author": "bakery", "text": "Bakery | Pastry Chef | On-site"}],
    )
    assert HNWhoIsHiringSource().fetch(limit=10) == []
