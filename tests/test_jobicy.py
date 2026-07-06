"""Offline tests for the Jobicy adapter.

``httpx.get`` is monkeypatched with an inline Jobicy-style JSON fixture — no real
HTTP. Asserts a DevSecOps job becomes a Lead (HTML stripped), an off-topic job is
excluded, and network errors yield [].
"""
from __future__ import annotations

import httpx

from sources.jobicy import JobicySource


class FakeResponse:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._json


PAYLOAD = {
    "jobs": [
        {
            "id": 101,
            "jobTitle": "Senior DevOps / Kubernetes Engineer",
            "companyName": "CloudCo",
            "url": "https://jobicy.com/jobs/101",
            "jobDescription": "<p>Harden our <b>EKS</b> clusters, Terraform, CI/CD.</p>",
            "pubDate": "2026-07-06 09:00:00",
        },
        {
            "id": 102,
            "jobTitle": "Customer Support Representative",
            "companyName": "SupportInc",
            "url": "https://jobicy.com/jobs/102",
            "jobDescription": "<p>Answer tickets and emails.</p>",
            "pubDate": "2026-07-06 09:00:00",
        },
    ]
}


def test_devsecops_job_becomes_lead_html_stripped(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse(PAYLOAD))
    leads = JobicySource(endpoints=("https://jobicy.com/api/v2/remote-jobs?tag=devops",)).fetch()

    assert len(leads) == 1  # the support role is filtered out (no DevSecOps keyword)
    lead = leads[0]
    assert lead.source == "jobicy"
    assert lead.external_id == "101"
    assert "<b>" not in lead.description and "EKS" in lead.description  # HTML stripped
    assert lead.company == "CloudCo"
    assert lead.tags  # DevSecOps tags extracted


def test_network_error_yields_empty(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "get", boom)
    assert JobicySource().fetch() == []


def test_dedupes_repeated_ids_across_endpoints(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse(PAYLOAD))
    src = JobicySource(
        endpoints=(
            "https://jobicy.com/api/v2/remote-jobs?tag=devops",
            "https://jobicy.com/api/v2/remote-jobs?tag=kubernetes",
        )
    )
    leads = src.fetch()
    assert [lead.external_id for lead in leads] == ["101"]  # id 101 not duplicated
