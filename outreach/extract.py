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

# Obfuscated forms people use to dodge scrapers, e.g.
#   "name [at] domain [dot] com", "name (at) domain (dot) com".
# We de-obfuscate the whole text first, then run the normal matcher over it.
_AT_RE = re.compile(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", re.IGNORECASE)
_DOT_RE = re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.IGNORECASE)
# Bare " at "/" dot " forms, only when they clearly sit between address parts.
_AT_WORD_RE = re.compile(r"(?<=[\w])\s+at\s+(?=[\w])", re.IGNORECASE)
_DOT_WORD_RE = re.compile(r"(?<=[\w])\s+dot\s+(?=[\w])", re.IGNORECASE)


def _deobfuscate(text: str) -> str:
    """Normalize common ``x [at] y [dot] z`` obfuscations into ``x@y.z``."""
    out = _AT_RE.sub("@", text)
    out = _DOT_RE.sub(".", out)
    # Only touch bare-word forms when a bracketed/at marker was present, to
    # avoid mangling ordinary prose like "meet at dot com office".
    if "@" in out and "@" not in text:
        out = _DOT_WORD_RE.sub(".", out)
    elif _AT_WORD_RE.search(text) and (
        _DOT_RE.search(text) or " dot " in text.lower()
    ):
        out = _AT_WORD_RE.sub("@", out)
        out = _DOT_WORD_RE.sub(".", out)
    return out

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
        # Scan the raw text first (catches "email: x@y.com", "contact: x@y.com"),
        # then a de-obfuscated copy ("name [at] domain [dot] com").
        candidates = [text]
        deob = _deobfuscate(text)
        if deob != text:
            candidates.append(deob)
        for candidate in candidates:
            for match in _EMAIL_RE.findall(candidate):
                email = match.lower().strip(".,;:<>()[]\"'")
                if _is_good_email(email):
                    return email
    return None


# --- deliverability verification ------------------------------------------------
#
# Measured context: of 25 fully-researched, fully-drafted proposals, 18 died at
# the contact step ("no_email"). Emailing a domain that cannot receive mail burns
# sender reputation, which is the one asset a cold-outreach system cannot rebuy —
# so an address is only considered sendable if its domain publishes MX (or A)
# records. This is a DNS lookup, not an SMTP probe: no connection is made to the
# recipient's mail server, so it is invisible to them and costs nothing.

_MX_CACHE: dict[str, bool] = {}


def domain_accepts_mail(domain: str) -> bool:
    """True if ``domain`` publishes MX (or fallback A) records.

    Cached per-process. Fails **open** on resolver errors: a DNS hiccup should not
    silently discard a real lead, since a bad send merely bounces while a wrongly
    discarded lead is invisible.
    """
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain or "." not in domain:
        return False
    if domain in _MX_CACHE:
        return _MX_CACHE[domain]

    ok = True
    try:  # dnspython is optional — degrade to a plain A-record check without it.
        import dns.resolver  # type: ignore[import-untyped]

        try:
            answers = dns.resolver.resolve(domain, "MX")
            ok = bool(list(answers))
        except Exception:
            try:
                dns.resolver.resolve(domain, "A")
                ok = True
            except Exception:
                ok = False
    except ImportError:
        import socket

        try:
            socket.getaddrinfo(domain, None)
            ok = True
        except Exception:
            ok = False
    except Exception:  # pragma: no cover - resolver misconfiguration: fail open
        ok = True

    _MX_CACHE[domain] = ok
    return ok


def find_deliverable_email(lead: Lead) -> str | None:
    """Like :func:`find_contact_email`, but also require a mail-accepting domain.

    The DNS check is skipped when ``COPILOT_VERIFY_CONTACT_DOMAIN=false`` so that
    the test suite (and offline dev) stays hermetic — fixture domains like
    ``acme.io`` don't resolve, and a unit test must not depend on a resolver.
    """
    email = find_contact_email(lead)
    if not email:
        return None
    try:
        from config import get_settings

        if not get_settings().verify_contact_domain:
            return email
    except Exception:  # pragma: no cover - config unavailable: stay strict
        pass
    _, _, domain = email.partition("@")
    if not domain_accepts_mail(domain):
        return None
    return email
