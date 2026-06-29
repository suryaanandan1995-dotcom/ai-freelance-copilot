"""Researcher agent: light LLM summarization of a lead into enrichment.

No real web access — it summarizes the lead text itself into a structured
:class:`CompanyResearch`. Deterministic-friendly so tests can pin the output via
an injected ``FakeChat``.
"""
from __future__ import annotations

from typing import Any

from config import get_settings
from core.schemas import CompanyResearch, Lead

from .llm import get_chat

_SYSTEM = (
    "You are a research assistant enriching a freelance lead. Read the opportunity "
    "and produce: a one-paragraph summary, the likely technology stack mentioned or "
    "implied, the client's probable pain points, and any named contacts. Do not "
    "invent facts beyond what the text reasonably supports."
)


def research(lead: Lead, chat: Any = None) -> CompanyResearch:
    """Summarize ``lead`` into a :class:`CompanyResearch`."""
    settings = get_settings()
    model = get_chat(settings.model_sonnet, chat=chat)
    structured = model.with_structured_output(CompanyResearch)

    text = (
        f"Title: {lead.title}\n"
        f"Company: {lead.company or 'unknown'}\n"
        f"Tags: {', '.join(lead.tags)}\n"
        f"Description:\n{lead.description}"
    )
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": text},
    ]
    result: CompanyResearch = structured.invoke(messages)
    return result
