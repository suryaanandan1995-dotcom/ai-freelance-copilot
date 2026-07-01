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

    uvicorn.run("interfaces.dashboard:app", host=args.host, port=args.port, reload=False)
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


def _cmd_content(args: argparse.Namespace) -> int:
    from content.engine import generate

    result = generate(kind=args.kind, topic=args.topic)
    print(result)
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

    p_content = sub.add_parser("content", help="Generate inbound content.")
    p_content.add_argument(
        "--kind", choices=["post", "case-study", "gig"], default="post"
    )
    p_content.add_argument("--topic", default="", help="Topic / subject for the content.")
    p_content.set_defaults(func=_cmd_content)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
