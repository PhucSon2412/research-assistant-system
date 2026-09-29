from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Text,
                        UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import ReviewRejectReason, ReviewStatus, sql_in

WITHDRAWN_BY_REQUESTER = "withdrawn_by_requester"
APPROVED_BY_ADMIN = "approved_by_admin"
DECLARED_TEXT_MAX_CHARS = 2000
DECLARED_URL_MAX_CHARS = 2048
REJECTION_NOTE_MAX_CHARS = 500


class ReviewRequest(Base):
    """An owner's request that an Admin review one exact private version/file.

    While ``open`` it is the only grant letting an Admin read that private
    original (and nothing else of the item). Closing is final (DB trigger);
    the ``declared_*`` fields are the User's own claim, not verified rights.
    ``approved`` (M6b-1) always comes with one ``AccessRights`` row and a
    pending common copy, checked at commit by deferred DB triggers.
    """

    __tablename__ = "review_requests"
    __table_args__ = (
        ForeignKeyConstraint(["version_id", "item_id"], ["paper_versions.id", "paper_versions.item_id"],
                             name="fk_review_requests_version_item", ondelete="RESTRICT"),
        ForeignKeyConstraint(["source_file_id", "version_id", "file_checksum_sha256"],
                             ["source_files.id", "source_files.version_id", "source_files.checksum_sha256"],
                             name="fk_review_requests_file_version_checksum", ondelete="RESTRICT"),
        # Only the item's owner can be the requester; common items have no owner.
        ForeignKeyConstraint(["item_id", "requester_user_id"], ["library_items.id", "library_items.owner_user_id"],
                             name="fk_review_requests_item_owner", ondelete="RESTRICT"),
        CheckConstraint(f"status IN ({sql_in(ReviewStatus)})", name="status_valid"),
        CheckConstraint("file_checksum_sha256 ~ '^[0-9a-f]{64}$'", name="checksum_hex"),
        CheckConstraint("rights_epoch_at_submit >= 0", name="epoch_non_negative"),
        CheckConstraint("length(notice_version) > 0", name="notice_not_empty"),
        CheckConstraint(f"length(btrim(declared_source)) BETWEEN 1 AND {DECLARED_TEXT_MAX_CHARS}",
                        name="declared_source_length"),
        CheckConstraint(f"length(btrim(declared_rights)) BETWEEN 1 AND {DECLARED_TEXT_MAX_CHARS}",
                        name="declared_rights_length"),
        CheckConstraint("declared_source_url IS NULL OR (declared_source_url ~ '^https?://[^[:space:]]+$'"
                        f" AND length(declared_source_url) <= {DECLARED_URL_MAX_CHARS})", name="source_url"),
        CheckConstraint("declared_license_url IS NULL OR (declared_license_url ~ '^https?://[^[:space:]]+$'"
                        f" AND length(declared_license_url) <= {DECLARED_URL_MAX_CHARS})", name="license_url"),
        CheckConstraint("(status = 'open') = (closed_at IS NULL AND closed_by_user_id IS NULL"
                        " AND close_reason_code IS NULL)", name="closed_fields"),
        CheckConstraint("closed_at IS NULL OR closed_at >= submitted_at", name="closed_after"),
        CheckConstraint(f"status <> 'withdrawn' OR (close_reason_code = '{WITHDRAWN_BY_REQUESTER}'"
                        " AND closed_by_user_id = requester_user_id)", name="withdrawn"),
        CheckConstraint(f"status <> 'rejected' OR close_reason_code IN ({sql_in(ReviewRejectReason)})",
                        name="rejected_reason"),
        CheckConstraint(f"status <> 'approved' OR close_reason_code = '{APPROVED_BY_ADMIN}'", name="approved"),
        # Target for access_rights: rights carry the checksum the owner submitted.
        UniqueConstraint("id", "file_checksum_sha256", name="uq_review_requests_id_file_checksum_sha256"),
        CheckConstraint("rejection_note IS NULL OR (status = 'rejected'"
                        f" AND length(btrim(rejection_note)) BETWEEN 1 AND {REJECTION_NOTE_MAX_CHARS})",
                        name="rejection_note"),
        Index("uq_review_requests_open_file", "source_file_id", "version_id", unique=True,
              postgresql_where=text("status = 'open'")),
        Index("ix_review_requests_requester_user_id", "requester_user_id"),
        Index("ix_review_requests_status_submitted_at", "status", "submitted_at"),
        Index("ix_review_requests_item_id", "item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          server_default=text("gen_random_uuid()"))
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    requester_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text)
    file_checksum_sha256: Mapped[str] = mapped_column(Text)
    # Any later revoke/delete that bumps the item's epoch ends the grant too.
    rights_epoch_at_submit: Mapped[int] = mapped_column(BigInteger)
    notice_version: Mapped[str] = mapped_column(Text)
    declared_source: Mapped[str] = mapped_column(Text)
    declared_source_url: Mapped[str | None] = mapped_column(Text)
    declared_rights: Mapped[str] = mapped_column(Text)
    declared_license_url: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"))
    close_reason_code: Mapped[str | None] = mapped_column(Text)
    rejection_note: Mapped[str | None] = mapped_column(Text)
