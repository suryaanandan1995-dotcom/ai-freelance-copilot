"""Proposal Writer agent (the RAG step): draft a tailored proposal.

Pulls proof points from the portfolio knowledge base via an injected retriever,
then drafts a non-spammy, tailored proposal that cites the matched projects and
the user's quantified wins, suggests a rate, and ends with a soft CTA to the
Calendly link. Uses the strong (opus) model.
"""
from __future__ import annotations

from typing import Any

from config import get_settings
from core.schemas import CompanyResearch, ProposalDraft, ScoredLead
from voice import HUMAN_VOICE

from .llm import get_chat

# Quantified, reusable proof points the proposal should weave in.
QUANTIFIED_WINS = [
    "50% faster deploys",
    "40% infrastructure cost cut",
    "75% increase in deploy frequency",
]

_SYSTEM = (
    "You are an expert freelance proposal writer for a DevSecOps / Kubernetes / "
    "AI-infrastructure engineer. Write a tailored, specific, non-spammy proposal "
    "for the opportunity below. Requirements:\n"
    "- Address the client's actual stack and pain points.\n"
    "- Cite at least one of the user's portfolio projects by name as proof.\n"
    "- Reference the user's quantified wins where relevant "
    f"({', '.join(QUANTIFIED_WINS)}).\n"
    "- Be concise (roughly 120-220 words), warm, and confident — no filler, no "
    "generic flattery.\n"
    "- End with a soft call-to-action inviting a short call at the provided link.\n"
    "Do not fabricate client details.\n\n" + HUMAN_VOICE
)


def _format_proof(chunks: list[dict[str, Any]]) -> str:
    if not chunks:
        return "(no extra proof points retrieved)"
    return "\n".join(f"- {c.get('text', '')} [{c.get('source', '')}]" for c in chunks)


def write_proposal(
    scored: ScoredLead,
    research: CompanyResearch,
    retriever: Any = None,
    chat: Any = None,
) -> ProposalDraft:
    """Draft a :class:`ProposalDraft` for ``scored`` using RAG proof points.

    ``retriever`` exposes ``.retrieve(query, k)`` returning dicts with ``text`` /
    ``source`` / ``score``. If ``None``, the real retriever is lazily imported so
    this module imports fine before the ``rag`` package exists.
    """
    settings = get_settings()
    lead = scored.lead

    if retriever is None:
        from rag.retriever import get_retriever  # lazy import: keeps module import-safe

        retriever = get_retriever()

    query = f"{lead.title} {' '.join(lead.tags)} {' '.join(scored.matched_projects)}"
    proof_chunks = retriever.retrieve(query, 3) or []

    model = get_chat(settings.model_opus, chat=chat)

    prompt = (
        f"Opportunity title: {lead.title}\n"
        f"Client: {lead.company or 'the client'}\n"
        f"Budget: {lead.budget or 'unspecified'}\n"
        f"Fit reasons: {'; '.join(scored.reasons)}\n"
        f"Matched portfolio projects: {', '.join(scored.matched_projects)}\n\n"
        f"Research summary: {research.summary}\n"
        f"Tech stack: {', '.join(research.tech_stack)}\n"
        f"Pain points: {', '.join(research.pain_points)}\n\n"
        f"Retrieved proof points:\n{_format_proof(proof_chunks)}\n\n"
        f"Sign as {settings.owner_name}. Soft CTA link: {settings.owner_calendly}\n"
        "Write the proposal body now (plain text, no subject line)."
    )
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": prompt},
    ]
    ai = model.invoke(messages)
    body = ai.content if isinstance(ai.content, str) else str(ai.content)

    # Projects we can defensibly claim we cited: matched projects plus any repo
    # name surfaced by the retriever that the body actually mentions.
    cited = list(scored.matched_projects)
    for chunk in proof_chunks:
        src = str(chunk.get("source", ""))
        if src and src not in cited and src in body:
            cited.append(src)

    return ProposalDraft(
        lead_external_id=lead.external_id,
        title=f"Proposal: {lead.title}",
        body=body,
        suggested_rate=lead.budget or "$85/hr (negotiable to project scope)",
        cited_projects=cited,
    )
