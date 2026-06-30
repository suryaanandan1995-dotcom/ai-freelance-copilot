"""Draft a short, human cold-intro email for an email-reachable lead.

Uses the strong (opus) model with the shared HUMAN_VOICE guidance so the pitch
reads like a real engineer wrote it. The email is deliberately short (a cold
intro, not a full proposal): one specific opening line about their post, one
cited portfolio project as proof, optionally one quantified win if it fits, and
a soft CTA to the Calendly link. The compliance/opt-out footer is NOT added here
— ``outreach.sender.send_outreach`` always appends it.
"""
from __future__ import annotations

from typing import Any

from agents.llm import get_chat
from config import get_settings
from core.schemas import CompanyResearch, ScoredLead
from voice import HUMAN_VOICE

# Quantified, reusable proof points (weave in at most one, only if it fits).
QUANTIFIED_WINS = [
    "50% faster deploys",
    "40% cloud cost cut",
    "75% increase in deploy frequency",
]

_SYSTEM = (
    "You are writing a SHORT cold introduction email on behalf of a freelance "
    "DevSecOps / Kubernetes / AI-infrastructure engineer to someone who publicly "
    "posted that they are hiring. Rules:\n"
    "- 110-150 words in the body. Cold intro, NOT a full proposal.\n"
    "- The FIRST line must be specific to THEIR post (their stack / what they "
    "said they need) — never a generic opener.\n"
    "- Cite ONE of the engineer's portfolio projects by name as proof of fit.\n"
    "- Weave in AT MOST ONE quantified win "
    f"({', '.join(QUANTIFIED_WINS)}) and ONLY if it genuinely fits — don't force it.\n"
    "- End with a soft call-to-action offering a short call at the provided link.\n"
    "- Sign off with the engineer's name, site, and LinkedIn.\n"
    "- Do not fabricate any details about them.\n"
    "Output format: the FIRST line must be exactly 'Subject: <subject line>' "
    "then a blank line then the email body. The subject must be specific and "
    "non-spammy: no 'RE:', no ALL CAPS, no exclamation marks.\n\n" + HUMAN_VOICE
)


def _format_proof(chunks: list[dict[str, Any]]) -> str:
    if not chunks:
        return "(no extra proof points retrieved)"
    return "\n".join(f"- {c.get('text', '')} [{c.get('source', '')}]" for c in chunks)


def _parse(raw: str, fallback_subject: str) -> dict:
    """Split model output into {subject, body}, robust to a missing prefix."""
    text = (raw or "").strip()
    subject = fallback_subject
    body = text
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        if low.startswith("subject:"):
            subject = stripped.split(":", 1)[1].strip() or fallback_subject
            body = "\n".join(lines[i + 1 :]).strip()
        break  # only inspect the first non-empty line
    # Sanitise the subject line against the spam rules.
    subject = subject.replace("\n", " ").strip().strip('"').lstrip("RE:").strip()
    subject = subject.replace("!", "")
    if subject.isupper():
        subject = subject.capitalize()
    if not subject:
        subject = fallback_subject
    return {"subject": subject[:200], "body": body or text}


def draft_email(
    scored: ScoredLead,
    research: CompanyResearch | None = None,
    retriever: Any = None,
    chat: Any = None,
) -> dict:
    """Draft a cold-intro email. Returns ``{"subject": str, "body": str}``."""
    settings = get_settings()
    lead = scored.lead
    research = research or CompanyResearch()

    if retriever is None:
        from rag.retriever import get_retriever  # lazy: keeps module import-safe

        retriever = get_retriever()

    query = f"{lead.title} {' '.join(lead.tags)} {' '.join(scored.matched_projects)}"
    proof_chunks = retriever.retrieve(query, 3) or []

    model = get_chat(settings.model_opus, chat=chat)

    fallback_subject = f"Help with {lead.title}".strip()[:120] or "A quick intro"

    prompt = (
        f"Their post / opportunity: {lead.title}\n"
        f"What they wrote: {lead.description or '(no description)'}\n"
        f"Their company: {lead.company or 'unknown'}\n"
        f"Tech stack (if known): {', '.join(research.tech_stack)}\n"
        f"Pain points (if known): {', '.join(research.pain_points)}\n"
        f"Why this engineer fits: {'; '.join(scored.reasons)}\n"
        f"Portfolio projects that prove it: {', '.join(scored.matched_projects)}\n\n"
        f"Retrieved proof points:\n{_format_proof(proof_chunks)}\n\n"
        f"Engineer name: {settings.owner_name}\n"
        f"Website: {settings.owner_site}\n"
        f"LinkedIn: {settings.owner_linkedin}\n"
        f"Soft CTA link (a 15-min call): {settings.owner_calendly}\n\n"
        "Write the cold intro email now in the required Subject + body format."
    )
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": prompt},
    ]
    ai = model.invoke(messages)
    raw = ai.content if isinstance(ai.content, str) else str(ai.content)
    return _parse(raw, fallback_subject)
