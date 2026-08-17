"""Offline tests for contact discovery (``outreach/discover.py``).

Fully hermetic: HTTP is an :class:`httpx.MockTransport` (so every request is recorded
and asserted on, and none leaves the machine) and DNS is monkeypatched at
``discover.domain_accepts_mail``. Nothing here touches a real host, which matters more
than usual for this module — the code under test fetches *other people's* sites, so a
test that accidentally hit the network would be a defect in itself.

What these pin, in the order the module's docstring argues them:

* an address is only ever returned when a page **published** it (never constructed);
* the accept/reject decision is :func:`outreach.extract.find_contact_email`'s, reused,
  so a ``support@`` page yields nothing without this module knowing why;
* the address has to be on the company's own domain, and an aggregator host
  (greenhouse, jobicy, …) can never *become* the company domain;
* ``robots.txt``, the page budget, the body cap and the per-domain cache are all
  honoured, because this is someone else's server;
* every failure mode returns ``None`` rather than raising.
"""
from __future__ import annotations

import httpx
import pytest

import outreach.discover as discover
import outreach.extract as extract
from core.schemas import Lead

CONTACT_HTML = (
    "<html><head><title>Contact</title></head><body>"
    "<p>Talk to us about contract work: "
    '<a href="mailto:hello@acme.com">Email us</a></p>'
    "</body></html>"
)


class Site:
    """A fake web: a dict of ``"host/path" -> html``, plus a request log."""

    def __init__(
        self,
        pages: dict[str, str] | None = None,
        *,
        robots: str | None = None,
        statuses: dict[str, int] | None = None,
        errors: dict[str, Exception] | None = None,
        default_status: int = 404,
        default_error: Exception | None = None,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self.pages = pages or {}
        self.robots = robots
        self.statuses = statuses or {}
        self.errors = errors or {}
        self.default_status = default_status
        self.default_error = default_error
        self.content_type = content_type
        self.requests: list[str] = []
        self.user_agents: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = f"{request.url.host}{request.url.path}"
        self.requests.append(key)
        self.user_agents.append(request.headers.get("user-agent", ""))
        if key in self.errors:
            raise self.errors[key]
        if request.url.path == "/robots.txt":
            if self.robots is None:
                return httpx.Response(404, text="")
            return httpx.Response(200, text=self.robots, headers={"content-type": "text/plain"})
        if self.default_error is not None:
            raise self.default_error
        if key in self.statuses:
            return httpx.Response(self.statuses[key], text="")
        if key in self.pages:
            return httpx.Response(
                200, text=self.pages[key], headers={"content-type": self.content_type}
            )
        return httpx.Response(self.default_status, text="not found")

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    @property
    def fetched_pages(self) -> list[str]:
        return [r for r in self.requests if not r.endswith("/robots.txt")]


@pytest.fixture(autouse=True)
def _clean_caches():
    """Discovery caches are per-process; a leaked entry would silently pass a test."""
    discover.clear_caches()
    extract._MX_CACHE.clear()
    yield
    discover.clear_caches()
    extract._MX_CACHE.clear()


@pytest.fixture(autouse=True)
def _mx_ok(monkeypatch):
    """Every domain accepts mail unless a test says otherwise. No resolver is used."""
    monkeypatch.setattr(discover, "domain_accepts_mail", lambda domain: True)


@pytest.fixture(autouse=True)
def _discovery_enabled(monkeypatch):
    """Turn the feature flag ON for this file, since the suite pins it OFF.

    ``tests/conftest.py`` disables ``discover_contacts`` for every test because this
    module makes REAL outbound requests and it is on by default in production — unpinned,
    it reached the live internet from an unrelated pipeline test and returned a stranger's
    address. This file supplies its own web (``Site`` + ``MockTransport``), so it opts
    back in explicitly: the flag is what is under test here, not what is being avoided.
    """
    monkeypatch.setenv("COPILOT_DISCOVER_CONTACTS", "true")


def _lead(
    url: str = "https://acme.com/careers/devops-contract",
    company: str | None = "Acme Corp",
    external_id: str = "job-1",
) -> Lead:
    return Lead(
        source="contract_jobs",
        external_id=external_id,
        title="Kubernetes + Terraform hardening (contract)",
        description="6 week engagement hardening our EKS platform. Apply on our site.",
        url=url,
        company=company,
    )


# --- the happy path ------------------------------------------------------------------


def test_a_published_mailto_on_the_contact_page_is_discovered_with_its_source_url():
    site = Site({"acme.com/contact": CONTACT_HTML})
    # The address exists ONLY inside the href, which is the common real shape and the
    # thing naive tag-stripping destroys.
    assert CONTACT_HTML.count("hello@acme.com") == 1
    assert "mailto:hello@acme.com" in CONTACT_HTML

    with site.client() as client:
        found = discover.discover_contact(_lead(), client=client)

    assert found is not None
    assert found.email == "hello@acme.com"
    assert found.domain == "acme.com"
    assert found.source_url == "https://acme.com/contact"
    assert site.fetched_pages == ["acme.com/contact"]


def test_the_crawl_stops_at_the_first_accepted_address():
    site = Site(
        {
            "acme.com/contact": CONTACT_HTML,
            "acme.com/about": '<a href="mailto:other@acme.com">us</a>',
        }
    )
    with site.client() as client:
        found = discover.discover_contact(_lead(), client=client)

    assert found is not None and found.email == "hello@acme.com"
    assert "acme.com/about" not in site.requests


def test_a_subdomain_address_still_counts_as_the_company_domain():
    site = Site(
        {
            "acme.com/contact": (
                '<p>Reach out: <a href="mailto:hello@mail.acme.com">mail us</a></p>'
            )
        }
    )
    with site.client() as client:
        found = discover.discover_contact(_lead(), client=client)

    assert found is not None
    assert found.email == "hello@mail.acme.com"
    assert found.domain == "acme.com"


def test_the_user_agent_names_the_tool_and_the_owners_site():
    site = Site({"acme.com/contact": CONTACT_HTML})
    with site.client() as client:
        discover.discover_contact(_lead(), client=client)

    assert site.user_agents, "no request was made"
    for agent in site.user_agents:
        assert "ai-freelance-copilot" in agent
        assert "suryaanandan1995-dotcom.github.io" in agent


# --- the gate is shared, not reimplemented -------------------------------------------


def test_a_support_only_page_yields_nothing_because_the_shared_gate_rejects_it():
    """``support@`` is rejected by ``extract``'s local-part rules, which this module
    never re-implements. If discovery ever returns this address, the two gates have
    drifted apart and institutional mailboxes are reachable again."""
    site = Site({"acme.com/contact": "<p>Questions? support@acme.com</p>"})
    with site.client() as client:
        assert discover.discover_contact(_lead(), client=client) is None
    # ...and the identical string is what the shared gate rejects on its own:
    probe = Lead(source="discover", external_id="p", title="t", description="support@acme.com")
    assert extract.find_contact_email(probe) is None


def test_an_accommodations_desk_on_the_contact_page_is_not_discovered():
    """The measured worst case from ``extract``'s history, now reachable by crawling."""
    site = Site(
        {
            "acme.com/contact": (
                "<p>If you require an accommodation due to a disability, email "
                '<a href="mailto:recruiting@acme.com">recruiting@acme.com</a>.</p>'
            )
        }
    )
    with site.client() as client:
        assert discover.discover_contact(_lead(), client=client) is None


def test_an_address_on_a_different_domain_than_the_company_is_rejected():
    site = Site(
        {"acme.com/contact": '<p>Email us: <a href="mailto:hello@partner.io">here</a></p>'}
    )
    with site.client() as client:
        assert discover.discover_contact(_lead(), client=client) is None


def test_the_companys_address_still_wins_when_an_off_domain_one_outranks_it():
    """``find_contact_email`` returns one winner, and a hiring-stemmed off-domain local
    outranks the company's ``hello@``. The rejected literal is blanked and the *same*
    gate asked again, rather than re-ranking here."""
    site = Site(
        {
            "acme.com/contact": (
                "<p>Recruiters: jobs@partner.io. Everyone else, email us at "
                "hello@acme.com</p>"
            )
        }
    )
    with site.client() as client:
        found = discover.discover_contact(_lead(), client=client)

    assert found is not None and found.email == "hello@acme.com"


def test_no_address_is_ever_constructed_from_a_domain():
    """A site with contact pages that name the domain everywhere and publish no
    mailbox yields ``None``. Guessing ``careers@acme.com`` here would bounce, and a
    bounce is charged to the sending domain."""
    body = "<p>Contact us through the form at acme.com/contact. Careers at acme.com.</p>"
    site = Site({f"acme.com{p}": body for p in ("/contact", "/contact-us", "/about", "/careers")})
    with site.client() as client:
        assert discover.discover_contact(_lead(), client=client) is None


def test_a_domain_with_no_mx_records_is_not_discovered_and_is_not_even_crawled(monkeypatch):
    monkeypatch.setattr(discover, "domain_accepts_mail", lambda domain: False)
    site = Site({"acme.com/contact": CONTACT_HTML})
    with site.client() as client:
        assert discover.discover_contact(_lead(), client=client) is None

    assert site.requests == [], "a mail-less domain must not cost the owner a page view"


# --- domain resolution ---------------------------------------------------------------


def test_a_blocklisted_lead_url_never_becomes_the_company_domain():
    site = Site({"boards.greenhouse.io/contact": '<a href="mailto:hi@greenhouse.io">x</a>'})
    lead = _lead(url="https://boards.greenhouse.io/acmecorp/jobs/4102", company=None)
    with site.client() as client:
        assert discover.discover_contact(lead, client=client) is None

    assert site.requests == [], "the ATS host was crawled as if it were the company"


def test_the_company_name_derives_a_verified_domain_when_the_post_is_on_a_board():
    home = (
        "<html><body><h1>Acme Robotics</h1><p>We build warehouse robots.</p>"
        '<footer><a href="mailto:hello@acmerobotics.com">Contact</a></footer>'
        "</body></html>"
    )
    site = Site({"acmerobotics.com/": home})
    lead = _lead(url="https://jobicy.com/jobs/9912", company="Acme Robotics Ltd")
    with site.client() as client:
        found = discover.discover_contact(lead, client=client)

    assert found is not None
    assert found.email == "hello@acmerobotics.com"
    assert found.source_url == "https://acmerobotics.com/"
    # robots + the one homepage. The verified homepage is re-read from memory, so
    # verification and extraction do not each pay for it.
    assert site.requests == ["acmerobotics.com/robots.txt", "acmerobotics.com/"]


def test_a_parking_page_is_never_accepted_as_the_company_domain():
    parked = (
        "<h1>acmerobotics.com</h1><p>This domain is for sale. Buy this domain now.</p>"
        '<a href="mailto:owner@acmerobotics.com">Make an offer</a>'
    )
    site = Site({"acmerobotics.com/": parked})
    lead = _lead(url="https://jobicy.com/jobs/9912", company="Acme Robotics Ltd")
    with site.client() as client:
        assert discover.discover_contact(lead, client=client) is None

    assert not any("/contact" in r for r in site.requests)


def test_a_homepage_that_never_mentions_the_company_is_rejected():
    squatter = "<h1>Bob's Plumbing Supplies</h1><p>Taps and fittings. bob@acmerobotics.com</p>"
    site = Site({"acmerobotics.com/": squatter})
    lead = _lead(url="https://jobicy.com/jobs/9912", company="Acme Robotics Ltd")
    with site.client() as client:
        assert discover.discover_contact(lead, client=client) is None


def test_a_lead_with_neither_a_usable_url_nor_a_company_name_makes_no_requests():
    site = Site({})
    lead = _lead(url="", company=None)
    with site.client() as client:
        assert discover.discover_contact(lead, client=client) is None
    assert site.requests == []


# --- robots.txt ----------------------------------------------------------------------


def test_robots_disallowing_the_contact_path_skips_it_and_tries_the_next_page():
    site = Site(
        {
            "acme.com/contact": CONTACT_HTML,
            "acme.com/about": '<p>Say hi: <a href="mailto:hello@acme.com">hello</a></p>',
        },
        robots="User-agent: *\nDisallow: /contact\n",
    )
    with site.client() as client:
        found = discover.discover_contact(_lead(), client=client)

    assert found is not None
    assert found.source_url == "https://acme.com/about"
    assert "acme.com/contact" not in site.requests
    assert "acme.com/contact-us" not in site.requests  # the prefix covers it too


def test_robots_disallowing_everything_aborts_the_domain_before_any_page_is_fetched():
    site = Site({"acme.com/contact": CONTACT_HTML}, robots="User-agent: *\nDisallow: /\n")
    with site.client() as client:
        assert discover.discover_contact(_lead(), client=client) is None

    assert site.requests == ["acme.com/robots.txt"]


def test_a_rule_naming_this_crawler_beats_the_wildcard_group():
    site = Site(
        {"acme.com/contact": CONTACT_HTML},
        robots=(
            "User-agent: *\nDisallow:\n\n"
            "User-agent: ai-freelance-copilot\nDisallow: /\n"
        ),
    )
    with site.client() as client:
        assert discover.discover_contact(_lead(), client=client) is None
    assert site.fetched_pages == []


def test_an_allow_rule_reinstates_a_page_inside_a_narrower_disallowed_prefix():
    site = Site(
        {"acme.com/contact-us": '<p>Email us: <a href="mailto:hello@acme.com">hi</a></p>'},
        robots="User-agent: *\nDisallow: /contact\nAllow: /contact-us\n",
    )
    with site.client() as client:
        found = discover.discover_contact(_lead(), client=client)

    assert found is not None and found.email == "hello@acme.com"
    assert "acme.com/contact" not in site.requests  # still blocked; only the Allow won


def test_a_disallow_all_with_an_allow_carve_out_still_aborts_the_domain():
    """The carve-out in a ``Disallow: /`` file is written for search engines. Reading it
    as an invitation addressed to this crawler is the liberty robots.txt withholds."""
    site = Site(
        {"acme.com/contact": CONTACT_HTML},
        robots="User-agent: *\nDisallow: /\nAllow: /contact\n",
    )
    with site.client() as client:
        assert discover.discover_contact(_lead(), client=client) is None
    assert site.fetched_pages == []


def test_a_robots_fetch_failure_means_proceed_rather_than_abort():
    site = Site(
        {"acme.com/contact": CONTACT_HTML},
        errors={"acme.com/robots.txt": httpx.ConnectError("no route")},
    )
    with site.client() as client:
        found = discover.discover_contact(_lead(), client=client)

    assert found is not None and found.email == "hello@acme.com"


def test_robots_is_fetched_once_per_domain_not_once_per_path():
    site = Site({}, robots="User-agent: *\nDisallow:\n")
    with site.client() as client:
        discover.discover_contact(_lead(), client=client)

    assert site.requests.count("acme.com/robots.txt") == 1


# --- failure modes never raise -------------------------------------------------------


def test_a_500_on_every_page_returns_none_without_raising():
    site = Site({}, default_status=500)
    with site.client() as client:
        assert discover.discover_contact(_lead(), client=client) is None


def test_a_timeout_returns_none_without_raising():
    site = Site({}, default_error=httpx.ReadTimeout("too slow"))
    with site.client() as client:
        assert discover.discover_contact(_lead(), client=client) is None


def test_malformed_html_returns_none_without_raising():
    junk = "<<<div <p>>> <a href='mailto:'> <script>var s = '</p>';</script> <b>contact"
    site = Site({f"acme.com{p}": junk for p in discover.CONTACT_PATHS})
    with site.client() as client:
        assert discover.discover_contact(_lead(), client=client) is None


def test_an_unexpected_internal_failure_is_swallowed(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(discover, "_resolve_domain", boom)
    site = Site({"acme.com/contact": CONTACT_HTML})
    with site.client() as client:
        assert discover.discover_contact(_lead(), client=client) is None


def test_a_non_text_response_is_not_parsed():
    site = Site({"acme.com/contact": CONTACT_HTML}, content_type="application/pdf")
    with site.client() as client:
        assert discover.discover_contact(_lead(), client=client) is None


def test_a_redirect_onto_an_aggregator_does_not_yield_that_hosts_address():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="")
        if request.url.host == "acme.com" and request.url.path == "/contact":
            return httpx.Response(302, headers={"location": "https://linkedin.com/company/acme"})
        return httpx.Response(
            200,
            text='<a href="mailto:hello@linkedin.com">x</a>',
            headers={"content-type": "text/html"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        found = discover.discover_contact(_lead(), client=client)

    assert found is None or found.domain == "acme.com"


# --- budget, body cap, caching -------------------------------------------------------


def test_the_page_budget_is_respected(monkeypatch):
    monkeypatch.setenv("COPILOT_MAX_PAGES_PER_COMPANY", "2")
    site = Site({})  # every page 404s, so the crawl spends its whole budget
    with site.client() as client:
        assert discover.discover_contact(_lead(), client=client) is None

    assert site.fetched_pages == ["acme.com/contact", "acme.com/contact-us"]
    assert len(site.requests) == 3  # + one robots.txt


def test_an_address_past_the_body_cap_is_never_read():
    padding = "<p>" + ("acme " * 200_000) + "</p>"
    assert len(padding.encode()) > discover.MAX_BODY_BYTES
    site = Site({"acme.com/contact": padding + '<a href="mailto:hello@acme.com">x</a>'})
    with site.client() as client:
        assert discover.discover_contact(_lead(), client=client) is None


def test_a_second_lead_for_the_same_company_issues_no_further_requests():
    home = (
        "<h1>Acme Robotics</h1><p>Warehouse robots.</p>"
        '<a href="mailto:hello@acmerobotics.com">Contact</a>'
    )
    site = Site({"acmerobotics.com/": home})
    first = _lead(url="https://jobicy.com/jobs/1", company="Acme Robotics Ltd", external_id="a")
    second = _lead(url="https://jobicy.com/jobs/2", company="Acme Robotics Ltd", external_id="b")

    with site.client() as client:
        found_a = discover.discover_contact(first, client=client)
        after_first = list(site.requests)
        found_b = discover.discover_contact(second, client=client)

    assert found_a == found_b
    assert found_b is not None and found_b.email == "hello@acmerobotics.com"
    assert site.requests == after_first, "the second lead re-crawled the same company"


def test_a_company_with_no_published_address_is_not_re_crawled_for_the_next_lead():
    """The negative answer is cached too: re-deriving "nobody home" costs the owner of
    that site another five page views for no new information."""
    site = Site({})
    with site.client() as client:
        assert discover.discover_contact(_lead(external_id="a"), client=client) is None
        after_first = list(site.requests)
        assert discover.discover_contact(_lead(external_id="b"), client=client) is None

    assert site.requests == after_first


def test_clear_caches_lets_a_later_run_crawl_again():
    site = Site({"acme.com/contact": CONTACT_HTML})
    with site.client() as client:
        discover.discover_contact(_lead(), client=client)
        count = len(site.requests)
        discover.clear_caches()
        discover.discover_contact(_lead(), client=client)

    assert len(site.requests) == count * 2


def test_discovery_is_a_no_op_when_the_feature_is_switched_off(monkeypatch):
    monkeypatch.setenv("COPILOT_DISCOVER_CONTACTS", "false")
    site = Site({"acme.com/contact": CONTACT_HTML})
    with site.client() as client:
        assert discover.discover_contact(_lead(), client=client) is None
    assert site.requests == []


# --- host helpers --------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "boards.greenhouse.io",
        "jobs.lever.co",
        "acme.myworkdayjobs.com",
        "www.linkedin.com",
        "jobicy.com",
        "weworkremotely.com",
        "acme.github.io",
        "gmail.com",
        "x.com",
    ],
)
def test_aggregator_and_freemail_hosts_are_blocklisted(host):
    assert discover._is_blocked_host(host)


@pytest.mark.parametrize("host", ["acme.com", "acmerobotics.io", "acme.co.uk", "jobs.acme.com"])
def test_ordinary_company_hosts_are_not_blocklisted(host):
    assert not discover._is_blocked_host(host)


@pytest.mark.parametrize(
    ("address_domain", "company_domain", "expected"),
    [
        ("acme.com", "acme.com", True),
        ("mail.acme.com", "acme.com", True),
        ("acme.com", "www.acme.com", True),
        ("acme.com", "jobs.acme.com", True),
        ("partner.io", "acme.com", False),
        ("acme.co.uk", "other.co.uk", False),
        ("acmecorp.com", "acme.com", False),
    ],
)
def test_domain_matching_allows_subdomains_but_not_strangers(
    address_domain, company_domain, expected
):
    assert discover._same_company(address_domain, company_domain) is expected


@pytest.mark.parametrize(
    ("company", "slug"),
    [
        ("Acme Robotics Ltd", "acmerobotics"),
        ("Acme Robotics Pty Ltd", "acmerobotics"),
        ("Acme, Inc.", "acme"),
        ("Beispiel GmbH", "beispiel"),
        ("acme.com", "acme"),
        ("Co", ""),
        (None, ""),
        ("", ""),
    ],
)
def test_a_company_name_reduces_to_a_domain_label(company, slug):
    assert discover._company_slug(company) == slug


def test_mailto_hrefs_survive_html_stripping_and_scripts_do_not():
    html = (
        "<script>var email = 'tracker@analytics.io';</script>"
        '<p>Write to <a class="btn" href="mailto:hello@acme.com?subject=Hi">us</a>&nbsp;today</p>'
    )
    text = discover._html_to_text(html)
    assert "hello@acme.com" in text
    assert "tracker@analytics.io" not in text
    assert "subject=Hi" not in text
    assert "<" not in text and "&nbsp;" not in text
