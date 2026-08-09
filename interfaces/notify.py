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
from html import escape

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


def _source_lines(stats: dict) -> list[str]:
    """Per-source verdicts, worst first, or [] if the run reported none.

    The digest is the only part of this system anyone reads. ``fit.bottleneck`` can say
    "the lead mix is off-ICP" but not which sources caused it, so the actionable fix —
    retire the dead ones, widen the productive one — stayed invisible while every run
    looked uniformly mediocre. Dead/stale/unreachable sources are listed BEFORE the
    productive ones: a source silently yielding nothing is the finding, and burying it
    under working sources is how it went unnoticed for a month.
    """
    rows = stats.get("by_source") or {}
    if not rows:
        return []
    order = {
        # "broken" ranks above "dead": it is the only verdict that blames the code
        # rather than the market, so it is the only one fixable the same day.
        "broken": 0,
        "dead": 1,
        "starved": 2,
        "stale": 3,
        "unscored": 4,
        "off-ICP": 5,
        # "no-output" is a real finding and must not fall to the default rank 9, which
        # would put it BELOW email-blocked — i.e. last, read as "nothing to do here".
        # It means the opposite: the source scored well and still delivered nothing, so
        # something between the score and the queue is eating its leads. Ranked just
        # under off-ICP because both need a fix, but off-ICP names its own cause.
        "no-output": 6,
        # "email-blocked" ranks LAST, below off-ICP, because it is the only verdict
        # that is not a defect: the source is working and its leads are good, they
        # just carry no address (Adzuna sells the click). Ranked as a failure it
        # advised retiring the feed that found a $208k-$249k FDE role. Those leads
        # still reach the dashboard for human submission, so this line is a routing
        # note, not a problem to fix.
        "email-blocked": 7,
    }

    def rank(item) -> tuple[int, str]:
        verdict = item[1].get("verdict", "")
        return (order.get(verdict.split(":")[0], 9), item[0])

    return [
        f"  {name:<16} fetched={row.get('fetched', 0):<4} "
        f"considered={row.get('considered', row.get('fetched', 0)):<4} "
        f"new={row.get('new', 0):<4} contactable={row.get('contactable', 0):<4} "
        f"queued={row.get('queued', 0):<3} {row.get('verdict', '')}"
        for name, row in sorted(rows.items(), key=rank)
    ]


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
    source_lines = _source_lines(stats)
    if source_lines:
        lines += ["", "BY SOURCE (worst first — retire what never produces)"]
        lines += source_lines
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
    # Qualified leads with no contact address. These have no automated route at all —
    # auto_submit is permanently off — so the digest is the ONLY place they can ever
    # appear. They used to be scored at real cost and then dropped in silence, which
    # made the automation's dead end the owner's dead end too.
    apply = stats.get("apply_yourself") or []
    if apply:
        lines += [
            "",
            f"APPLY YOURSELF — {len(apply)} qualified lead(s) with no email "
            "(apply form only):",
        ]
        for lead in apply[:5]:
            company = lead.get("company") or lead.get("source", "")
            lines.append(
                f"  - [{lead.get('fit_score', 0)}] {lead.get('title', '')}"
                f"{f' — {company}' if company else ''}"
            )
            # The real listing URL, not a dashboard link: this is the one thing that
            # has to work without any hosted UI.
            lines.append(f"      {lead.get('url', '')}")
        if len(apply) > 5:
            lines.append(f"  ... and {len(apply) - 5} more (top 5 shown, best fit first)")

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
    source_lines = _source_lines(stats)
    source_html = (
        "<h3>By source</h3><pre style=\"background:#f6f8fa;padding:10px;"
        'border-radius:6px">' + "\n".join(source_lines) + "</pre>"
        if source_lines
        else ""
    )

    # Qualified leads with no email. The href is the REAL listing URL — with no hosted
    # dashboard, this link is the entire hand-off, so it has to work on its own.
    # Titles and companies come from third-party job posts, so they are escaped: this
    # HTML is assembled by f-string concatenation, and unescaped remote text in it is
    # an injection into the owner's own inbox.
    apply_rows = ""
    for lead in (stats.get("apply_yourself") or [])[:5]:
        url = escape(str(lead.get("url") or ""), quote=True)
        title = escape(str(lead.get("title") or ""))
        company = escape(str(lead.get("company") or lead.get("source") or ""))
        apply_rows += (
            f"<li><strong>[{int(lead.get('fit_score', 0))}]</strong> "
            f'<a href="{url}">{title}</a>'
            f'{f" — {company}" if company else ""}</li>'
        )
    apply_html = (
        "<h3>Apply yourself — qualified, no email (apply form only)</h3>"
        f"<ol>{apply_rows}</ol>"
        if apply_rows
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
{source_html}
{note}
<h3>Top leads awaiting your review</h3>
<ol>{rows}</ol>
{apply_html}
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
    # A dead source is named in the subject, because the body is skimmed and this is
    # the one failure that makes every downstream number meaningless.
    rows = stats.get("by_source") or {}

    def _with_verdict(prefix: str) -> list[str]:
        return sorted(
            n for n, r in rows.items() if (r.get("verdict") or "").startswith(prefix)
        )

    dead = _with_verdict("dead")
    # A *broken* source is named ahead of a dead one even on an otherwise good run:
    # it means a request is being rejected, which is a code fix available today,
    # whereas "dead" may just be an empty market.
    broken = _with_verdict("broken")
    if queued or emailed:
        subject = f"[Copilot] {queued} draft(s), {emailed} email(s) sent"
        if broken:
            subject += f" — {', '.join(broken[:2])} BROKEN"
        elif dead:
            subject += f" — {len(dead)} dead source(s)"
    elif broken:
        subject = f"[Copilot] SOURCE BROKEN — {', '.join(broken[:3])}"
    elif contactable:
        subject = f"[Copilot] NOTHING QUEUED — {contactable} contactable, 0 drafted"
    elif dead and len(dead) == len(rows):
        subject = f"[Copilot] EVERY SOURCE DEAD — {', '.join(dead[:3])}"
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
