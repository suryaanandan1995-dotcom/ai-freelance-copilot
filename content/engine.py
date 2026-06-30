"""Inbound content engine.

Generates marketing **drafts** (LinkedIn posts, case studies, productized-gig
copy) grounded in the user's portfolio proof points via the same RAG store the
proposal writer uses. Each draft cites retrieved evidence and ends with a soft
CTA to the user's cal.com link.

SAFETY: drafts only — the human reviews and posts everything by hand.
Auto-posting to LinkedIn / Fiverr / Upwork violates platform Terms of Service
and risks an account ban, so this module never publishes anything; it returns
text for a person to paste. This mirrors the rest of the system, which never
auto-submits proposals either.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from config import get_settings

# Quantified, reusable wins woven into case studies and gig copy.
QUANTIFIED_WINS = [
    "50% faster deploys",
    "75% higher deploy frequency",
    "40% cloud-cost reduction",
]

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# kind -> (canonical_key, human title, default RAG query)
_KINDS: dict[str, tuple[str, str, str]] = {
    "post": ("post", "LinkedIn Post", "Kubernetes security DevSecOps platform engineering"),
    "case_study": (
        "case_study",
        "Case Study",
        "Kubernetes security CI/CD automation cost optimization results",
    ),
    "case-study": (
        "case_study",
        "Case Study",
        "Kubernetes security CI/CD automation cost optimization results",
    ),
    "gig": (
        "gig",
        "Productized Gig",
        "DevSecOps Kubernetes hardening Terraform CI/CD freelance service",
    ),
}


def _normalize(kind: str) -> str:
    key = (kind or "").strip().lower()
    if key not in _KINDS:
        raise ValueError(f"unknown content kind: {kind!r} (expected post/case_study/gig)")
    return key


def _load_prompt(canonical: str) -> str:
    return (_PROMPTS_DIR / f"{canonical}.md").read_text(encoding="utf-8")


def _format_proof(proof: list[dict[str, Any]]) -> str:
    if not proof:
        return "(no portfolio proof points retrieved)"
    return "\n".join(
        f"- {(p.get('text') or '').strip()} [{p.get('source', '')}]" for p in proof
    )


def generate(
    kind: str,
    topic: str | None = None,
    retriever: Any = None,
    chat: Any = None,
) -> dict:
    """Generate a content draft of ``kind`` (optionally about ``topic``).

    Returns ``{kind, title, body, sources}``. ``retriever`` and ``chat`` are
    injectable so tests run fully offline with a fake retriever and ``FakeChat``.
    """
    key = _normalize(kind)
    canonical, title, default_query = _KINDS[key]

    settings = get_settings()
    topic = (topic or "").strip()
    query = f"{topic} {default_query}".strip() if topic else default_query

    if retriever is None:
        from rag.retriever import get_retriever  # lazy: keeps module import-safe

        retriever = get_retriever()
    proof = retriever.retrieve(query, k=5) or []

    from voice import HUMAN_VOICE

    system = _load_prompt(canonical) + "\n\n" + HUMAN_VOICE
    user = (
        f"Topic / angle: {topic or '(your strongest DevSecOps story)'}\n"
        f"Quantified wins to weave in where they fit: {', '.join(QUANTIFIED_WINS)}\n\n"
        f"Portfolio proof points (cite these, do not invent others):\n"
        f"{_format_proof(proof)}\n\n"
        f"Author: {settings.owner_name} | Site: {settings.owner_site}\n"
        f"Soft CTA link (cal.com): {settings.owner_calendly}\n"
        "Write the draft now."
    )

    from agents.llm import get_chat

    c = get_chat(settings.model_opus, chat=chat)
    result = c.invoke(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
    )
    body = result.content if isinstance(result.content, str) else str(result.content)
    sources = [p.get("source", "") for p in proof]

    return {"kind": canonical, "title": title, "body": body, "sources": sources}
