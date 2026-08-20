"""SQLAlchemy models for the lead/proposal CRM pipeline.

Default engine is SQLite (offline-friendly); set COPILOT_DATABASE_URL to a
PostgreSQL DSN in production.
"""
from __future__ import annotations

import datetime as _dt
import enum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class LeadStatus(enum.StrEnum):
    new = "new"
    qualified = "qualified"
    drafted = "drafted"
    approved = "approved"
    submitted = "submitted"   # a HUMAN submitted it on the platform
    rejected = "rejected"
    won = "won"
    lost = "lost"


class ProposalStatus(enum.StrEnum):
    draft = "draft"
    approved = "approved"
    submitted = "submitted"


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


class OutreachRecord(Base):
    """One auto-sent cold email. The UNIQUE email enforces never-email-twice
    dedupe across runs (critical for the unattended cloud schedule)."""

    __tablename__ = "outreach"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"), nullable=True, index=True
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    subject: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(32), default="sent")  # sent | suppressed | failed
    sent_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)
    # follow-up + funnel tracking
    replied: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    followups_sent: Mapped[int] = mapped_column(Integer, default=0)
    last_contact_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)
    call_booked_at: Mapped[_dt.datetime | None] = mapped_column(DateTime, nullable=True)


class StrategyRecord(Base):
    """A versioned self-optimizer strategy (pitch/subject variant, thresholds,
    project/source weights). Exactly one row is active; the optimizer creates a new
    active row on each tuning step and can reactivate a prior one on auto-revert."""

    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    params: Mapped[dict] = mapped_column(JSON, default=dict)  # {pitch_variant, subject_style, fit_threshold, weights...}
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    baseline_reply_rate: Mapped[float] = mapped_column(Float, default=0.0)  # rate to beat when this went active
    note: Mapped[str] = mapped_column(String(512), default="")  # why the optimizer made this change
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class RunRecord(Base):
    """One pipeline/workflow execution — powers run history, failure alerts, and
    the funnel/analytics views. Written at the end of every run (ok or error)."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow: Mapped[str] = mapped_column(String(32), index=True)  # outreach | reply | followup
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class ReplyRecord(Base):
    """A message in an auto-reply conversation (inbound from a prospect or the
    outbound auto-reply). Used to thread correctly and cap replies per prospect."""

    __tablename__ = "replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)  # the prospect's address
    direction: Mapped[str] = mapped_column(String(8))            # "in" | "out"
    subject: Mapped[str] = mapped_column(String(512), default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    message_id: Mapped[str | None] = mapped_column(String(512), nullable=True)  # for threading
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)


class PostRecord(Base):
    """One LinkedIn post drafted by the content engine. Rows with status='published'
    have gone live via the API; 'draft' rows were generated but not published. Powers
    the per-day cap and content dedupe (never publish the same body twice)."""

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(32), default="linkedin", index=True)
    kind: Mapped[str] = mapped_column(String(32), default="post")  # post | case_study | gig
    topic: Mapped[str] = mapped_column(String(512), default="")
    body: Mapped[str] = mapped_column(Text)
    body_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # dedupe
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)  # draft | published | failed
    post_urn: Mapped[str | None] = mapped_column(String(256), nullable=True)  # LinkedIn share URN
    post_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    published_at: Mapped[_dt.datetime | None] = mapped_column(DateTime, nullable=True)


class LeadRecord(Base):
    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_source_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(256))
    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1024), default="")
    company: Mapped[str | None] = mapped_column(String(256), nullable=True)
    budget: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    posted_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fit_score: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[LeadStatus] = mapped_column(Enum(LeadStatus), default=LeadStatus.new, index=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)

    proposals: Mapped[list[ProposalRecord]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )


class ProposalRecord(Base):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(Text)
    suggested_rate: Mapped[str] = mapped_column(String(128), default="")
    cited_projects: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[ProposalStatus] = mapped_column(
        Enum(ProposalStatus), default=ProposalStatus.draft
    )
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)
    submitted_at: Mapped[_dt.datetime | None] = mapped_column(DateTime, nullable=True)
    outcome_at: Mapped[_dt.datetime | None] = mapped_column(DateTime, nullable=True)

    lead: Mapped[LeadRecord] = relationship(back_populates="proposals")


class CallRecord(Base):
    """One booked call, detected from the cal.com confirmation email.

    Why this is a table and not a log line: the owner reads email once a day and never
    opens a UI, so a booked call has to arrive as a briefing email — and a briefing that
    re-sends itself on every 2-hourly pass is worse than none. ``booking_uid`` (the
    cal.com video-link id, falling back to the Message-ID) is the idempotency key that
    makes "alert exactly once" true across restarts and re-reads.

    The cal.com WEBHOOK would carry this data more directly, but it needs a public URL
    to POST to and the dashboard is deliberately unhosted, so ``call_booked_at`` was
    never once stamped and the funnel read 0 calls while a call sat in the inbox. The
    inbox is the one production surface that is already reachable — the reply pass logs
    into it every two hours — so detection lives there instead.
    """

    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_uid: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    invitee_name: Mapped[str] = mapped_column(String(256), default="")
    invitee_email: Mapped[str] = mapped_column(String(320), default="", index=True)
    when_text: Mapped[str] = mapped_column(String(256), default="")
    join_url: Mapped[str] = mapped_column(String(512), default="")
    subject: Mapped[str] = mapped_column(String(512), default="")
    # "outreach" when the invitee is someone we cold-emailed, "inbound" otherwise. The
    # distinction changes the whole briefing: for outreach we know the job, the pitch and
    # the company; for inbound we know nothing and the call has to open with a question.
    origin: Mapped[str] = mapped_column(String(32), default="inbound")
    lead_id: Mapped[int | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="booked")  # booked | cancelled
    notified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, index=True)
