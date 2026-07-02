"""Offline tests for the scam/fraud detector and the auto-reply fraud gate."""
from __future__ import annotations

import pytest

from reply.respond import classify_and_draft
from reply.scam import scan


# --- detector: things that MUST be flagged --------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "We accidentally overpaid you — please send back the difference via wire.",
        "To start, pay a small refundable registration fee of $150.",
        "I'll mail you a cashier's check; deposit it and forward the balance.",
        "Payment will be made in Amazon gift cards, is that ok?",
        "Send your bank account number and routing number to receive funds.",
        "Please share the one-time code we just texted you to verify.",
        "Install AnyDesk so I can set up your workstation remotely.",
        "You have been hired immediately, no experience needed, earn $5,000 per week.",
        "Send bitcoin to this wallet address to unlock the contract.",
    ],
)
def test_obvious_scams_are_flagged(text):
    v = scan(text, "recruiter123@gmail.com")
    assert v["is_scam"] is True
    assert v["families"]


# --- detector: legitimate prospect messages must NOT be flagged -----------
@pytest.mark.parametrize(
    "text",
    [
        "Hi Surya, we need help hardening our EKS clusters. What's your availability?",
        "Thanks for reaching out — can you share a couple of relevant case studies?",
        "We're evaluating vendors for a GitOps migration. Are you open to a call next week?",
        "Great portfolio. Do you have experience with Istio and vLLM autoscaling?",
    ],
)
def test_legit_messages_not_flagged(text):
    v = scan(text, "cto@realcompany.com")
    assert v["is_scam"] is False


def test_single_soft_signal_alone_is_not_enough():
    # WhatsApp mention alone (soft) should not flag a message.
    v = scan("Sure, feel free to reach me on WhatsApp when convenient.", "cto@acme.io")
    assert v["is_scam"] is False


def test_freemail_only_nudges_when_paired_with_signal():
    # A soft off-platform push from a free-mail sender accumulates but one soft
    # signal + freemail nudge still stays under the critical threshold.
    v = scan("Let's move this to Telegram to discuss.", "guy@gmail.com")
    assert "off_platform" in v["families"]
    assert v["is_scam"] is False


# --- integration: the auto-reply gate returns action="flag", no body ------
def test_classify_and_draft_flags_scam_without_model_call():
    res = classify_and_draft(
        "recruiter@gmail.com",
        "Congratulations, you are hired! Send back the difference after you deposit "
        "the check we mail you.",
    )
    assert res["action"] == "flag"
    assert res["body"] == ""
    assert res["scam"]["is_scam"] is True
