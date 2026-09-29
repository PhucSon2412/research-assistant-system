from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint,
                        Index, Text, UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import RightsAudience, RightsOrigin, sql_in

RIGHTS_TEXT_MAX_CHARS = 2000
RIGHTS_URL_MAX_CHARS = 2048
SCOPE_CONDITIONS_MAX_CHARS = 1000


def _url_check(column: str) -> str:
    return (f"{column} IS NULL OR ({column} ~ '^https?://[^[:space:]]+$'"
            f" AND length({column}) <= {RIGHTS_URL_MAX_CHARS})")


class AccessRights(Base):
    """What an Admin checked and recorded for one common file/version/checksum.

    Only the Admin's own findings live here: the User's ``declared_*`` claim on
    the review request is never copied in. Recorded rights are not a
    publication (see ``LibraryItem.publication_state``). Rows are append-only
    until revocation lands (DB trigger); deferred triggers require the origin
    request to be ``approved`` at commit, and the common file to use its own
    object key rather than the private one.
    """

    __tablename__ = "access_rights"
    __table_args__ = (
        ForeignKeyConstraint(["item_id", "item_visibility"], ["library_items.id", "library_items.visibility"],
                             name="fk_access_rights_common_item", ondelete="RESTRICT"),
        ForeignKeyConstraint(["version_id", "item_id"], ["paper_versions.id", "paper_versions.item_id"],
                             name="fk_access_rights_version_item", ondelete="RESTRICT"),
        ForeignKeyConstraint(["source_file_id", "version_id", "file_checksum_sha256"],
                             ["source_files.id", "source_files.version_id", "source_files.checksum_sha256"],
                             name="fk_access_rights_file_version_checksum", ondelete="RESTRICT"),
        # Same checksum as the request, hence as the reviewed private file.
        ForeignKeyConstraint(["origin_review_request_id", "file_checksum_sha256"],
                             ["review_requests.id", "review_requests.file_checksum_sha256"],
                             name="fk_access_rights_review_request_checksum", ondelete="RESTRICT"),
        # One publication candidate per request, also under concurrent approvals.
        UniqueConstraint("origin_review_request_id", name="uq_access_rights_origin_review_request_id"),
        CheckConstraint("item_visibility = 'common'", name="common_only"),
        CheckConstraint("file_checksum_sha256 ~ '^[0-9a-f]{64}$'", name="checksum_hex"),
        CheckConstraint(f"origin_kind IN ({sql_in(RightsOrigin)})", name="origin_kind_valid"),
        CheckConstraint("(origin_kind = 'review_request') = (origin_review_request_id IS NOT NULL)",
                        name="origin_matches_kind"),
        CheckConstraint(f"length(btrim(verified_source)) BETWEEN 1 AND {RIGHTS_TEXT_MAX_CHARS}",
                        name="verified_source_length"),
        CheckConstraint(_url_check("verified_source_url"), name="source_url"),
        CheckConstraint(_url_check("license_url"), name="license_url"),
        CheckConstraint("evidence_note IS NULL OR length(btrim(evidence_note))"
                        f" BETWEEN 1 AND {RIGHTS_TEXT_MAX_CHARS}", name="evidence_note_length"),
        CheckConstraint("license_url IS NOT NULL OR evidence_note IS NOT NULL", name="evidence_required"),
        # One day of slack for an Admin whose local date is ahead of UTC.
        CheckConstraint("checked_on <= (recorded_at AT TIME ZONE 'UTC')::date + 1", name="checked_not_future"),
        CheckConstraint(f"scope_audience IN ({sql_in(RightsAudience)})", name="scope_audience_valid"),
        # RAG/embedding answers cite chunks, so provider processing needs full-text use too.
        CheckConstraint("NOT permits_external_processing OR permits_fulltext_search",
                        name="processing_needs_fulltext"),
        CheckConstraint("scope_conditions IS NULL OR length(btrim(scope_conditions))"
                        f" BETWEEN 1 AND {SCOPE_CONDITIONS_MAX_CHARS}", name="scope_conditions_length"),
        CheckConstraint("rights_epoch_at_grant >= 0", name="epoch_non_negative"),
        Index("ix_access_rights_item_id", "item_id"),
        Index("ix_access_rights_source_file_id", "source_file_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          server_default=text("gen_random_uuid()"))
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    item_visibility: Mapped[str] = mapped_column(Text, server_default=text("'common'"))
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    file_checksum_sha256: Mapped[str] = mapped_column(Text)
    origin_kind: Mapped[str] = mapped_column(Text)
    origin_review_request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    verified_source: Mapped[str] = mapped_column(Text)
    verified_source_url: Mapped[str | None] = mapped_column(Text)
    license_url: Mapped[str | None] = mapped_column(Text)
    evidence_note: Mapped[str | None] = mapped_column(Text)
    checked_on: Mapped[date] = mapped_column(Date)
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
                                                        ForeignKey("users.id", ondelete="RESTRICT"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scope_audience: Mapped[str] = mapped_column(Text)
    permits_original_download: Mapped[bool] = mapped_column(Boolean)
    permits_fulltext_search: Mapped[bool] = mapped_column(Boolean)
    permits_external_processing: Mapped[bool] = mapped_column(Boolean)
    scope_conditions: Mapped[str | None] = mapped_column(Text)
    # The common item's epoch when recorded; a later revoke bumps the item's.
    rights_epoch_at_grant: Mapped[int] = mapped_column(BigInteger)
