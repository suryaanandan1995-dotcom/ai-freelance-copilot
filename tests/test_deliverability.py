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
