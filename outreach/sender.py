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
        subject, body = deliverability.sanitize(subject, body)
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
        return True
    except Exception as exc:  # never break the caller's loop
        logger.warning("send_outreach: send to %s failed: %s", to, exc)
        return False
