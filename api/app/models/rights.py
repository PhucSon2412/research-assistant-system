from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (ARRAY, BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey,
                        ForeignKeyConstraint, Index, Integer, Text, UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import (CleanupObjectOutcome, PublicationState, RevocationReason, RevokedMetadataField,
                              RightsAudience, RightsOrigin, TombstoneKind, sql_in)

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
    publication (see ``LibraryItem.publication_state``). Rows are never
    deleted and the only update is revocation (M6b-2a, DB trigger): the four
    ``revoked*``/``rights_epoch_at_revoke`` columns go from NULL to set once,
    by an Admin, together with the item's epoch bump; every recorded column
    stays as the Admin wrote it. Deferred triggers require the origin request
    to be ``approved`` at commit, the common file to use its own object key
    rather than the private one, and revoked rights to have a revoked copy.
    After revocation, cleanup may set ``verified_source``,
    ``verified_source_url``, ``license_url``, ``evidence_note`` and
    ``scope_conditions`` to NULL once (``metadata_scrubbed_at``, 0023).
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
        CheckConstraint("license_url IS NOT NULL OR evidence_note IS NOT NULL"
                        " OR metadata_scrubbed_at IS NOT NULL", name="evidence_required"),
        CheckConstraint("verified_source IS NOT NULL OR metadata_scrubbed_at IS NOT NULL",
                        name="verified_source_present"),
        CheckConstraint("metadata_scrubbed_at IS NULL OR revoked_at IS NOT NULL", name="scrub_after_revoke"),
        # One day of slack for an Admin whose local date is ahead of UTC.
        CheckConstraint("checked_on <= (recorded_at AT TIME ZONE 'UTC')::date + 1", name="checked_not_future"),
        CheckConstraint(f"scope_audience IN ({sql_in(RightsAudience)})", name="scope_audience_valid"),
        # RAG/embedding answers cite chunks, so provider processing needs full-text use too.
        CheckConstraint("NOT permits_external_processing OR permits_fulltext_search",
                        name="processing_needs_fulltext"),
        CheckConstraint("scope_conditions IS NULL OR length(btrim(scope_conditions))"
                        f" BETWEEN 1 AND {SCOPE_CONDITIONS_MAX_CHARS}", name="scope_conditions_length"),
        CheckConstraint("rights_epoch_at_grant >= 0", name="epoch_non_negative"),
        CheckConstraint("(revoked_at IS NULL) = (revoked_by_user_id IS NULL)"
                        " AND (revoked_at IS NULL) = (revocation_reason_code IS NULL)"
                        " AND (revoked_at IS NULL) = (rights_epoch_at_revoke IS NULL)", name="revocation_fields"),
        CheckConstraint("revocation_reason_code IS NULL OR revocation_reason_code IN"
                        f" ({sql_in(RevocationReason)})", name="revocation_reason_valid"),
        CheckConstraint("rights_epoch_at_revoke IS NULL OR rights_epoch_at_revoke > rights_epoch_at_grant",
                        name="revoke_bumps_epoch"),
        # Target for common_publications: the exact rights row and checksum.
        UniqueConstraint("id", "item_id", "file_checksum_sha256",
                         name="uq_access_rights_id_item_id_file_checksum_sha256"),
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
    verified_source: Mapped[str | None] = mapped_column(Text)
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
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT",
                                       name="fk_access_rights_revoked_by_user_id_users"))
    revocation_reason_code: Mapped[str | None] = mapped_column(Text)
    rights_epoch_at_revoke: Mapped[int | None] = mapped_column(BigInteger)
    metadata_scrubbed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CommonPublication(Base):
    """An Admin's publication of one reviewed common copy's *metadata* (M6b-2a).

    A live row (``revoked_at IS NULL``) exists exactly while the item is
    ``listed``; revoking sets ``revoked_at`` and the item becomes ``revoked``
    (deferred DB triggers). ``verified_byte_size`` is the size of the common
    object read back and hashed just before this row was committed. The
    partial unique index is the exact-byte duplicate rule: at most one live
    publication per SHA-256, whatever the concurrency. It says nothing about
    two files with different checksums being the same paper.
    """

    __tablename__ = "common_publications"
    __table_args__ = (
        ForeignKeyConstraint(["item_id", "item_visibility"], ["library_items.id", "library_items.visibility"],
                             name="fk_common_publications_common_item", ondelete="RESTRICT"),
        ForeignKeyConstraint(["access_rights_id", "item_id", "file_checksum_sha256"],
                             ["access_rights.id", "access_rights.item_id", "access_rights.file_checksum_sha256"],
                             name="fk_common_publications_rights", ondelete="RESTRICT"),
        UniqueConstraint("access_rights_id", name="uq_common_publications_access_rights_id"),
        CheckConstraint("item_visibility = 'common'", name="common_only"),
        CheckConstraint("verified_byte_size > 0", name="size_positive"),
        CheckConstraint("revoked_at IS NULL OR revoked_at >= published_at", name="revoked_after"),
        Index("uq_common_publications_live_checksum", "file_checksum_sha256", unique=True,
              postgresql_where=text("revoked_at IS NULL")),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    item_visibility: Mapped[str] = mapped_column(Text, server_default=text("'common'"))
    access_rights_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    file_checksum_sha256: Mapped[str] = mapped_column(Text)
    verified_byte_size: Mapped[int] = mapped_column(BigInteger)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_by_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
                                                            ForeignKey("users.id", ondelete="RESTRICT"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeletionTombstone(Base):
    """Durable record that a common copy was revoked, and its cleanup progress.

    Written in the revocation transaction, whose commit is ``t_rev``. No
    foreign keys: it outlives the rows it names and can be replayed into a
    restored snapshot that predates them (``app.cli.tombstones``). Never
    deleted; only the cleanup columns change (DB trigger), and ``purged_at``
    cannot be set while a ``RevokedMetadataField`` of the copy is still
    present without a retention basis (0023). It carries IDs, the object key,
    codes, field names and times only: no title, text, evidence or bytes.
    """

    __tablename__ = "deletion_tombstones"
    __table_args__ = (
        UniqueConstraint("target_kind", "item_id", name="uq_deletion_tombstones_target_kind_item_id"),
        CheckConstraint(f"target_kind IN ({sql_in(TombstoneKind)})", name="target_kind_valid"),
        CheckConstraint(f"reason_code IN ({sql_in(RevocationReason)})", name="reason_valid"),
        CheckConstraint(f"prior_publication_state IN ('{PublicationState.PENDING.value}',"
                        f" '{PublicationState.LISTED.value}')", name="prior_state_valid"),
        CheckConstraint("rights_epoch > 0", name="epoch_positive"),
        CheckConstraint("purge_due_at > recorded_at", name="purge_after_record"),
        CheckConstraint("(purged_at IS NULL) = (object_outcome IS NULL)", name="purged_fields"),
        CheckConstraint(f"object_outcome IS NULL OR object_outcome IN ({sql_in(CleanupObjectOutcome)})",
                        name="object_outcome_valid"),
        CheckConstraint("cleanup_attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("last_cleanup_error IS NULL OR last_cleanup_error ~ '^[a-z_]{1,64}$'",
                        name="error_is_code"),
        Index("ix_deletion_tombstones_unpurged", "purge_due_at", postgresql_where=text("purged_at IS NULL")),
        CheckConstraint("(purged_at IS NULL) = (scrubbed_fields IS NULL)"
                        " AND (purged_at IS NULL) = (retained_fields IS NULL)", name="field_lists"),
        # Target for retention records: a basis names the tombstone and its item.
        UniqueConstraint("id", "item_id", name="uq_deletion_tombstones_id_item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          server_default=text("gen_random_uuid()"))
    target_kind: Mapped[str] = mapped_column(Text)
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    storage_key: Mapped[str | None] = mapped_column(Text)
    access_rights_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reason_code: Mapped[str] = mapped_column(Text)
    prior_publication_state: Mapped[str] = mapped_column(Text)
    rights_epoch: Mapped[int] = mapped_column(BigInteger)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_by_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    purge_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    object_outcome: Mapped[str | None] = mapped_column(Text)
    cleanup_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_cleanup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_cleanup_error: Mapped[str | None] = mapped_column(Text)
    # Field names only, set with purged_at: what cleanup removed and what a basis kept.
    scrubbed_fields: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    retained_fields: Mapped[list[str] | None] = mapped_column(ARRAY(Text))


class RevocationMetadataRetention(Base):
    """A specific basis to keep one metadata field of one revoked common copy.

    Without such a row cleanup removes the field; having once had the right to
    publish is not a basis. It records the policy source, its scope, the
    confirming Admin and the reason, never the kept value itself. Append-only,
    and only insertable before the tombstone is purged (DB trigger).
    """

    __tablename__ = "revocation_metadata_retentions"
    __table_args__ = (
        ForeignKeyConstraint(["tombstone_id", "item_id"], ["deletion_tombstones.id", "deletion_tombstones.item_id"],
                             name="fk_revocation_metadata_retentions_tombstone", ondelete="RESTRICT"),
        UniqueConstraint("tombstone_id", "field", name="uq_revocation_metadata_retentions_tombstone_id_field"),
        CheckConstraint(f"field IN ({sql_in(RevokedMetadataField)})", name="field_valid"),
        CheckConstraint("length(btrim(policy_source)) BETWEEN 1 AND 500", name="policy_source_length"),
        CheckConstraint("length(btrim(scope)) BETWEEN 1 AND 500", name="scope_length"),
        CheckConstraint("length(btrim(reason)) BETWEEN 1 AND 1000", name="reason_length"),
        Index("ix_revocation_metadata_retentions_item_id", "item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          server_default=text("gen_random_uuid()"))
    tombstone_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    field: Mapped[str] = mapped_column(Text)
    policy_source: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    confirmed_by_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
                                                            ForeignKey("users.id", ondelete="RESTRICT"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
