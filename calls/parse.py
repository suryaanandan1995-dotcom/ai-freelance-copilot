"""Parse a cal.com notification email into a booking.

Deliberately label-agnostic where it can be. cal.com's plain-text body is laid out as
``What`` / ``When`` / ``Who`` / ``Where`` sections, but those labels are localisable and
have changed before, so the two fields that must never be wrong are derived from
content instead of position:

* the **invitee address** is "every address in the body, minus ours, minus cal.com's" —
  which holds regardless of layout, wording or language;
* the **booking id** is the cal.com video link's last path segment, falling back to the
  Message-ID, so it is stable across re-reads of the same mail.

Everything else (display name, human-readable time) is best-effort: getting it wrong
makes the briefing uglier, while getting the address or the id wrong would mean briefing
the owner about the wrong person or briefing them twice.
"""
from __future__ import annotations

import re

#: Providers whose addresses carry no company information. A booking from one of these
#: cannot be researched, which is itself the useful signal: it means "you have no idea
#: who this is, so open the call with a question, not a pitch".
FREEMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "yahoo.co.uk",
        "ymail.com",
        "proton.me",
        "protonmail.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "gmx.com",
        "mail.com",
        "zoho.com",
        "yandex.com",
        "rediffmail.com",
    }
)

_ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_VIDEO_RE = re.compile(r"https?://[^\s<>\"]*cal\.com/video/([A-Za-z0-9_\-]+)")
_MEETING_URL_RE = re.compile(
    r"https?://(?:[^\s<>\"]*cal\.com/video/[^\s<>\"]+"
    r"|meet\.google\.com/[^\s<>\"]+"
    r"|[^\s<>\"]*zoom\.us/[^\s<>\"]+"
    r"|teams\.microsoft\.com/[^\s<>\"]+)"
)
#: A weekday name anchors the date line without hardcoding a date format.
_WHEN_DATE_RE = re.compile(
    r"^(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\b.*\d{4}", re.IGNORECASE
)
_ROLE_SUFFIX_RE = re.compile(r"\s*(?:Organizer|Guest|Attendee|Host)\s*$", re.IGNORECASE)
#: cal.com's own section headings and status words. Each of these can appear on the line
#: directly above an address, where a name is otherwise expected.
_LABEL_WORDS = frozenset(
    {
        "what",
        "when",
        "who",
        "where",
        "why",
        "description",
        "invitee",
        "attendees",
        "event",
        "location",
        "notes",
        "confirmed",
        "cancelled",
        "canceled",
        "rescheduled",
        "pending",
        "email",
        "name",
        "cal.com",
    }
)
#: Splits " on Friday, August 21, 2026" off the end of a subject-derived name.
_SUBJECT_TAIL_RE = re.compile(
    r"\s+on\s+(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\b", re.IGNORECASE
)
_WHEN_TIME_RE = re.compile(r"\d{1,2}[:.]\d{2}\s*(?:am|pm)?\s*[-–—]\s*\d{1,2}[:.]\d{2}", re.IGNORECASE)

#: Phrases that mean "a booking now exists" and "a booking no longer exists". Matched
#: case-insensitively against subject + body. A cancellation that briefed as a booking
#: would send the owner to a dead call, so the two are never conflated.
#: Kept broad on purpose. cal.com words the same event differently depending on who the
#: recipient is ("A new event has been scheduled" to the organizer, "This meeting is
#: scheduled" / "Confirmed:" to the attendee) and has reworded it between releases. Only
#: mail that already passed the ``From: *cal.com`` check reaches here, so a loose match
#: costs nothing, while a narrow one silently drops the booking it was written for.
_BOOKED_MARKERS = (
    "has been scheduled",
    "is scheduled",
    "new event",
    "confirmed",
)
_CANCELLED_MARKERS = (
    "has been cancelled",
    "has been canceled",
    "event is cancelled",
    "event is canceled",
    "cancelled:",
    "canceled:",
)
_RESCHEDULED_MARKERS = ("has been rescheduled", "rescheduled:")


def classify(subject: str, body: str) -> str:
    """``"booked"``, ``"cancelled"``, ``"rescheduled"`` or ``""`` (not a booking mail).

    Cancellation is checked FIRST: a cancellation email quotes the original event and
    therefore also contains the booking wording, so testing "booked" first would read
    every cancellation as a new booking.
    """
    text = f"{subject or ''}\n{body or ''}".lower()
    if any(m in text for m in _CANCELLED_MARKERS):
        return "cancelled"
    if any(m in text for m in _RESCHEDULED_MARKERS):
        return "rescheduled"
    if any(m in text for m in _BOOKED_MARKERS):
        return "booked"
    return ""


def _invitee_email(body: str, ours: set[str]) -> str:
    """First address in the body that is neither ours nor infrastructure."""
    for raw in _ADDRESS_RE.findall(body or ""):
        addr = raw.strip().lower().rstrip(".")
        if addr in ours:
            continue
        domain = addr.rpartition("@")[2]
        # cal.com's own senders and generic no-reply mailboxes are never the invitee.
        if domain.endswith("cal.com") or domain.endswith("calendly.com"):
            continue
        if addr.startswith(("no-reply@", "noreply@", "notifications@", "support@")):
            continue
        return addr
    return ""


def _looks_like_a_name(text: str) -> bool:
    """Reject the template's own furniture, which also sits above the address.

    Without this the line above the invitee's address is taken on trust, and in a layout
    where that line is a status word the briefing goes out addressed to "Confirmed" — the
    kind of wrongness that makes the owner distrust every other field in the email.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > 80:
        return False
    if stripped.endswith((":", "?", "!", ".")) or "://" in stripped:
        return False
    if len(stripped.split()) > 5:  # a sentence, not a person
        return False
    return stripped.strip(" -–—").lower() not in _LABEL_WORDS


def _name_from_body(body: str, invitee_email: str) -> str:
    """The display name printed next to the invitee's address in the ``Who`` block."""
    if not invitee_email:
        return ""
    lines = [ln.strip() for ln in (body or "").splitlines()]
    for index, line in enumerate(lines):
        if invitee_email in line.lower():
            for previous in reversed(lines[max(0, index - 3):index]):
                if not previous or "@" in previous:
                    continue
                # Strip cal.com's role labels, which appear either on their own line or
                # glued to the name ("Senthil GovindarajanGuest").
                cleaned = _ROLE_SUFFIX_RE.sub("", previous).strip(" .-")
                if _looks_like_a_name(cleaned):
                    return cleaned
            break
    return ""


def _name_from_subject(subject: str, owner_name: str) -> str:
    """cal.com titles a booking ``"<n> min meeting between <organizer> and <guest>"``."""
    subject = (subject or "").strip()
    if " and " not in subject:
        return ""
    head, _, tail = subject.rpartition(" and ")
    candidate = tail.strip()
    owner = (owner_name or "").strip().lower()
    if owner and owner in candidate.lower():
        # Owner listed second; the guest is on the other side of " and ".
        candidate = head.split("between", 1)[-1].strip()
    # Subjects frequently carry the date too ("… and Senthil Govindarajan on Friday,
    # August 21, 2026"). Without this the whole date ends up inside the person's name.
    candidate = _SUBJECT_TAIL_RE.split(candidate)[0].strip(" .,-")
    if candidate and "@" not in candidate and len(candidate) < 80:
        return candidate
    return ""


def _invitee_name(subject: str, body: str, invitee_email: str, owner_name: str) -> str:
    """Best-effort display name; never empty.

    The body is tried BEFORE the subject: the body prints the name adjacent to the
    address we already resolved, so it cannot name the wrong person, whereas the subject
    is a sentence that also contains durations, dates and the owner's own name. Falls
    back to the address local part, because "CALL BOOKED — " with nothing after it reads
    like a bug rather than a booking.
    """
    for candidate in (
        _name_from_body(body, invitee_email),
        _name_from_subject(subject, owner_name),
    ):
        if candidate:
            return candidate

    local = invitee_email.partition("@")[0]
    return local.replace(".", " ").replace("_", " ").title() if local else "(unknown)"


def _when_text(body: str) -> str:
    """A human-readable date+time, joined from whichever lines look like one."""
    date_line = ""
    time_line = ""
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if not date_line and _WHEN_DATE_RE.match(line):
            date_line = line
        elif not time_line and _WHEN_TIME_RE.search(line):
            time_line = line
        if date_line and time_line:
            break
    return " ".join(part for part in (date_line, time_line) if part).strip()


def parse_booking(
    *,
    subject: str,
    body: str,
    message_id: str | None,
    owner_addresses: set[str],
    owner_name: str = "",
) -> dict | None:
    """A booking dict, or ``None`` when this mail is not a cal.com booking notice.

    Returns ``kind`` ("booked" / "cancelled" / "rescheduled"), ``booking_uid``,
    ``invitee_name``, ``invitee_email``, ``when_text``, ``join_url`` and ``subject``.
    """
    kind = classify(subject, body)
    if not kind:
        return None

    ours = {a.strip().lower() for a in owner_addresses if a and a.strip()}
    invitee_email = _invitee_email(body, ours)

    video = _VIDEO_RE.search(body or "")
    join = _MEETING_URL_RE.search(body or "")
    if video:
        booking_uid = video.group(1)
    elif message_id:
        # No video link (an in-person or phone booking): the Message-ID still makes
        # "alert exactly once" work, which is the only job this field has.
        booking_uid = message_id.strip()
    elif invitee_email:
        # Last resort — pair the invitee with the time so two different bookings by the
        # same person are still distinct rows.
        booking_uid = f"{invitee_email}|{_when_text(body)}"
    else:
        return None

    return {
        "kind": kind,
        "booking_uid": booking_uid[:256],
        "invitee_name": _invitee_name(subject, body, invitee_email, owner_name)[:256],
        "invitee_email": invitee_email[:320],
        "when_text": _when_text(body)[:256],
        "join_url": (join.group(0) if join else "")[:512],
        "subject": (subject or "").strip()[:512],
    }


def is_freemail(address: str) -> bool:
    """True when the address carries no company to research."""
    return (address or "").rpartition("@")[2].lower() in FREEMAIL_DOMAINS
