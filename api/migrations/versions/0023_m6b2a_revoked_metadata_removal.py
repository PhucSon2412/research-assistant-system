"""Remove a revoked common copy's metadata at cleanup unless a per-field basis allows keeping it (M6b-2a review).

Having once had the right to publish a copy is not a right to keep its
metadata after revocation. Cleanup therefore nulls, for the revoked copy only,
seven fields: ``title`` (``library_items.title``), ``source_url`` (the common
version's ``source_url``/``source_identifier``/``version_label`` and the
rights' ``verified_source_url``), ``original_filename`` (common
``source_files``), and the rights' ``verified_source``, ``license_url``,
``evidence_note`` and ``scope_conditions``. A field is kept only when a
``revocation_metadata_retentions`` row names it for that copy, with the policy
source, scope, confirming Admin and reason.

The DB enforces it: a tombstone cannot be marked ``purged_at`` while one of
those fields is still set without a basis, revoked rights and the revoked
copy's version/file/title may only have those fields set to NULL, and a
retention row is append-only and must be written before the purge. The
User's private item, version, file and review request are never touched.
"""
from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

FIELDS = ("'title', 'source_url', 'original_filename', 'verified_source', 'license_url', "
          "'evidence_note', 'scope_conditions'")
SCRUBBABLE_RIGHTS = ("verified_source", "verified_source_url", "license_url", "evidence_note", "scope_conditions")
# access_rights columns that never change once recorded (0022's list minus the scrubbable ones).
FIXED_RIGHTS = ("id", "item_id", "item_visibility", "version_id", "source_file_id", "file_checksum_sha256",
                "origin_kind", "origin_review_request_id", "checked_on", "reviewer_user_id", "recorded_at",
                "scope_audience", "permits_original_download", "permits_fulltext_search",
                "permits_external_processing", "rights_epoch_at_grant")
REVOCATION = ("revoked_at", "revoked_by_user_id", "revocation_reason_code", "rights_epoch_at_revoke")
RECORDED_0022 = FIXED_RIGHTS[:8] + ("verified_source", "verified_source_url", "license_url", "evidence_note") \
    + FIXED_RIGHTS[8:14] + ("scope_conditions",) + FIXED_RIGHTS[14:]
TOMBSTONE_IDENTITY = ("id", "target_kind", "item_id", "version_id", "source_file_id", "storage_key",
                      "access_rights_id", "reason_code", "prior_publication_state", "rights_epoch",
                      "recorded_at", "recorded_by_user_id", "purge_due_at")


def _row(prefix: str, columns: tuple[str, ...]) -> str:
    return "ROW(" + ", ".join(f"{prefix}.{c}" for c in columns) + ")"


def _kept_or_null(columns: tuple[str, ...]) -> str:
    return " AND ".join(f"(NEW.{c} IS NULL OR NEW.{c} IS NOT DISTINCT FROM OLD.{c})" for c in columns)


SOURCE_FILES_0001 = """
    CREATE OR REPLACE FUNCTION ras_source_files_immutable() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.storage_key IS DISTINCT FROM OLD.storage_key
           OR NEW.verified_mime IS DISTINCT FROM OLD.verified_mime
           OR NEW.byte_size IS DISTINCT FROM OLD.byte_size
           OR NEW.checksum_sha256 IS DISTINCT FROM OLD.checksum_sha256
           OR NEW.original_filename IS DISTINCT FROM OLD.original_filename
           OR NEW.file_source IS DISTINCT FROM OLD.file_source
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR (OLD.version_id IS NOT NULL AND NEW.version_id IS DISTINCT FROM OLD.version_id)
           OR (OLD.job_id IS NOT NULL AND NEW.job_id IS DISTINCT FROM OLD.job_id)
           OR (OLD.page_count IS NOT NULL AND NEW.page_count IS DISTINCT FROM OLD.page_count) THEN
            RAISE EXCEPTION 'source file record is immutable'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END $$;
"""
# The one new exception: a revoked common copy's file name may be removed (never changed).
SOURCE_FILES_0023 = SOURCE_FILES_0001.replace(
    "           OR NEW.original_filename IS DISTINCT FROM OLD.original_filename\n",
    "           OR (NEW.original_filename IS DISTINCT FROM OLD.original_filename AND NOT (\n"
    "               NEW.original_filename IS NULL AND EXISTS (SELECT 1 FROM library_items\n"
    "               WHERE id = OLD.item_id AND publication_state = 'revoked')))\n")

RIGHTS_0022 = f"""
    CREATE OR REPLACE FUNCTION ras_access_rights_revocation_only() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'access rights are never deleted' USING ERRCODE = 'check_violation';
        END IF;
        IF OLD.revoked_at IS NOT NULL THEN
            RAISE EXCEPTION 'revoked access rights are final' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.revoked_at IS NULL THEN
            RAISE EXCEPTION 'the only update to access rights is their revocation'
                USING ERRCODE = 'check_violation';
        END IF;
        IF {_row('NEW', RECORDED_0022)} IS DISTINCT FROM {_row('OLD', RECORDED_0022)} THEN
            RAISE EXCEPTION 'recorded source, evidence and scope are immutable' USING ERRCODE = 'check_violation';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.revoked_by_user_id AND role = 'admin') THEN
            RAISE EXCEPTION 'only an admin revokes access rights' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.rights_epoch_at_revoke IS DISTINCT FROM
           (SELECT rights_epoch FROM library_items WHERE id = NEW.item_id) THEN
            RAISE EXCEPTION 'revocation must record the item''s bumped rights epoch'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END $$;
"""

RIGHTS_0023 = f"""
    CREATE OR REPLACE FUNCTION ras_access_rights_revocation_only() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'access rights are never deleted' USING ERRCODE = 'check_violation';
        END IF;
        IF OLD.revoked_at IS NOT NULL THEN
            -- After revocation the only change is cleanup removing metadata, once.
            IF OLD.metadata_scrubbed_at IS NOT NULL OR NEW.metadata_scrubbed_at IS NULL
               OR {_row('NEW', FIXED_RIGHTS + REVOCATION)} IS DISTINCT FROM {_row('OLD', FIXED_RIGHTS + REVOCATION)}
               OR NOT ({_kept_or_null(SCRUBBABLE_RIGHTS)}) THEN
                RAISE EXCEPTION 'revoked access rights are final except for removing their metadata once'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END IF;
        IF NEW.revoked_at IS NULL THEN
            RAISE EXCEPTION 'the only update to access rights is their revocation'
                USING ERRCODE = 'check_violation';
        END IF;
        IF {_row('NEW', RECORDED_0022)} IS DISTINCT FROM {_row('OLD', RECORDED_0022)}
           OR NEW.metadata_scrubbed_at IS NOT NULL THEN
            RAISE EXCEPTION 'recorded source, evidence and scope are immutable' USING ERRCODE = 'check_violation';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.revoked_by_user_id AND role = 'admin') THEN
            RAISE EXCEPTION 'only an admin revokes access rights' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.rights_epoch_at_revoke IS DISTINCT FROM
           (SELECT rights_epoch FROM library_items WHERE id = NEW.item_id) THEN
            RAISE EXCEPTION 'revocation must record the item''s bumped rights epoch'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END $$;
"""

TOMBSTONE_GUARD_0022 = f"""
    CREATE OR REPLACE FUNCTION ras_deletion_tombstones_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
            RAISE EXCEPTION 'tombstones are never deleted' USING ERRCODE = 'check_violation';
        END IF;
        IF {_row('NEW', TOMBSTONE_IDENTITY)} IS DISTINCT FROM {_row('OLD', TOMBSTONE_IDENTITY)}
           OR (OLD.purged_at IS NOT NULL AND ROW(NEW.purged_at, NEW.object_outcome)
                                           IS DISTINCT FROM ROW(OLD.purged_at, OLD.object_outcome))
           OR NEW.cleanup_attempts < OLD.cleanup_attempts THEN
            RAISE EXCEPTION 'a tombstone only records cleanup progress' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END $$;
"""

TOMBSTONE_GUARD_0023 = f"""
    CREATE OR REPLACE FUNCTION ras_deletion_tombstones_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE left_over text[];
    BEGIN
        IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
            RAISE EXCEPTION 'tombstones are never deleted' USING ERRCODE = 'check_violation';
        END IF;
        IF {_row('NEW', TOMBSTONE_IDENTITY)} IS DISTINCT FROM {_row('OLD', TOMBSTONE_IDENTITY)}
           OR (OLD.purged_at IS NOT NULL
               AND ROW(NEW.purged_at, NEW.object_outcome, NEW.scrubbed_fields, NEW.retained_fields)
                   IS DISTINCT FROM ROW(OLD.purged_at, OLD.object_outcome, OLD.scrubbed_fields, OLD.retained_fields))
           OR NEW.cleanup_attempts < OLD.cleanup_attempts THEN
            RAISE EXCEPTION 'a tombstone only records cleanup progress' USING ERRCODE = 'check_violation';
        END IF;
        IF OLD.purged_at IS NULL AND NEW.purged_at IS NOT NULL THEN
            left_over := ras_common_copy_unremoved_fields(NEW.item_id);
            IF cardinality(left_over) > 0 THEN
                RAISE EXCEPTION 'cleanup must remove % before purging (no retention basis)',
                    array_to_string(left_over, ', ') USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        RETURN NEW;
    END $$;
"""


def upgrade() -> None:
    # --- access_rights: metadata may be removed after revocation ------------
    op.add_column("access_rights", sa.Column("metadata_scrubbed_at", sa.DateTime(timezone=True)))
    op.alter_column("access_rights", "verified_source", nullable=True)
    op.create_check_constraint(op.f("ck_access_rights_verified_source_present"), "access_rights",
                               "verified_source IS NOT NULL OR metadata_scrubbed_at IS NOT NULL")
    op.drop_constraint(op.f("ck_access_rights_evidence_required"), "access_rights", type_="check")
    op.create_check_constraint(op.f("ck_access_rights_evidence_required"), "access_rights",
                               "license_url IS NOT NULL OR evidence_note IS NOT NULL"
                               " OR metadata_scrubbed_at IS NOT NULL")
    op.create_check_constraint(op.f("ck_access_rights_scrub_after_revoke"), "access_rights",
                               "metadata_scrubbed_at IS NULL OR revoked_at IS NOT NULL")
    op.execute(RIGHTS_0023)

    # --- deletion_tombstones: what cleanup removed and kept (names only) ------
    op.add_column("deletion_tombstones", sa.Column("scrubbed_fields", sa.ARRAY(sa.Text())))
    op.add_column("deletion_tombstones", sa.Column("retained_fields", sa.ARRAY(sa.Text())))
    op.create_unique_constraint("uq_deletion_tombstones_id_item_id", "deletion_tombstones", ["id", "item_id"])

    # --- revocation_metadata_retentions ---------------------------------------
    op.create_table("revocation_metadata_retentions",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tombstone_id", sa.UUID(), nullable=False),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("policy_source", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("confirmed_by_user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tombstone_id", "item_id"], ["deletion_tombstones.id", "deletion_tombstones.item_id"],
                                name="fk_revocation_metadata_retentions_tombstone", ondelete="RESTRICT"),
        sa.UniqueConstraint("tombstone_id", "field", name="uq_revocation_metadata_retentions_tombstone_id_field"),
        sa.CheckConstraint(f"field IN ({FIELDS})", name="field_valid"),
        sa.CheckConstraint("length(btrim(policy_source)) BETWEEN 1 AND 500", name="policy_source_length"),
        sa.CheckConstraint("length(btrim(scope)) BETWEEN 1 AND 500", name="scope_length"),
        sa.CheckConstraint("length(btrim(reason)) BETWEEN 1 AND 1000", name="reason_length"))
    op.create_index("ix_revocation_metadata_retentions_item_id", "revocation_metadata_retentions", ["item_id"])

    op.execute(f"""
        CREATE FUNCTION ras_common_copy_unremoved_fields(target uuid) RETURNS text[]
        LANGUAGE sql STABLE AS $$
            SELECT coalesce(array_agg(DISTINCT f.field ORDER BY f.field), '{{}}') FROM (
                SELECT 'title' AS field FROM library_items WHERE id = target AND title IS NOT NULL
                UNION ALL SELECT 'source_url' FROM paper_versions WHERE item_id = target
                    AND (source_url IS NOT NULL OR source_identifier IS NOT NULL OR version_label IS NOT NULL)
                UNION ALL SELECT 'source_url' FROM access_rights WHERE item_id = target
                    AND verified_source_url IS NOT NULL
                UNION ALL SELECT 'original_filename' FROM source_files WHERE item_id = target
                    AND original_filename IS NOT NULL
                UNION ALL SELECT 'verified_source' FROM access_rights WHERE item_id = target
                    AND verified_source IS NOT NULL
                UNION ALL SELECT 'license_url' FROM access_rights WHERE item_id = target AND license_url IS NOT NULL
                UNION ALL SELECT 'evidence_note' FROM access_rights WHERE item_id = target
                    AND evidence_note IS NOT NULL
                UNION ALL SELECT 'scope_conditions' FROM access_rights WHERE item_id = target
                    AND scope_conditions IS NOT NULL
            ) f
            WHERE NOT EXISTS (SELECT 1 FROM revocation_metadata_retentions r
                              WHERE r.item_id = target AND r.field = f.field)
        $$;
        {RIGHTS_0023}
        {SOURCE_FILES_0023}

        -- A revoked copy's title and version source may only be removed, never rewritten.
        CREATE FUNCTION ras_library_items_revoked_title() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.publication_state = 'revoked' AND NEW.title IS NOT NULL
               AND NEW.title IS DISTINCT FROM OLD.title THEN
                RAISE EXCEPTION 'a revoked common copy''s title can only be removed'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_library_items_revoked_title
            BEFORE UPDATE OF title ON library_items
            FOR EACH ROW EXECUTE FUNCTION ras_library_items_revoked_title();

        CREATE FUNCTION ras_paper_versions_revoked_source() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM library_items WHERE id = OLD.item_id AND publication_state = 'revoked')
               AND NOT ({_kept_or_null(("source_url", "source_identifier", "version_label"))}
                        AND ROW(NEW.id, NEW.item_id, NEW.coverage, NEW.received_at, NEW.created_at)
                            IS NOT DISTINCT FROM ROW(OLD.id, OLD.item_id, OLD.coverage, OLD.received_at,
                                                     OLD.created_at)) THEN
                RAISE EXCEPTION 'a revoked common copy''s version can only have its source removed'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_paper_versions_revoked_source
            BEFORE UPDATE ON paper_versions
            FOR EACH ROW EXECUTE FUNCTION ras_paper_versions_revoked_source();

        CREATE FUNCTION ras_revocation_metadata_retentions_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'retention records are append-only' USING ERRCODE = 'check_violation';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.confirmed_by_user_id AND role = 'admin') THEN
                RAISE EXCEPTION 'only an admin confirms a retention basis' USING ERRCODE = 'check_violation';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM deletion_tombstones WHERE id = NEW.tombstone_id AND purged_at IS NULL) THEN
                RAISE EXCEPTION 'a retention basis must be recorded before cleanup' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_revocation_metadata_retentions_guard
            BEFORE INSERT OR UPDATE OR DELETE ON revocation_metadata_retentions
            FOR EACH ROW EXECUTE FUNCTION ras_revocation_metadata_retentions_guard();
        CREATE TRIGGER trg_revocation_metadata_retentions_no_truncate
            BEFORE TRUNCATE ON revocation_metadata_retentions
            FOR EACH STATEMENT EXECUTE FUNCTION ras_revocation_metadata_retentions_guard();
    """)

    # Backfill: a copy purged under 0022 kept its metadata. No retention basis
    # can exist for it, so remove all seven fields now, then record that.
    op.execute("""
        UPDATE library_items SET title = NULL WHERE id IN
            (SELECT item_id FROM deletion_tombstones WHERE purged_at IS NOT NULL);
        UPDATE paper_versions SET source_url = NULL, source_identifier = NULL, version_label = NULL
            WHERE item_id IN (SELECT item_id FROM deletion_tombstones WHERE purged_at IS NOT NULL);
        UPDATE source_files SET original_filename = NULL
            WHERE item_id IN (SELECT item_id FROM deletion_tombstones WHERE purged_at IS NOT NULL);
        UPDATE access_rights SET verified_source = NULL, verified_source_url = NULL, license_url = NULL,
                evidence_note = NULL, scope_conditions = NULL, metadata_scrubbed_at = now()
            WHERE item_id IN (SELECT item_id FROM deletion_tombstones WHERE purged_at IS NOT NULL)
              AND revoked_at IS NOT NULL;
        UPDATE deletion_tombstones SET retained_fields = '{}',
            scrubbed_fields = ARRAY['title', 'source_url', 'original_filename', 'verified_source', 'license_url',
                                    'evidence_note', 'scope_conditions']
            WHERE purged_at IS NOT NULL;
    """)
    op.create_check_constraint(op.f("ck_deletion_tombstones_field_lists"), "deletion_tombstones",
                               "(purged_at IS NULL) = (scrubbed_fields IS NULL)"
                               " AND (purged_at IS NULL) = (retained_fields IS NULL)")
    op.execute(TOMBSTONE_GUARD_0023)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM revocation_metadata_retentions)
               OR EXISTS (SELECT 1 FROM access_rights WHERE metadata_scrubbed_at IS NOT NULL) THEN
                RAISE EXCEPTION 'removed metadata or retention records exist; downgrade below 0023 would lose them';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER trg_revocation_metadata_retentions_no_truncate ON revocation_metadata_retentions")
    op.execute("DROP TRIGGER trg_revocation_metadata_retentions_guard ON revocation_metadata_retentions")
    op.drop_table("revocation_metadata_retentions")
    op.execute("DROP FUNCTION ras_revocation_metadata_retentions_guard()")
    op.execute("DROP TRIGGER trg_paper_versions_revoked_source ON paper_versions")
    op.execute("DROP FUNCTION ras_paper_versions_revoked_source()")
    op.execute("DROP TRIGGER trg_library_items_revoked_title ON library_items")
    op.execute("DROP FUNCTION ras_library_items_revoked_title()")
    op.execute(SOURCE_FILES_0001)
    op.execute(TOMBSTONE_GUARD_0022)
    op.execute(RIGHTS_0022)
    op.execute("DROP FUNCTION ras_common_copy_unremoved_fields(uuid)")
    op.drop_constraint("uq_deletion_tombstones_id_item_id", "deletion_tombstones", type_="unique")
    op.drop_constraint(op.f("ck_deletion_tombstones_field_lists"), "deletion_tombstones", type_="check")
    op.drop_column("deletion_tombstones", "retained_fields")
    op.drop_column("deletion_tombstones", "scrubbed_fields")
    op.drop_constraint(op.f("ck_access_rights_scrub_after_revoke"), "access_rights", type_="check")
    op.drop_constraint(op.f("ck_access_rights_evidence_required"), "access_rights", type_="check")
    op.create_check_constraint(op.f("ck_access_rights_evidence_required"), "access_rights",
                               "license_url IS NOT NULL OR evidence_note IS NOT NULL")
    op.drop_constraint(op.f("ck_access_rights_verified_source_present"), "access_rights", type_="check")
    op.alter_column("access_rights", "verified_source", nullable=False)
    op.drop_column("access_rights", "metadata_scrubbed_at")
