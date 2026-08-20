"""AI Freelance Copilot — command-line entrypoint.

Subcommands:
    run        Run one discovery -> qualify -> research -> draft -> queue pass.
    dashboard  Serve the human approval dashboard (FastAPI/uvicorn).
    mcp        Run the MCP stdio server for AI clients.
    build-kb   (Re)build the portfolio RAG knowledge base.
    stats      Print pipeline stats (lead counts by status).
    content    Generate inbound content (post / case-study / gig).
    reply      Read prospect replies (IMAP) and respond autonomously (guardrailed).
    followup   Send spaced follow-ups to cold-emailed leads who never replied.
    optimize   Autonomously tune the outreach strategy (auto-reverts regressions).

Dashboard / content / uvicorn are imported lazily INSIDE their handlers so that
``import main`` works even before those sibling modules exist.
"""
from __future__ import annotations

import argparse
import sys


def _cmd_run(args: argparse.Namespace) -> int:
    from pipeline import run_pipeline
    from runlog import record_run

    stats = record_run(
        "outreach",
        lambda: run_pipeline(
            limit=args.limit, notify=args.notify, auto_email=args.auto_email
        ),
    )
    try:
        from rich import print as rprint

        rprint(stats)
    except Exception:
        print(stats)
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    import uvicorn

    kwargs: dict = {"host": args.host, "port": args.port, "reload": False}
    if args.tls_cert and args.tls_key:
        # Serve HTTPS (self-signed cert is fine for a private/internal dashboard).
        kwargs["ssl_certfile"] = args.tls_cert
        kwargs["ssl_keyfile"] = args.tls_key
    uvicorn.run("interfaces.dashboard:app", **kwargs)
    return 0


def _cmd_mcp(_args: argparse.Namespace) -> int:
    from interfaces.mcp_server import mcp

    mcp.run()
    return 0


def _cmd_build_kb(_args: argparse.Namespace) -> int:
    import sys

    from scripts.build_kb import main as build_kb_main

    # build_kb.main() parses sys.argv itself; isolate it from our subcommand args.
    saved = sys.argv
    sys.argv = [saved[0]]
    try:
        build_kb_main()
    finally:
        sys.argv = saved
    return 0


def _cmd_stats(_args: argparse.Namespace) -> int:
    from pipeline import pipeline_stats

    stats = pipeline_stats()
    try:
        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(title="AI Freelance Copilot — pipeline")
        table.add_column("Status")
        table.add_column("Count", justify="right")
        for status, count in stats["by_status"].items():
            table.add_row(status, str(count))
        table.add_row("[bold]total leads[/bold]", f"[bold]{stats['total_leads']}[/bold]")
        table.add_row("total proposals", str(stats["total_proposals"]))
        console.print(table)
    except Exception:
        print(stats)

    # Status counts say what exists; KPIs say whether any of it worked. Printing only
    # the former is how a month of zero replies looked like a healthy pipeline.
    try:
        from monitor.kpi import format_kpis, funnel

        print()
        print(format_kpis(funnel()))
    except Exception as exc:  # noqa: BLE001
        print(f"(KPIs unavailable: {exc})")
    return 0


def _cmd_kpi(args: argparse.Namespace) -> int:
    """Print the outcome funnel: contactable -> emailed -> replied -> booked -> won."""
    from monitor.kpi import format_kpis, funnel

    print(format_kpis(funnel(window_days=args.days)))
    return 0


def _cmd_reply(_args: argparse.Namespace) -> int:
    from reply.runner import run_reply_pass
    from runlog import record_run

    stats = record_run("reply", lambda: run_reply_pass())
    try:
        from rich import print as rprint

        rprint(stats)
    except Exception:
        print(stats)
    return 0


def _cmd_followup(_args: argparse.Namespace) -> int:
    from followup.runner import run_followups
    from runlog import record_run

    stats = record_run("followup", lambda: run_followups())
    try:
        from rich import print as rprint

        rprint(stats)
    except Exception:
        print(stats)
    return 0


def _cmd_optimize(_args: argparse.Namespace) -> int:
    from runlog import record_run

    def _run() -> dict:
        from optimizer.optimizer import run_optimizer

        return run_optimizer()

    stats = record_run("optimize", _run)
    try:
        from rich import print as rprint

        rprint(stats)
    except Exception:
        print(stats)
    return 0


def _cmd_content(args: argparse.Namespace) -> int:
    from content.engine import generate

    result = generate(kind=args.kind, topic=args.topic)
    print(result)
    return 0


def _cmd_linkedin_post(args: argparse.Namespace) -> int:
    from linkedin.poster import post_to_linkedin
    from runlog import record_run

    # Capture stats even when we raise, so a failed publish (e.g. an expired token)
    # both (a) triggers record_run's owner failure-alert email + red CI, and
    # (b) still prints what happened here.
    holder: dict = {}

    # An empty --topic auto-rotates. The rotation used to live as a bash array in
    # .github/workflows/linkedin.yml, where it was untestable and drifted away from
    # what the pipeline targeted; content.topics is now the single source of truth.
    topic = args.topic
    if not topic:
        import datetime as _date

        from content.topics import topic_for_day

        topic = topic_for_day(_date.date.today().timetuple().tm_yday)

    def _run() -> dict:
        stats = post_to_linkedin(kind=args.kind, topic=topic, publish=args.publish)
        holder.update(stats or {})
        if (stats or {}).get("status") == "failed":
            # Raise so record_run alerts the owner + the CI step goes red. A
            # "skipped" (daily cap / gate off) is NOT a failure and won't alert.
            raise RuntimeError(f"LinkedIn publish failed: {stats.get('reason')}")
        return stats

    failed = False
    try:
        stats = record_run("linkedin", _run)
    except Exception:
        stats = holder  # record_run already persisted + emailed the alert
        failed = True

    body = stats.pop("body", "") if isinstance(stats, dict) else ""
    try:
        from rich import print as rprint

        rprint(stats)
    except Exception:
        print(stats)
    if body:
        print("\n--- post body ---\n" + body)
    return 1 if failed else 0


def _cmd_ledger(args: argparse.Namespace) -> int:
    """Print WHO we contacted, and what came back — one masked line per send.

    Written on 2026-08-20, the day a cal.com call was booked and the pipeline could not
    say who had booked it. ``kpi`` counts the funnel (6 emailed, 1 replied); nothing
    named the rows. The counts live in the database, the database DSN is a repo secret,
    the dashboard is deliberately unhosted, and the run log recorded only *failed*
    sends — so the one question that matters after a booking, "which company is this?",
    had no answer anywhere. A funnel you cannot enumerate is a funnel you cannot work.

    Runs on GitHub (``ledger.yml``, workflow_dispatch) so the answer needs no local
    machine, and masks local-parts because this repository — and therefore its Actions
    log — is public.
    """
    import datetime as _dt

    from db.models import LeadRecord, OutreachRecord, ProposalRecord
    from db.session import get_session, init_db
    from outreach.sender import mask_address

    days = max(1, int(getattr(args, "days", 30) or 30))
    show_body = bool(getattr(args, "body", False))
    cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=days)

    init_db()
    with get_session() as session:
        rows = (
            session.query(OutreachRecord)
            .filter(OutreachRecord.sent_at >= cutoff)
            .order_by(OutreachRecord.sent_at.desc())
            .all()
        )
        leads = {
            lead.id: lead
            for lead in session.query(LeadRecord)
            .filter(LeadRecord.id.in_([r.lead_id for r in rows if r.lead_id]))
            .all()
        } if rows else {}

        # What we actually claimed. Walking into a booked call without it means
        # risking contradicting your own pitch in the first two minutes — the pitch
        # was written by an agent, so the human on the call has not read it.
        pitches: dict[int, str] = {}
        if show_body and rows:
            for proposal in (
                session.query(ProposalRecord)
                .filter(ProposalRecord.lead_id.in_([r.lead_id for r in rows if r.lead_id]))
                .order_by(ProposalRecord.created_at.asc())
                .all()
            ):
                pitches[proposal.lead_id] = proposal.body

        print(f"Outreach ledger — last {days} day(s): {len(rows)} contact(s)\n")
        if not rows:
            print("  (nothing sent in this window)")
            return 0
        for r in rows:
            lead = leads.get(r.lead_id)
            # Booked first, then replied: the strongest signal leads the line, so the
            # row worth acting on is findable by eye in a log with no search.
            if r.call_booked_at:
                flag = f"CALL BOOKED {r.call_booked_at:%Y-%m-%d}"
            elif r.replied:
                flag = "replied"
            else:
                flag = f"no reply ({r.followups_sent} follow-up(s))"
            print(
                f"  {r.sent_at:%Y-%m-%d %H:%M}  {mask_address(r.email):<34}"
                f"  {r.status:<10}  {flag}"
            )
            print(f"      subject: {r.subject or '(none)'}")
            if lead is not None:
                print(
                    f"      lead   : {lead.company or '?'} — {lead.title or '?'} "
                    f"[{lead.source}]"
                )
                if lead.url:
                    print(f"      post   : {lead.url}")
            else:
                # Sends predate the lead_id link, or the lead row was deleted. Say so
                # rather than print a blank line that reads like "no such company".
                print("      lead   : (not linked to a stored lead)")
            if show_body:
                pitch = pitches.get(r.lead_id or -1)
                if pitch:
                    print("      --- what we claimed ---")
                    for line in pitch.splitlines():
                        print(f"      {line}")
                    print("      --- end ---")
                else:
                    print("      (no stored pitch for this contact)")
    return 0


def _cmd_calls(args: argparse.Namespace) -> int:
    """Sweep the inbox for cal.com bookings and email the owner a briefing for each.

    Runs inside the reply pass too; it exists as its own command so the daily monitor can
    also call it. A booking made on a Saturday would otherwise wait until Monday's reply
    schedule, and a call on Monday morning is exactly the one that needs the briefing.
    """
    from calls.detect import scan_for_bookings

    if getattr(args, "list", False):
        return _list_calls()

    stats = scan_for_bookings()
    print(
        "calls: scanned={scanned} booked={booked} cancelled={cancelled} "
        "briefed={briefed} already_known={already_known} purged={purged} "
        "errors={errors}".format(**stats)
    )
    # Deliberately no addresses in the output: Actions logs on a public repo are public.
    if stats["booked"] and not stats["briefed"]:
        # The row exists but the owner was not told, which is the only failure mode that
        # matters here — say so loudly instead of exiting 0 on a silent success.
        print("calls: WARNING a booking was detected but no briefing was sent (SMTP?)")
    return 0


def _list_calls() -> int:
    """Print every stored booking. Reads only; sends nothing; masks every address.

    The counters above say *how many* bookings were found, which is exactly as useless as
    "0 calls booked" was: on the first production sweep the answer was ``booked=2`` and
    naming the second one required a database password. Same lesson as the ledger — a
    funnel you cannot enumerate is a funnel you cannot work.
    """
    from db.models import CallRecord, LeadRecord
    from db.session import get_session, init_db
    from outreach.sender import mask_address

    init_db()
    with get_session() as session:
        rows = session.query(CallRecord).order_by(CallRecord.created_at.desc()).all()
        if not rows:
            print("calls: no bookings recorded yet.")
            return 0
        print(f"calls: {len(rows)} booking(s) recorded\n")
        for row in rows:
            company = ""
            if row.lead_id:
                lead = session.query(LeadRecord).filter(LeadRecord.id == row.lead_id).first()
                if lead is not None:
                    company = f" | {lead.company or '?'} — {lead.title or '?'}"
            flag = row.status.upper()
            if not row.notified:
                # The one state worth acting on: the call exists and the owner was
                # never told. Say so per row, not just in the aggregate.
                flag += " (BRIEFING NOT SENT)"
            print(f"  [{flag}] {row.invitee_name or '(unknown)'} — {row.when_text or '?'}")
            print(f"      who    : {mask_address(row.invitee_email)} ({row.origin}){company}")
            print(f"      subject: {row.subject or '(none)'}")
            print(f"      uid    : {row.booking_uid}")
    return 0


def _cmd_doctor(_args: argparse.Namespace) -> int:
    from monitor.doctor import format_report, run_healthcheck
    from runlog import record_run

    result = record_run("monitor", run_healthcheck)
    print(format_report(result))
    # Exit 0 even when issues are found: the monitor itself ran fine and already
    # emailed a diagnosis. A non-zero code is reserved for the monitor crashing
    # (record_run re-raises that). This keeps "issue found" distinct from "monitor broke".
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="copilot", description="AI Freelance Copilot")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run one pipeline pass (discover/qualify/draft).")
    p_run.add_argument("--limit", type=int, default=None, help="Max leads to process.")
    p_run.add_argument("--notify", action="store_true", help="Send a digest after the run.")
    p_run.add_argument(
        "--auto-email",
        action="store_true",
        help=(
            "Auto-send a short cold email to queued, email-reachable, strong-fit "
            "leads (deduped, rate-limited, opt-out). Still gated by COPILOT_AUTO_EMAIL "
            "+ SMTP config; no-op otherwise. Never submits to Upwork/LinkedIn."
        ),
    )
    p_run.set_defaults(func=_cmd_run)

    p_dash = sub.add_parser("dashboard", help="Serve the human approval dashboard.")
    p_dash.add_argument("--host", default="0.0.0.0")
    p_dash.add_argument("--port", type=int, default=8000)
    p_dash.add_argument("--tls-cert", default=None, help="PEM cert file → serve HTTPS.")
    p_dash.add_argument("--tls-key", default=None, help="PEM private key file → serve HTTPS.")
    p_dash.set_defaults(func=_cmd_dashboard)

    p_mcp = sub.add_parser("mcp", help="Run the MCP stdio server.")
    p_mcp.set_defaults(func=_cmd_mcp)

    p_kb = sub.add_parser("build-kb", help="(Re)build the portfolio RAG knowledge base.")
    p_kb.set_defaults(func=_cmd_build_kb)

    p_stats = sub.add_parser("stats", help="Print pipeline stats.")
    p_stats.set_defaults(func=_cmd_stats)

    p_kpi = sub.add_parser(
        "kpi",
        help="Print outcome KPIs (contactable -> emailed -> replied -> booked -> won).",
        description=(
            "Reports what the system achieved, not that it ran. Names the bottleneck "
            "stage so effort goes where it changes the outcome."
        ),
    )
    p_kpi.add_argument(
        "--days", type=int, default=30, help="Rolling window in days (default 30)."
    )
    p_kpi.set_defaults(func=_cmd_kpi)

    p_ledger = sub.add_parser(
        "ledger",
        help="List WHO was contacted and what came back (masked addresses).",
        description=(
            "kpi counts the funnel; ledger names the rows. Answers 'a call was just "
            "booked — which company is that?' from the Actions log alone."
        ),
    )
    p_ledger.add_argument(
        "--days", type=int, default=30, help="Rolling window in days (default 30)."
    )
    p_ledger.add_argument(
        "--body",
        action="store_true",
        help=(
            "Also print the pitch that was sent. Needed before a booked call: the "
            "email was written by an agent, so the human taking the call has not read "
            "it and can otherwise contradict their own claims in the first two minutes."
        ),
    )
    p_ledger.set_defaults(func=_cmd_ledger)

    p_reply = sub.add_parser(
        "reply",
        help=(
            "Read prospect replies over IMAP. Two independently gated halves: "
            "DETECTION (on by default via COPILOT_REPLY_DETECTION) records each "
            "inbound, marks the lead replied so follow-ups stop, and honours "
            "opt-outs — it sends nothing; RESPONSE (COPILOT_AUTO_REPLY) also answers "
            "autonomously in the owner's voice, never committing pricing/scope/"
            "timeline/contracts (defers to a cal.com call), BCCs the owner, capped "
            "per thread. Needs SMTP/IMAP credentials either way."
        ),
    )
    p_reply.set_defaults(func=_cmd_reply)

    p_calls = sub.add_parser(
        "calls",
        help="Detect cal.com bookings in the inbox and email a briefing for each.",
        description=(
            "The cal.com webhook needs a public host and has therefore never fired, so "
            "booked calls were invisible: the funnel showed 0 booked while a real call "
            "sat unread. This reads the confirmation email instead, stamps "
            "call_booked_at, and emails who booked, why they probably booked and how to "
            "run the 15 minutes. Read-only against mail; one briefing per booking."
        ),
    )
    p_calls.add_argument(
        "--list",
        action="store_true",
        help=(
            "Do not sweep or send: print the bookings already recorded (masked "
            "addresses), including any whose briefing never left. Answers 'it says two "
            "calls were booked — who?' without a database password."
        ),
    )
    p_calls.set_defaults(func=_cmd_calls)

    p_followup = sub.add_parser(
        "followup",
        help=(
            "Send spaced, polite follow-ups to cold-emailed leads who never "
            "replied (bounded touches, min days of silence, daily-capped, "
            "suppression-aware). Gated by COPILOT_AUTO_EMAIL + SMTP; no-op otherwise."
        ),
    )
    p_followup.set_defaults(func=_cmd_followup)

    p_optimize = sub.add_parser(
        "optimize",
        help=(
            "Autonomously tune the outreach STRATEGY (pitch/subject variant + fit "
            "threshold), measure reply rate, and auto-revert a change that hurts. "
            "Never edits source or safety invariants. Gated by COPILOT_SELF_OPTIMIZE; "
            "no-op otherwise."
        ),
    )
    p_optimize.set_defaults(func=_cmd_optimize)

    p_content = sub.add_parser("content", help="Generate inbound content.")
    p_content.add_argument(
        "--kind", choices=["post", "case-study", "gig"], default="post"
    )
    p_content.add_argument("--topic", default="", help="Topic / subject for the content.")
    p_content.set_defaults(func=_cmd_content)

    p_li = sub.add_parser(
        "linkedin-post",
        help=(
            "Generate a RAG-grounded post and (with --publish) publish it to your OWN "
            "LinkedIn feed via the official API. Gated by COPILOT_LINKEDIN_AUTO_POST + "
            "an OAuth token (w_member_social); deduped + daily-capped. Without --publish "
            "it only drafts. Never touches other accounts and never scrapes."
        ),
    )
    p_li.add_argument("--kind", choices=["post", "case-study", "gig"], default="post")
    p_li.add_argument(
        "--topic",
        default="",
        help="Topic / angle for the post. Blank auto-rotates via content.topics.",
    )
    p_li.add_argument(
        "--publish",
        action="store_true",
        help="Actually publish to LinkedIn (default: draft only).",
    )
    p_li.set_defaults(func=_cmd_linkedin_post)

    p_doctor = sub.add_parser(
        "doctor",
        help=(
            "Health monitor: checks DB, schema, recent run failures, and the "
            "LinkedIn token; auto-heals schema drift; emails the owner a diagnosis "
            "for anything that needs attention. Never edits source or safety flags."
        ),
    )
    p_doctor.set_defaults(func=_cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
