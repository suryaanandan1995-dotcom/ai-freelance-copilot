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
            # Live payloads carry these four; the adapter used to ignore all of them.
            "salaryMin": 500,
            "salaryMax": 650,
            "salaryCurrency": "GBP",
            "salaryPeriod": "daily",
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


def test_salary_reaches_the_budget_field(monkeypatch):
    """A dropped field the scorer is explicitly told to reward.

    The qualifier prompt scores day-rate/contract pay HIGH and renders ``Budget:
    unknown`` when ``Lead.budget`` is None — which it was for every Jobicy lead ever
    fetched, despite the payload stating the rate.
    """
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse(PAYLOAD))
    lead = JobicySource(endpoints=("https://jobicy.com/api/v2/remote-jobs?tag=devops",)).fetch()[0]
    assert lead.budget == "£500-£650/day"


def test_a_yearly_salary_is_never_printed_as_a_day_rate():
    """Respect ``salaryPeriod``. £143,000/day is the kind of number that loses a
    client, and the same class of error contract_jobs refuses to make by guessing."""
    from sources.jobicy import _format_budget

    assert _format_budget(
        {"salaryMin": 120000, "salaryMax": 143000, "salaryCurrency": "GBP",
         "salaryPeriod": "yearly"}
    ) == "£120,000-£143,000/year"
    # Equal bounds collapse to one figure rather than "£90,000-£90,000".
    assert _format_budget(
        {"salaryMin": 90000, "salaryMax": 90000, "salaryCurrency": "USD",
         "salaryPeriod": "yearly"}
    ) == "$90,000/year"


def test_unstated_pay_stays_unknown_rather_than_invented():
    """Never invent a rate the payload does not state — contract_jobs' rule, and the
    reason a budget is worth having at all."""
    from sources.jobicy import _format_budget

    assert _format_budget({"jobTitle": "DevOps Engineer"}) is None
    assert _format_budget({"salaryMin": 0, "salaryMax": 0, "salaryCurrency": "USD"}) is None
    assert _format_budget({"salaryMin": "", "salaryMax": None}) is None
    # An unknown period is left unlabelled, not assumed to be annual.
    assert _format_budget({"salaryMin": 700, "salaryCurrency": "EUR"}) == "€700"
    # An unknown currency prints its code rather than a guessed symbol.
    assert _format_budget(
        {"salaryMin": 12000, "salaryCurrency": "PLN", "salaryPeriod": "monthly"}
    ) == "PLN 12,000/month"


def test_network_error_yields_empty(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "get", boom)
    assert JobicySource().fetch() == []


def test_a_failed_fetch_records_why_it_was_empty(monkeypatch):
    """``pipeline.py`` reads ``last_error`` to tell ``broken`` from ``dead``.

    Without it a 500 from Jobicy reported as ``dead: fetched nothing`` — the verdict
    that says "change your queries" about a problem only a code/endpoint fix touches.
    """
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse({}, status=500))
    src = JobicySource(endpoints=("https://jobicy.com/api/v2/remote-jobs?tag=devops",))
    assert src.fetch() == []
    assert src.last_error and "HTTPStatusError" in src.last_error


def test_last_error_is_per_fetch(monkeypatch):
    """So a recovered source does not look broken forever."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse({}, status=500))
    src = JobicySource(endpoints=("https://jobicy.com/api/v2/remote-jobs?tag=devops",))
    src.fetch()
    assert src.last_error

    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse(PAYLOAD))
    assert src.fetch()
    assert src.last_error is None


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
