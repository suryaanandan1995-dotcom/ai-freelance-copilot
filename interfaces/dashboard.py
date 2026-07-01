"""Human-in-the-loop approval dashboard (FastAPI + Jinja2).

A polished dark-theme UI for reviewing AI-drafted proposals, running the
learning loop (won/lost), and generating inbound marketing content. NOTHING is
ever auto-submitted to a platform — a human reviews and submits every proposal
by hand (auto-submit violates Upwork / LinkedIn / Fiverr ToS).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Iterator
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

import analytics
from config import get_settings
from content.engine import generate as generate_content
from db.models import LeadRecord, LeadStatus, OutreachRecord, ProposalRecord, ProposalStatus
from db.session import get_session, init_db
from observability import metrics
from rag.learning import record_outcome

_log = logging.getLogger(__name__)

# Ensure tables exist on startup so a fresh deployment (empty DB, before any
# pipeline run) serves every page instead of 500-ing. Idempotent.
try:
    init_db()
except Exception:  # pragma: no cover - never block app import on a transient DB issue
    _log.exception("init_db() on startup failed")

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

app = FastAPI(title="AI Freelance Copilot — Dashboard")

# --- HTTP Basic auth -----------------------------------------------------------
# Protects the UI + write actions before the dashboard is exposed on a public URL.
# If ``settings.dashboard_password`` is blank, auth is DISABLED (local/SSH-tunnel
# dev + the offline tests keep working). When it's set, every protected route
# requires the configured user/password; ``/healthz``, ``/metrics``, and the
# HMAC-verified ``POST /webhooks/cal`` are intentionally left open. Settings are
# read per-request so the password can be toggled without restarting the app.
_basic = HTTPBasic(auto_error=False)


def require_auth(
    credentials: HTTPBasicCredentials | None = Depends(_basic),
) -> None:
    """FastAPI dependency enforcing HTTP Basic auth on protected routes.

    A blank ``dashboard_password`` disables auth entirely. Otherwise the username
    AND password are compared with ``secrets.compare_digest`` (constant time) and
    a mismatch raises 401 with a ``WWW-Authenticate: Basic`` challenge.
    """
    settings = get_settings()
    password = settings.dashboard_password or ""
    if not password:  # auth disabled
        return
    unauthorized = HTTPException(
        status_code=401,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Basic"},
    )
    if credentials is None:
        raise unauthorized
    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        (settings.dashboard_user or "").encode("utf-8"),
    )
    pass_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        password.encode("utf-8"),
    )
    if not (user_ok and pass_ok):
        raise unauthorized


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def get_db() -> Iterator[Session]:
    """Request-scoped session (commits on clean exit) via the session ctx manager."""
    with get_session() as session:
        yield session


def _counts(db: Session) -> dict[str, int]:
    out: dict[str, int] = {}
    for status in LeadStatus:
        count = db.query(LeadRecord).filter(LeadRecord.status == status).count()
        if count:
            out[status.value] = count
    return out


def _latest_proposal(lead: LeadRecord) -> ProposalRecord | None:
    if not lead.proposals:
        return None
    return max(lead.proposals, key=lambda p: (p.created_at or _dt.datetime.min, p.id))


# --- inbox ---------------------------------------------------------------------
@app.get("/")
def inbox(
    request: Request, db: Session = Depends(get_db), _auth: None = Depends(require_auth)
) -> Response:
    leads = (
        db.query(LeadRecord)
        .filter(LeadRecord.status.in_([LeadStatus.drafted, LeadStatus.approved]))
        .order_by(LeadRecord.fit_score.desc())
        .all()
    )
    return templates.TemplateResponse(
        request, "inbox.html", {"leads": leads, "counts": _counts(db)}
    )


@app.get("/lead/{lead_id}")
def lead_detail(
    lead_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_auth),
) -> Response:
    lead = db.get(LeadRecord, lead_id)
    if lead is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "lead.html",
        {"lead": lead, "proposal": _latest_proposal(lead), "counts": _counts(db)},
    )


# --- proposal actions ----------------------------------------------------------
@app.post("/lead/{lead_id}/proposal")
def save_proposal(
    lead_id: int,
    body: str = Form(""),
    suggested_rate: str = Form(""),
    db: Session = Depends(get_db),
    _auth: None = Depends(require_auth),
) -> RedirectResponse:
    lead = db.get(LeadRecord, lead_id)
    if lead is not None:
        proposal = _latest_proposal(lead)
        if proposal is None:
            proposal = ProposalRecord(lead_id=lead.id, body=body)
            db.add(proposal)
        proposal.body = body
        proposal.suggested_rate = suggested_rate
    return RedirectResponse(f"/lead/{lead_id}", status_code=303)


@app.post("/lead/{lead_id}/approve")
def approve(
    lead_id: int, db: Session = Depends(get_db), _auth: None = Depends(require_auth)
) -> RedirectResponse:
    lead = db.get(LeadRecord, lead_id)
    if lead is not None:
        lead.status = LeadStatus.approved
        proposal = _latest_proposal(lead)
        if proposal is not None:
            proposal.status = ProposalStatus.approved
    return RedirectResponse(f"/lead/{lead_id}", status_code=303)


@app.post("/lead/{lead_id}/submitted")
def mark_submitted(
    lead_id: int, db: Session = Depends(get_db), _auth: None = Depends(require_auth)
) -> RedirectResponse:
    lead = db.get(LeadRecord, lead_id)
    if lead is not None:
        lead.status = LeadStatus.submitted
        proposal = _latest_proposal(lead)
        if proposal is not None:
            proposal.status = ProposalStatus.submitted
            proposal.submitted_at = _utcnow()
    return RedirectResponse(f"/lead/{lead_id}", status_code=303)


@app.post("/lead/{lead_id}/reject")
def reject(
    lead_id: int, db: Session = Depends(get_db), _auth: None = Depends(require_auth)
) -> RedirectResponse:
    lead = db.get(LeadRecord, lead_id)
    if lead is not None:
        lead.status = LeadStatus.rejected
    return RedirectResponse(f"/lead/{lead_id}", status_code=303)


@app.post("/lead/{lead_id}/won")
def won(lead_id: int, _auth: None = Depends(require_auth)) -> RedirectResponse:
    record_outcome(lead_id, won=True)
    return RedirectResponse(f"/lead/{lead_id}", status_code=303)


@app.post("/lead/{lead_id}/lost")
def lost(lead_id: int, _auth: None = Depends(require_auth)) -> RedirectResponse:
    record_outcome(lead_id, won=False)
    return RedirectResponse(f"/lead/{lead_id}", status_code=303)


# --- pipeline board ------------------------------------------------------------
@app.get("/pipeline")
def pipeline(
    request: Request, db: Session = Depends(get_db), _auth: None = Depends(require_auth)
) -> Response:
    statuses = [s.value for s in LeadStatus]
    board: dict[str, list[LeadRecord]] = {s: [] for s in statuses}
    for lead in db.query(LeadRecord).order_by(LeadRecord.fit_score.desc()).all():
        board[lead.status.value].append(lead)
    return templates.TemplateResponse(
        request,
        "pipeline.html",
        {"statuses": statuses, "board": board, "counts": _counts(db)},
    )


# --- content engine ------------------------------------------------------------
@app.get("/content")
def content_get(request: Request, _auth: None = Depends(require_auth)) -> Response:
    return templates.TemplateResponse(
        request, "content.html", {"draft": None, "selected_kind": "post", "topic": ""}
    )


@app.post("/content/generate")
def content_generate(
    request: Request,
    kind: str = Form("post"),
    topic: str = Form(""),
    _auth: None = Depends(require_auth),
) -> Response:
    draft = generate_content(kind, topic or None)
    return templates.TemplateResponse(
        request,
        "content.html",
        {"draft": draft, "selected_kind": kind, "topic": topic},
    )


# --- mission control: outreach / conversations / analytics / runs --------------
@app.get("/outreach")
def outreach(
    request: Request, db: Session = Depends(get_db), _auth: None = Depends(require_auth)
) -> Response:
    return templates.TemplateResponse(
        request,
        "outreach.html",
        {"rows": analytics.outreach_list(), "counts": _counts(db)},
    )


@app.get("/conversations")
def conversations(
    request: Request, db: Session = Depends(get_db), _auth: None = Depends(require_auth)
) -> Response:
    return templates.TemplateResponse(
        request,
        "conversations.html",
        {"threads": analytics.conversations(), "counts": _counts(db)},
    )


@app.get("/analytics")
def analytics_page(
    request: Request, db: Session = Depends(get_db), _auth: None = Depends(require_auth)
) -> Response:
    return templates.TemplateResponse(
        request,
        "analytics.html",
        {"stats": analytics.funnel_stats(), "counts": _counts(db)},
    )


@app.get("/runs")
def runs(
    request: Request, db: Session = Depends(get_db), _auth: None = Depends(require_auth)
) -> Response:
    return templates.TemplateResponse(
        request,
        "runs.html",
        {"runs": analytics.recent_runs(), "counts": _counts(db)},
    )


@app.get("/strategy")
def strategy(
    request: Request, db: Session = Depends(get_db), _auth: None = Depends(require_auth)
) -> Response:
    return templates.TemplateResponse(
        request,
        "strategy.html",
        {
            "current": analytics.current_strategy(),
            "history": analytics.strategy_history(),
            "counts": _counts(db),
        },
    )


# --- cal.com booking webhook ---------------------------------------------------
def _extract_booking_emails(payload: dict) -> list[str]:
    """Pull attendee email(s) from a cal.com BOOKING_CREATED payload (tolerant)."""
    emails: list[str] = []
    for attendee in payload.get("attendees") or []:
        if isinstance(attendee, dict):
            email = attendee.get("email")
            if email:
                emails.append(email)
    responses = payload.get("responses") or {}
    email_resp = responses.get("email")
    if isinstance(email_resp, dict):
        value = email_resp.get("value")
        if value:
            emails.append(value)
    elif isinstance(email_resp, str) and email_resp:
        emails.append(email_resp)
    # dedupe (case-insensitive) preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for email in emails:
        key = email.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(email.strip())
    return unique


@app.post("/webhooks/cal")
async def cal_webhook(request: Request, db: Session = Depends(get_db)) -> Response:
    """Receive cal.com BOOKING_CREATED webhooks and stamp ``call_booked_at``.

    Verifies the ``X-Cal-Signature-256`` HMAC-SHA256 of the raw body when a
    secret is configured; a blank secret skips verification. Always returns 2xx
    for accepted requests so cal.com does not retry forever.
    """
    raw = await request.body()
    settings = get_settings()
    secret = settings.cal_webhook_secret or ""
    if secret:
        provided = request.headers.get("X-Cal-Signature-256", "")
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(provided, expected):
            return Response(
                json.dumps({"ok": False, "error": "bad signature"}),
                status_code=401,
                media_type="application/json",
            )

    matched = 0
    try:
        body = json.loads(raw or b"{}")
        if body.get("triggerEvent") == "BOOKING_CREATED":
            payload = body.get("payload") or {}
            for email in _extract_booking_emails(payload):
                record = (
                    db.query(OutreachRecord)
                    .filter(func.lower(OutreachRecord.email) == email.lower())
                    .first()
                )
                if record is not None:
                    record.call_booked_at = _utcnow()
                    record.replied = True  # a booking implies engagement
                    matched += 1
    except Exception:  # noqa: BLE001 — never fail a webhook; log and 200
        _log.exception("cal webhook processing failed")

    return Response(
        json.dumps({"ok": True, "matched": matched}),
        status_code=200,
        media_type="application/json",
    )


# --- ops -----------------------------------------------------------------------
@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
def metrics_endpoint() -> Response:
    body, ctype = metrics.render()
    return Response(body, media_type=ctype)
