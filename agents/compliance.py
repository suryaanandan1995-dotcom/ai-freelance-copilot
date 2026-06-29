"""Compliance / Reviewer agent: deterministic gate before a draft is queued.

No LLM — pure rules so the verdict is reproducible and cheap:
  * length bounds (not too short / not absurdly long),
  * not generic spam (must cite at least one portfolio project, and that project
    must actually appear in the body),
  * forbidden / spammy phrasing checks,
  * dedupe against previously-seen draft keys.
"""
from __future__ import annotations

from core.schemas import ComplianceVerdict, ProposalDraft

MIN_WORDS = 60
MAX_WORDS = 400

# Phrasing that reads as low-effort spam.
FORBIDDEN_PHRASES = [
    "dear sir/madam",
    "dear sir or madam",
    "to whom it may concern",
    "i can do this easily",
    "cheapest price",
    "guaranteed lowest",
    "100% guarantee",
    "buy now",
]


def _draft_key(draft: ProposalDraft) -> str:
    return f"{draft.lead_external_id}:{draft.title}".lower()


def review(
    draft: ProposalDraft,
    existing_keys: set[str] | None = None,
) -> ComplianceVerdict:
    """Score and approve/reject ``draft``.

    ``existing_keys`` is the set of already-queued draft keys for dedupe.
    """
    existing_keys = existing_keys or set()
    issues: list[str] = []
    body = draft.body or ""
    lower = body.lower()
    words = draft.word_count

    # --- length ---
    if words < MIN_WORDS:
        issues.append(f"too short ({words} words < {MIN_WORDS})")
    if words > MAX_WORDS:
        issues.append(f"too long ({words} words > {MAX_WORDS})")

    # --- not generic spam: must cite a project, and it must appear in the body ---
    if not draft.cited_projects:
        issues.append("generic: no portfolio project cited")
    elif not any(p in body for p in draft.cited_projects):
        issues.append("generic: cited project not referenced in body")

    # --- forbidden / spammy phrasing ---
    for phrase in FORBIDDEN_PHRASES:
        if phrase in lower:
            issues.append(f"forbidden phrasing: '{phrase}'")

    # --- dedupe ---
    is_duplicate = _draft_key(draft) in existing_keys
    if is_duplicate:
        issues.append("duplicate of an already-queued proposal")

    # --- quality score: start at 100, dock per issue, reward citations ---
    quality = 100 - 25 * len(issues)
    if draft.cited_projects and not is_duplicate:
        quality = min(100, quality + 5 * min(len(draft.cited_projects), 2))
    quality = max(0, min(100, quality))

    approved = not issues
    return ComplianceVerdict(
        approved=approved,
        issues=issues,
        is_duplicate=is_duplicate,
        quality_score=quality,
    )
