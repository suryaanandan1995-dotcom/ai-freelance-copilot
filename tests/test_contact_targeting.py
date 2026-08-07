"""Which address inside a job description we are allowed to cold-email.

The defect, measured on live leads and not hypothetical: ``find_contact_email``
returned the **first** email-shaped string in the description
(``outreach/extract.py:132`` before this change) and ``_is_good_email``
(``extract.py:85-99``) only rejected bounce-type locals — ``noreply``,
``postmaster``, ``bounce``, ``notifications`` — plus placeholder/asset domains.

ATS-backed descriptions run 3,000-15,000 chars and open with a legal/compliance
footer, so the first match is routinely a mailbox nobody at the company would ever
route a vendor pitch to. All four of these passed the old gate and were eligible to
be sent to:

* ``accessibilitysupport@nbcuni.com``
* ``candidate_accommodations@upstart.com``
* ``security@harness.io``
* ``hr@launchdarkly.com``

The single lead the last production run marked ``productive`` and queued for
auto-email resolved to one of the accommodations inboxes. That is the worst possible
first impression, and it spends sender reputation, which ``extract.py`` notes above
:func:`~outreach.extract.find_deliverable_email` is the one asset a cold-outreach
system cannot rebuy.

These tests pin the *defect*, in three parts, because fixing only one of them leaves
the bug live:

1. ``test_measured_*`` — the four real addresses are rejected.
2. ``test_hiring_*`` / ``test_personal_*`` — the mirror failure. An over-strict gate
   makes the market look empty, which this repo has shipped repeatedly; ``jobs@``,
   ``careers@``, ``hello@``, ``info@``, ``contact@`` and human names must still pass.
3. ``test_long_jd_*`` — ranking. A JD with ``accessibility@`` early and ``careers@``
   6,000 chars later must return ``careers@``. This one fails against the old code
   even with a perfect reject list, because rejecting the footer address is not the
   same as *choosing* the hiring one.

Offline by construction: extraction touches no network, and ``tests/conftest.py``
pins ``COPILOT_VERIFY_CONTACT_DOMAIN=false`` so no test here needs a resolver.
"""
from __future__ import annotations

import pytest

from core.schemas import Lead
from outreach.extract import _best_email_in, _is_good_email, find_contact_email

#: The exact addresses observed on production leads. Kept as literals rather than
#: sanitised look-alikes so a future reader can grep the incident and find them.
MEASURED_NON_HIRING = [
    "accessibilitysupport@nbcuni.com",
    "candidate_accommodations@upstart.com",
    "security@harness.io",
    "hr@launchdarkly.com",
]

#: Addresses that ARE the hiring contact, or a person. Rejecting any of these is the
#: failure mode that looks like "there are no leads this week".
MUST_STILL_PASS = [
    "jobs@acmecorp.io",
    "careers@acmecorp.io",
    "hiring@acmecorp.io",
    "recruiting@acmecorp.io",
    "recruitment@acmecorp.io",
    "talent@acmecorp.io",
    "hello@acmecorp.io",
    "info@acmecorp.io",
    "contact@acmecorp.io",
    "apply@acmecorp.io",
    "jane.doe@acmecorp.io",
    "j.okonkwo@acmecorp.io",
    "priya@acmecorp.io",
    "cto@acmecorp.io",
    "founders@acmecorp.io",
]


def _lead(desc: str, *, raw: dict | None = None) -> Lead:
    return Lead(
        source="hn_hiring",
        external_id="jd-1",
        title="Senior Platform Engineer (Kubernetes)",
        description=desc,
        company="Acme Corp",
        raw=raw or {},
    )


# --------------------------------------------------------------------------- #
# 1. the four measured addresses
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("address", MEASURED_NON_HIRING)
def test_measured_non_hiring_addresses_are_rejected(address):
    """Each of these was extractable from a real lead and would have been emailed."""
    assert find_contact_email(_lead(f"Great role. Questions? {address}")) is None


def test_measured_accommodations_inbox_is_not_returned_even_when_it_is_the_only_one():
    """The production incident, reproduced end to end.

    The lead was marked ``productive`` and queued for auto-email. "No contact" is the
    correct answer here — a lead we cannot reach politely is not a lead.
    """
    jd = (
        "Senior Platform Engineer, remote. You will own our EKS estate.\n\n"
        "NBCUniversal is an equal opportunity employer. If you require a reasonable "
        "accommodation to complete the application process, please contact "
        "accessibilitysupport@nbcuni.com.\n"
    )
    assert find_contact_email(_lead(jd)) is None


@pytest.mark.parametrize(
    "address",
    [
        "accessibility@corp.com",
        "accommodations@corp.com",
        "candidate.accommodation@corp.com",
        "security@corp.com",
        "abuse@corp.com",
        "privacy@corp.com",
        "dpo@corp.com",
        "legal@corp.com",
        "compliance@corp.com",
        "dmca@corp.com",
        "press@corp.com",
        "media@corp.com",
        "investor.relations@corp.com",
        "support@corp.com",
        "webmaster@corp.com",
        "billing@corp.com",
        "invoices@corp.com",
        "sales@corp.com",
        "marketing@corp.com",
        "unsubscribe@corp.com",
        "hr@corp.com",
        "payroll@corp.com",
    ],
)
def test_institutional_mailboxes_are_rejected(address):
    """The whole family, not just the four we happened to measure."""
    assert find_contact_email(_lead(f"Contact {address} for details.")) is None


# --------------------------------------------------------------------------- #
# 2. the mirror failure — an over-strict gate
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("address", MUST_STILL_PASS)
def test_hiring_and_personal_addresses_still_pass(address):
    assert find_contact_email(_lead(f"Email {address} to apply.")) == address


@pytest.mark.parametrize(
    "address",
    [
        # "salesi" contains "sales"; "legaspi" contains "legal"'s prefix letters;
        # "irving" contains "ir"; "adam" contains "ada"; "presswood" contains "press".
        # Substring matching would reject every one of these real surnames, so the
        # reject list is matched against whole tokens of the local-part instead.
        "salesi@acmecorp.io",
        "n.salesi@acmecorp.io",
        "legaspi@acmecorp.io",
        "irving@acmecorp.io",
        "adam@acmecorp.io",
        "presswood@acmecorp.io",
        "mediavilla@acmecorp.io",
        "abusaid@acmecorp.io",
        "peoplesmith@acmecorp.io",
    ],
)
def test_personal_names_containing_a_rejected_word_still_pass(address):
    """Word-boundary sanity, and the tradeoff we chose.

    The reject list matches whole tokens (split on ``. _ - +`` and digits), never
    substrings, so a person named Salesi or Presswood is reachable. The cost is the
    residual hole this buys: a company whose only published inbox is the bare token
    ``sales`` is caught, but a glued-together oddity like ``salesteam@`` is not — we
    lose nothing there, and if we ever do, we lose one lead. The opposite mistake
    sends a cold pitch to somebody's accommodations desk, which cannot be undone.
    """
    assert find_contact_email(_lead(f"Reach out to {address}.")) == address


@pytest.mark.parametrize(
    "address",
    ["jobs-support@acmecorp.io", "careers.accessibility@acmecorp.io"],
)
def test_hiring_word_outranks_a_department_word_in_the_same_local(address):
    """``jobs-support@`` is a hiring inbox that names a department, not a helpdesk.

    A hiring token anywhere in the local-part wins, so naming a department cannot
    disqualify an address that literally says "jobs".
    """
    assert find_contact_email(_lead(f"Apply: {address}")) == address


# --------------------------------------------------------------------------- #
# 3. ranking — a later hiring address beats an earlier institutional one
# --------------------------------------------------------------------------- #
def _long_jd(*, early: str, late: str) -> str:
    """A realistic ATS description: compliance boilerplate up top, apply line at the
    bottom, ~6k chars of role copy between them (real ones run 3k-15k)."""
    body = (
        "You will own our Kubernetes platform, tighten CI/CD, and pair with product "
        "teams on release automation. We run EKS, Terraform, and Argo CD. "
    ) * 45
    return (
        "Senior Platform Engineer — Remote (US)\n\n"
        "Acme Corp is an equal opportunity employer and does not discriminate. "
        f"If you require an accommodation, write to {early}.\n\n"
        f"{body}\n\n"
        f"Interested? Send your resume to {late} and we'll get back to you.\n"
    )


def test_long_jd_prefers_the_hiring_address_over_the_earlier_footer_address():
    """The ranking fix. Fails against the old first-match code even with the reject
    list in place, because ``accessibility@`` appears ~200 chars in and ``careers@``
    about 6,000 chars later."""
    jd = _long_jd(early="accessibility@corp.com", late="careers@corp.com")
    assert jd.index("accessibility@corp.com") < jd.index("careers@corp.com")
    assert find_contact_email(_lead(jd)) == "careers@corp.com"


def test_long_jd_prefers_a_hiring_address_over_an_acceptable_generic_one():
    """Both candidates are sendable; ``jobs@`` is still the better first impression."""
    jd = _long_jd(early="hello@corp.com", late="jobs@corp.com")
    assert find_contact_email(_lead(jd)) == "jobs@corp.com"


def test_cue_proximity_breaks_the_tie_when_neither_local_is_a_hiring_word():
    """Rank (b): no hiring word anywhere, so the address the prose invites you to
    write to wins over the one that merely appears earlier."""
    jd = (
        "Acme Corp — Senior Platform Engineer.\n\n"
        "Our newsletter archive lives at archive@corp.com.\n\n"
        + ("We run EKS, Terraform and Argo CD across three regions. " * 40)
        + "\n\nTo apply, email dana.whitfield@corp.com with a short note.\n"
    )
    assert find_contact_email(_lead(jd)) == "dana.whitfield@corp.com"


def test_single_candidate_is_returned_unchanged():
    """The common case must not have changed shape: one address in, that address out
    — no cue needed, no hiring word needed."""
    assert (
        find_contact_email(_lead("Questions to dana.whitfield@corp.com."))
        == "dana.whitfield@corp.com"
    )


def test_first_found_remains_the_fallback_when_nothing_distinguishes_candidates():
    """Rank (c): equal on hiring words and equal on cues, so the earlier one wins —
    the pre-existing behaviour, deliberately preserved."""
    jd = "Team leads: dana@corp.com and morgan@corp.com."
    assert find_contact_email(_lead(jd)) == "dana@corp.com"


# --------------------------------------------------------------------------- #
# preserved guarantees
# --------------------------------------------------------------------------- #
def test_deobfuscation_still_works_and_is_ranked_too():
    """The ``x [at] y [dot] z`` path (HN posters dodging scrapers) must survive the
    ranking rewrite — including when the obfuscated hiring address is not first."""
    assert (
        find_contact_email(_lead("Reach me at jane [at] acme [dot] io for the k8s gig."))
        == "jane@acme.io"
    )
    jd = (
        "Accommodation requests: accessibility (at) corp (dot) com. "
        "To apply, email jobs (at) corp (dot) com."
    )
    assert find_contact_email(_lead(jd)) == "jobs@corp.com"


def test_description_still_beats_raw():
    """Ordering guarantee: the human-written body is where the poster put the address
    they want used, so a hiring address hidden in ``raw`` must not pre-empt it."""
    lead = _lead(
        "Email dana@corp.com to apply.",
        raw={"comment": "internal note: jobs@other-corp.com"},
    )
    assert find_contact_email(lead) == "dana@corp.com"


def test_raw_is_still_searched_when_the_description_has_nothing():
    lead = _lead("Apply through our portal.", raw={"comment": "ping careers@corp.com"})
    assert find_contact_email(lead) == "careers@corp.com"


def test_raw_is_gated_too():
    """The reject list applies to every haystack, not just the description — the
    ``raw`` payload from an ATS scrape is exactly where footers end up."""
    lead = _lead("Apply through our portal.", raw={"html": "<a>security@corp.com</a>"})
    assert find_contact_email(lead) is None


@pytest.mark.parametrize(
    "desc",
    [
        "Automated: no-reply@news.ycombinator.com",
        "write to you@example.com",
        "errors go to hi@sentry.io",
        "our logo is logo@2x.png",
        "no email here, just prose",
        "",
    ],
)
def test_pre_existing_rejections_are_unchanged(desc):
    assert find_contact_email(_lead(desc)) is None


def test_never_raises_on_hostile_input():
    """Control flow is return values, not exceptions — the pipeline calls this inside
    a per-lead loop and one malformed description must not end the run."""
    for desc in ["@@@", "a@", "@b.com", "x" * 20000, "user@@corp..com", "@ [at] [dot]"]:
        find_contact_email(_lead(desc))  # must not raise
    find_contact_email(_lead("ok dana@corp.com", raw={"n": [None, 3, {"k": ["x@y.io"]}]}))


# --------------------------------------------------------------------------- #
# a template is not a mailbox
# --------------------------------------------------------------------------- #
def test_a_placeholder_address_pattern_is_not_a_contact():
    """Measured live in the HN August 2026 thread: a Grafana Labs hiring manager
    wrote "get in touch with me directly via <linkedin> or first.last@grafana.com" —
    i.e. "work out my name and use this form". The domain is real, so it publishes MX
    and cleared the deliverability check; the mailbox does not exist.

    That makes it worse than extracting nothing: a hard bounce is precisely what
    costs sender reputation, the one asset this system cannot rebuy.
    """
    for placeholder in (
        "first.last@grafana.com",
        "firstname.lastname@acme.com",
        "yourname@acme.com",
        "flast@acme.com",
        "email@acme.com",
    ):
        assert not _is_good_email(placeholder), placeholder


def test_a_real_person_whose_address_looks_like_the_template_still_passes():
    """The mirror failure, and the reason the match is on the whole local-part.

    ``john.smith@`` IS the first.last convention, filled in — the overwhelmingly
    common shape of a real hiring manager's address. A token-wise match on
    "first"/"last" would reject the entire convention and gut the only source that
    has ever produced a sent email.
    """
    for real in (
        "john.smith@acme.com",
        "a.lastname@acme.com",
        "firstenberg@acme.com",
        "lastova@acme.com",
        "namita@acme.com",
    ):
        assert _is_good_email(real), real


def test_a_placeholder_never_outranks_a_real_address_in_the_same_post():
    """The Grafana post's real signal was its Greenhouse links; had it also carried a
    genuine address, ranking must not prefer the template just because it is nearer a
    hiring cue ("get in touch with me directly")."""
    text = (
        "Grafana Labs | Senior Software Engineer - AI | 100% Remote. "
        "Apply: https://job-boards.greenhouse.io/grafanalabs/jobs/6100673004 "
        "or send your CV to careers@grafana.com. Also feel free to get in touch "
        "with me directly via first.last@grafana.com"
    )
    assert _best_email_in(text) == "careers@grafana.com"
