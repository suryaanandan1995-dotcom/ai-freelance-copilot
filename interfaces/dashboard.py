"""Human-in-the-loop approval dashboard (FastAPI + Jinja2).

A polished dark-theme UI for reviewing AI-drafted proposals, running the
learning loop (won/lost), and generating inbound marketing content. NOTHING is
ever auto-submitted to a platform — a human reviews and submits every proposal
by hand (auto-submit violates Upwork / LinkedIn / Fiverr ToS).
"""
from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from content.engine import generate as generate_content
from db.models import LeadRecord, LeadStatus, ProposalRecord, ProposalStatus
from db.session import get_session
from observability import metrics
from rag.learning import record_outcome

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

app = FastAPI(title="AI Freelance Copilot — Dashboard")


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
def inbox(request: Request, db: Session = Depends(get_db)) -> Response:
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
def lead_detail(lead_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
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
def approve(lead_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    lead = db.get(LeadRecord, lead_id)
    if lead is not None:
        lead.status = LeadStatus.approved
        proposal = _latest_proposal(lead)
        if proposal is not None:
            proposal.status = ProposalStatus.approved
    return RedirectResponse(f"/lead/{lead_id}", status_code=303)


@app.post("/lead/{lead_id}/submitted")
def mark_submitted(lead_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    lead = db.get(LeadRecord, lead_id)
    if lead is not None:
        lead.status = LeadStatus.submitted
        proposal = _latest_proposal(lead)
        if proposal is not None:
            proposal.status = ProposalStatus.submitted
            proposal.submitted_at = _utcnow()
    return RedirectResponse(f"/lead/{lead_id}", status_code=303)


@app.post("/lead/{lead_id}/reject")
def reject(lead_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    lead = db.get(LeadRecord, lead_id)
    if lead is not None:
        lead.status = LeadStatus.rejected
    return RedirectResponse(f"/lead/{lead_id}", status_code=303)


@app.post("/lead/{lead_id}/won")
def won(lead_id: int) -> RedirectResponse:
    record_outcome(lead_id, won=True)
    return RedirectResponse(f"/lead/{lead_id}", status_code=303)


@app.post("/lead/{lead_id}/lost")
def lost(lead_id: int) -> RedirectResponse:
    record_outcome(lead_id, won=False)
    return RedirectResponse(f"/lead/{lead_id}", status_code=303)


# --- pipeline board ------------------------------------------------------------
@app.get("/pipeline")
def pipeline(request: Request, db: Session = Depends(get_db)) -> Response:
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
def content_get(request: Request) -> Response:
    return templates.TemplateResponse(
        request, "content.html", {"draft": None, "selected_kind": "post", "topic": ""}
    )


@app.post("/content/generate")
def content_generate(
    request: Request,
    kind: str = Form("post"),
    topic: str = Form(""),
) -> Response:
    draft = generate_content(kind, topic or None)
    return templates.TemplateResponse(
        request,
        "content.html",
        {"draft": draft, "selected_kind": kind, "topic": topic},
    )


# --- ops -----------------------------------------------------------------------
@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
def metrics_endpoint() -> Response:
    body, ctype = metrics.render()
    return Response(body, media_type=ctype)
