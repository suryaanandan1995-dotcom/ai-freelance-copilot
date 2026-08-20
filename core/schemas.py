"""Shared domain schemas (Pydantic v2) used across all subsystems.

These are the stable contracts every agent/source/module imports. Do not
rename fields without updating db/models.py and the subsystem modules.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class Lead(BaseModel):
    """A raw opportunity discovered by a source adapter."""

    source: str = Field(..., description="Source adapter name, e.g. 'upwork_rss'")
    external_id: str = Field(..., description="Stable id within the source (dedupe key)")
    title: str
    description: str = ""
    url: str = ""
    company: str | None = None
    budget: str | None = None
    tags: list[str] = Field(default_factory=list)
    posted_at: str | None = None  # ISO8601 string if known
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        return f"{self.source}:{self.external_id}"


class FitVerdict(BaseModel):
    """The Qualifier's judgement ALONE — the schema handed to the model.

    Split out of :class:`ScoredLead` after a production crash on 2026-08-20. The model
    was asked for ``ScoredLead``, whose required ``lead`` field the caller then
    **discarded and replaced** with the lead it already had. So Sonnet was made to
    re-serialize a whole job description back to us for nothing, and that echo was a
    validation surface: on run 32339343714 it returned ``lead`` as a JSON *string*
    instead of an object and omitted ``fit_score``, pydantic raised inside
    ``with_structured_output``, and the exception took down a 39-minute, $2.46 run —
    losing the digest, five apply packs and the proposed addresses along with it.

    A model cannot fail to produce a field it was never asked for. Keeping the wire
    schema to exactly what the model decides is both cheaper and one fewer way to die.
    """

    fit_score: int = Field(..., ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)
    matched_projects: list[str] = Field(
        default_factory=list, description="Portfolio repo names that prove fit"
    )

    @field_validator("reasons", "matched_projects", mode="before")
    @classmethod
    def _coerce_list(cls, value):
        """Accept a JSON-encoded list where a list was asked for.

        Narrowing the schema stopped the crash but not the underlying habit: on
        2026-08-20 the run failed again on ``reasons``, with
        ``input_value='["Core requirement is Sc...qualifier on its own."]'`` — the right
        content, serialized one layer too many. That is not a model that misunderstood
        the task; it is a model that stringified a field, and rejecting it throws away a
        perfectly good judgement over an encoding detail.

        So the boundary decodes instead of refusing. A lenient *parser* at the edge and
        a strict *type* inside is the standard shape; the alternative is one lost lead
        per run forever, each costing a Sonnet call that already succeeded.
        """
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                import json

                try:
                    decoded = json.loads(text)
                except ValueError:
                    return [text]  # looked like JSON, wasn't: keep the prose
                if isinstance(decoded, list):
                    return [str(item) for item in decoded]
                return [str(decoded)]
            # One reason given as a bare sentence: a real answer, not a malformed list.
            return [text] if text else []
        return value


class ScoredLead(BaseModel):
    """A lead after the Qualifier agent scores fit against the user's skills.

    Still the internal contract every downstream node reads; only the *wire* schema
    narrowed (see :class:`FitVerdict`). ``lead`` is attached by the caller from the lead
    it already holds, never parsed back from the model.
    """

    lead: Lead
    fit_score: int = Field(..., ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)
    matched_projects: list[str] = Field(
        default_factory=list, description="Portfolio repo names that prove fit"
    )


class CompanyResearch(BaseModel):
    """Enrichment produced by the Researcher agent."""

    summary: str = ""
    tech_stack: list[str] = Field(default_factory=list)
    pain_points: list[str] = Field(default_factory=list)
    contacts: list[str] = Field(default_factory=list)


class ProposalDraft(BaseModel):
    """A tailored proposal drafted by the Proposal Writer agent (RAG)."""

    lead_external_id: str
    title: str
    body: str
    suggested_rate: str = ""
    cited_projects: list[str] = Field(default_factory=list)

    @property
    def word_count(self) -> int:
        return len(self.body.split())


class ComplianceVerdict(BaseModel):
    """Output of the Compliance/Reviewer agent. Gate before a draft is queued."""

    approved: bool
    issues: list[str] = Field(default_factory=list)
    is_duplicate: bool = False
    quality_score: int = Field(default=0, ge=0, le=100)
