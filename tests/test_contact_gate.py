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
# find_deliverable_email_with_reason — the missing denominator inside a lead
#
# ``pipeline`` recorded one skip reason, ``no_email_pregate``, for every lead it could
# not write to, and over the 6 production runs of 2026-08-10..17 that counter read
# **851 of 1047 leads**. It summed three outcomes with opposite fixes: no address was
# published (buy a different lead source), an address was published and our own gate
# refused it (fix this module — recoverable at zero acquisition cost), or an address
# passed the gate onto a domain with no MX/A records (nothing can fix it). The tests
# below pin the vocabulary, because these strings are report keys: a renamed reason
# silently zeroes a row in the funnel report rather than failing anything.
#
# Same shape as two fixes this project already shipped a layer up — ``last_error``
# separated "the API rejected us" from "nothing matched", ``LeadSource.scanned``
# separated "nothing matched" from "the feed was empty".
# --------------------------------------------------------------------------- #
def _lead_with_raw(i: int, desc: str, raw: dict) -> Lead:
    """A lead whose ``raw`` payload also carries text, like every real source.

    Kept separate from :func:`_lead` so the existing fixtures stay byte-identical.
    """
    lead = _lead(i, desc)
    lead.raw = raw
    return lead


def test_a_reachable_address_reports_the_found_reason_alongside_it(monkeypatch):
    _settings(monkeypatch, verify_contact_domain=True)
    monkeypatch.setattr(extract, "domain_accepts_mail", lambda d: True)

    email, reason = extract.find_deliverable_email_with_reason(
        _lead(30, "Secure our EKS. Email jobs@good.io to apply.")
    )
    assert (email, reason) == ("jobs@good.io", "found")


def test_a_post_with_no_address_at_all_is_a_lead_source_problem(monkeypatch):
    """``no_address_in_post`` is the one reason whose lever is outside this module.

    Measured 2026-08-07: ``contract_jobs`` fetched 36 Adzuna listings scoring up to 78
    and published zero addresses, because the listing IS an apply form. No amount of
    gate loosening reaches those leads; only a different feed does.
    """
    _settings(monkeypatch, verify_contact_domain=True)
    monkeypatch.setattr(extract, "domain_accepts_mail", lambda d: True)

    assert extract.find_deliverable_email_with_reason(
        _lead(31, "Great role. Apply on our careers page.")
    ) == (None, "no_address_in_post")


def test_an_institutional_mailbox_is_reported_as_our_own_gate_rejecting_it(monkeypatch):
    """``rejected_non_hiring`` is the recoverable slice — the address exists.

    This is the number that decides whether there are hundreds of reachable leads
    hiding in the 851 or none, so it must not be folded into "no address".
    """
    _settings(monkeypatch, verify_contact_domain=True)
    monkeypatch.setattr(extract, "domain_accepts_mail", lambda d: True)

    for desc in (
        "Questions about the role? support@corp.com",
        "Data requests: privacy@corp.com",
        # A bounce local and a template local land in the same bucket on purpose:
        # the string looked like an address and is not a person we can pitch, and
        # the lever for all of them is this module's reject lists.
        "Automated posting: no-reply@corp.com",
        "Get in touch directly via first.last@corp.com",
    ):
        assert extract.find_deliverable_email_with_reason(_lead(32, desc)) == (
            None,
            "rejected_non_hiring",
        ), desc


def test_an_accommodations_desk_is_reported_as_a_do_not_contact_context(monkeypatch):
    """``rejected_do_not_contact`` ranks above ``rejected_non_hiring`` because the
    address got further: it passed every local-part check and only the surrounding
    prose disqualified it. ``dana@corp.com`` is an ordinary human name, so no reject
    list could ever have caught it — only the sentence gives it away.
    """
    _settings(monkeypatch, verify_contact_domain=True)
    monkeypatch.setattr(extract, "domain_accepts_mail", lambda d: True)

    assert extract.find_deliverable_email_with_reason(
        _lead(33, "If you require an accommodation to apply, contact dana@corp.com")
    ) == (None, "rejected_do_not_contact")


def test_an_accepted_address_on_a_dead_domain_reports_that_nothing_can_fix_it(monkeypatch):
    """``domain_refused_mail`` is the verdict with no lever, and that is the point.

    The gate did its job, picked the hiring address, and the domain publishes no MX or
    A records. Counting these separately is what stops someone loosening a gate that
    was already right.
    """
    _settings(monkeypatch, verify_contact_domain=True)
    monkeypatch.setattr(extract, "domain_accepts_mail", lambda d: d == "good.io")

    assert extract.find_deliverable_email_with_reason(
        _lead(34, "Hiring SRE — email jobs@deadmail.io")
    ) == (None, "domain_refused_mail")


def test_the_best_candidate_supplies_the_reason_when_several_fail_differently(monkeypatch):
    """Reason priority: ``domain_refused_mail`` > ``rejected_non_hiring``.

    This post is the ambiguous case the ordering exists for. Reporting the weaker
    reason would say "our gate refused a support@ address" — sending someone to loosen
    a filter that correctly preferred the ``jobs@`` line — when the truth is that the
    right address was found and is undeliverable.
    """
    _settings(monkeypatch, verify_contact_domain=True)
    monkeypatch.setattr(extract, "domain_accepts_mail", lambda d: d != "deadmail.io")

    desc = "Questions? support@corp.com. To apply, email jobs@deadmail.io"
    assert extract.find_deliverable_email_with_reason(_lead(35, desc)) == (
        None,
        "domain_refused_mail",
    )

    # And the support@ address never becomes the answer: give the same post a live
    # domain and the hiring address wins outright, proving the reason above described
    # the ranked winner rather than whichever candidate happened to fail last.
    monkeypatch.setattr(extract, "domain_accepts_mail", lambda d: True)
    assert extract.find_deliverable_email_with_reason(_lead(35, desc)) == (
        "jobs@deadmail.io",
        "found",
    )


def test_a_rejection_in_the_description_outranks_silent_boilerplate_in_raw(monkeypatch):
    """Reasons fold across text blocks instead of last-one-wins.

    A real lead's ``raw`` payload contributes a dozen address-free strings after the
    description, so taking the final block's verdict would report
    ``no_address_in_post`` — buy a new source — for a post that plainly printed
    ``support@``. The recoverable slice would read as zero.
    """
    _settings(monkeypatch, verify_contact_domain=True)
    monkeypatch.setattr(extract, "domain_accepts_mail", lambda d: True)

    lead = _lead_with_raw(
        36,
        "Questions? support@corp.com",
        {"company": "Corp", "location": "Remote", "how_to_apply": "Use the portal."},
    )
    assert extract.find_deliverable_email_with_reason(lead) == (
        None,
        "rejected_non_hiring",
    )


def test_disabled_verification_reports_found_rather_than_a_refused_domain(monkeypatch):
    """The escape hatch means "this machine cannot answer the deliverability question".

    Inventing ``domain_refused_mail`` for a question we never asked would put a
    fabricated row in the report, so with ``verify_contact_domain=False`` an extracted
    address is ``found`` and no resolver is consulted at all.
    """
    _settings(monkeypatch, verify_contact_domain=False)

    def boom(domain):  # must never be called
        raise AssertionError("DNS lookup attempted while verification is disabled")

    monkeypatch.setattr(extract, "domain_accepts_mail", boom)
    assert extract.find_deliverable_email_with_reason(
        _lead(37, "email jobs@acme.io")
    ) == ("jobs@acme.io", "found")

    # Extraction still applies with DNS off — a rejection is still reported as one.
    assert extract.find_deliverable_email_with_reason(
        _lead(38, "Automated: no-reply@news.ycombinator.com")
    ) == (None, "rejected_non_hiring")


def test_every_unreachable_reason_is_named_so_a_report_can_seed_it_at_zero():
    """The strings are report keys, and an absent key reads as "never happened".

    That misreading is the whole 851-of-1047 defect, one level up, so the four
    unreachable reasons are exported as a tuple in priority order and the rank table is
    derived from that tuple — one ordering, stated once, rather than two lists that can
    drift apart.
    """
    assert extract.CONTACT_REASONS == (
        "domain_refused_mail",
        "rejected_do_not_contact",
        "rejected_non_hiring",
        "no_address_in_post",
    )
    assert extract.REASON_FOUND == "found"
    assert extract.REASON_FOUND not in extract.CONTACT_REASONS

    # Priority is transitive and total across the four, most recoverable first.
    for stronger, weaker in zip(
        extract.CONTACT_REASONS, extract.CONTACT_REASONS[1:], strict=False
    ):
        assert extract._preferred_reason(stronger, weaker) == stronger
        assert extract._preferred_reason(weaker, stronger) == stronger


def test_the_wrappers_return_exactly_what_they_returned_before_the_refactor(monkeypatch):
    """One decision, three entry points: the reason plumbing must be invisible.

    ``pipeline`` calls ``find_deliverable_email`` twice and four test modules assert on
    ``find_contact_email``; both are now wrappers that discard the reason, so this pins
    the address half of the answer against representative inputs of each shape —
    description, ``raw`` payload, ranked winner, obfuscated form, and each rejection.
    """
    _settings(monkeypatch, verify_contact_domain=True)
    monkeypatch.setattr(extract, "domain_accepts_mail", lambda d: d != "deadmail.io")

    cases = [
        ("Secure our EKS. Email jobs@good.io to apply.", "jobs@good.io"),
        ("Reach me at jane [at] acme [dot] io for the k8s gig.", "jane@acme.io"),
        ("Questions? support@corp.com. To apply, email careers@corp.com", "careers@corp.com"),
        ("Questions? support@corp.com", None),
        ("If you require an accommodation, contact dana@corp.com", None),
        ("Great role. Apply on our careers page.", None),
    ]
    for desc, expected in cases:
        assert extract.find_contact_email(_lead(40, desc)) == expected, desc
        # find_contact_email never consults DNS, so the two agree except on the one
        # case the domain check is for.
        assert extract.find_deliverable_email(_lead(40, desc)) == expected, desc

    # The raw-payload path (Reddit/HN sources put the body there, not in description).
    from_raw = _lead_with_raw(41, "No address in the body.", {"selftext": "ceo@foo.co"})
    assert extract.find_contact_email(from_raw) == "ceo@foo.co"
    assert extract.find_deliverable_email(from_raw) == "ceo@foo.co"

    # ...and the one divergence: extraction succeeds, deliverability does not.
    dead = _lead(42, "Hiring SRE — email jobs@deadmail.io")
    assert extract.find_contact_email(dead) == "jobs@deadmail.io"
    assert extract.find_deliverable_email(dead) is None


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


def test_uncontactable_lead_is_scored_but_never_drafted(temp_db, monkeypatch):
    """The core fix: no research, no draft, no Opus spend on a lead with no address.

    This test used to assert ``chat.calls == 0`` — the lead was skipped entirely,
    including qualification. That saved the *cheap* Sonnet call in order to protect
    the *expensive* Opus ones, which ``route_after_qualify`` already gates on fit
    score anyway, and it cost far more than it saved: a source whose leads are all
    uncontactable produced no scores at all, so the funnel reported
    "unreachable: scoring them is wasted spend" about a source the code had refused
    to score, and the run-level bottleneck declared the lead mix off-ICP over a
    sample that excluded the best-targeted feed in the mix.

    So the assertion is now ``== 1``, which is *stricter* than ``== 0`` was: it pins
    both halves — that qualification happened, and that nothing after it did. A bare
    ``&lt;= 1`` or a "no draft was queued" check would pass even if research ran.
    """
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
    # Reason-coded: the post published no address at all, which is a different lever from
    # an address whose domain refuses mail. The single ``no_email_pregate`` key this
    # replaced read 851 of 1047 leads and named none of them.
    assert stats["emailed_skipped"].get("no_email_no_address_in_post") == 1
    # Exactly one call: qualification. Research, drafting and review never ran.
    assert chat.calls == 1
    # And the score is now visible to the funnel report, which is the whole point.
    assert stats["fit"]["n"] == 1
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
    assert stats["emailed_skipped"].get("no_email_no_address_in_post") == 2


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


def test_a_qualified_uncontactable_lead_is_handed_to_the_owner(temp_db, monkeypatch):
    """Scoring it is only worth the spend if someone ever sees the result.

    Measured 2026-08-07: ``contract_jobs`` — the day-rate feed, the source aimed
    squarely at paid contract work — fetched 36 leads scoring up to 78 and produced
    zero outreach, because Adzuna listings carry an apply form and no address. The
    previous fix made those leads *scored* instead of invisible to the funnel report,
    which fixed the reporting; the leads themselves were still dropped at the
    disposition check and never shown to anyone.

    ``auto_submit`` is permanently off (bot-submitting to job forms breaks platform
    ToS), so a human hand-off is the ONLY route these leads have. Applying by hand
    takes a minute; the automation's dead end must not silently be the owner's.
    """
    import outreach.sender as sender
    from pipeline import run_pipeline

    monkeypatch.setattr(sender, "send_outreach", lambda to, subject, body: True)

    stats = run_pipeline(
        sources=[FakeSource([_lead(1, "Great role. Apply on our careers page.")])],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )

    assert stats["uncontactable_skipped"] == 1
    assert stats["queued"] == 0  # still not queued: there is nobody to email
    handoff = stats["apply_yourself"]
    assert len(handoff) == 1
    assert handoff[0]["fit_score"] == 90
    # The listing URL is the entire hand-off — there is no hosted dashboard to link to.
    assert handoff[0]["url"] == _lead(1, "x").url


def test_a_low_scoring_uncontactable_lead_is_not_handed_over(temp_db, monkeypatch):
    """The hand-off must stay worth reading.

    Every uncontactable lead would mean 136 entries per run, which is a list nobody
    opens — the same way a section that always renders stops being read.
    """
    import config
    import outreach.sender as sender
    from pipeline import run_pipeline

    monkeypatch.setattr(sender, "send_outreach", lambda to, subject, body: True)
    real = config.get_settings

    def strict():
        cfg = real()
        cfg.min_fit_score = 95  # above the fake qualifier's 90
        return cfg

    monkeypatch.setattr("pipeline.get_settings", strict)

    stats = run_pipeline(
        sources=[FakeSource([_lead(1, "Great role. Apply on our careers page.")])],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )

    assert stats["uncontactable_skipped"] == 1
    assert stats["apply_yourself"] == []
