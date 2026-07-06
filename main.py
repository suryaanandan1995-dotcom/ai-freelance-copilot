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

    def _run() -> dict:
        stats = post_to_linkedin(kind=args.kind, topic=args.topic, publish=args.publish)
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

    p_reply = sub.add_parser(
        "reply",
        help=(
            "Read prospect replies (IMAP) and respond autonomously in the owner's "
            "voice. Fully auto-negotiates but never commits pricing/scope/timeline/"
            "contracts (defers to a cal.com call), BCCs the owner, capped per thread. "
            "Gated by COPILOT_AUTO_REPLY + SMTP config; no-op otherwise."
        ),
    )
    p_reply.set_defaults(func=_cmd_reply)

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
    p_li.add_argument("--topic", default="", help="Topic / angle for the post.")
    p_li.add_argument(
        "--publish",
        action="store_true",
        help="Actually publish to LinkedIn (default: draft only).",
    )
    p_li.set_defaults(func=_cmd_linkedin_post)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
