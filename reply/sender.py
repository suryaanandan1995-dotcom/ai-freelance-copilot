"""Send a single auto-reply via stdlib SMTP.

Same transport pattern as ``interfaces/notify.py`` (STARTTLS + optional login).
Threading headers (``In-Reply-To`` / ``References``) are set so Gmail keeps the
conversation together, and the owner is ALWAYS BCC'd (opt_out_mailbox or
owner_email) so they see every autonomous reply and can jump in.

Hard safety properties:
  * NO-OP (returns ``False``) unless ``settings.auto_reply`` is True AND
    ``settings.smtp_host`` is set.
  * Never raises — any failure degrades to ``False`` so the runner loop is safe.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from config import get_settings

logger = logging.getLogger(__name__)


def _signature(settings) -> str:
    """Short identity signature appended to every reply."""
    return (
        f"\n\n— {settings.owner_name} · {settings.owner_site} · "
        f"{settings.owner_linkedin}"
    )


def send_reply(
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> bool:
    """Send one auto-reply. Returns True only if it was actually sent."""
    settings = get_settings()
    if not settings.auto_reply:
        logger.info("send_reply: auto_reply disabled — not sending")
        return False
    if not settings.smtp_host:
        logger.info("send_reply: smtp_host empty — not sending")
        return False
    if not to:
        return False

    try:
        sender = settings.smtp_from or settings.owner_email
        bcc = settings.opt_out_mailbox or settings.owner_email

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to
        if bcc:
            # Bcc header is stripped by send_message; pass the recipient explicitly.
            msg["Bcc"] = bcc
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        # References should chain any prior references plus the message we reply to.
        ref = " ".join(r for r in (references, in_reply_to) if r).strip()
        if ref:
            msg["References"] = ref
        msg.set_content((body or "") + _signature(settings))

        recipients = [to] + ([bcc] if bcc else [])
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
            smtp.ehlo()
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPException:
                pass  # server may not advertise STARTTLS (e.g. local test server)
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg, to_addrs=recipients)
        return True
    except Exception as exc:  # never break the runner loop
        logger.warning("send_reply: send to %s failed: %s", to, exc)
        return False
