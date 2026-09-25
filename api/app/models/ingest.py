from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import JobOrigin, JobState, Visibility, sql_in


class IngestionJob(Base):
    """Ingest request trace. Job state is separate from item content_status.

    The job's scope must equal its item's visibility/owner (DB trigger), and
    its version must belong to its item (composite FK). Step attempts are M2.
    """

    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint(f"origin IN ({sql_in(JobOrigin)})", name="origin_valid"),
        CheckConstraint(f"state IN ({sql_in(JobState)})", name="state_valid"),
        CheckConstraint(f"scope_visibility IN ({sql_in(Visibility)})", name="scope_visibility_valid"),
        CheckConstraint(
            "(scope_visibility = 'private' AND scope_owner_user_id IS NOT NULL)"
            " OR (scope_visibility = 'common' AND scope_owner_user_id IS NULL)",
            name="scope_owner_matches_visibility",
        ),
        CheckConstraint("origin <> 'upload' OR scope_visibility = 'private'", name="upload_is_private"),
        CheckConstraint("length(idempotency_key) > 0", name="idempotency_key_not_empty"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        CheckConstraint(
            "cancel_requested = (cancel_requested_at IS NOT NULL)", name="cancel_requested_at_consistent"
        ),
        CheckConstraint("version_id IS NULL OR item_id IS NOT NULL", name="version_requires_item"),
        ForeignKeyConstraint(
            ["version_id", "item_id"],
            ["paper_versions.id", "paper_versions.item_id"],
            name="fk_ingestion_jobs_version_paper_versions",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "scope_visibility",
            "scope_owner_user_id",
            "idempotency_key",
            name="uq_ingestion_jobs_scope_idempotency_key",
            postgresql_nulls_not_distinct=True,
        ),
        UniqueConstraint("id", "item_id", name="uq_ingestion_jobs_id_item_id"),
        Index("ix_ingestion_jobs_item_id", "item_id"),
        Index("ix_ingestion_jobs_state", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    origin: Mapped[str] = mapped_column(Text)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    source_label: Mapped[str | None] = mapped_column(Text)
    scope_visibility: Mapped[str] = mapped_column(Text)
    scope_owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("library_items.id", ondelete="RESTRICT")
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    idempotency_key: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default=JobState.RECEIVED.value)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Item rights_epoch observed when the job started (M5/M6 revocation).
    rights_epoch_at_start: Mapped[int | None] = mapped_column(BigInteger)
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error_code: Mapped[str | None] = mapped_column(Text)
    warnings: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resume_state: Mapped[str | None] = mapped_column(Text)


class IngestStepAttempt(Base):
    __tablename__ = "ingest_step_attempts"
    __table_args__ = (
        CheckConstraint("step IN ('validate', 'extract', 'chunk')", name="step_valid"),
        CheckConstraint("outcome IN ('running', 'success', 'retry', 'failed', 'cancelled')", name="outcome_valid"),
        CheckConstraint("attempt_no > 0", name="attempt_no_positive"),
        UniqueConstraint("job_id", "step", "attempt_no", name="uq_ingest_step_attempt"),
        Index("ix_ingest_step_attempts_job_id", "job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ingestion_jobs.id", ondelete="RESTRICT"))
    step: Mapped[str] = mapped_column(Text)
    attempt_no: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MetadataCandidate(Base):
    __tablename__ = "metadata_candidates"
    __table_args__ = (
        ForeignKeyConstraint(["version_id", "item_id"], ["paper_versions.id", "paper_versions.item_id"],
                             name="fk_metadata_candidate_version_item", ondelete="RESTRICT"),
        ForeignKeyConstraint(["source_file_id", "version_id"], ["source_files.id", "source_files.version_id"],
                             name="fk_metadata_candidate_file_version", ondelete="RESTRICT"),
        CheckConstraint("locator_kind IN ('markdown', 'pdf')", name="locator_kind_valid"),
        CheckConstraint("(locator_kind = 'markdown' AND start_offset IS NOT NULL AND end_offset IS NOT NULL"
                        " AND start_offset >= 0 AND end_offset > start_offset AND start_page IS NULL)"
                        " OR (locator_kind = 'pdf' AND start_page IS NOT NULL AND start_page > 0"
                        " AND start_offset IS NULL AND end_offset IS NULL)", name="locator_valid"),
        UniqueConstraint("source_file_id", "field_name", "pipeline_version", name="uq_metadata_candidate_field"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    field_name: Mapped[str] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)
    locator_kind: Mapped[str] = mapped_column(Text)
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    start_page: Mapped[int | None] = mapped_column(Integer)
    pipeline_version: Mapped[str] = mapped_column(Text)


class Section(Base):
    __tablename__ = "sections"
    __table_args__ = (
        ForeignKeyConstraint(["version_id", "item_id"], ["paper_versions.id", "paper_versions.item_id"],
                             name="fk_section_version_item", ondelete="RESTRICT"),
        ForeignKeyConstraint(["source_file_id", "version_id"], ["source_files.id", "source_files.version_id"],
                             name="fk_section_file_version", ondelete="RESTRICT"),
        CheckConstraint("kind IN ('body', 'abstract', 'references')", name="kind_valid"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        CheckConstraint("locator_kind IN ('markdown', 'pdf')", name="locator_kind_valid"),
        CheckConstraint("(locator_kind = 'markdown' AND start_offset IS NOT NULL AND end_offset IS NOT NULL"
                        " AND start_line IS NOT NULL AND end_line IS NOT NULL AND start_offset >= 0"
                        " AND end_offset > start_offset AND start_line > 0 AND end_line >= start_line"
                        " AND start_page IS NULL AND end_page IS NULL)"
                        " OR (locator_kind = 'pdf' AND start_page IS NOT NULL AND end_page IS NOT NULL"
                        " AND start_page > 0 AND end_page >= start_page"
                        " AND start_offset IS NULL AND end_offset IS NULL AND start_line IS NULL AND end_line IS NULL)",
                        name="locator_valid"),
        UniqueConstraint("source_file_id", "pipeline_version", "ordinal", name="uq_section_ordinal"),
        UniqueConstraint("id", "item_id", "version_id", "source_file_id", name="uq_section_identity"),
        Index("ix_sections_item_id", "item_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(Text)
    heading: Mapped[str | None] = mapped_column(Text)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    locator_kind: Mapped[str] = mapped_column(Text)
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    start_line: Mapped[int | None] = mapped_column(Integer)
    end_line: Mapped[int | None] = mapped_column(Integer)
    start_page: Mapped[int | None] = mapped_column(Integer)
    end_page: Mapped[int | None] = mapped_column(Integer)
    locator_detail: Mapped[dict | None] = mapped_column(JSONB)
    pipeline_version: Mapped[str] = mapped_column(Text)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        ForeignKeyConstraint(["section_id", "item_id", "version_id", "source_file_id"],
                             ["sections.id", "sections.item_id", "sections.version_id", "sections.source_file_id"],
                             name="fk_chunk_section_identity", ondelete="RESTRICT"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        CheckConstraint("locator_kind IN ('markdown', 'pdf')", name="locator_kind_valid"),
        CheckConstraint("(locator_kind = 'markdown' AND start_offset IS NOT NULL AND end_offset IS NOT NULL"
                        " AND start_line IS NOT NULL AND end_line IS NOT NULL AND start_offset >= 0"
                        " AND end_offset > start_offset AND start_line > 0 AND end_line >= start_line"
                        " AND start_page IS NULL AND end_page IS NULL)"
                        " OR (locator_kind = 'pdf' AND start_page IS NOT NULL AND end_page IS NOT NULL"
                        " AND start_page > 0 AND end_page >= start_page"
                        " AND start_offset IS NULL AND end_offset IS NULL AND start_line IS NULL AND end_line IS NULL)",
                        name="locator_valid"),
        CheckConstraint("chunk_hash ~ '^[0-9a-f]{64}$'", name="hash_hex"),
        UniqueConstraint("section_id", "pipeline_version", "ordinal", name="uq_chunk_ordinal"),
        Index("ix_chunks_item_id", "item_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    section_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    locator_kind: Mapped[str] = mapped_column(Text)
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    start_line: Mapped[int | None] = mapped_column(Integer)
    end_line: Mapped[int | None] = mapped_column(Integer)
    start_page: Mapped[int | None] = mapped_column(Integer)
    end_page: Mapped[int | None] = mapped_column(Integer)
    locator_detail: Mapped[dict | None] = mapped_column(JSONB)
    chunk_hash: Mapped[str] = mapped_column(Text)
    pipeline_version: Mapped[str] = mapped_column(Text)
