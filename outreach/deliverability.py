"""Free deliverability content-hygiene layer.

No domain warm-up, no paid reputation service, no external calls. This module
just inspects and lightly cleans the *content* of an outgoing email so it is
less likely to trip common spam filters (SpamAssassin-style heuristics).

Three pure functions:

  * ``lint(subject, body)``  -> human-readable list of spam signals found.
  * ``sanitize(subject, body)`` -> a lightly-cleaned (subject, body) safe to send.
  * ``score(subject, body)`` -> 0..100 rough inbox-friendliness score.

Everything here is deterministic, idempotent, and safe on empty strings. It
never rewrites meaning and never strips the cal.com booking link or the
plain-text opt-out / unsubscribe line — it only *softens* spam signals.
"""
from __future__ import annotations

import re

# Well-known spam-filter trigger phrases (kept focused, ~20).
SPAM_TRIGGERS: list[str] = [
    "act now",
    "limited time",
    "click here",
    "100% free",
    "risk-free",
    "guarantee",
    "buy now",
    "cash",
    "earn $",
    "make money",
    "winner",
    "congratulations",
    "urgent",
    "cheap",
    "discount",
    "$$$",
    "free money",
    "no obligation",
    "double your",
    "order now",
]

# A "word" for ALL-CAPS detection: run of letters (optionally with digits).
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
# Runs of 2+ exclamation marks.
_BANG_RUN_RE = re.compile(r"!{2,}")


def _found_triggers(text: str) -> list[str]:
    low = text.lower()
    return [t for t in SPAM_TRIGGERS if t in low]


def _shouted_words(text: str) -> list[str]:
    """ALL-CAPS words longer than 3 chars (letters only, must contain a letter)."""
    out: list[str] = []
    for m in _WORD_RE.finditer(text):
        w = m.group(0)
        if len(w) > 3 and w.isupper():
            out.append(w)
    return out


def lint(subject: str, body: str) -> list[str]:
    """Return human-readable issues found in the outgoing email.

    Flags: spam-trigger phrases, ALL-CAPS shouted words (len > 3), more than one
    exclamation mark total, more than two links (``http`` occurrences), and a
    subject that is ALL CAPS or contains ``!``.
    """
    subject = subject or ""
    body = body or ""
    combined = f"{subject}\n{body}"
    issues: list[str] = []

    for trig in _found_triggers(combined):
        issues.append(f"spam trigger phrase: '{trig}'")

    shouts = _shouted_words(combined)
    for w in shouts:
        issues.append(f"ALL-CAPS word: '{w}'")

    bang_count = combined.count("!")
    if bang_count > 1:
        issues.append(f"excessive exclamation marks: {bang_count}")

    link_count = combined.lower().count("http")
    if link_count > 2:
        issues.append(f"too many links: {link_count}")

    subj_letters = [c for c in subject if c.isalpha()]
    if subj_letters and subject.upper() == subject and subject.lower() != subject:
        issues.append("subject is ALL CAPS")
    if "!" in subject:
        issues.append("subject contains '!'")

    return issues


#: A GitHub repo link inside our own account, e.g.
#: ``https://github.com/suryaanandan1995-dotcom/devsecops-pipeline-templates``.
_OWN_REPO_LINK_RE = re.compile(
    r"(https?://(?:www\.)?github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+))",
    re.IGNORECASE,
)


def strip_unknown_repo_links(body: str, known: set[str] | None = None) -> str:
    """Remove links to our own repos that do not exist, leaving the prose intact.

    The last defence behind the prompt fix in :func:`outreach.pitch._project_links`.
    A prompt instruction is a request, not a guarantee, and the failure it guards
    against is measured: a live pitch drafted 2026-08-07 offered
    ``github.com/<user>/devsecops-pipeline-templates`` — "the code's here if you want
    to poke at it" — and that repo **404s**. The real one is
    ``devsecops-cicd-pipeline``.

    A dead link is worse than no link. The reader who clicks it is by definition the
    interested one, and what they learn is that the proof is fabricated — which is
    precisely the suspicion a portfolio link exists to remove.

    Only links under OUR account are touched, and only when we have a list of real
    repos to check against; an empty/None ``known`` means "cannot verify", so nothing
    is stripped rather than risking the removal of a good link. The surrounding
    sentence is left alone: rewriting prose is how a sanitiser starts producing
    garbled email.
    """
    if not body or not known:
        return body

    from config import get_settings

    settings = get_settings()
    own_account = (getattr(settings, "owner_github", "") or "").rstrip("/").rsplit("/", 1)[-1]
    if not own_account:
        return body
    known_lower = {k.lower() for k in known}

    def _repl(m: re.Match) -> str:
        account, repo = m.group(2), m.group(3).rstrip(".,);:")
        # Only OUR repos. An early draft rewrote github.com/kubernetes/kubernetes,
        # mangling a legitimate reference to someone else's project — a sanitiser that
        # edits third-party links is doing active damage, not hygiene.
        if account.lower() != own_account.lower():
            return m.group(0)
        if repo.lower() in known_lower:
            return m.group(0)
        # Fall back to the portfolio hub, which always exists, rather than deleting
        # the URL and leaving a dangling "the code's here:".
        return (settings.owner_site or "").rstrip("/") or m.group(0)

    return _OWN_REPO_LINK_RE.sub(_repl, body)


def sanitize(subject: str, body: str, known_repos: set[str] | None = None) -> tuple[str, str]:
    """Return a lightly-cleaned (subject, body) that is safe to send.

    Softens spam signals only:
      * collapses runs of ``!!!`` into a single ``.``
      * title-cases ALL-CAPS words longer than 3 chars (de-emphasis)
      * trims trailing exclamation marks from the subject
      * replaces links to non-existent repos of ours with the portfolio hub
        (see :func:`strip_unknown_repo_links`)

    Does not rewrite meaning and does not remove the cal.com link or opt-out
    line. Idempotent and safe on empty strings.
    """
    subject = subject or ""
    body = body or ""

    subject = _soften(subject)
    body = _soften(body)
    body = strip_unknown_repo_links(body, known_repos)

    # Trim trailing exclamation (and any whitespace) from the subject.
    subject = re.sub(r"[!\s]+$", "", subject)

    return subject, body


def _soften(text: str) -> str:
    if not text:
        return text
    # Collapse runs of 2+ '!' into a single '.'
    text = _BANG_RUN_RE.sub(".", text)
    # Title-case ALL-CAPS words longer than 3 chars.
    def _fix(m: re.Match) -> str:
        w = m.group(0)
        if len(w) > 3 and w.isupper():
            return w.capitalize()
        return w

    text = _WORD_RE.sub(_fix, text)
    return text


def score(subject: str, body: str) -> int:
    """Rough 0..100 inbox-friendliness score (100 = clean). Informational only.

    Starts at 100 and subtracts a penalty per issue reported by ``lint``,
    clamped to the 0..100 range.
    """
    issues = lint(subject, body)
    penalty = 0
    for issue in issues:
        if issue.startswith("spam trigger"):
            penalty += 15
        elif issue.startswith("ALL-CAPS word"):
            penalty += 8
        elif issue.startswith("excessive exclamation"):
            penalty += 12
        elif issue.startswith("too many links"):
            penalty += 10
        elif issue.startswith("subject is ALL CAPS"):
            penalty += 12
        elif issue.startswith("subject contains"):
            penalty += 8
        else:
            penalty += 5
    return max(0, min(100, 100 - penalty))
