"""Classify a prospect's reply and draft the owner's response.

``classify_and_draft`` calls Claude (Opus) with the shared HUMAN_VOICE plus a set
of HARD RULES that make the autonomy safe: the model may fully auto-negotiate the
conversation, but it may NEVER commit a firm price, rate, scope, timeline, or
agree to a contract/NDA/legal terms — anything money/scope/deadline-shaped is
deferred to a short cal.com call. Not-interested / unsubscribe / hostile messages
return ``action="suppress"``.

Returns ``{action, subject, body}`` with ``action`` in
``{"reply", "suppress", "skip"}``.
"""
from __future__ import annotations

import logging

from agents.llm import get_chat
from config import get_settings
from voice import HUMAN_VOICE

logger = logging.getLogger(__name__)

# Phrases that mean "stop contacting me" — a fast, deterministic pre-check so we
# never spend a Claude call (or risk a mis-classification) on an obvious opt-out.
_SUPPRESS_MARKERS = (
    "unsubscribe",
    "remove me",
    "take me off",
    "stop emailing",
    "stop contacting",
    "do not contact",
    "don't contact",
    "not interested",
    "no thanks",
    "no thank you",
    "please stop",
    "leave me alone",
)


def _build_system_prompt(settings) -> str:
    rate_clause = (
        f"If it helps, a rough ballpark is {settings.standard_rate}, but confirm "
        "specifics on the call before anything is agreed. "
        if settings.standard_rate
        else ""
    )
    return f"""{HUMAN_VOICE}

You are replying AS {settings.owner_name} to a prospect who responded to a cold
email you sent. Write in the first person, warm and human, concise (60-120 words).

HARD RULES (these override everything else):
- Full auto-negotiate: answer technical and logistical questions helpfully and
  keep the conversation moving toward a call.
- NEVER commit to a firm price, rate, hourly/day figure, fixed scope, timeline,
  deadline, or agree to a contract / NDA / legal terms. If the prospect asks
  about rate, cost, budget, scope, or a deadline, say it depends on the specifics
  and propose a short call, including this link: {settings.owner_calendly}
  {rate_clause}
- Always include or work toward the call link ({settings.owner_calendly}).
- If the message is not-interested, "unsubscribe", "remove", "stop", or hostile,
  do NOT try to win them back — respond with a one-line polite acknowledgement.
- Never invent experience beyond the portfolio. Stay truthful.
- Sign the message as {settings.owner_name}.

Reply with the email body only (no "Subject:" line, no preamble)."""


def _looks_like_optout(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _SUPPRESS_MARKERS)


def _ensure_re(subject: str) -> str:
    subj = (subject or "").strip()
    if not subj:
        return "Re: your message"
    return subj if subj.lower().startswith("re:") else f"Re: {subj}"


def classify_and_draft(
    prospect_email: str,
    inbound_text: str,
    history: list | None = None,
    chat=None,
) -> dict:
    """Return ``{action, subject, body}`` for a prospect's inbound reply."""
    settings = get_settings()
    inbound_text = inbound_text or ""

    # Deterministic opt-out short-circuit — no model call needed.
    if _looks_like_optout(inbound_text):
        return {
            "action": "suppress",
            "subject": _ensure_re("your message"),
            "body": (
                f"No problem, I've taken you off my list and won't reach out "
                f"again. All the best.\n\n{settings.owner_name}"
            ),
        }

    system = _build_system_prompt(settings)
    messages: list = [{"role": "system", "content": system}]
    for turn in history or []:
        role = turn.get("role") if isinstance(turn, dict) else None
        content = turn.get("content") if isinstance(turn, dict) else None
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append(
        {
            "role": "user",
            "content": (
                f"The prospect ({prospect_email}) replied:\n\n{inbound_text}\n\n"
                "Write my reply."
            ),
        }
    )

    try:
        model = get_chat(settings.model_opus, chat=chat)
        result = model.invoke(messages)
        body = getattr(result, "content", None)
        if isinstance(body, list):  # some providers return content blocks
            body = "".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in body
            )
        body = (body or "").strip()
    except Exception as exc:
        logger.warning("classify_and_draft: model call failed: %s", exc)
        return {"action": "skip", "subject": "", "body": ""}

    if not body:
        return {"action": "skip", "subject": "", "body": ""}

    return {
        "action": "reply",
        "subject": _ensure_re("your message"),
        "body": body,
    }
