"""Offline tests for the contact pre-gate and deliverability check.

The defect being fixed: of 25 fully-researched, Opus-drafted proposals, **18 died at
the contact step** ("no_email") — the money was spent before anyone checked whether
the lead could be reached at all. A gate placed after the expensive work filters out
the system's own product.

So the gate now runs *before* research+drafting. These tests pin both halves:

* ``find_deliverable_email`` — extraction plus a domain that can receive mail;
* ``run_pipeline`` — an uncontactable lead is skipped before any LLM call.

No network: ``domain_accepts_mail`` is monkeypatched, and DNS is disabled suite-wide
by ``tests/conftest.py`` (fixture domains like ``acme.io`` publish no MX records).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
import outreach.extract as extract
from agents.llm import FakeChat
from core.schemas import Lead
from db.models import Base, OutreachRecord


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'gate.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    yield engine


@pytest.fixture(autouse=True)
def _clear_mx_cache():
    extract._MX_CACHE.clear()
    yield
    extract._MX_CACHE.clear()


def _lead(i: int, desc: str) -> Lead:
    return Lead(
        source="hn_hiring",
        external_id=f"job-{i}",
        title=f"Kubernetes + DevSecOps hardening #{i}",
        description=desc,
        company="Acme Corp",
        budget="$90/hr",
        tags=["kubernetes", "devsecops"],
    )


def _settings(monkeypatch, **overrides):
    """Override settings for modules that read them.

    ``pipeline`` does ``from config import get_settings``, so it holds its own
    reference — patching ``config.get_settings`` alone does not reach it. Patch every
    module that imported the name.
    """
    import config
    import pipeline

    real = config.get_settings

    def s():
        cfg = real()
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg

    monkeypatch.setattr(config, "get_settings", s)
    monkeypatch.setattr(pipeline, "get_settings", s, raising=False)


# --------------------------------------------------------------------------- #
# domain_accepts_mail
# --------------------------------------------------------------------------- #
def test_domain_without_dot_is_rejected():
    assert extract.domain_accepts_mail("localhost") is False
    assert extract.domain_accepts_mail("") is False


def test_result_is_cached_per_domain(monkeypatch):
    """One DNS lookup per domain per process — a run can see the same domain often."""
    calls: list[str] = []

    class FakeResolver:
        @staticmethod
        def resolve(domain, rdtype):
            calls.append(domain)
            return ["mx1"]

    import sys
    import types

    fake_dns = types.ModuleType("dns")
    fake_resolver = types.ModuleType("dns.resolver")
    fake_resolver.resolve = FakeResolver.resolve
    fake_dns.resolver = fake_resolver
    monkeypatch.setitem(sys.modules, "dns", fake_dns)
    monkeypatch.setitem(sys.modules, "dns.resolver", fake_resolver)

    assert extract.domain_accepts_mail("example.org") is True
    assert extract.domain_accepts_mail("example.org") is True
    assert calls == ["example.org"]  # second call served from cache


# --------------------------------------------------------------------------- #
# find_deliverable_email
# --------------------------------------------------------------------------- #
def test_deliverable_email_requires_a_mail_accepting_domain(monkeypatch):
    _settings(monkeypatch, verify_contact_domain=True)
    monkeypatch.setattr(extract, "domain_accepts_mail", lambda d: d == "good.io")

    good = _lead(1, "Hiring SRE — email jobs@good.io")
    bad = _lead(2, "Hiring SRE — email jobs@nonexistent-domain.invalid")

    assert extract.find_deliverable_email(good) == "jobs@good.io"
    assert extract.find_deliverable_email(bad) is None


def test_no_email_at_all_returns_none(monkeypatch):
    _settings(monkeypatch, verify_contact_domain=True)
    monkeypatch.setattr(extract, "domain_accepts_mail", lambda d: True)
    assert extract.find_deliverable_email(_lead(3, "Apply via our website.")) is None


def test_verification_can_be_disabled_for_offline_runs(monkeypatch):
    """The escape hatch tests/dev rely on — extraction still applies, DNS does not."""
    _settings(monkeypatch, verify_contact_domain=False)

    def boom(domain):  # must never be called
        raise AssertionError("DNS lookup attempted while verification is disabled")

    monkeypatch.setattr(extract, "domain_accepts_mail", boom)
    assert extract.find_deliverable_email(_lead(4, "email jobs@acme.io")) == "jobs@acme.io"


def test_noreply_is_still_rejected_when_verification_is_off(monkeypatch):
    """Disabling DNS must not weaken the address-quality rules."""
    _settings(monkeypatch, verify_contact_domain=False)
    lead = _lead(5, "Automated: no-reply@news.ycombinator.com")
    assert extract.find_deliverable_email(lead) is None


# --------------------------------------------------------------------------- #
# pipeline pre-gate — the expensive-work ordering fix
# --------------------------------------------------------------------------- #
class FakeSource:
    name = "hn_hiring"

    def __init__(self, leads):
        self._leads = leads

    def fetch(self, limit: int = 50):
        return list(self._leads[:limit])


class FakeRetriever:
    def retrieve(self, query, k=3):
        return [
            {
                "text": "multi-cloud-k8s-terraform cut infra cost 40%.",
                "source": "multi-cloud-k8s-terraform",
                "kind": "win",
                "score": 0.9,
            }
        ]


#: Kept in sync with tests/test_outreach.py — the reviewer agent enforces a minimum
#: body length and the presence of a matched project, so a shortened body would fail
#: review and make these tests look like gate failures when they are draft failures.
_EMAIL_BODY = (
    "Subject: Hardening your EKS clusters\n\n"
    "Saw you're working on securing EKS clusters and tightening your CI/CD "
    "pipelines, which is squarely what I do. I've done exactly this kind of "
    "work on multi-cloud-k8s-terraform, where I cut cloud cost 40% while "
    "keeping the security gates green and the developers happy. I'd audit your "
    "clusters, add policy-as-code guardrails, and wire signed supply-chain "
    "checks into the pipeline, shipping in small reviewable increments so you "
    "stay in control the whole way through. If any of that sounds useful, I'm "
    "happy to walk through a concrete plan on a short call: "
    "https://cal.com/surya-devsecops/15min\n"
    "Surya A — https://suryaanandan1995-dotcom.github.io"
)


def _route_structured(messages):
    system = ""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system":
            system = m.get("content", "")
            break
    if "qualifier" in system:
        return {
            "lead": _lead(0, "x").model_dump(),
            "fit_score": 90,
            "reasons": ["strong k8s + devsecops fit"],
            "matched_projects": ["multi-cloud-k8s-terraform"],
        }
    return {
        "summary": "Acme runs EKS and wants CI/CD hardening.",
        "tech_stack": ["EKS", "Terraform"],
        "pain_points": ["insecure pipelines"],
        "contacts": [],
    }


def _email_chat() -> FakeChat:
    return FakeChat(responses=[_EMAIL_BODY] * 12, structured=_route_structured)


class CountingChat(FakeChat):
    """FakeChat that records how many generation calls the pipeline made.

    Used to prove the pre-gate saves *spend*, not merely a send: an uncontactable
    lead must produce zero LLM invocations, since that was the actual waste (25
    Opus drafts, 18 of them thrown away at the contact step).
    """

    def __init__(self):
        super().__init__(responses=[_EMAIL_BODY] * 12, structured=_route_structured)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return super().invoke(messages)

    def with_structured_output(self, schema, **kw):
        clone = super().with_structured_output(schema, **kw)
        # Route the clone's counting back to this instance so nested structured
        # calls are counted too (FakeChat.with_structured_output returns a copy).
        parent = self

        class _Counting(type(clone)):  # pragma: no cover - thin shim
            def invoke(self, messages):
                parent.calls += 1
                return super().invoke(messages)

        clone.__class__ = _Counting
        return clone


def test_uncontactable_lead_is_skipped_before_drafting(temp_db, monkeypatch):
    """The core fix: no research, no draft, no spend on a lead with no address."""
    import outreach.sender as sender
    from pipeline import run_pipeline

    monkeypatch.setattr(sender, "send_outreach", lambda to, subject, body: True)
    chat = CountingChat()

    stats = run_pipeline(
        sources=[FakeSource([_lead(1, "Great role. Apply on our careers page.")])],
        retriever=FakeRetriever(),
        chat=chat,
        auto_email=True,
    )

    assert stats["contactable"] == 0
    assert stats["uncontactable_skipped"] == 1
    assert stats["queued"] == 0
    assert stats["emailed"] == 0
    assert stats["emailed_skipped"].get("no_email_pregate") == 1
    # The point of the whole change: zero LLM calls were made for this lead.
    assert chat.calls == 0
    with dbsession.get_session() as session:
        assert session.query(OutreachRecord).count() == 0


def test_contactable_lead_still_flows_through(temp_db, monkeypatch):
    import outreach.sender as sender
    from pipeline import run_pipeline

    monkeypatch.setattr(sender, "send_outreach", lambda to, subject, body: True)

    stats = run_pipeline(
        sources=[FakeSource([_lead(2, "Secure our EKS. Email jobs@acme.io to apply.")])],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )

    assert stats["contactable"] == 1
    assert stats["uncontactable_skipped"] == 0
    assert stats["queued"] == 1
    assert stats["emailed"] == 1


def test_mixed_batch_counts_both_sides(temp_db, monkeypatch):
    import outreach.sender as sender
    from pipeline import run_pipeline

    monkeypatch.setattr(sender, "send_outreach", lambda to, subject, body: True)

    leads = [
        _lead(10, "Email jobs@acme.io"),
        _lead(11, "Apply via ATS only."),
        _lead(12, "Reach hiring@startup.dev"),
        _lead(13, "No contact details."),
    ]
    stats = run_pipeline(
        sources=[FakeSource(leads)],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )

    assert stats["contactable"] == 2
    assert stats["uncontactable_skipped"] == 2
    assert stats["emailed_skipped"].get("no_email_pregate") == 2


def test_pregate_is_off_when_not_auto_emailing(temp_db, monkeypatch):
    """With auto_email off the product is a human-submitted proposal, so a lead with
    no public address is still worth drafting for. The gate must not apply."""
    from pipeline import run_pipeline

    stats = run_pipeline(
        sources=[FakeSource([_lead(20, "Great role. Apply on our careers page.")])],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=False,
    )

    assert stats["uncontactable_skipped"] == 0
    assert stats["queued"] == 1


def test_pregate_can_be_disabled_by_setting(temp_db, monkeypatch):
    import outreach.sender as sender
    from pipeline import run_pipeline

    monkeypatch.setattr(sender, "send_outreach", lambda to, subject, body: True)
    _settings(monkeypatch, require_contact_before_draft=False)

    stats = run_pipeline(
        sources=[FakeSource([_lead(21, "Apply on our careers page.")])],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )

    assert stats["uncontactable_skipped"] == 0
    assert stats["queued"] == 1  # drafted anyway, for human submission
