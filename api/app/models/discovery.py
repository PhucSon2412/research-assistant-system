from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DiscoverySource(Base):
    __tablename__ = "discovery_sources"
    __table_args__ = (
        CheckConstraint("state IN ('idle', 'running', 'error')", name="state_valid"),
        CheckConstraint("consecutive_errors >= 0", name="errors_non_negative"),
    )

    topic: Mapped[str] = mapped_column(Text, primary_key=True)
    window_from: Mapped[date] = mapped_column(Date, nullable=False)
    window_until: Mapped[date | None] = mapped_column(Date)
    resumption_token: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="idle")
    consecutive_errors: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    pages_seen: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    records_seen: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class DiscoveryPaper(Base):
    __tablename__ = "discovery_papers"
    __table_args__ = (
        UniqueConstraint("source", "remote_id", name="uq_discovery_papers_source_remote_id"),
        CheckConstraint("content_status = 'metadata_only'", name="metadata_only"),
        Index("ix_discovery_papers_first_discovered_at", "first_discovered_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="arxiv")
    remote_id: Mapped[str] = mapped_column(Text, nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    authors: Mapped[str | None] = mapped_column(Text)
    abstract: Mapped[str | None] = mapped_column(Text)
    categories: Mapped[str | None] = mapped_column(Text)
    license_url: Mapped[str | None] = mapped_column(Text)
    latest_version: Mapped[str | None] = mapped_column(Text)
    source_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    oai_datestamp: Mapped[date] = mapped_column(Date, nullable=False)
    first_discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    content_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="metadata_only")


class DiscoveryPaperTopic(Base):
    __tablename__ = "discovery_paper_topics"
    paper_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("discovery_papers.id", ondelete="CASCADE"), primary_key=True)
    topic: Mapped[str] = mapped_column(Text, ForeignKey("discovery_sources.topic", ondelete="RESTRICT"), primary_key=True)


class DiscoveryPaperLicense(Base):
    """A license URL observed for the then-latest arXiv version, not a grant."""
    __tablename__ = "discovery_paper_licenses"
    paper_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("discovery_papers.id", ondelete="CASCADE"), primary_key=True)
    version_label: Mapped[str] = mapped_column(Text, primary_key=True)
    license_url: Mapped[str] = mapped_column(Text, nullable=False)
    oai_datestamp: Mapped[date] = mapped_column(Date, nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DiscoveryDeletedRecord(Base):
    """Persistent OAI tombstone, including IDs not previously discovered."""
    __tablename__ = "discovery_deleted_records"
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    remote_id: Mapped[str] = mapped_column(Text, primary_key=True)
    oai_datestamp: Mapped[date] = mapped_column(Date, nullable=False)


class DiscoveryRequestClock(Base):
    __tablename__ = "discovery_request_clock"
    __table_args__ = (
        CheckConstraint("(lease_token IS NULL) = (lease_until IS NULL)", name="lease_pair"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
