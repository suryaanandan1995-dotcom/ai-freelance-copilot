"""Scam / fraud detector for inbound prospect messages.

Freelance inboxes attract a lot of fraud: fake "clients" who overpay with a bad
check and ask for the difference back, advance-fee ("pay to get the job") scams,
gift-card / crypto payment requests, identity-theft phishing (bank details, SSN,
ID, OTP codes), requests to install remote-access software, and pushes to move
off-platform to Telegram/WhatsApp before any real vetting.

This module is a fast, deterministic, offline first line of defence. It runs
BEFORE the auto-reply model call. If it flags a message, the auto-responder does
NOT engage or negotiate — the message is surfaced to the human instead. It never
sends money/info and never crosses safety lines on its own.

``scan(text, sender_email)`` -> ``{is_scam, score, families, reasons}``.

The detector is intentionally conservative about auto-*replying*: when in doubt it
flags for human review rather than let the agent converse with a possible scammer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Weight of a single match. A CRITICAL family alone is enough to flag; SOFT
# families must accumulate. Tunable via the flag threshold in scan().
_CRITICAL = 3
_MED = 2
_SOFT = 1

# family key -> (weight, human reason, list of lowercase substrings / regexes)
# Substrings are matched literally; entries wrapped in re.compile are regexes.
_FAMILIES: dict[str, tuple[int, str, list]] = {
    "overpayment": (
        _CRITICAL,
        "Overpayment / 'send back the difference' scam signal",
        [
            "send back the difference",
            "send the difference",
            "refund the excess",
            "refund the balance",
            "overpaid",
            "over payment",
            "overpayment",
            "keep your share and send",
            "wire back",
            "send back the remaining",
            "return the excess",
        ],
    ),
    "advance_fee": (
        _CRITICAL,
        "Advance-fee / pay-to-get-hired signal (legit clients never charge you)",
        [
            "registration fee",
            "processing fee",
            "activation fee",
            "application fee",
            "onboarding fee",
            "training fee",
            "pay a fee",
            "pay to apply",
            "refundable deposit",
            "purchase your own equipment",
            "buy the equipment yourself",
            "you will be reimbursed",
            "start-up kit",
            "startup kit",
        ],
    ),
    "bad_check": (
        _CRITICAL,
        "Fake-check / mailed-payment scam signal",
        [
            "cashier's check",
            "cashier check",
            "certified check",
            "mail you a check",
            "send you a check",
            "deposit the check",
            "deposit this check",
            "money order",
        ],
    ),
    "gift_cards": (
        _CRITICAL,
        "Gift-card payment request (a hallmark of fraud)",
        [
            "gift card",
            "gift cards",
            "steam card",
            "itunes card",
            "google play card",
            "amazon gift",
            "ebay card",
            "vanilla card",
        ],
    ),
    "crypto_payment": (
        _CRITICAL,
        "Unsolicited crypto payment / wallet request",
        [
            "send bitcoin",
            "in bitcoin",
            "btc wallet",
            "usdt",
            "send crypto",
            "crypto wallet",
            "send eth",
            "to this wallet address",
        ],
    ),
    "identity_phishing": (
        _CRITICAL,
        "Request for sensitive identity / financial data",
        [
            "bank account number",
            "routing number",
            "account and routing",
            "social security",
            "ssn",
            "passport number",
            "driver's license",
            "copy of your id",
            "photo of your id",
            "one-time code",
            "one time code",
            "verification code",
            "otp",
            "your password",
            "credit card number",
            "date of birth and address",
        ],
    ),
    "remote_access": (
        _CRITICAL,
        "Request to install remote-access software",
        [
            "anydesk",
            "teamviewer",
            "install this software so i can",
            "give me remote access",
            "screen share and enter your",
        ],
    ),
    "money_muling": (
        _CRITICAL,
        "Money-movement / mule signal",
        [
            "receive payments on my behalf",
            "receive money on my behalf",
            "process payments for me",
            "forward the payment",
            "transfer the funds to",
            "act as a payment agent",
        ],
    ),
    # --- SOFT signals: need to accumulate (or pair with a critical one) ---
    "off_platform": (
        _SOFT,
        "Pushing the conversation to an off-platform / unverifiable channel",
        [
            "telegram",
            "signal app",
            "on signal",
            "hangouts",
            "google chat",
            "skype",
            "text me at",
            "text me on",
            "add me on",
            "contact me on whatsapp",
            "reach me on whatsapp",
        ],
    ),
    "too_good": (
        _MED,
        "Too-good-to-be-true pay / no real vetting",
        [
            "no experience needed",
            "guaranteed income",
            "guaranteed weekly",
            "earn $",
            "weekly pay of",
            "per week working",
            "1-2 hours a day",
            "2 hours daily",
            "hired immediately",
            "you have been selected",
            "you are hired",
        ],
    ),
    "urgency": (
        _SOFT,
        "High-pressure urgency",
        [
            "act now",
            "urgent response",
            "reply immediately",
            "as soon as possible before",
            "limited slots",
            "offer expires",
        ],
    ),
    "link_bait": (
        _SOFT,
        "Suspicious shortened / bait link",
        [
            "bit.ly/",
            "tinyurl.com/",
            "cutt.ly/",
            re.compile(r"https?://[^\s]*\.(?:xyz|top|click|work|zip)\b"),
            "click here to claim",
            "verify your account at",
        ],
    ),
}

# Free-mail domains — a "hiring manager" writing from these while asking for money
# or pushing off-platform is a soft signal (many legit small clients also use them,
# so weight is low and it only matters combined with other flags).
_FREEMAIL = (
    "gmail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "mail.com",
)


@dataclass
class ScamVerdict:
    is_scam: bool = False
    score: int = 0
    families: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "is_scam": self.is_scam,
            "score": self.score,
            "families": self.families,
            "reasons": self.reasons,
        }


def _matches(patterns: list, text: str) -> bool:
    for p in patterns:
        if isinstance(p, re.Pattern):
            if p.search(text):
                return True
        elif p in text:
            return True
    return False


def scan(text: str, sender_email: str | None = None, *, threshold: int = _CRITICAL) -> dict:
    """Score an inbound message for scam signals.

    Returns ``{is_scam, score, families, reasons}``. ``is_scam`` is True when the
    accumulated weight reaches ``threshold`` (default 3 = one CRITICAL family, or
    three SOFT signals). A single critical fraud signal is enough to flag.
    """
    low = (text or "").lower()
    verdict = ScamVerdict()

    for key, (weight, reason, patterns) in _FAMILIES.items():
        if _matches(patterns, low):
            verdict.score += weight
            verdict.families.append(key)
            verdict.reasons.append(reason)

    # Free-mail sender + any soft push adds a small nudge (never flags on its own).
    addr = (sender_email or "").strip().lower()
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    if domain in _FREEMAIL and verdict.families:
        verdict.score += _SOFT
        verdict.reasons.append(f"Free-mail sender ({domain}) combined with other signals")

    verdict.is_scam = verdict.score >= threshold
    return verdict.as_dict()
