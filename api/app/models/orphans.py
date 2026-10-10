from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, SmallInteger, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

ORIGINAL_KEY_PATTERN = r"^originals/[0-9a-f]{32}$"


class OrphanObjectFence(Base):
    """Permanent authority to delete ONE original key that no SourceFile names (C2d-5e2, 0045).

    Written only by ``ras_auth_orphan_fence`` under the per-key advisory lock
    of 0044, when no ``source_files`` row anywhere names the key. From its
    commit every ``source_files`` INSERT of the key is refused (same lock, DB
    trigger). Separate from :class:`ObjectDeleteFence`: no tombstone, item or
    epoch exists for an orphan and none is faked. Key, run ID, the observed
    HEAD size/Last-Modified, the grace cutoff and a time only: no content,
    file name, URL or ETag. Never updated or deleted.
    """

    __tablename__ = "orphan_object_fences"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_orphan_object_fences_storage_key"),
        CheckConstraint(f"storage_key ~ '{ORIGINAL_KEY_PATTERN}'", name="original_key"),
        CheckConstraint("observed_size >= 0", name="size_not_negative"),
        CheckConstraint("observed_last_modified < grace_cutoff", name="older_than_cutoff"),
        CheckConstraint("grace_cutoff < fenced_at", name="cutoff_before_fence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          server_default=text("gen_random_uuid()"))
    storage_key: Mapped[str] = mapped_column(Text)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    observed_size: Mapped[int] = mapped_column(BigInteger)
    observed_last_modified: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    grace_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fenced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrphanObjectCompletion(Base):
    """At most one storage-verified completion per orphan fence; append-only (0045)."""

    __tablename__ = "orphan_object_completions"
    __table_args__ = (
        CheckConstraint("outcome IN ('deleted', 'already_absent', 'kept_identity_changed')", name="outcome_valid"),
    )

    fence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orphan_object_fences.id"),
                                                primary_key=True)
    outcome: Mapped[str] = mapped_column(Text)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrphanReconcileCursor(Base):
    """Where the next orphan scan starts listing. A hint only, never deletion authority."""

    __tablename__ = "orphan_reconcile_cursors"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("start_after IS NULL OR (start_after LIKE 'originals/%' AND length(start_after) <= 1024)",
                        name="original_prefix"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    start_after: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
