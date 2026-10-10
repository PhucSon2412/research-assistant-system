"""Full-text generations (private, and common since M6b-2b2b) and durable embedding reservations."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, CheckConstraint, Computed, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, Numeric, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class IndexGeneration(Base):
    __tablename__ = "index_generations"
    __table_args__ = (
        ForeignKeyConstraint(["version_id", "item_id"], ["paper_versions.id", "paper_versions.item_id"], name="fk_index_generation_version_item", ondelete="RESTRICT"),
        ForeignKeyConstraint(["source_file_id", "version_id"], ["source_files.id", "source_files.version_id"], name="fk_index_generation_file_version", ondelete="RESTRICT"),
        CheckConstraint("state IN ('building', 'active', 'superseded', 'abandoned')", name="state_valid"),
        CheckConstraint("expected_chunks > 0 AND rights_epoch >= 0 AND (dimensions IS NULL OR dimensions > 0)", name="counts_valid"),
        UniqueConstraint("job_id", "model_id", "index_version", name="uq_index_generation_job_model_version"),
        UniqueConstraint("id", "item_id", name="uq_index_generation_id_item"),
        Index("ix_index_generations_item_id", "item_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ingestion_jobs.id", ondelete="RESTRICT"))
    model_id: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    dimensions: Mapped[int | None] = mapped_column(Integer)
    pipeline_version: Mapped[str] = mapped_column(Text)
    index_version: Mapped[str] = mapped_column(Text)
    expected_chunks: Mapped[int] = mapped_column(Integer)
    rights_epoch: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(Text, server_default="building")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmbeddingCall(Base):
    __tablename__ = "embedding_calls"
    __table_args__ = (
        CheckConstraint("status IN ('reserved', 'completed', 'uncertain', 'rejected')", name="status_valid"),
        CheckConstraint("chunk_hash ~ '^[0-9a-f]{64}$'", name="hash_hex"),
        CheckConstraint("reserved_usd >= 0 AND (cost_usd IS NULL OR cost_usd >= 0)", name="cost_non_negative"),
        CheckConstraint("dimensions IS NULL OR dimensions > 0", name="dimensions_positive"),
        CheckConstraint("vector IS NULL OR (dimensions IS NOT NULL AND vector_dims(vector) = dimensions)", name="vector_dimensions_match"),
        # 0024: cleanup of a revoked common copy keeps the cost ledger and removes vector + chunk hash.
        CheckConstraint("chunk_hash IS NOT NULL OR content_removed_at IS NOT NULL", name="content_present"),
        CheckConstraint("content_removed_at IS NULL OR (vector IS NULL AND chunk_hash IS NULL)", name="content_removed"),
        CheckConstraint('(reservation_token IS NULL AND reservation_job_id IS NULL AND reservation_job_token IS NULL) OR '
                        '(reservation_token IS NOT NULL AND reservation_job_id IS NOT NULL AND reservation_job_token IS NOT NULL)',
                        name='reservation_binding'),
        UniqueConstraint("source_file_id", "model_id", "pipeline_version", "chunk_hash", name="uq_embedding_call_content"),
        Index("ix_embedding_calls_status", "status"),
        Index("ix_embedding_calls_reserved_lease", "lease_until",
              postgresql_where=text("status = 'reserved'")),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    source_file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("source_files.id", ondelete="RESTRICT"))
    # The user whose quota the call counts against; NULL (0025) charges a common
    # copy's call to the application buckets only (DB trigger keeps it = item scope).
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    model_id: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    response_model_id: Mapped[str | None] = mapped_column(Text)
    response_provider: Mapped[str | None] = mapped_column(Text)
    pipeline_version: Mapped[str] = mapped_column(Text)
    chunk_hash: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    vector: Mapped[list[float] | None] = mapped_column(Vector())
    dimensions: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    cost_status: Mapped[str] = mapped_column(Text)
    reserved_day: Mapped[date] = mapped_column(Date)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reservation_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reservation_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey('ingestion_jobs.id', ondelete='RESTRICT'))
    reservation_job_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IndexEntry(Base):
    __tablename__ = "index_entries"
    __table_args__ = (
        CheckConstraint("chunk_hash ~ '^[0-9a-f]{64}$'", name="hash_hex"),
        UniqueConstraint("generation_id", "chunk_id", name="uq_index_entry_chunk"),
        Index("ix_index_entries_keywords", "keywords", postgresql_using="gin"),
        Index("ix_index_entries_generation_id", "generation_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    generation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("index_generations.id", ondelete="RESTRICT"))
    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chunks.id", ondelete="RESTRICT"))
    embedding_call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("embedding_calls.id", ondelete="RESTRICT"))
    chunk_hash: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    keywords: Mapped[str] = mapped_column(TSVECTOR, Computed("to_tsvector('simple'::regconfig, text)", persisted=True))


class EmbeddingBudgetDay(Base):
    __tablename__ = "embedding_budget_days"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    committed_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8), server_default="0")
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8), server_default="0")


class EmbeddingUserBudgetDay(Base):
    __tablename__ = "embedding_user_budget_days"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True)
    committed_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8), server_default="0")
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8), server_default="0")
