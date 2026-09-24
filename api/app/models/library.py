from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
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
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import ContentStatus, Coverage, VerifiedMime, Visibility, sql_in


class LibraryItem(Base):
    """One copy of a paper inside one library (common or a user's private one).

    ``visibility``/``owner_user_id`` are immutable (DB trigger): promotion
    creates a new common item instead of flipping a private one.
    """

    __tablename__ = "library_items"
    __table_args__ = (
        CheckConstraint(f"visibility IN ({sql_in(Visibility)})", name="visibility_valid"),
        CheckConstraint(
            "(visibility = 'private' AND owner_user_id IS NOT NULL)"
            " OR (visibility = 'common' AND owner_user_id IS NULL)",
            name="owner_matches_visibility",
        ),
        CheckConstraint(f"content_status IN ({sql_in(ContentStatus)})", name="content_status_valid"),
        # M3 will additionally require an active index pointer.
        CheckConstraint(
            "content_status <> 'indexed' OR active_version_id IS NOT NULL",
            name="indexed_requires_active_version",
        ),
        CheckConstraint("rights_epoch >= 0", name="rights_epoch_non_negative"),
        # The active version must belong to this item.
        ForeignKeyConstraint(
            ["active_version_id", "id"],
            ["paper_versions.id", "paper_versions.item_id"],
            name="fk_library_items_active_version_paper_versions",
            use_alter=True,
            ondelete="RESTRICT",
        ),
        Index("ix_library_items_owner_user_id", "owner_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    visibility: Mapped[str] = mapped_column(Text)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    content_status: Mapped[str] = mapped_column(Text, server_default=ContentStatus.METADATA_ONLY.value)
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    title: Mapped[str | None] = mapped_column(Text)
    # Bumped on every revoke/delete (M5/M6); workers compare against the
    # epoch recorded on their job before calling out or publishing.
    rights_epoch: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaperVersion(Base):
    __tablename__ = "paper_versions"
    __table_args__ = (
        CheckConstraint(f"coverage IS NULL OR coverage IN ({sql_in(Coverage)})", name="coverage_valid"),
        # Composite key target for (version, item) references from items,
        # jobs and, later, AccessRights.
        UniqueConstraint("id", "item_id", name="uq_paper_versions_id_item_id"),
        Index("ix_paper_versions_item_id", "item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("library_items.id", ondelete="RESTRICT")
    )
    version_label: Mapped[str | None] = mapped_column(Text)
    source_identifier: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    coverage: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceFile(Base):
    """Original file metadata. The object itself lives in S3/MinIO (M1c).

    Identity columns are immutable (DB trigger); ``version_id``, ``job_id`` and
    ``page_count`` may only be filled once from NULL. ``storage_key`` is not
    unique because one physical object may back several valid items.
    """

    __tablename__ = "source_files"
    __table_args__ = (
        CheckConstraint(f"verified_mime IN ({sql_in(VerifiedMime)})", name="verified_mime_valid"),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        CheckConstraint("page_count IS NULL OR page_count > 0", name="page_count_positive"),
        CheckConstraint("checksum_sha256 ~ '^[0-9a-f]{64}$'", name="checksum_sha256_hex"),
        CheckConstraint("version_id IS NOT NULL OR job_id IS NOT NULL", name="has_owner_record"),
        CheckConstraint("length(storage_key) > 0", name="storage_key_not_empty"),
        UniqueConstraint("id", "version_id", name="uq_source_files_id_version_id"),
        Index("ix_source_files_version_id", "version_id"),
        Index("ix_source_files_job_id", "job_id"),
        Index("ix_source_files_storage_key", "storage_key"),
        Index("ix_source_files_checksum_sha256", "checksum_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("paper_versions.id", ondelete="RESTRICT")
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingestion_jobs.id", ondelete="RESTRICT")
    )
    storage_key: Mapped[str] = mapped_column(Text)
    verified_mime: Mapped[str] = mapped_column(Text)
    byte_size: Mapped[int] = mapped_column(BigInteger)
    page_count: Mapped[int | None] = mapped_column(Integer)
    checksum_sha256: Mapped[str] = mapped_column(Text)
    original_filename: Mapped[str | None] = mapped_column(Text)
    file_source: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
