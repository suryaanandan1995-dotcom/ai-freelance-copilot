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
from sources import contract_jobs
from sources.contract_jobs import ContractJobsSource, Region


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            # ``response=self`` matters: the retry logic decides whether a failure is
            # transient by reading ``exc.response.status_code``. A fake that raises with
            # response=None makes every status indistinguishable, so a test could not
            # tell "retried the 503" from "retried the 400" — the whole distinction.
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self
            )

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


def _install(monkeypatch, payload, *, fail=False, capture=None, urls=None):
    """Patch httpx.Client.get.

    ``urls`` captures the request URL as well as the params: the country is in the URL
    path, not the query string, so a params-only capture cannot tell a US request from
    a UK one — and a multi-region source that silently queried one country ten times
    would pass every param assertion.
    """

    def fake_get(self, url, params=None):
        if capture is not None:
            capture.append(params or {})
        if urls is not None:
            urls.append(url)
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
        leads = ContractJobsSource().fetch(limit=10)

    assert leads == []
    text = caplog.text
    assert "COPILOT_ADZUNA_APP_ID" in text
    assert "COPILOT_ADZUNA_APP_KEY" in text
    assert "developer.adzuna.com" in text  # tells the operator how to fix it


def test_credentials_set_only_in_dotenv_are_honoured(monkeypatch, tmp_path, caplog):
    """Keys in `.env` must work, not just keys exported into the environment.

    This source used to read ``os.environ`` directly. pydantic-settings loads ``.env``
    into the ``Settings`` object and NEVER into ``os.environ``, so an operator who put
    the keys in ``.env`` — the file every other setting in this project lives in — got
    a source that reported itself DISABLED. That message reads as "you never configured
    it" when the truth was "your config is being ignored", which is the more expensive
    of the two to debug: the fix looks already applied.
    """
    import config
    import sources.contract_jobs as uk

    monkeypatch.delenv("COPILOT_ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("COPILOT_ADZUNA_APP_KEY", raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "COPILOT_ADZUNA_APP_ID=from-dotenv\nCOPILOT_ADZUNA_APP_KEY=key-from-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        uk, "get_settings", lambda: config.Settings(_env_file=str(env_file))
    )

    assert uk._credentials() == ("from-dotenv", "key-from-dotenv")

    captured: list[dict] = []
    _install(monkeypatch, PAYLOAD, capture=captured)
    with caplog.at_level("WARNING"):
        leads = uk.ContractJobsSource().fetch(limit=5)

    assert "DISABLED" not in caplog.text
    assert leads, "a configured source must actually fetch"
    assert captured[0]["app_id"] == "from-dotenv"


def test_partial_credentials_also_disabled(monkeypatch, caplog):
    monkeypatch.setenv("COPILOT_ADZUNA_APP_ID", "only-id")
    monkeypatch.delenv("COPILOT_ADZUNA_APP_KEY", raising=False)

    with caplog.at_level("WARNING"):
        assert ContractJobsSource().fetch(limit=10) == []
    assert "DISABLED" in caplog.text


# --------------------------------------------------------------------------- #
# mapping
# --------------------------------------------------------------------------- #
def test_maps_contract_jobs_to_leads(creds, monkeypatch):
    _install(monkeypatch, PAYLOAD)
    src = ContractJobsSource(queries=("devops",), locations=("London",))
    leads = src.fetch(limit=10)

    assert [lead.external_id for lead in leads] == ["gb:1001", "gb:1002"]  # sales role dropped
    lead = leads[0]
    assert isinstance(lead, Lead)
    assert lead.source == "contract_jobs"
    assert lead.company == "Acme Ltd"
    assert "Senior DevOps Engineer" in lead.title
    assert lead.url.endswith("/1001")
    assert "kubernetes" in lead.tags
    assert "London" in lead.tags
    assert lead.posted_at == "2026-08-01T09:00:00Z"


def test_offtopic_listing_is_filtered(creds, monkeypatch):
    _install(monkeypatch, PAYLOAD)
    src = ContractJobsSource(queries=("devops",), locations=("London",))
    assert "gb:1003" not in {lead.external_id for lead in src.fetch(limit=10)}


def test_ai_infra_listing_is_kept(creds, monkeypatch):
    """The growth segment (LLM/RAG contract roles, +247% YoY) must not be dropped."""
    _install(monkeypatch, PAYLOAD)
    src = ContractJobsSource(queries=("llm",), locations=("London",))
    llm = [lead for lead in src.fetch(limit=10) if lead.external_id == "gb:1002"]
    assert llm, "LLM platform role should qualify"
    assert "llm" in llm[0].tags or "rag" in llm[0].tags


# --------------------------------------------------------------------------- #
# budget rendering
# --------------------------------------------------------------------------- #
def test_budget_keeps_annualised_range_not_a_guessed_day_rate(creds, monkeypatch):
    """Adzuna annualises contract pay. Inventing a day rate would put a wrong
    number in a proposal, which is worse than omitting it."""
    _install(monkeypatch, PAYLOAD)
    src = ContractJobsSource(queries=("devops",), locations=("London",))
    by_id = {lead.external_id: lead for lead in src.fetch(limit=10)}

    assert by_id["gb:1001"].budget == "£130,000-£143,000 (annualised)"
    assert by_id["gb:1002"].budget == "£150,000 (annualised)"  # collapsed equal bounds
    assert "day" not in (by_id["gb:1001"].budget or "")


def test_missing_salary_yields_no_budget(creds, monkeypatch):
    _install(monkeypatch, {"results": [_job("2001", "DevOps Engineer", "k8s work")]})
    src = ContractJobsSource(queries=("devops",), locations=("London",))
    assert src.fetch(limit=5)[0].budget is None


# --------------------------------------------------------------------------- #
# request shape
# --------------------------------------------------------------------------- #
def test_requests_contract_only_and_recent(creds, monkeypatch):
    """Permanent roles are not the product: the contract filter must always be set."""
    captured: list[dict] = []
    _install(monkeypatch, PAYLOAD, capture=captured)
    ContractJobsSource(queries=("devops",), locations=("London",), max_days_old=7).fetch(
        limit=5
    )

    assert captured, "no request was made"
    params = captured[0]
    assert params["contract"] == 1
    assert params["max_days_old"] == 7
    assert params["sort_by"] == "date"
    assert params["where"] == "London"
    assert params["app_id"] == "test-id" and params["app_key"] == "test-key"


def test_searches_every_query_and_location(creds, monkeypatch):
    captured: list[dict] = []
    _install(monkeypatch, {"results": []}, capture=captured)
    ContractJobsSource(queries=("a", "b"), locations=("London", "UK")).fetch(limit=50)

    pairs = {(p["what_or"], p["where"]) for p in captured}
    assert pairs == {("a", "London"), ("b", "London"), ("a", "UK"), ("b", "UK")}


# --------------------------------------------------------------------------- #
# multi-region: UK onsite + remote across US / EU / ANZ
# --------------------------------------------------------------------------- #
def test_every_configured_region_is_actually_requested(creds, monkeypatch):
    """The country lives in the URL path, so this is the only place it can be checked.

    A source that claims four markets and queries one would pass every parameter
    assertion in this file — the params are identical, only the host path differs.
    """
    urls: list[str] = []
    _install(monkeypatch, {"results": []}, urls=urls)
    ContractJobsSource(queries=("devops",)).fetch(limit=500)

    countries = {u.rsplit("/jobs/", 1)[1].split("/")[0] for u in urls}
    assert {"gb", "us", "au", "de", "nl", "fr", "nz", "ch", "at", "be"} <= countries


def test_only_the_uk_is_searched_onsite(creds, monkeypatch):
    """Everywhere else is remote-only: relocation roles are not leads.

    ``what_phrase=remote`` is the filter that works — ``where=remote`` returns 0 in
    every country, which would have failed silently rather than loudly.
    """
    captured: list[dict] = []
    urls: list[str] = []
    _install(monkeypatch, {"results": []}, capture=captured, urls=urls)
    ContractJobsSource(queries=("devops",)).fetch(limit=500)

    by_country: dict[str, list[dict]] = {}
    for url, params in zip(urls, captured, strict=True):
        by_country.setdefault(url.rsplit("/jobs/", 1)[1].split("/")[0], []).append(params)

    assert all("what_phrase" not in p for p in by_country["gb"])
    for country, calls in by_country.items():
        if country == "gb":
            continue
        assert all(p.get("what_phrase") == "remote" for p in calls), country


def test_the_limit_samples_regions_instead_of_exhausting_the_first(creds, monkeypatch):
    """Nested region/query loops would spend the whole limit on the UK.

    Same prefix-vs-sample bug the run cap had: with 10 regions and a limit of 50, the
    UK's queries fill it and nine countries are never requested — every run, silently.
    """
    src = ContractJobsSource(queries=("a", "b"))
    order = [region.country for region, _, _ in src._slices()]

    # The first round covers one slice of every region before any region's second.
    assert order[:10] == ["gb", "us", "au", "de", "nl", "fr", "nz", "ch", "at", "be"]
    # And nothing is dropped: every (region, location, query) still gets requested.
    assert len(order) == sum(len(r.locations) * 2 for r in src.regions)


def test_a_job_id_is_namespaced_by_country(creds, monkeypatch):
    """Adzuna ids are unique per country endpoint, not globally.

    A bare id lets a German role collide with a British one and be dropped as a
    duplicate — silently, because dedupe logs nothing.
    """
    _install(monkeypatch, PAYLOAD)
    de = Region("de", "Germany", ("Germany",), remote_only=True)
    src = ContractJobsSource(queries=("devops",), regions=(de,))
    ids = {lead.external_id for lead in src.fetch(limit=10)}

    assert ids == {"de:1001", "de:1002"}


def test_the_same_id_in_two_countries_yields_two_leads(creds, monkeypatch):
    """The point of namespacing: two real roles must not collapse into one."""
    _install(monkeypatch, PAYLOAD)
    src = ContractJobsSource(
        queries=("devops",),
        regions=(
            Region("gb", "UK", ("London",), remote_only=False),
            Region("us", "US", ("Remote",), remote_only=True),
        ),
    )
    ids = {lead.external_id for lead in src.fetch(limit=10)}

    assert {"gb:1001", "us:1001"} <= ids


def test_salary_carries_the_currency_of_its_country(creds, monkeypatch):
    """A US salary rendered with "£" is a wrong number in a proposal."""
    _install(monkeypatch, PAYLOAD)
    for country, symbol in (("us", "$"), ("de", "€"), ("au", "A$"), ("gb", "£")):
        region = Region(country, country.upper(), (country,), remote_only=country != "gb")
        src = ContractJobsSource(queries=("devops",), regions=(region,))
        budgets = [lead.budget for lead in src.fetch(limit=10) if lead.budget]
        assert budgets and all(b.startswith(symbol) for b in budgets), country


def test_the_region_is_tagged_on_every_lead(creds, monkeypatch):
    """The proposal writer needs to know the market; "remote, Germany" changes the pitch."""
    _install(monkeypatch, PAYLOAD)
    de = Region("de", "Germany", ("Germany",), remote_only=True)
    leads = ContractJobsSource(queries=("devops",), regions=(de,)).fetch(limit=10)

    assert leads and all("Germany" in lead.tags for lead in leads)


def test_passing_locations_keeps_the_old_uk_only_behaviour(creds, monkeypatch):
    """`locations=` is the pre-multi-region signature and must not fan out to 10 countries."""
    urls: list[str] = []
    _install(monkeypatch, {"results": []}, urls=urls)
    ContractJobsSource(queries=("devops",), locations=("London",)).fetch(limit=50)

    assert {u.rsplit("/jobs/", 1)[1].split("/")[0] for u in urls} == {"gb"}


# --------------------------------------------------------------------------- #
# transient failures vs real ones
# --------------------------------------------------------------------------- #
def _install_statuses(monkeypatch, statuses, payload=None):
    """Return the given status codes in order, then 200 forever. Counts calls."""
    calls = {"n": 0}
    seq = list(statuses)

    def fake_get(self, url, params=None):
        i = calls["n"]
        calls["n"] += 1
        status = seq[i] if i < len(seq) else 200
        return FakeResponse(payload if payload is not None else PAYLOAD, status)

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr(contract_jobs.time, "sleep", lambda _s: None)
    return calls


def test_a_transient_503_is_retried_and_not_reported_broken(creds, monkeypatch):
    """Adzuna's free tier emits sporadic 503s; losing the slice is bad, but calling
    the source broken is worse — it teaches you to ignore the verdict that means
    "there is a bug", which is exactly how the real 400 survived a month."""
    calls = _install_statuses(monkeypatch, [503, 503])
    src = ContractJobsSource(
        queries=("devops",), regions=(Region("gb", "UK", ("London",), False),)
    )
    leads = src.fetch(limit=10)

    assert calls["n"] == 3, "two failures then a success"
    assert leads, "the retry must actually return the payload"
    assert src.last_error is None, "a recovered slice is not an error"


def test_retries_are_bounded_and_the_failure_is_still_reported(creds, monkeypatch):
    """Retrying forever would hang the run; the error must survive the last attempt."""
    calls = _install_statuses(monkeypatch, [503, 503, 503, 503, 503])
    src = ContractJobsSource(
        queries=("devops",), regions=(Region("gb", "UK", ("London",), False),)
    )

    assert src.fetch(limit=10) == []
    assert calls["n"] == contract_jobs.RETRIES + 1
    assert src.last_error and "503" in src.last_error


def test_a_400_is_not_retried(creds, monkeypatch):
    """An invalid request fails identically forever.

    Retrying it wastes the run and delays the report that would fix it — this is the
    exact failure mode of `contract_only`, which was a permanent 400.
    """
    calls = _install_statuses(monkeypatch, [400, 400, 400])
    src = ContractJobsSource(
        queries=("devops",), regions=(Region("gb", "UK", ("London",), False),)
    )

    assert src.fetch(limit=10) == []
    assert calls["n"] == 1, "a 400 must be believed the first time"
    assert src.last_error and "400" in src.last_error


def test_a_404_country_is_not_retried(creds, monkeypatch):
    """ie/se/no/dk/fi have no Adzuna endpoint. Retrying a nonexistent country is waste."""
    calls = _install_statuses(monkeypatch, [404, 404, 404])
    src = ContractJobsSource(
        queries=("devops",), regions=(Region("ie", "Ireland", ("",), True),)
    )

    assert src.fetch(limit=10) == []
    assert calls["n"] == 1


def test_one_bad_region_does_not_lose_the_others(creds, monkeypatch):
    """A single failing country must not zero out the whole source."""
    def fake_get(self, url, params=None):
        country = url.rsplit("/jobs/", 1)[1].split("/")[0]
        return FakeResponse(PAYLOAD, 400 if country == "gb" else 200)

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    src = ContractJobsSource(
        queries=("devops",),
        regions=(
            Region("gb", "UK", ("London",), False),
            Region("us", "US", ("",), True),
        ),
    )
    leads = src.fetch(limit=10)

    assert leads and all(lead.external_id.startswith("us:") for lead in leads)
    # ...but the failure is still reported, or a half-working source looks healthy.
    assert src.last_error and "400" in src.last_error


# --------------------------------------------------------------------------- #
# robustness
# --------------------------------------------------------------------------- #
def test_dedupes_the_same_job_across_queries(creds, monkeypatch):
    """London and UK searches overlap heavily; one job must yield one lead."""
    _install(monkeypatch, PAYLOAD)
    src = ContractJobsSource(queries=("devops", "sre"), locations=("London", "UK"))
    ids = [lead.external_id for lead in src.fetch(limit=50)]
    assert ids == sorted(set(ids), key=ids.index)
    assert len(ids) == 2


def test_only_parameters_the_real_api_accepts_are_sent(creds, monkeypatch):
    """Every parameter name must be one Adzuna actually documents.

    This is the test that was missing. ``contract_only=1`` was sent for the life of
    this source and the real API answered *every* request with a 400 HTML error page,
    yet the assertion ``params["contract_only"] == 1`` passed — the mock accepted any
    name a real server would reject, so the test pinned the bug in place instead of
    catching it. Asserting the corrected name is not enough; the mock has to be as
    strict as the server, or the next invented parameter passes too.
    """
    # Verified one-by-one against the live API on 2026-08-05, NOT copied from the
    # docs: `location1`, `sort_dir`, `company` and `page` are all documented and all
    # answer 400. Documentation is what produced `contract_only` in the first place,
    # so this list is measurement.
    allowed = {
        "app_id", "app_key", "what", "what_and", "what_or", "what_phrase",
        "what_exclude", "title_only", "where", "distance", "location0",
        "max_days_old", "category", "sort_by", "salary_min", "salary_max",
        "salary_include_unknown", "full_time", "part_time", "contract",
        "permanent", "results_per_page", "content-type",
    }
    captured: list[dict] = []
    _install(monkeypatch, PAYLOAD, capture=captured)
    ContractJobsSource().fetch(limit=5)

    assert captured, "no request was made"
    for params in captured:
        unknown = set(params) - allowed
        assert not unknown, f"Adzuna would answer 400 for: {sorted(unknown)}"


def test_a_rejected_request_is_reported_as_broken_not_empty(creds, monkeypatch):
    """An HTTP error must leave a reason behind, not just an empty list.

    Returning [] in silence made "the API rejected our filter" identical to "no jobs
    matched": the funnel report called it ``dead: fetched nothing`` and pointed at the
    queries, while the actual fix was one parameter name in this file.
    """
    _install(monkeypatch, PAYLOAD, fail=True)
    src = ContractJobsSource(queries=("devops",), locations=("London",))
    assert src.fetch() == []
    assert src.last_error, "a failed fetch must record why"
    assert "ConnectError" in src.last_error


def test_a_recovered_source_stops_reporting_a_stale_error(creds, monkeypatch):
    """last_error is per-fetch, so a fixed source doesn't look broken forever."""
    _install(monkeypatch, PAYLOAD, fail=True)
    src = ContractJobsSource(queries=("devops",), locations=("London",))
    src.fetch()
    assert src.last_error

    _install(monkeypatch, PAYLOAD)
    assert src.fetch(limit=5)
    assert src.last_error is None


def test_an_unconfigured_source_says_so_rather_than_looking_dead(monkeypatch):
    """No key is a different problem from no results, so it gets its own reason."""
    monkeypatch.setenv("COPILOT_ADZUNA_APP_ID", "")
    monkeypatch.setenv("COPILOT_ADZUNA_APP_KEY", "")
    src = ContractJobsSource(queries=("devops",), locations=("London",))
    assert src.fetch() == []
    assert src.last_error and "not configured" in src.last_error


def test_network_error_returns_empty(creds, monkeypatch):
    _install(monkeypatch, PAYLOAD, fail=True)
    assert ContractJobsSource(queries=("devops",), locations=("London",)).fetch() == []


def test_malformed_payload_does_not_raise(creds, monkeypatch):
    for payload in ({}, {"results": None}, {"results": ["not-a-dict"]}, []):
        _install(monkeypatch, payload)
        src = ContractJobsSource(queries=("devops",), locations=("London",))
        assert src.fetch(limit=5) == []


def test_respects_limit(creds, monkeypatch):
    _install(monkeypatch, PAYLOAD)
    src = ContractJobsSource(queries=("devops",), locations=("London",))
    assert len(src.fetch(limit=1)) == 1
