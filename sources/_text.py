"""Shared text normalisation for the HTML / comment-based source adapters.

Everything here exists because of what the *qualifier prompt* actually receives.
``agents/qualifier.py`` renders exactly five fields — Title, Company, Budget,
Tags, Description — and the model sees nothing else, so a field that is corrupt
is indistinguishable from a lead that is a bad fit. Measured across 50 live
``hn_hiring`` leads on 2026-08-07:

* 48 of 50 carried raw HTML entities (350 ``&#x2F;``, 100 ``&#x27;``, 16
  ``&quot;``, 14 ``&amp;``) — the model was reading ``We&#x27;re hiring`` and
  ``https:&#x2F;&#x2F;cogram.com``.
* 46 of 50 had a Title that was a truncated first line ending in ``...``, not a
  job title.
* Every Company was an HN username (``Company: kcartmell``).

Production runs never scored above 78 and mostly landed 28-58. These helpers are
shared rather than copied because the two HN adapters read the *same* threads
with the same conventions, and the entity bug survived in ``hn_freelancer`` as a
hand-rolled ``.replace("&amp;", "&")`` that fixed 14 of 480 measured entities.
"""
from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def strip_html(text: str | None) -> str:
    """Plain text from an HTML fragment: tags removed **first**, then entities decoded.

    The order is load-bearing, not cosmetic. HN escapes the angle brackets a
    commenter typed (``&lt;your-name&gt;@corp.com``, ``<3``) while emitting real
    ``<p>``/``<a>`` markup of its own. Unescaping first synthesises tag-looking
    text out of that content, and ``<[^>]+>`` then deletes everything up to the
    next ``>`` — which can swallow a paragraph, including the address the whole
    lead is worth. Stripping first means only HN's real markup is removed, and
    the commenter's literal brackets survive for ``outreach.extract`` to read.
    """
    return html.unescape(_TAG_RE.sub(" ", text or "")).strip()


def first_line(text: str, limit: int = 120) -> str:
    """The first line, clipped. Fallback title when no convention is detectable."""
    line = text.strip().splitlines()[0] if text.strip() else ""
    return (line[: limit - 3] + "...") if len(line) > limit else line


#: The pipe convention only holds in a comment's *header*. ``strip_html`` turns
#: ``<p>`` into a space, so "the first line" is in practice the entire comment —
#: which is why 46 of 50 titles were a 117-character truncation. A pipe 900
#: characters in is prose, not a field separator, and the header fields measured on
#: the live threads sit well under 80 characters. Length is therefore what
#: separates the convention from a stray pipe.
_MAX_SEGMENT = 80

#: Words that mean a header segment names *what the job is*. Deliberately matched
#: as prefixes (``engineer`` covers "engineers"/"engineering") and deliberately
#: missing bare ``dev`` and bare ``contract``: "Exeter, Devon" is a location and
#: "Full-time, Contract" is an employment type, and both sit in the same header as
#: the role. Picking either as the Title would just move the corruption.
_ROLE_WORDS: tuple[str, ...] = (
    "engineer",
    "developer",
    "programmer",
    "architect",
    "devops",
    "devsecops",
    "sre",
    "ops",
    "scientist",
    "analyst",
    "designer",
    "researcher",
    "manager",
    "lead",
    "head of",
    "director",
    "cto",
    "founding",
    "intern",
    "technician",
    "specialist",
    "consultant",
    "contractor",
    "administrator",
    "admin",
    "role",
    "position",
    "backend",
    "back-end",
    "frontend",
    "front-end",
    "full stack",
    "full-stack",
    "fullstack",
    "platform",
    "infrastructure",
    "security",
    "data",
    "product",
    "qa",
)

_ROLE_RE = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in _ROLE_WORDS) + r")")

#: Thread-side markers, not companies. A "SEEKING FREELANCER" header puts the
#: marker in segment 0 and the client's name in segment 1, so taking segment 0
#: unconditionally sends ``Company: SEEKING FREELANCER`` to the scorer — the same
#: class of defect as ``Company: kcartmell``, one segment further along.
_MARKER_RE = re.compile(
    r"^(?:seeking\s+(?:freelancer|work|contractor|employment)|for\s+hire|hiring)\b"
)


def _segments(text: str | None) -> list[str]:
    """Header segments of the pipe convention, or [] when it isn't in use."""
    body = (text or "").strip()
    first = body.splitlines()[0] if body else ""
    if "|" not in first:
        return []
    parts = [s.strip() for s in first.split("|")]
    # A long leading segment means the pipe is prose, not a delimiter.
    if not parts[0] or len(parts[0]) > _MAX_SEGMENT:
        return []
    return parts


def _clean(segment: str) -> str:
    """A header segment without the URL posters wedge in beside the company name."""
    return re.sub(r"\s{2,}", " ", _URL_RE.sub(" ", segment)).strip(" -–—,;:·|")


def detect_company(text: str | None) -> str | None:
    """Best-effort company name from a ``Company | Role | ...`` header.

    Only the first two segments are considered: past that we are reading the body,
    and a guessed company is worse than ``unknown`` because the researcher agent
    then goes and researches a phrase. Live measurement: 50 of 50 ``hn_hiring``
    leads had an HN username here, e.g. ``Company: m00dy``.
    """
    for segment in _segments(text)[:2]:
        candidate = _clean(segment)
        if candidate and not _MARKER_RE.match(candidate.lower()) and len(candidate) <= 60:
            return candidate
    return None


def detect_role(text: str | None) -> str | None:
    """The role segment of a ``Company | Role | Location | Type`` header, or None.

    None means "this comment does not follow the convention" — many don't, and the
    caller must fall back to the first line rather than promote a location to the
    Title. Real measured failure this replaces (2026-08-07, one of 46):
    ``'Snout  https:&#x2F;&#x2F;snout.com&#x2F;  | Multiple Engineering + Product
    Roles | Remote US or Ontario, Canada | Ful...'``
    """
    for segment in _segments(text)[1:]:
        if len(segment) > _MAX_SEGMENT:
            continue
        if _ROLE_RE.search(segment.lower()):
            cleaned = _clean(segment)
            if cleaned:
                return cleaned
    return None


def company_from_title(title: str | None) -> str | None:
    """Company from a ``<Role> at <Company>`` title, as job boards write it.

    NoDesk entries carry no ``author``, so 9 of 10 live ``contra_startup`` leads
    reached the scorer as ``Company: unknown`` even though the name was sitting in
    the title ("Software engineer at Sticker Mule"). Split on the *last* " at " so
    "Engineer at Acme" survives a role that contains the word itself.
    """
    if not title or " at " not in title:
        return None
    candidate = title.rsplit(" at ", 1)[1].strip(" -–—,;:")
    if not candidate or len(candidate) > 60:
        return None
    return candidate
