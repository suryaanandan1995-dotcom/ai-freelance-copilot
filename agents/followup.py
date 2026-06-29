"""Follow-up agent: draft a short, polite nudge for a lead gone quiet."""
from __future__ import annotations

from typing import Any

from config import get_settings
from core.schemas import Lead

from .llm import get_chat

_SYSTEM = (
    "You write short, polite, low-pressure follow-up messages for a freelance "
    "engineer. Keep it under 80 words, friendly and specific to the opportunity, "
    "and never pushy or guilt-tripping. End with an easy out for the client."
)


def draft_followup(lead: Lead, days_since: int, chat: Any = None) -> str:
    """Return a short follow-up message body for ``lead`` after ``days_since`` days."""
    settings = get_settings()
    model = get_chat(settings.model_sonnet, chat=chat)

    prompt = (
        f"Opportunity: {lead.title}\n"
        f"Client: {lead.company or 'the client'}\n"
        f"Days since my last message: {days_since}\n"
        f"Sign as {settings.owner_name}.\n"
        "Write the follow-up message body now."
    )
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": prompt},
    ]
    ai = model.invoke(messages)
    return ai.content if isinstance(ai.content, str) else str(ai.content)
