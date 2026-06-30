"""SQLAlchemy models for the lead/proposal CRM pipeline.

Default engine is SQLite (offline-friendly); set COPILOT_DATABASE_URL to a
PostgreSQL DSN in production.
"""
from __future__ import annotations

import datetime as _dt
import enum

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
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
