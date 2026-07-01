"""Run recording + failure alerting.

Every workflow (outreach / reply / followup) is executed through
``record_run(workflow, fn)``, which persists a ``RunRecord`` row at the end of
the run — success or failure — powering run history and analytics. On failure it
also emails the owner an alert and re-raises so CI surfaces the error.

``send_alert`` uses the same stdlib smtplib transport as ``outreach.sender`` /
``interfaces.notify`` (STARTTLS + optional login). Unlike prospect email, alerts
go to the OWNER and therefore ignore ``auto_email`` — they are gated only on SMTP
being configured, and never raise.
"""
from __future__ import annotations

import logging
import smtplib
from collections.abc import Callable
from email.message import EmailMessage

from config import get_settings

logger = logging.getLogger(__name__)


def _persist_run(workflow: str, ok: bool, cost_usd: float, stats: dict, error: str | None) -> None:
    """Write one RunRecord. Never raises — recording a run must not break the run."""
    try:
        from db.models import RunRecord
        from db.session import get_session, init_db

        init_db()
        with get_session() as session:
            session.add(
                RunRecord(
                    workflow=workflow,
                    ok=ok,
                    cost_usd=cost_usd,
                    stats=stats or {},
                    error=error,
                )
            )
    except Exception as exc:  # persistence must never crash the run
        logger.warning("record_run: could not persist RunRecord for %s: %s", workflow, exc)


def record_run(workflow: str, fn: Callable[[], dict]) -> dict:
    """Run ``fn`` and persist a RunRecord for the outcome.

    On success: persist ``ok=True`` with the returned stats + its ``cost_usd``,
    then return the stats. On exception: persist ``ok=False`` with the error,
    send a failure alert to the owner, and re-raise (so CI surfaces the failure).
    """
    try:
        stats = fn() or {}
    except Exception as exc:
        err = str(exc)[:2000]
        _persist_run(workflow, ok=False, cost_usd=0.0, stats={}, error=err)
        try:
            send_alert(
                subject=f"[Copilot] {workflow} run FAILED",
                body=f"The '{workflow}' workflow raised an error:\n\n{err}\n",
            )
        except Exception:  # send_alert already swallows, but be doubly safe
            logger.warning("record_run: alert for failed %s run could not be sent", workflow)
        raise

    try:
        cost_usd = float(stats.get("cost_usd", 0) or 0)
    except (TypeError, ValueError):
        cost_usd = 0.0
    _persist_run(workflow, ok=True, cost_usd=cost_usd, stats=stats, error=None)
    return stats


def send_alert(subject: str, body: str) -> bool:
    """Email a failure alert to the owner. Returns True only if actually sent.

    NO-OP (returns False) when ``smtp_host`` is empty. Ignores ``auto_email``
    (alerts go to the owner, not prospects). Never raises.
    """
    settings = get_settings()
    if not settings.smtp_host:
        logger.info("send_alert: smtp_host empty — alert not sent")
        return False

    try:
        sender = settings.smtp_from or settings.owner_email
        recipient = settings.alert_email or settings.owner_email
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recipient
        msg.set_content(body or "")

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
    except Exception as exc:  # alerting must never break anything
        logger.warning("send_alert: could not send alert to owner: %s", exc)
        return False
