"""Offline tests for the Working Nomads adapter (mocked httpx, no real HTTP)."""
from __future__ import annotations

import httpx

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


JOBS = [
    {
        "url": "https://www.workingnomads.com/jobs/devops-1",
        "title": "Remote DevOps / Kubernetes Engineer",
        "description": "<p>Own our <b>Terraform</b> + EKS CI/CD pipeline.</p>",
        "company_name": "Globex",
        "category_name": "Development",
        "tags": "devops, kubernetes",
        "pub_date": "2026-07-06T09:00:00Z",
    },
    {
        "url": "https://www.workingnomads.com/jobs/writer-2",
        "title": "Content Marketing Writer",
        "description": "<p>Write blog posts.</p>",
        "company_name": "WordsInc",
        "category_name": "Marketing",
        "tags": "writing",
        "pub_date": "2026-07-06T09:00:00Z",
    },
]


def test_devsecops_job_becomes_lead_html_stripped(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse(JOBS))
    leads = WorkingNomadsSource().fetch()

    assert len(leads) == 1  # marketing role filtered out
    lead = leads[0]
    assert lead.source == "working_nomads"
    assert lead.external_id == "https://www.workingnomads.com/jobs/devops-1"
    assert "<b>" not in lead.description and "Terraform" in lead.description
    assert lead.company == "Globex"
    assert lead.tags


def test_network_error_yields_empty(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "get", boom)
    assert WorkingNomadsSource().fetch() == []


def test_non_list_payload_is_safe(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse({"error": "nope"}))
    assert WorkingNomadsSource().fetch() == []
