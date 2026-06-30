"""Auto-email outreach subsystem — the only fully-automatable, ToS-safe channel.

Sending email from the owner's own address (not via a platform API) is not a
platform ToS violation. Emails go ONLY to leads that publicly posted a contact
address looking to hire (B2B legitimate interest), are rate-limited, deduped
against an OutreachRecord table (never email an address twice), carry a plain
opt-out footer, and honour a suppression list. Everything is gated behind the
``auto_email`` master switch and is a no-op by default.

Upwork / LinkedIn submission stays human-only; this subsystem is email-only.
"""
from __future__ import annotations

from .extract import find_contact_email
from .pitch import draft_email
from .sender import send_outreach
from .suppression import is_suppressed, load_suppressed

__all__ = [
    "find_contact_email",
    "draft_email",
    "send_outreach",
    "is_suppressed",
    "load_suppressed",
]
