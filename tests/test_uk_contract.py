"""Offline tests for the UK day-rate contract adapter (Adzuna).

``httpx.Client.get`` is monkeypatched with an inline Adzuna-shaped payload — no real
HTTP, no API key required. The behaviours pinned here are the ones that caused real
production defects elsewhere in this pipeline:

* an unconfigured source must warn **loudly**, not return [] in silence;
* a loose relevance ranking must still be keyword-gated;
* the same job returned by two queries/locations must not become two leads.
"""
from __future__ import annotations

import httpx
import pytest

from core.schemas import Lead
from sources.uk_contract import UKContractSource


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._payload


def _job(job_id, title, description, **extra):
    job = {
        "id": job_id,
        "title": title,
        "description": description,
        "redirect_url": f"https://www.adzuna.co.uk/jobs/details/{job_id}",
        "company": {"display_name": "Acme Ltd"},
        "location": {"display_name": "London"},
        "created": "2026-08-01T09:00:00Z",
    }
    job.update(extra)
    return job


PAYLOAD = {
    "count": 3,
    "results": [
        _job(
            "1001",
            "Senior DevOps Engineer (Kubernetes, Terraform)",
            "Contract role hardening EKS and CI/CD pipelines. Outside IR35.",
            salary_min=130000,
            salary_max=143000,
        ),
        _job(
            "1002",
            "LLM Platform Engineer",
            "Build RAG inference infrastructure on GCP with vLLM.",
            salary_min=150000,
            salary_max=150000,
        ),
        # Adzuna relevance is loose: a "devops" query returns sales roles at
        # devops-adjacent companies. Must be filtered by the keyword gate.
        _job(
            "1003",
            "Enterprise Sales Executive",
            "Sell our monitoring product to engineering leaders. OTE uncapped.",
        ),
    ],
}


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("COPILOT_ADZUNA_APP_ID", "test-id")
    monkeypatch.setenv("COPILOT_ADZUNA_APP_KEY", "test-key")


def _install(monkeypatch, payload, *, fail=False, capture=None):
    def fake_get(self, url, params=None):
        if capture is not None:
            capture.append(params or {})
        if fail:
            raise httpx.ConnectError("down")
        return FakeResponse(payload)

    monkeypatch.setattr(httpx.Client, "get", fake_get)


# --------------------------------------------------------------------------- #
# unconfigured behaviour
# --------------------------------------------------------------------------- #
def test_missing_credentials_warns_loudly_and_returns_empty(monkeypatch, caplog):
    """Silence is the bug: a zero-yield source must say why it yielded zero.

    A month of "success" runs happened because dead sources returned [] quietly.
    """
    monkeypatch.delenv("COPILOT_ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("COPILOT_ADZUNA_APP_KEY", raising=False)

    with caplog.at_level("WARNING"):
        leads = UKContractSource().fetch(limit=10)

    assert leads == []
    text = caplog.text
    assert "COPILOT_ADZUNA_APP_ID" in text
    assert "COPILOT_ADZUNA_APP_KEY" in text
    assert "developer.adzuna.com" in text  # tells the operator how to fix it


def test_partial_credentials_also_disabled(monkeypatch, caplog):
    monkeypatch.setenv("COPILOT_ADZUNA_APP_ID", "only-id")
    monkeypatch.delenv("COPILOT_ADZUNA_APP_KEY", raising=False)

    with caplog.at_level("WARNING"):
        assert UKContractSource().fetch(limit=10) == []
    assert "DISABLED" in caplog.text


# --------------------------------------------------------------------------- #
# mapping
# --------------------------------------------------------------------------- #
def test_maps_contract_jobs_to_leads(creds, monkeypatch):
    _install(monkeypatch, PAYLOAD)
    src = UKContractSource(queries=("devops",), locations=("London",))
    leads = src.fetch(limit=10)

    assert [lead.external_id for lead in leads] == ["1001", "1002"]  # sales role dropped
    lead = leads[0]
    assert isinstance(lead, Lead)
    assert lead.source == "uk_contract"
    assert lead.company == "Acme Ltd"
    assert "Senior DevOps Engineer" in lead.title
    assert lead.url.endswith("/1001")
    assert "kubernetes" in lead.tags
    assert "London" in lead.tags
    assert lead.posted_at == "2026-08-01T09:00:00Z"


def test_offtopic_listing_is_filtered(creds, monkeypatch):
    _install(monkeypatch, PAYLOAD)
    src = UKContractSource(queries=("devops",), locations=("London",))
    assert "1003" not in {lead.external_id for lead in src.fetch(limit=10)}


def test_ai_infra_listing_is_kept(creds, monkeypatch):
    """The growth segment (LLM/RAG contract roles, +247% YoY) must not be dropped."""
    _install(monkeypatch, PAYLOAD)
    src = UKContractSource(queries=("llm",), locations=("London",))
    llm = [lead for lead in src.fetch(limit=10) if lead.external_id == "1002"]
    assert llm, "LLM platform role should qualify"
    assert "llm" in llm[0].tags or "rag" in llm[0].tags


# --------------------------------------------------------------------------- #
# budget rendering
# --------------------------------------------------------------------------- #
def test_budget_keeps_annualised_range_not_a_guessed_day_rate(creds, monkeypatch):
    """Adzuna annualises contract pay. Inventing a day rate would put a wrong
    number in a proposal, which is worse than omitting it."""
    _install(monkeypatch, PAYLOAD)
    src = UKContractSource(queries=("devops",), locations=("London",))
    by_id = {lead.external_id: lead for lead in src.fetch(limit=10)}

    assert by_id["1001"].budget == "£130,000-£143,000 (annualised)"
    assert by_id["1002"].budget == "£150,000 (annualised)"  # collapsed equal bounds
    assert "day" not in (by_id["1001"].budget or "")


def test_missing_salary_yields_no_budget(creds, monkeypatch):
    _install(monkeypatch, {"results": [_job("2001", "DevOps Engineer", "k8s work")]})
    src = UKContractSource(queries=("devops",), locations=("London",))
    assert src.fetch(limit=5)[0].budget is None


# --------------------------------------------------------------------------- #
# request shape
# --------------------------------------------------------------------------- #
def test_requests_contract_only_and_recent(creds, monkeypatch):
    """Permanent roles are not the product: contract_only must always be set."""
    captured: list[dict] = []
    _install(monkeypatch, PAYLOAD, capture=captured)
    UKContractSource(queries=("devops",), locations=("London",), max_days_old=7).fetch(
        limit=5
    )

    assert captured, "no request was made"
    params = captured[0]
    assert params["contract_only"] == 1
    assert params["max_days_old"] == 7
    assert params["sort_by"] == "date"
    assert params["where"] == "London"
    assert params["app_id"] == "test-id" and params["app_key"] == "test-key"


def test_searches_every_query_and_location(creds, monkeypatch):
    captured: list[dict] = []
    _install(monkeypatch, {"results": []}, capture=captured)
    UKContractSource(queries=("a", "b"), locations=("London", "UK")).fetch(limit=50)

    pairs = {(p["what_or"], p["where"]) for p in captured}
    assert pairs == {("a", "London"), ("b", "London"), ("a", "UK"), ("b", "UK")}


# --------------------------------------------------------------------------- #
# robustness
# --------------------------------------------------------------------------- #
def test_dedupes_the_same_job_across_queries(creds, monkeypatch):
    """London and UK searches overlap heavily; one job must yield one lead."""
    _install(monkeypatch, PAYLOAD)
    src = UKContractSource(queries=("devops", "sre"), locations=("London", "UK"))
    ids = [lead.external_id for lead in src.fetch(limit=50)]
    assert ids == sorted(set(ids), key=ids.index)
    assert len(ids) == 2


def test_network_error_returns_empty(creds, monkeypatch):
    _install(monkeypatch, PAYLOAD, fail=True)
    assert UKContractSource(queries=("devops",), locations=("London",)).fetch() == []


def test_malformed_payload_does_not_raise(creds, monkeypatch):
    for payload in ({}, {"results": None}, {"results": ["not-a-dict"]}, []):
        _install(monkeypatch, payload)
        src = UKContractSource(queries=("devops",), locations=("London",))
        assert src.fetch(limit=5) == []


def test_respects_limit(creds, monkeypatch):
    _install(monkeypatch, PAYLOAD)
    src = UKContractSource(queries=("devops",), locations=("London",))
    assert len(src.fetch(limit=1)) == 1
