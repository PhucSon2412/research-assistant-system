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
