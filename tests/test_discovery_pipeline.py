"""How the pipeline uses contact discovery, and what it refuses to do with it.

``outreach/discover.py`` has its own hermetic suite (``test_discover.py``) covering how
an address is found. This file covers the decision the *caller* owns: whether a found
address may be mailed.

That decision exists because discovery resolves a company domain two ways, and only one
of them is evidence:

* the post is hosted on the company's own site — the domain is where the listing was
  published, so mailing it means replying to the party that posted;
* the post is on a job board, so the domain is derived from the company NAME
  ("Acme Corp" -> acme.com -> .io -> .ai) and accepted if the homepage mentions the
  company. That is the whole 262-lead population discovery was built for, and it is
  also the path that can email a stranger.

The second path was measured the first time it ever ran — by accident, from an unrelated
unit test with discovery unpinned — and it resolved a fixture's "Acme Corp" to the real
acme.com and returned ``frobozz07@mail.acme.com``. First try. Generic company names all
have a .com owner who is not the client, and a spam complaint from one of them burns the
sending domain, which is the one failure in this project that later code cannot undo.

So guessed addresses are proposed to the owner and not sent, and these tests pin that
they are still *reported* — an address nobody ever sees can never be evaluated, and the
whole point of proposing is to earn the right to send.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
import outreach.discover as discover
from db.models import Base, OutreachRecord
from tests.test_contact_gate import (  # reuse the proven pipeline scaffolding
    FakeRetriever,
    FakeSource,
    _email_chat,
)

BOARD_URL = "https://www.jobicy.com/jobs/12345-devops-contract"
OWN_SITE_URL = "https://acme.com/careers/devops-contract"


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'discovery.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    yield engine


def _lead(url: str = OWN_SITE_URL):
    """A qualified-looking lead whose post publishes NO address."""
    from core.schemas import Lead

    return Lead(
        source="contract_jobs",
        external_id="job-1",
        title="Kubernetes + DevSecOps hardening",
        description="6 week contract hardening our EKS platform. Apply on our site.",
        company="Acme Corp",
        url=url,
        budget="$90/hr",
        tags=["kubernetes", "devsecops"],
    )


def _settings(monkeypatch, **overrides):
    """Patch the settings ``pipeline`` sees (it holds its own imported reference)."""
    import config
    import pipeline

    real = config.get_settings

    def patched():
        cfg = real()
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg

    monkeypatch.setattr(pipeline, "get_settings", patched)
    return patched


def _finds(email: str, domain: str, page: str):
    """A stub discovery that always returns one contact, and counts its calls."""
    calls: list[str] = []

    def _discover(lead, **kw):
        calls.append(lead.external_id)
        return discover.DiscoveredContact(email=email, domain=domain, source_url=page)

    _discover.calls = calls  # type: ignore[attr-defined]
    return _discover


@pytest.fixture(autouse=True)
def _no_send(monkeypatch):
    """Nothing leaves the box, and every send 'succeeds' so counters are readable."""
    import outreach.sender as sender

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sender, "send_outreach", lambda to, subject, body: (sent.append((to, subject)), True)[1]
    )
    return sent


# --------------------------------------------------------------------------- #
# the trusted path: the post is on the company's own site
# --------------------------------------------------------------------------- #
def test_an_address_on_the_posts_own_domain_is_emailed(temp_db, monkeypatch, _no_send):
    """The strongest case discovery has, and the one it is allowed to act on alone.

    Also pins the re-run: the lead was scored with ``draft_allowed=False`` because it had
    no address, so without a second pass there is a sendable address and no proposal to
    send. ``emailed == 1`` is what proves the re-run happened.
    """
    from pipeline import run_pipeline

    _settings(monkeypatch, discover_contacts=True, max_contact_discoveries_per_run=10)
    stub = _finds("hello@acme.com", "acme.com", "https://acme.com/contact")
    monkeypatch.setattr(discover, "discover_contact", stub)

    stats = run_pipeline(
        sources=[FakeSource([_lead()])],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )

    assert stats["discovery_attempts"] == 1
    assert stats["discovered"] == 1
    assert stats["queued"] == 1
    assert stats["emailed"] == 1
    assert _no_send[0][0] == "hello@acme.com"
    # And it is NOT counted as contactable: that column means "the listing published an
    # address", which is the measurement the whole qualified-vs-reachable split rests on.
    assert stats["contactable"] == 0
    assert stats["by_source"]["contract_jobs"]["discovered"] == 1
    # Nothing left for the owner to do by hand.
    assert stats["apply_yourself"] == []
    assert stats["proposed_contacts"] == []
    with dbsession.get_session() as session:
        assert session.query(OutreachRecord).count() == 1


def test_a_subdomain_of_the_posts_domain_is_still_the_same_company(temp_db, monkeypatch):
    """``jobs.acme.com`` publishing ``hello@acme.com`` is one company, not two."""
    from pipeline import run_pipeline

    _settings(monkeypatch, discover_contacts=True, max_contact_discoveries_per_run=10)
    monkeypatch.setattr(
        discover,
        "discover_contact",
        _finds("hello@acme.com", "acme.com", "https://acme.com/contact"),
    )

    stats = run_pipeline(
        sources=[FakeSource([_lead("https://jobs.acme.com/openings/9")])],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )
    assert stats["discovered"] == 1
    assert stats["proposed_contacts"] == []


# --------------------------------------------------------------------------- #
# the guessed path: proposed, never sent
# --------------------------------------------------------------------------- #
def test_an_address_on_a_guessed_domain_is_proposed_and_not_emailed(
    temp_db, monkeypatch, _no_send
):
    """The acme.com case, as a test instead of an accident.

    The post is on a job board, so nothing about it vouches for ``acme.com``. The address
    is reported with the page it came from — which is what a human needs to bin it in two
    seconds — and no message is sent.
    """
    from pipeline import run_pipeline

    _settings(monkeypatch, discover_contacts=True, max_contact_discoveries_per_run=10)
    monkeypatch.setattr(
        discover,
        "discover_contact",
        _finds("frobozz07@mail.acme.com", "acme.com", "https://acme.com/"),
    )

    stats = run_pipeline(
        sources=[FakeSource([_lead(BOARD_URL)])],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )

    assert stats["emailed"] == 0
    assert _no_send == []
    assert stats["discovered"] == 0, "a guessed hit is not a usable address"
    assert stats["discovery_attempts"] == 1, "but the lookup still happened"
    proposed = stats["proposed_contacts"]
    assert len(proposed) == 1
    assert proposed[0]["email"] == "frobozz07@mail.acme.com"
    # Both links, because the judgement is "does this page belong to that post".
    assert proposed[0]["source_url"] == "https://acme.com/"
    assert proposed[0]["lead_url"] == BOARD_URL
    # And the lead still reaches the owner the way it did before discovery existed.
    assert [r["url"] for r in stats["apply_yourself"]] == [BOARD_URL]
    with dbsession.get_session() as session:
        assert session.query(OutreachRecord).count() == 0


def test_the_owner_can_turn_guessed_domains_into_sends(temp_db, monkeypatch, _no_send):
    """The setting is a real switch, not a comment.

    It ships off, and it exists so the decision can be revisited from the proposals
    rather than re-argued from the heuristic.
    """
    from pipeline import run_pipeline

    _settings(
        monkeypatch,
        discover_contacts=True,
        max_contact_discoveries_per_run=10,
        discover_send_to_guessed_domains=True,
    )
    monkeypatch.setattr(
        discover,
        "discover_contact",
        _finds("hello@acme.io", "acme.io", "https://acme.io/contact"),
    )

    stats = run_pipeline(
        sources=[FakeSource([_lead(BOARD_URL)])],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )

    assert stats["emailed"] == 1
    assert _no_send[0][0] == "hello@acme.io"
    assert stats["discovered"] == 1
    # Still reported as a guess even when sending is allowed: the send does not make the
    # domain evidence, and the owner is the one who chose to trust it.
    assert len(stats["proposed_contacts"]) == 1


def test_a_lead_with_no_url_at_all_is_treated_as_a_guess(temp_db, monkeypatch):
    """HN and Reddit posts sometimes carry no URL. Absent evidence is not evidence.

    ``_domain_is_evidence`` returning False on an empty URL is the difference between
    "the post vouched for this domain" and "we could not tell", and only one of those may
    be mailed.
    """
    from pipeline import run_pipeline

    _settings(monkeypatch, discover_contacts=True, max_contact_discoveries_per_run=10)
    monkeypatch.setattr(
        discover,
        "discover_contact",
        _finds("hello@acme.com", "acme.com", "https://acme.com/contact"),
    )

    stats = run_pipeline(
        sources=[FakeSource([_lead("")])],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )
    assert stats["emailed"] == 0
    assert len(stats["proposed_contacts"]) == 1


# --------------------------------------------------------------------------- #
# budgets, flags, failures
# --------------------------------------------------------------------------- #
def test_discovery_is_skipped_entirely_when_the_flag_is_off(temp_db, monkeypatch):
    """And the run reports a denominator of zero, not a zero with no denominator."""
    from pipeline import run_pipeline

    _settings(monkeypatch, discover_contacts=False)
    stub = _finds("hello@acme.com", "acme.com", "https://acme.com/contact")
    monkeypatch.setattr(discover, "discover_contact", stub)

    stats = run_pipeline(
        sources=[FakeSource([_lead()])],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )
    assert stub.calls == []
    assert stats["discovery_attempts"] == 0
    assert stats["discovered"] == 0
    assert len(stats["apply_yourself"]) == 1


def test_the_per_run_lookup_budget_is_enforced(temp_db, monkeypatch):
    """Discovery is HTTP against other people's servers, so it is capped per run.

    Counted on ATTEMPTS, not on hits: a cap that only counted successes would let a run
    where nothing is findable fetch every qualified lead's worth of pages.
    """
    from pipeline import run_pipeline

    _settings(monkeypatch, discover_contacts=True, max_contact_discoveries_per_run=2)

    def _never_finds(lead, **kw):
        _never_finds.calls.append(lead.external_id)
        return None

    _never_finds.calls = []
    monkeypatch.setattr(discover, "discover_contact", _never_finds)

    leads = []
    for i in range(5):
        lead = _lead()
        lead.external_id = f"job-{i}"
        leads.append(lead)

    stats = run_pipeline(
        sources=[FakeSource(leads)],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )
    assert len(_never_finds.calls) == 2
    assert stats["discovery_attempts"] == 2
    assert stats["discovered"] == 0
    # All five still reach the owner: a spent budget must not lose the hand-off.
    assert len(stats["apply_yourself"]) == 5


def test_a_discovery_error_loses_the_lookup_and_nothing_else(temp_db, monkeypatch):
    """Discovery reaches out to arbitrary websites, so it will fail in ways nobody
    anticipated. The lead was already paid for; it must still reach the owner."""
    from pipeline import run_pipeline

    _settings(monkeypatch, discover_contacts=True, max_contact_discoveries_per_run=10)

    def _explodes(lead, **kw):
        raise RuntimeError("TLS handshake failed")

    monkeypatch.setattr(discover, "discover_contact", _explodes)

    stats = run_pipeline(
        sources=[FakeSource([_lead()])],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )
    assert stats["discovery_attempts"] == 1
    assert stats["discovered"] == 0
    assert len(stats["apply_yourself"]) == 1


def test_a_discovered_address_still_obeys_the_suppression_list(temp_db, monkeypatch):
    """``email_override`` bypasses extraction and NOTHING else.

    A discovered address is the one kind this system infers rather than reads, so it is
    the last one that should get a shortcut past an unsubscribe.
    """
    import outreach.suppression as suppression
    from pipeline import run_pipeline

    _settings(monkeypatch, discover_contacts=True, max_contact_discoveries_per_run=10)
    monkeypatch.setattr(
        discover,
        "discover_contact",
        _finds("hello@acme.com", "acme.com", "https://acme.com/contact"),
    )
    monkeypatch.setattr(suppression, "is_suppressed", lambda email: True)

    stats = run_pipeline(
        sources=[FakeSource([_lead()])],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )
    assert stats["emailed"] == 0
    assert stats["emailed_skipped"].get("suppressed") == 1


# --------------------------------------------------------------------------- #
# apply packs: built for the hand-off, and metered
# --------------------------------------------------------------------------- #
def test_packs_are_built_for_the_handed_over_leads(temp_db, monkeypatch):
    """The leads automation cannot reach still come back as something pasteable."""
    from pipeline import run_pipeline

    _settings(monkeypatch, discover_contacts=False, apply_packs=True,
              max_apply_packs_per_run=3)

    leads = []
    for i in range(5):
        lead = _lead(BOARD_URL)
        lead.external_id = f"job-{i}"
        leads.append(lead)

    stats = run_pipeline(
        sources=[FakeSource(leads)],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )

    assert len(stats["apply_yourself"]) == 5
    assert len(stats["apply_packs"]) == 3, "capped, best-fit first"
    for pack in stats["apply_packs"]:
        assert pack["text"] and pack["html"]
        # The listing URL is the entire hand-off with no hosted dashboard.
        assert BOARD_URL in pack["text"]


def test_pack_spend_is_metered_against_the_runs_budget(temp_db, monkeypatch):
    """Packs are Opus calls made AFTER the lead loop, and the cost tracker is uninstalled
    when that loop ends. Without re-installing it, pack spend is billed to the account and
    reported by nothing — a model call no budget gates and no run shows, which is the same
    shape as every guard in this project that passed while doing nothing.
    """
    import agents.llm as llm
    from pipeline import run_pipeline

    _settings(monkeypatch, discover_contacts=False, apply_packs=True,
              max_apply_packs_per_run=2)

    seen: list[object] = []
    real_get = llm.get_chat

    def _spy(model, chat=None):
        # Records the tracker installed at the moment the pack model handle is taken.
        seen.append(llm.get_cost_tracker())
        return real_get(model, chat=chat)

    monkeypatch.setattr("outreach.apply_pack.get_chat", _spy)

    leads = []
    for i in range(2):
        lead = _lead(BOARD_URL)
        lead.external_id = f"job-{i}"
        leads.append(lead)

    stats = run_pipeline(
        sources=[FakeSource(leads)],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )
    assert stats["apply_packs"], "precondition: packs were built"
    # A tracker was installed while the packs were drafted, not None.
    assert seen and seen[0] is not None
    # And it is uninstalled again on the way out: a tracker left in place would meter the
    # follow-up runner, the reply responder and every later job into this run's budget,
    # and the first thing to notice would be an unrelated job dying on BudgetExhausted.
    assert llm.get_cost_tracker() is None
