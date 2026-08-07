"""Offline tests for the deliverability content-hygiene layer (no DB, no network)."""
from __future__ import annotations

from outreach import deliverability


def test_lint_flags_spam_trigger_bangs_and_shout():
    issues = deliverability.lint(
        "Big news",
        "ACT NOW and claim your prize!!! This is URGENT.",
    )
    joined = " ".join(issues)
    # spam trigger phrase
    assert any("act now" in i for i in issues)
    # excessive exclamation marks
    assert any(i.startswith("excessive exclamation") for i in issues)
    # ALL-CAPS shout (URGENT is both a trigger and a shout)
    assert "ALL-CAPS word" in joined


def test_sanitize_collapses_bangs_and_titlecases_shout():
    subject, body = deliverability.sanitize(
        "Hello",
        "This is AMAZING news!!! Read on.",
    )
    assert "!!!" not in body
    assert "AMAZING" not in body
    assert "Amazing" in body


def test_sanitize_trims_trailing_bang_from_subject():
    subject, body = deliverability.sanitize("Quick question!", "Body text.")
    assert not subject.endswith("!")
    assert subject == "Quick question"


def test_sanitize_preserves_calcom_link_and_optout():
    body = (
        "Grab a slot: https://cal.com/surya/intro\n"
        "Not relevant? Reply 'unsubscribe' and I won't email again."
    )
    _, out = deliverability.sanitize("Intro", body)
    assert "https://cal.com/surya/intro" in out
    assert "unsubscribe" in out


def test_sanitize_is_idempotent():
    subject = "Big WINNER offer!!!"
    body = "Get CASH now!!! Visit https://cal.com/x"
    once = deliverability.sanitize(subject, body)
    twice = deliverability.sanitize(*once)
    assert once == twice


def test_sanitize_safe_on_empty_strings():
    assert deliverability.sanitize("", "") == ("", "")


def test_score_clean_high_spammy_low():
    clean_subject = "Quick idea for your onboarding flow"
    clean_body = (
        "Hi Alex, I noticed your signup could convert better. "
        "Happy to share a couple of ideas — grab a slot here: "
        "https://cal.com/surya/intro\n"
        "Not relevant? Reply 'unsubscribe' and I won't email again."
    )
    spammy_subject = "ACT NOW!!!"
    spammy_body = "100% FREE CASH!!! CLICK HERE to WIN. Limited time GUARANTEE!!!"

    clean_score = deliverability.score(clean_subject, clean_body)
    spam_score = deliverability.score(spammy_subject, spammy_body)

    assert clean_score >= 90
    assert spam_score < clean_score


# --------------------------------------------------------------------------- #
# a dead proof link is worse than no proof link
#
# Measured on a live pitch drafted 2026-08-07: the email named
# "devsecops-pipeline-templates" and offered it as "the code's here if you want to
# poke at it". That repo 404s — the real one is "devsecops-cicd-pipeline".
#
# The cause was a prompt that demanded something it never supplied: it says a named
# project with no link is an unverifiable claim (correct), but the only URL in the
# context was the portfolio root, so the model invented a plausible repo name.
#
# The reader who clicks is by definition the interested one, and what they learn is
# that the proof is fabricated — the exact suspicion a portfolio link exists to remove.
# --------------------------------------------------------------------------- #
_KNOWN = {"devsecops-cicd-pipeline", "multi-cloud-k8s-terraform", "llm-guardrails-gateway"}


def test_a_link_to_a_repo_we_do_not_have_is_replaced_with_the_portfolio_hub():
    from outreach.deliverability import strip_unknown_repo_links

    body = (
        "I built a reference DevSecOps pipeline end to end. The code's here if you "
        "want to poke at it: "
        "https://github.com/suryaanandan1995-dotcom/devsecops-pipeline-templates"
    )
    out = strip_unknown_repo_links(body, _KNOWN)
    assert "devsecops-pipeline-templates" not in out
    # Replaced, not deleted: removing the URL would leave a dangling "here:".
    assert "github.io" in out
    assert "The code's here if you want to poke at it" in out


def test_a_link_to_a_repo_we_really_have_is_left_alone():
    """The mirror. Stripping good links would destroy the one verifiable claim in the
    email, which is worse than the bug being fixed."""
    from outreach.deliverability import strip_unknown_repo_links

    url = "https://github.com/suryaanandan1995-dotcom/multi-cloud-k8s-terraform"
    assert url in strip_unknown_repo_links(f"Proof: {url}", _KNOWN)


def test_third_party_repo_links_are_never_touched():
    """An early draft rewrote github.com/kubernetes/kubernetes.

    A sanitiser that edits other people's links is doing damage, not hygiene: the
    account must match ours before anything is considered.
    """
    from outreach.deliverability import strip_unknown_repo_links

    body = "We follow https://github.com/kubernetes/kubernetes closely."
    assert strip_unknown_repo_links(body, _KNOWN) == body


def test_nothing_is_stripped_when_the_repo_list_is_unavailable():
    """Fail open. An empty list means "cannot verify", not "nothing is real" — and a
    wrongly-removed good link costs a proof point on every future email."""
    from outreach.deliverability import strip_unknown_repo_links

    body = "code: https://github.com/suryaanandan1995-dotcom/anything-at-all"
    assert strip_unknown_repo_links(body, set()) == body
    assert strip_unknown_repo_links(body, None) == body


def test_repo_link_repair_is_idempotent_and_runs_inside_sanitize():
    """It has to be in sanitize(), because that is what every send path calls."""
    from outreach.deliverability import sanitize

    body = "see https://github.com/suryaanandan1995-dotcom/not-a-real-repo"
    _, once = sanitize("Subject", body, _KNOWN)
    _, twice = sanitize("Subject", once, _KNOWN)
    assert once == twice
    assert "not-a-real-repo" not in once


def test_sanitize_is_backward_compatible_without_a_repo_list():
    """Existing callers pass two arguments; they must keep working untouched."""
    from outreach.deliverability import sanitize

    subject, body = sanitize("Hello!!!", "Body with https://github.com/x/y here")
    assert "!!!" not in subject
    assert "https://github.com/x/y" in body
