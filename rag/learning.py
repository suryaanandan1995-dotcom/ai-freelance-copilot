"""Win/loss learning loop.

When a human marks a lead **won** in the dashboard, the winning proposal is
embedded and appended to the RAG knowledge base as a ``kind="win"`` doc. Because
the Proposal Writer retrieves proof points from the same store, future proposals
start citing what has actually closed — the system compounds on its own wins.

Everything here works offline: the default embedder is the deterministic
``FakeEmbedder`` and the store is the JSON-backed ``InMemoryVectorStore``.
"""
from __future__ import annotations

import datetime as _dt
import os

from config import get_settings
from db.models import LeadRecord, LeadStatus, ProposalStatus
from db.session import get_session
from rag.embedder import get_embedder
from rag.store import InMemoryVectorStore


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def append_winning_proposal_to_kb(body: str, external_id: str, store_path: str | None = None) -> bool:
    """Embed a winning proposal and persist it into the KB store.

    Returns True on success, False if the body is empty or the store can't be
    written (never raises — outcome recording must not fail on a KB hiccup).
    """
    body = (body or "").strip()
    if not body:
        return False
    settings = get_settings()
    path = store_path or settings.rag_store_path
    try:
        embedder = get_embedder()
        store = InMemoryVectorStore.from_file(path) if os.path.exists(path) else InMemoryVectorStore()
        doc = {
            "text": body,
            "metadata": {"source": f"won:{external_id}", "kind": "win"},
            "vector": embedder.embed(body),
        }
        store.add([doc])
        store.save(path)
        return True
    except Exception:
        return False


def record_outcome(lead_id: int, won: bool) -> bool:
    """Mark a lead won/lost, stamp the proposal, and (on win) grow the KB.

    Returns True if the lead was found and updated.
    """
    won_flag = bool(won)
    updated = False
    body = ""
    external_id = ""
    with get_session() as session:
        lead = session.get(LeadRecord, lead_id)
        if lead is None:
            return False
        lead.status = LeadStatus.won if won_flag else LeadStatus.lost
        external_id = lead.external_id
        for proposal in lead.proposals:
            proposal.outcome_at = _utcnow()
            if won_flag and proposal.status != ProposalStatus.submitted:
                proposal.status = ProposalStatus.submitted
            if not body:
                body = proposal.body
        updated = True

    # metrics + KB growth happen outside the DB transaction
    try:
        from observability import metrics

        metrics.inc("proposals_won_total" if won_flag else "proposals_lost_total")
    except Exception:
        pass
    if updated and won_flag:
        append_winning_proposal_to_kb(body, external_id)
    return updated
