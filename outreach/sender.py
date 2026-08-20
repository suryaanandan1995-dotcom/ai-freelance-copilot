"""Send a single outreach email via stdlib SMTP.

Same transport pattern as ``interfaces/notify.py`` (STARTTLS + optional login).
Two hard safety properties:

  1. It is a NO-OP returning ``False`` unless ``settings.auto_email`` is True AND
     ``settings.smtp_host`` is set — so the default config can never send mail.
  2. A compliance footer (real identity + plain-text opt-out) is ALWAYS appended
     to the body, satisfying B2B legitimate-interest / PECR / CAN-SPAM identity
     and opt-out requirements.

It never raises: any failure degrades to ``False`` so the pipeline loop is safe.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from config import get_settings
from outreach import deliverability

logger = logging.getLogger(__name__)


def _known_repos(settings) -> set[str]:
    """Directory names of the portfolio repos, i.e. the repo links that actually work.

    Read from disk rather than hardcoded so the list cannot drift from reality — the
    defect this guards against was a *plausible* repo name that never existed, so a
    hand-maintained allowlist would be vulnerable to the identical mistake.

    Returns an empty set if the path is unreadable, which
    :func:`outreach.deliverability.strip_unknown_repo_links` treats as "cannot
    verify" and therefore leaves every link untouched. Failing open is right here: a
    wrongly-stripped good link costs a proof point, while a crash costs the send.
    """
    try:
        from pathlib import Path

        root = Path(settings.portfolio_repos_path or "..").expanduser().resolve()
        return {
            p.name
            for p in root.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        }
    except Exception as exc:  # noqa: BLE001 - never break a send over this
        logger.warning("send_outreach: could not list portfolio repos: %s", exc)
        return set()


def mask_address(address: str) -> str:
    """``HR@bactrix.com`` -> ``h***@bactrix.com``: identifies the company, not the person.

    Every send used to be logged as nothing at all — only *failures* named the
    recipient — so on 2026-08-20, with a cal.com call booked the morning after a run
    that reported ``'emailed': 1``, there was no readable record anywhere of who that
    one email went to. The run log had the four *proposed* addresses (never sent) and
    the rejected Bactrix post, and the one address that mattered was the only one
    absent. "Sent 1" without a recipient is a success counter with no subject.

    Masked rather than plain because **this repository is public**, so its Actions logs
    are public: printing a prospect's full address publishes it to scrapers and turns
    our own outreach into someone else's spam problem. The domain is what answers
    "which company is this?", and the domain is not private — the post published it.
    """
    address = (address or "").strip()
    if not address:
        return "(none)"
    local, _, domain = address.rpartition("@")
    if not domain or not local:
        # Not an address shape; keep one character so a typo is still recognisable.
        return f"{address[0]}***"
    return f"{local[0].lower()}***@{domain.lower()}"


def _footer(settings) -> str:
    """Plain-text identity + opt-out footer, always appended to every send."""
    mailbox = settings.opt_out_mailbox or settings.owner_email
    return (
        f"\n\n— {settings.owner_name} · {settings.owner_site}\n"
        f"Not relevant? Reply 'unsubscribe' to {mailbox} and I won't email again."
    )


def send_outreach(to: str, subject: str, body: str) -> bool:
    """Send one cold email. Returns True only if it was actually sent.

    No-op (returns False) when auto_email is off or SMTP is not configured.
    """
    settings = get_settings()
    if not settings.auto_email:
        logger.info("send_outreach: auto_email disabled — not sending")
        return False
    if not settings.smtp_host:
        logger.info("send_outreach: smtp_host empty — not sending")
        return False
    if not to:
        return False

    try:
        subject, body = deliverability.sanitize(subject, body, _known_repos(settings))
        sender = settings.smtp_from or settings.owner_email
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to
        if settings.opt_out_mailbox or settings.owner_email:
            msg["Reply-To"] = settings.opt_out_mailbox or settings.owner_email
        msg.set_content((body or "") + _footer(settings))

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
            smtp.ehlo()
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPException:
                pass  # server may not advertise STARTTLS (e.g. local test server)
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
        # A send is the single most consequential event this system performs, and until
        # now it was the only one that left no trace in the log. Attribution has to be
        # possible from the Actions log alone, because that is the only production
        # surface readable without the database DSN (a repo secret).
        logger.info("send_outreach: SENT to %s | subject=%r", mask_address(to), subject)
        return True
    except Exception as exc:  # never break the caller's loop
        logger.warning("send_outreach: send to %s failed: %s", mask_address(to), exc)
        return False
