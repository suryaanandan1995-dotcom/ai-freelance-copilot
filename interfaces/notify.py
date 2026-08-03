"""Outbound digest notifications (email / WhatsApp / none).

When a pipeline run finishes, it can send the owner a short digest of what was
queued for review — counts plus the top leads, each linking back to the approval
dashboard. This module dispatches on ``settings.notify_channel`` and NEVER
raises: any failure (missing config, SMTP error, HTTP error) degrades to a
``False`` return so a notification hiccup can't break a run.

Channels:
  * ``email``    — stdlib smtplib + EmailMessage, HTML + plaintext (primary).
  * ``whatsapp`` — WhatsApp Business Cloud API via requests (optional).
  * ``none``     — disabled.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from config import get_settings

logger = logging.getLogger(__name__)


def _lead_url(base: str, lead_id) -> str:
    return f"{base.rstrip('/')}/lead/{lead_id}"


def _kpi_block() -> str:
    """Rolling outcome KPIs, or '' if unavailable.

    The digest used to report activity only (fetched/new/dropped), all of which looked
    healthy through a month that produced 1 email and 0 replies. Outcomes now lead.
    """
    try:
        from monitor.kpi import format_kpis, funnel

        return format_kpis(funnel())
    except Exception as exc:  # noqa: BLE001 - a digest must never fail on KPIs
        logger.warning("notify: KPI block unavailable: %s", exc)
        return ""


def _plaintext(stats: dict, top_leads: list[dict], base_url: str) -> str:
    lines = ["AI Freelance Copilot — pipeline digest", ""]

    kpis = _kpi_block()
    if kpis:
        lines += [kpis, ""]

    lines += [
        "THIS RUN",
        f"  Contactable : {stats.get('contactable', 0)}",
        f"  Fetched     : {stats.get('fetched', 0)}",
        f"  New         : {stats.get('new', 0)}",
        f"  Queued      : {stats.get('queued', 0)}",
        f"  Emailed     : {stats.get('emailed', 0)}",
        f"  Dropped     : {stats.get('dropped', 0)}",
        f"  Skipped     : {stats.get('skipped', 0)}",
        f"  Cost        : ${stats.get('cost_usd', 0.0):.4f}",
    ]
    skipped_reasons = stats.get("emailed_skipped") or {}
    if skipped_reasons:
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(skipped_reasons.items()))
        lines.append(f"  Not emailed : {reasons}")
    fit = stats.get("fit") or {}
    if fit.get("bottleneck"):
        lines.append(
            f"  Fit scores  : n={fit.get('n')} p50={fit.get('p50')} "
            f"max={fit.get('max')} (bar {fit.get('threshold')})"
        )
        lines.append(f"  Bottleneck  : {fit['bottleneck']}")
    if stats.get("budget_exhausted"):
        lines.append("NOTE: per-run budget cap reached; run stopped early.")
    lines += ["", "Top leads awaiting your review:"]
    if top_leads:
        for lead in top_leads[:5]:
            lines.append(
                f"  - [{lead.get('fit_score', 0)}] {lead.get('title', '')} "
                f"-> {_lead_url(base_url, lead.get('id'))}"
            )
    else:
        lines.append("  (nothing queued this run)")
    lines += ["", "Nothing was submitted automatically — review and submit yourself."]
    return "\n".join(lines)


def _html(stats: dict, top_leads: list[dict], base_url: str) -> str:
    rows = ""
    for lead in top_leads[:5]:
        url = _lead_url(base_url, lead.get("id"))
        rows += (
            f"<li><strong>[{lead.get('fit_score', 0)}]</strong> "
            f'<a href="{url}">{lead.get("title", "")}</a></li>'
        )
    if not rows:
        rows = "<li><em>nothing queued this run</em></li>"
    note = ""
    if stats.get("budget_exhausted"):
        note = "<p><em>Per-run budget cap reached; run stopped early.</em></p>"
    kpis = _kpi_block()
    kpi_html = (
        f'<h3>Outcomes</h3><pre style="background:#f6f8fa;padding:10px;'
        f'border-radius:6px">{kpis}</pre>'
        if kpis
        else ""
    )
    fit = stats.get("fit") or {}
    fit_html = (
        f'<p style="color:#666"><strong>Bottleneck:</strong> {fit["bottleneck"]}</p>'
        if fit.get("bottleneck")
        else ""
    )
    return f"""\
<html><body style="font-family:system-ui,Arial,sans-serif">
<h2>AI Freelance Copilot — pipeline digest</h2>
{kpi_html}
<h3>This run</h3>
<ul>
  <li>Contactable: {stats.get('contactable', 0)}</li>
  <li>Fetched: {stats.get('fetched', 0)}</li>
  <li>New: {stats.get('new', 0)}</li>
  <li>Queued: {stats.get('queued', 0)}</li>
  <li>Emailed: {stats.get('emailed', 0)}</li>
  <li>Dropped: {stats.get('dropped', 0)}</li>
  <li>Skipped: {stats.get('skipped', 0)}</li>
  <li>Cost: ${stats.get('cost_usd', 0.0):.4f}</li>
</ul>
{fit_html}
{note}
<h3>Top leads awaiting your review</h3>
<ol>{rows}</ol>
<p style="color:#666">Nothing was submitted automatically — review and submit yourself.</p>
</body></html>"""


def _send_email(stats: dict, top_leads: list[dict]) -> bool:
    settings = get_settings()
    if not settings.smtp_host:
        logger.info("notify: smtp_host empty — email digest skipped")
        return False

    sender = settings.smtp_from or settings.owner_email
    recipient = settings.notify_email_to or settings.owner_email
    base_url = settings.dashboard_base_url

    msg = EmailMessage()
    # The subject is the only part guaranteed to be read. A queue count alone read as
    # healthy for a month of zero sends, so surface sends + contactability too.
    queued = stats.get("queued", 0)
    emailed = stats.get("emailed", 0)
    contactable = stats.get("contactable", 0)
    if queued or emailed:
        subject = f"[Copilot] {queued} draft(s), {emailed} email(s) sent"
    elif contactable:
        subject = f"[Copilot] NOTHING QUEUED — {contactable} contactable, 0 drafted"
    else:
        subject = "[Copilot] NO CONTACTABLE LEADS — sourcing needs attention"
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(_plaintext(stats, top_leads, base_url))
    msg.add_alternative(_html(stats, top_leads, base_url), subtype="html")

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


def _send_whatsapp(stats: dict, top_leads: list[dict]) -> bool:
    settings = get_settings()
    if not (settings.whatsapp_token and settings.whatsapp_phone_id and settings.whatsapp_to):
        logger.info("notify: WhatsApp config incomplete — digest skipped")
        return False

    import requests

    base_url = settings.dashboard_base_url
    body = _plaintext(stats, top_leads, base_url)
    url = f"https://graph.facebook.com/v20.0/{settings.whatsapp_phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": settings.whatsapp_to,
        "type": "text",
        "text": {"body": body[:4096]},
    }
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    return True


def send_digest(stats: dict, top_leads: list[dict]) -> bool:
    """Send the pipeline digest over the configured channel. Never raises."""
    settings = get_settings()
    channel = (settings.notify_channel or "none").lower()
    try:
        if channel == "email":
            return _send_email(stats, top_leads or [])
        if channel == "whatsapp":
            return _send_whatsapp(stats, top_leads or [])
        return False
    except Exception as exc:
        logger.warning("notify: %s digest failed: %s", channel, exc)
        return False
