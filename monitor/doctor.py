"""Autonomous health monitor ("doctor").

Runs on a schedule, checks the whole system, AUTO-FIXES the safe/known classes of
runtime problems, and emails a precise diagnosis for anything that needs a human.

Auto-fixed automatically (whitelisted, reversible):
  * schema drift (missing columns) -> the same ``_ensure_columns`` self-heal that
    ``init_db`` runs; reported under ``auto_fixed``.

Detected + alerted (a human decides — we deliberately do NOT auto-edit/deploy code):
  * database unreachable
  * LinkedIn access token invalid / expired
  * repeated workflow-run failures in the last 24h

SAFETY BOUNDARY: this monitor never rewrites source code and never flips a safety
flag. Autonomous source edits are a hazard (unreviewed AI code, secret leakage,
platform bans). "Self-improvement" in this system is scoped to:
  * STRATEGY tuning with auto-revert  -> optimizer/ (learns from reply-rate outcomes)
  * schema self-heal                  -> here + init_db
Anything outside that whitelist is reported for a human, not silently changed.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)


def _check_schema() -> tuple[dict, list[str]]:
    """Heal missing columns (safe auto-fix), then VERIFY the heal. Returns (check, fixed).

    Verification is the point. ``_ensure_columns`` swallows each failed ``ALTER TABLE``
    by design, so this check used to report ``ok: up to date`` whenever the heal added
    nothing — whether the schema was complete or the ALTERs had all failed. Those need
    opposite responses, so the state is now read back from the database.
    """
    try:
        from db.session import _ensure_columns, engine, init_db, missing_columns

        init_db()
        added = _ensure_columns(engine)
        still_missing = missing_columns(engine)
        if still_missing:
            return {
                "name": "schema",
                "ok": False,
                "detail": (
                    f"{len(still_missing)} column(s) missing and could not be added: "
                    f"{', '.join(still_missing)} — queries touching them fail at "
                    "runtime; check DDL permissions for the database user"
                ),
            }, added
        detail = f"healed {len(added)} column(s): {', '.join(added)}" if added else "up to date"
        return {"name": "schema", "ok": True, "detail": detail}, added
    except Exception as exc:  # noqa: BLE001
        return {"name": "schema", "ok": False, "detail": f"schema check failed: {exc}"}, []


def _check_database() -> dict:
    try:
        from sqlalchemy import text

        from db.session import get_session

        with get_session() as session:
            session.execute(text("SELECT 1"))
        return {"name": "database", "ok": True, "detail": "reachable"}
    except Exception as exc:  # noqa: BLE001
        return {"name": "database", "ok": False, "detail": f"database unreachable: {exc}"}


def _check_recent_runs(hours: int = 24) -> dict:
    try:
        from db.models import RunRecord
        from db.session import get_session

        since = datetime.now(UTC) - timedelta(hours=hours)
        with get_session() as session:
            failed = (
                session.query(RunRecord)
                .filter(RunRecord.ok.is_(False), RunRecord.created_at >= since)
                .all()
            )
        if not failed:
            return {"name": "recent_runs", "ok": True, "detail": f"no failures in {hours}h"}
        by_wf: dict[str, int] = {}
        for r in failed:
            by_wf[r.workflow] = by_wf.get(r.workflow, 0) + 1
        summary = ", ".join(f"{k}: {v}" for k, v in sorted(by_wf.items()))
        return {"name": "recent_runs", "ok": False, "detail": f"failures in {hours}h — {summary}"}
    except Exception as exc:  # noqa: BLE001
        return {"name": "recent_runs", "ok": False, "detail": f"could not read run history: {exc}"}


def _check_linkedin_token() -> dict:
    try:
        from config import get_settings

        settings = get_settings()
        if not (settings.linkedin_access_token or "").strip():
            return {"name": "linkedin_token", "ok": True, "detail": "not configured (skipped)"}
        from linkedin.client import LinkedInClient, LinkedInError

        try:
            who = LinkedInClient(settings=settings).whoami()
            return {"name": "linkedin_token", "ok": True, "detail": f"valid ({who.get('name', '')})"}
        except LinkedInError as exc:
            return {
                "name": "linkedin_token",
                "ok": False,
                "detail": (
                    "token invalid/expired — regenerate and update "
                    f"COPILOT_LINKEDIN_ACCESS_TOKEN ({exc})"
                ),
            }
    except Exception as exc:  # noqa: BLE001
        return {"name": "linkedin_token", "ok": False, "detail": f"token check error: {exc}"}


def _check_imap_login(timeout: float = 20.0) -> dict:
    """Actually log in to IMAP, so a broken reader is found before it costs anything.

    The funnel check (``monitor.funnel.check_reply_detection_alive``) can only *infer*
    a broken reader from a long enough silence, which needs 25 sends and still cannot
    separate "credentials are wrong" from "nobody replied". This proves it directly:
    IMAP either accepts the SMTP credentials or it does not, and that answer is
    available today, at the cost of one login a day.

    Worth checking on a schedule rather than trusting once, because the failure is
    invisible in the place it happens — ``reply.inbox.fetch_replies`` degrades every
    IMAP error to ``[]`` — and its consequence is active rudeness: nobody gets marked
    replied, so follow-ups keep nudging prospects who already answered.

    Reads nothing and sends nothing: connect, authenticate, log out.
    """
    try:
        import imaplib

        from config import get_settings

        settings = get_settings()
        if not (settings.smtp_user or "").strip() or not (settings.smtp_password or "").strip():
            return {"name": "imap_login", "ok": True, "detail": "not configured (skipped)"}
        host = settings.resolved_imap_host()
        if not host:
            # Credentials exist but no IMAP host could be resolved. With smtp_host set,
            # that means a send-only relay (SendGrid/SES/Brevo/...) which has no IMAP
            # service — so replies are landing in a mailbox nothing is reading. That is
            # a real defect, not a skip, and it is silent by construction.
            if (settings.smtp_host or "").strip():
                return {
                    "name": "imap_login",
                    "ok": False,
                    "detail": (
                        f"smtp_host ({settings.smtp_host}) is a send-only relay with no "
                        "IMAP service, and COPILOT_IMAP_HOST is unset — so replies are "
                        "arriving in the mailbox behind the From address and nothing is "
                        "reading it. Nobody gets marked replied, and follow-ups keep "
                        "nudging prospects who already answered. Set COPILOT_IMAP_HOST "
                        "to that mailbox's IMAP server."
                    ),
                }
            return {"name": "imap_login", "ok": True, "detail": "no IMAP host (skipped)"}

        derived = "derived from smtp_host" if not (settings.imap_host or "").strip() else "explicit"
        conn = None
        try:
            conn = imaplib.IMAP4_SSL(host, settings.imap_port, timeout=timeout)
            conn.login(settings.smtp_user, settings.smtp_password)
            return {
                "name": "imap_login",
                "ok": True,
                "detail": f"{host} accepted the SMTP credentials ({derived})",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "name": "imap_login",
                "ok": False,
                "detail": (
                    f"cannot log in to {host} ({derived}) with smtp_user/smtp_password: "
                    f"{exc}. Replies are being read into a void: fetch_replies swallows "
                    "this and returns [], so nobody is marked replied and follow-ups "
                    "keep nudging prospects who already answered. Fix by setting "
                    "COPILOT_IMAP_HOST to this provider's IMAP server, or (Gmail) by "
                    "using an app password and enabling IMAP."
                ),
            }
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass
    except Exception as exc:  # noqa: BLE001
        return {"name": "imap_login", "ok": False, "detail": f"IMAP check error: {exc}"}


def format_report(result: dict) -> str:
    lines = [f"Health monitor — {'ALL OK' if result['ok'] else 'ISSUES FOUND'}", ""]
    for c in result.get("checks", []):
        lines.append(f"  [{'ok' if c['ok'] else 'XX'}] {c['name']}: {c['detail']}")
    if result.get("auto_fixed"):
        lines += ["", "Auto-fixed:"] + [f"  + {a}" for a in result["auto_fixed"]]
    if result.get("issues"):
        lines += ["", "Needs attention:"] + [f"  ! {i}" for i in result["issues"]]
    return "\n".join(lines)


def run_healthcheck(alert: bool = True) -> dict:
    """Check everything, auto-heal the safe stuff, alert on the rest.

    Returns ``{ok, checks, auto_fixed, issues}``. When ``alert`` and any issue is
    found, emails the owner a diagnosis (via runlog.send_alert). Never raises for a
    *found issue* — issues are data; only an unexpected internal error propagates.
    """
    checks: list[dict] = []
    auto_fixed: list[str] = []

    schema_check, healed = _check_schema()
    checks.append(schema_check)
    auto_fixed.extend(healed)
    checks.append(_check_database())
    checks.append(_check_recent_runs())
    checks.append(_check_linkedin_token())
    checks.append(_check_imap_login())
    # Output-based checks: a run that exits 0 while emailing nobody is a failure
    # this monitor previously could not see (24 such runs went unreported).
    try:
        from monitor.funnel import funnel_checks

        checks.extend(funnel_checks())
    except Exception as exc:  # noqa: BLE001
        checks.append(
            {"name": "funnel", "ok": False, "detail": f"funnel checks failed: {exc}"}
        )

    issues = [c["detail"] for c in checks if not c["ok"]]
    result = {"ok": not issues, "checks": checks, "auto_fixed": auto_fixed, "issues": issues}

    if issues and alert:
        try:
            from runlog import send_alert

            send_alert(
                subject=f"[Copilot] health monitor found {len(issues)} issue(s)",
                body=format_report(result),
            )
        except Exception as exc:  # alerting must never break the monitor
            logger.warning("run_healthcheck: could not send alert: %s", exc)

    return result
