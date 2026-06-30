"""Extract a real contact email from a lead.

Most email-reachable leads come from Hacker News "Who is hiring?" comments where
posters publish a direct address ("email jobs@acme.com"). We regex-scan the
lead's description and any string values in ``lead.raw``, lowercase + validate,
and reject obvious non-contact addresses (noreply, error/asset domains, example
placeholders). The first good address wins.
"""
from __future__ import annotations

import re
from typing import Any

from core.schemas import Lead

# Reasonable email matcher (not RFC-perfect, but good for scraped free text).
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}",
)

# Local-parts that are never a person you should cold-email.
_BAD_LOCAL_PREFIXES = (
    "noreply",
    "no-reply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "postmaster",
    "bounce",
    "notifications",
)

# Domains/substrings that are placeholders, infra, or asset hosts — not contacts.
_BAD_DOMAIN_SUBSTRINGS = (
    "@example.",
    "@sentry.",
    "@email.example",
    "@test.",
    "@localhost",
    "@domain.",
    "@yourcompany.",
    "@company.com",
)

# Image/asset file extensions sometimes captured as "name@2x.png" etc.
_ASSET_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".css",
    ".js",
    ".ico",
)


def _is_good_email(email: str) -> bool:
    email = email.lower()
    if "@" not in email or "." not in email.split("@", 1)[1]:
        return False
    local, _, domain = email.partition("@")
    if any(local.startswith(p) for p in _BAD_LOCAL_PREFIXES):
        return False
    if any(sub in email for sub in _BAD_DOMAIN_SUBSTRINGS):
        return False
    if email.endswith(_ASSET_EXTENSIONS):
        return False
    # A pure asset filename like "logo@2x.png" — domain segment is a file.
    if any(domain.endswith(ext) for ext in _ASSET_EXTENSIONS):
        return False
    return True


def _iter_raw_strings(value: Any) -> list[str]:
    """Flatten string values out of a (possibly nested) raw dict/list."""
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            found.extend(_iter_raw_strings(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            found.extend(_iter_raw_strings(v))
    return found


def find_contact_email(lead: Lead) -> str | None:
    """Return the first valid contact email found in the lead, else ``None``."""
    haystacks: list[str] = [lead.description or ""]
    haystacks.extend(_iter_raw_strings(lead.raw or {}))

    for text in haystacks:
        for match in _EMAIL_RE.findall(text):
            email = match.lower().strip(".,;:<>()[]\"'")
            if _is_good_email(email):
                return email
    return None
