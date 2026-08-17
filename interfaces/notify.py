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


#: How many apply-yourself leads the digest lists in full.
#:
#: Was 5, chosen when the section was written and before any production run measured
#: the real volume. The first live run produced **46** qualified uncontactable leads
#: scoring 97 down to 72, so the digest showed 5 and hid 41 — re-imposing a second,
#: much higher bar that the owner never set, on top of the ``min_fit_score`` bar these
#: leads had already cleared. That is the same defect the section was built to fix
#: (output the automation produced never reaching the owner), one order of magnitude
#: smaller. The ceiling exists only so a pathological run can't mail a thousand lines;
#: when it bites, ``_dropped_note`` names what was cut and which lever shortens it.
_APPLY_SHOWN = 30


def _dropped_note(apply: list[dict]) -> str:
    """Describe what the display cap cut, in terms that make the omission checkable.

    A bare "and N more" says nothing about whether the hidden leads were worth seeing.
    Because the list is sorted best-fit-first, truncation is really a score cut, so the
    honest report is the score it cut at — plus the setting that shortens the list, so
    the fix is the owner's choice rather than a number buried in this module.
    """
    hidden = len(apply) - _APPLY_SHOWN
    cutoff = apply[_APPLY_SHOWN].get("fit_score", 0)
    return (
        f"and {hidden} more at score {cutoff} or below "
        f"(top {_APPLY_SHOWN} shown; raise min_fit_score to shorten this list)"
    )


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
        # "filtered-out" ranks with off-ICP, NOT with dead. The source reached its
        # upstream and read a real feed; our own keyword list rejected every row. It used
        # to report as "dead: fetched nothing", listed at rank 1 under "retire what never
        # produces" — advice that would delete a working feed. The lever is the keyword
        # list, which is why it sits beside the other targeting verdict.
        "filtered-out": 5,
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
        # ``read`` is what the adapter looked at before its own keyword filters; "-" when
        # the adapter doesn't report it. It is printed FIRST because it is what makes
        # fetched=0 legible: read=13 fetched=0 is a filter decision, read=- fetched=0 is
        # a silent upstream, and those need opposite fixes.
        f"  {name:<16} read={row.get('scanned', '-')!s:<5} "
        f"fetched={row.get('fetched', 0):<4} "
        f"considered={row.get('considered', row.get('fetched', 0)):<4} "
        f"new={row.get('new', 0):<4} contactable={row.get('contactable', 0):<4} "
        # Printed next to ``contactable``, never merged into it. The two columns answer
        # different questions — "does this feed publish addresses" vs "can we find them
        # anyway" — and a feed that publishes none is still the wrong feed to widen even
        # when discovery rescues its leads.
        f"found={row.get('discovered', 0):<3} "
        f"queued={row.get('queued', 0):<3} {row.get('verdict', '')}"
        for name, row in sorted(rows.items(), key=rank)
    ]


def _discovered_li(stats: dict) -> str:
    """The discovery line for the HTML digest, or '' when discovery did not run.

    Omitted rather than shown as 0 when no lookups happened: a permanent "Discovered: 0"
    row trains the reader to ignore it, and then the day discovery breaks it says exactly
    what it said while working. Present means it ran, and it always carries the
    denominator — 0 of 0 and 0 of 40 need opposite fixes.
    """
    attempts = int(stats.get("discovery_attempts") or 0)
    if not attempts:
        return ""
    found = int(stats.get("discovered") or 0)
    return f"  <li>Addresses discovered: {found} of {attempts} looked up</li>"


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
    # Always with its denominator. "Discovered: 0" is unreadable on its own — 0 of 0 means
    # every qualified lead already had an address, 0 of 40 means discovery ran forty times
    # and failed, and this project has acted on the wrong one of those before.
    attempts = stats.get("discovery_attempts")
    if attempts:
        lines.insert(
            2,
            f"  Discovered  : {stats.get('discovered', 0)} of {attempts} "
            "qualified lead(s) with no published address",
        )
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
        for lead in apply[:_APPLY_SHOWN]:
            company = lead.get("company") or lead.get("source", "")
            lines.append(
                f"  - [{lead.get('fit_score', 0)}] {lead.get('title', '')}"
                f"{f' — {company}' if company else ''}"
            )
            # The real listing URL, not a dashboard link: this is the one thing that
            # has to work without any hosted UI.
            lines.append(f"      {lead.get('url', '')}")
        if len(apply) > _APPLY_SHOWN:
            lines.append(f"  ... {_dropped_note(apply)}")

    # The packs, for the top few of that same list. A link is not a hand-off: applying
    # still meant re-opening the post and writing the pitch from scratch, 262 times in the
    # measured window. These are the highest-scoring ones with the note already written,
    # so submitting is a paste rather than a rewrite. Rendered AFTER the list, not instead
    # of it — the list is complete and the packs are a subset, and a reader who sees only
    # 5 detailed blocks would reasonably assume there were only 5 leads.
    packs = stats.get("apply_packs") or []
    if packs:
        lines += [
            "",
            f"READY TO PASTE — {len(packs)} of those {len(apply)}, best fit first:",
        ]
        for pack in packs:
            lines += ["", str(pack.get("text") or "")]

    # Addresses discovery found on a domain it GUESSED from the company name. Nothing was
    # sent to these. They are here because a human can accept or bin one in two seconds
    # from the address and the page it came from, and because a guessed address that is
    # never shown can never be evaluated — the path stays off until these read right.
    proposed = stats.get("proposed_contacts") or []
    if proposed:
        lines += [
            "",
            f"POSSIBLE ADDRESSES — {len(proposed)} found on a guessed company domain "
            "(NOT emailed; check one and reply if they look right):",
        ]
        for item in proposed[:_APPLY_SHOWN]:
            company = item.get("company") or ""
            lines.append(
                f"  - [{item.get('fit_score', 0)}] {item.get('email', '')}"
                f"{f' — {company}' if company else ''}"
            )
            lines.append(f"      post: {item.get('lead_url', '')}")
            lines.append(f"      from: {item.get('source_url', '')}")
        if len(proposed) > _APPLY_SHOWN:
            lines.append(f"  ... and {len(proposed) - _APPLY_SHOWN} more")

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
    apply_all = stats.get("apply_yourself") or []
    apply_rows = ""
    for lead in apply_all[:_APPLY_SHOWN]:
        url = escape(str(lead.get("url") or ""), quote=True)
        title = escape(str(lead.get("title") or ""))
        company = escape(str(lead.get("company") or lead.get("source") or ""))
        apply_rows += (
            f"<li><strong>[{int(lead.get('fit_score', 0))}]</strong> "
            f'<a href="{url}">{title}</a>'
            f'{f" — {company}" if company else ""}</li>'
        )
    # The HTML digest is the one most likely to be read, so it must not silently show a
    # shorter list than the plaintext part of the same message.
    apply_more = (
        f'<p style="color:#666">… {escape(_dropped_note(apply_all))}</p>'
        if len(apply_all) > _APPLY_SHOWN
        else ""
    )
    apply_html = (
        f"<h3>Apply yourself — {len(apply_all)} qualified, no email "
        "(apply form only)</h3>"
        f"<ol>{apply_rows}</ol>{apply_more}"
        if apply_rows
        else ""
    )

    # Paste-and-submit packs for the best of that list. ``ApplyPack.to_html`` escapes every
    # remote string it interpolates (titles and companies come from third-party job posts),
    # which is why this is the one place here that concatenates HTML it did not escape
    # itself — see outreach/apply_pack.ApplyPack.to_html.
    packs = stats.get("apply_packs") or []
    pack_html = ""
    if packs:
        blocks = "".join(str(pack.get("html") or "") for pack in packs)
        pack_html = (
            f"<h3>Ready to paste — {len(packs)} of those {len(apply_all)}, "
            f"best fit first</h3>{blocks}"
        )

    # Guessed-domain addresses, not emailed. Every field is remote text (the address came
    # off a stranger's web page), so all of it is escaped — this is the least trustworthy
    # input in the whole digest.
    proposed = stats.get("proposed_contacts") or []
    proposed_html = ""
    if proposed:
        rows_html = ""
        for item in proposed[:_APPLY_SHOWN]:
            email = escape(str(item.get("email") or ""))
            company = escape(str(item.get("company") or ""))
            post = escape(str(item.get("lead_url") or ""), quote=True)
            page = escape(str(item.get("source_url") or ""), quote=True)
            rows_html += (
                f"<li><strong>[{int(item.get('fit_score', 0))}]</strong> "
                f"<code>{email}</code>{f' — {company}' if company else ''}<br>"
                f'<a href="{post}">the post</a> · '
                f'<a href="{page}">where the address was found</a></li>'
            )
        more = (
            f'<p style="color:#666">… and {len(proposed) - _APPLY_SHOWN} more</p>'
            if len(proposed) > _APPLY_SHOWN
            else ""
        )
        proposed_html = (
            f"<h3>Possible addresses — {len(proposed)} on a guessed company domain</h3>"
            '<p style="color:#666">Nothing was emailed to these. The domain was derived '
            "from the company name, not from the post, so a human confirms before any of "
            "them is used.</p>"
            f"<ol>{rows_html}</ol>{more}"
        )
    return f"""\
<html><body style="font-family:system-ui,Arial,sans-serif">
<h2>AI Freelance Copilot — pipeline digest</h2>
{kpi_html}
<h3>This run</h3>
<ul>
  <li>Contactable: {stats.get('contactable', 0)}</li>
{_discovered_li(stats)}
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
{pack_html}
{proposed_html}
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
