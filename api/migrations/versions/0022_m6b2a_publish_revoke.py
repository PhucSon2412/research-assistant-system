"""Controlled metadata publication and revocation of common copies (M6b-2a).

A ``pending`` common copy (M6b-1) may now become ``listed`` (metadata visible
to every User, still ``metadata_only``) and either may become ``revoked``,
which is final. The immutability trigger from 0021 is replaced by a gate:

- ``pending -> listed`` needs exactly one live ``access_rights`` row at the
  item's current rights epoch and a ``common_publications`` row (checked at
  commit). A partial unique index allows one live publication per checksum,
  so two concurrent publishes of the same bytes cannot both commit.
- ``pending|listed -> revoked`` must bump ``rights_epoch`` and, at commit,
  have its rights revoked, no live publication and a ``deletion_tombstones``
  row. ``access_rights`` accepts only that revocation update; the Admin's
  recorded source, evidence and scope stay immutable, and nothing is deleted.
- Tombstones have no foreign keys and cannot be deleted: they outlive the rows
  they describe so a restored snapshot can be made to hide the copy again.

Common items without recorded rights (the pre-M6b-1 ``listed`` ones) keep
their state and behaviour; no transition applies to them.
"""
from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

REVOCATION_REASONS = ("'rights_recorded_in_error', 'rights_withdrawn', 'rights_expired', "
                      "'wrong_file_or_version', 'duplicate_copy', 'other'")
OLD_STATE_CHECK = ("(visibility = 'private' AND publication_state IS NULL)"
                   " OR (visibility = 'common' AND publication_state IS NOT NULL"
                   " AND publication_state IN ('listed', 'pending'))")
NEW_STATE_CHECK = OLD_STATE_CHECK.replace("('listed', 'pending')", "('listed', 'pending', 'revoked')")
# Every access_rights column the Admin recorded at approval; never changes.
RECORDED_COLUMNS = ("id", "item_id", "item_visibility", "version_id", "source_file_id", "file_checksum_sha256",
                    "origin_kind", "origin_review_request_id", "verified_source", "verified_source_url",
                    "license_url", "evidence_note", "checked_on", "reviewer_user_id", "recorded_at",
                    "scope_audience", "permits_original_download", "permits_fulltext_search",
                    "permits_external_processing", "scope_conditions", "rights_epoch_at_grant")
TOMBSTONE_IDENTITY = ("id", "target_kind", "item_id", "version_id", "source_file_id", "storage_key",
                      "access_rights_id", "reason_code", "prior_publication_state", "rights_epoch",
                      "recorded_at", "recorded_by_user_id", "purge_due_at")


def _row(prefix: str, columns: tuple[str, ...]) -> str:
    return "ROW(" + ", ".join(f"{prefix}.{c}" for c in columns) + ")"


def upgrade() -> None:
    # --- library_items: a final 'revoked' state and a transition gate -------
    op.drop_constraint(op.f("ck_library_items_publication_state_valid"), "library_items", type_="check")
    op.create_check_constraint(op.f("ck_library_items_publication_state_valid"), "library_items", NEW_STATE_CHECK)
    op.execute("DROP TRIGGER trg_library_items_publication_immutable ON library_items")
    op.execute("DROP FUNCTION ras_library_items_publication_immutable()")

    # --- access_rights: revocation columns, nothing else changes ------------
    op.add_column("access_rights", sa.Column("revoked_at", sa.DateTime(timezone=True)))
    op.add_column("access_rights", sa.Column("revoked_by_user_id", sa.UUID(),
                                             sa.ForeignKey("users.id", ondelete="RESTRICT",
                                                           name="fk_access_rights_revoked_by_user_id_users")))
    op.add_column("access_rights", sa.Column("revocation_reason_code", sa.Text()))
    op.add_column("access_rights", sa.Column("rights_epoch_at_revoke", sa.BigInteger()))
    op.create_check_constraint(
        op.f("ck_access_rights_revocation_fields"), "access_rights",
        "(revoked_at IS NULL) = (revoked_by_user_id IS NULL)"
        " AND (revoked_at IS NULL) = (revocation_reason_code IS NULL)"
        " AND (revoked_at IS NULL) = (rights_epoch_at_revoke IS NULL)")
    op.create_check_constraint(op.f("ck_access_rights_revocation_reason_valid"), "access_rights",
                               f"revocation_reason_code IS NULL OR revocation_reason_code IN ({REVOCATION_REASONS})")
    op.create_check_constraint(op.f("ck_access_rights_revoke_bumps_epoch"), "access_rights",
                               "rights_epoch_at_revoke IS NULL OR rights_epoch_at_revoke > rights_epoch_at_grant")
    # Target for common_publications: a publication names the exact rights row and checksum.
    op.create_unique_constraint("uq_access_rights_id_item_id_file_checksum_sha256", "access_rights",
                                ["id", "item_id", "file_checksum_sha256"])
    op.execute("DROP TRIGGER trg_access_rights_append_only ON access_rights")
    op.execute("DROP FUNCTION ras_access_rights_append_only()")

    # --- common_publications ------------------------------------------------
    op.create_table("common_publications",
        sa.Column("item_id", sa.UUID(), primary_key=True),
        sa.Column("item_visibility", sa.Text(), nullable=False, server_default=sa.text("'common'")),
        sa.Column("access_rights_id", sa.UUID(), nullable=False),
        sa.Column("file_checksum_sha256", sa.Text(), nullable=False),
        sa.Column("verified_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_by_user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["item_id", "item_visibility"], ["library_items.id", "library_items.visibility"],
                                name="fk_common_publications_common_item", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["access_rights_id", "item_id", "file_checksum_sha256"],
                                ["access_rights.id", "access_rights.item_id", "access_rights.file_checksum_sha256"],
                                name="fk_common_publications_rights", ondelete="RESTRICT"),
        sa.UniqueConstraint("access_rights_id", name="uq_common_publications_access_rights_id"),
        sa.CheckConstraint("item_visibility = 'common'", name="common_only"),
        sa.CheckConstraint("verified_byte_size > 0", name="size_positive"),
        sa.CheckConstraint("revoked_at IS NULL OR revoked_at >= published_at", name="revoked_after"))
    # Exact-byte duplicate rule: one live publication per checksum.
    op.create_index("uq_common_publications_live_checksum", "common_publications", ["file_checksum_sha256"],
                    unique=True, postgresql_where=sa.text("revoked_at IS NULL"))

    # --- deletion_tombstones ------------------------------------------------
    # No foreign keys on purpose: a tombstone outlives the rows it names and is
    # replayed into restored snapshots that may predate them.
    op.create_table("deletion_tombstones",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("version_id", sa.UUID()),
        sa.Column("source_file_id", sa.UUID()),
        sa.Column("storage_key", sa.Text()),
        sa.Column("access_rights_id", sa.UUID()),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("prior_publication_state", sa.Text(), nullable=False),
        sa.Column("rights_epoch", sa.BigInteger(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_by_user_id", sa.UUID(), nullable=False),
        sa.Column("purge_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True)),
        sa.Column("object_outcome", sa.Text()),
        sa.Column("cleanup_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_cleanup_at", sa.DateTime(timezone=True)),
        sa.Column("last_cleanup_error", sa.Text()),
        sa.UniqueConstraint("target_kind", "item_id", name="uq_deletion_tombstones_target_kind_item_id"),
        sa.CheckConstraint("target_kind IN ('common_copy')", name="target_kind_valid"),
        sa.CheckConstraint(f"reason_code IN ({REVOCATION_REASONS})", name="reason_valid"),
        sa.CheckConstraint("prior_publication_state IN ('pending', 'listed')", name="prior_state_valid"),
        sa.CheckConstraint("rights_epoch > 0", name="epoch_positive"),
        sa.CheckConstraint("purge_due_at > recorded_at", name="purge_after_record"),
        sa.CheckConstraint("(purged_at IS NULL) = (object_outcome IS NULL)", name="purged_fields"),
        sa.CheckConstraint("object_outcome IS NULL OR object_outcome IN ('deleted', 'already_absent', "
                           "'shared_kept', 'no_object')", name="object_outcome_valid"),
        sa.CheckConstraint("cleanup_attempts >= 0", name="attempts_non_negative"),
        sa.CheckConstraint("last_cleanup_error IS NULL OR last_cleanup_error ~ '^[a-z_]{1,64}$'",
                           name="error_is_code"))
    op.create_index("ix_deletion_tombstones_unpurged", "deletion_tombstones", ["purge_due_at"],
                    postgresql_where=sa.text("purged_at IS NULL"))

    op.execute(f"""
        CREATE FUNCTION ras_library_items_publication_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            -- Scoped to reviewed copies: private items get their revocation path later.
            IF NEW.rights_epoch < OLD.rights_epoch
               AND EXISTS (SELECT 1 FROM access_rights WHERE item_id = NEW.id) THEN
                RAISE EXCEPTION 'rights epoch of a reviewed common copy never decreases'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.content_status <> 'metadata_only'
               AND EXISTS (SELECT 1 FROM access_rights WHERE item_id = NEW.id) THEN
                RAISE EXCEPTION 'a reviewed common copy has no full-text path yet' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.publication_state IS NOT DISTINCT FROM OLD.publication_state THEN
                RETURN NEW;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM access_rights WHERE item_id = NEW.id) THEN
                RAISE EXCEPTION 'only a common copy with recorded rights changes publication state'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.publication_state = 'pending' AND NEW.publication_state = 'listed' THEN
                IF NEW.rights_epoch <> OLD.rights_epoch
                   OR (SELECT count(*) FROM access_rights WHERE item_id = NEW.id) <> 1
                   OR NOT EXISTS (SELECT 1 FROM access_rights WHERE item_id = NEW.id AND revoked_at IS NULL
                                  AND rights_epoch_at_grant = NEW.rights_epoch) THEN
                    RAISE EXCEPTION 'publication needs exactly one live access rights row at the current epoch'
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSIF OLD.publication_state IN ('pending', 'listed') AND NEW.publication_state = 'revoked' THEN
                IF NEW.rights_epoch <= OLD.rights_epoch THEN
                    RAISE EXCEPTION 'revocation must bump the rights epoch' USING ERRCODE = 'check_violation';
                END IF;
            ELSE
                RAISE EXCEPTION 'publication state transition % -> % is not allowed',
                    OLD.publication_state, NEW.publication_state USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_library_items_publication_transition
            BEFORE UPDATE ON library_items
            FOR EACH ROW EXECUTE FUNCTION ras_library_items_publication_transition();

        CREATE FUNCTION ras_library_items_publication_consistent() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE state text;
        BEGIN
            SELECT publication_state INTO state FROM library_items WHERE id = NEW.id;
            IF state = 'listed' AND EXISTS (SELECT 1 FROM access_rights WHERE item_id = NEW.id)
               AND NOT EXISTS (SELECT 1 FROM common_publications WHERE item_id = NEW.id AND revoked_at IS NULL) THEN
                RAISE EXCEPTION 'a published common copy needs its publication record'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF state = 'revoked' THEN
                IF EXISTS (SELECT 1 FROM access_rights WHERE item_id = NEW.id AND revoked_at IS NULL)
                   OR EXISTS (SELECT 1 FROM common_publications WHERE item_id = NEW.id AND revoked_at IS NULL) THEN
                    RAISE EXCEPTION 'a revoked common copy needs its rights and publication revoked'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM deletion_tombstones
                               WHERE target_kind = 'common_copy' AND item_id = NEW.id) THEN
                    RAISE EXCEPTION 'a revoked common copy needs its tombstone' USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER trg_library_items_publication_consistent
            AFTER UPDATE OF publication_state ON library_items DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION ras_library_items_publication_consistent();

        CREATE FUNCTION ras_access_rights_revocation_only() RETURNS trigger
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
            IF {_row('NEW', RECORDED_COLUMNS)} IS DISTINCT FROM {_row('OLD', RECORDED_COLUMNS)} THEN
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
        CREATE TRIGGER trg_access_rights_revocation_only
            BEFORE UPDATE OR DELETE ON access_rights
            FOR EACH ROW EXECUTE FUNCTION ras_access_rights_revocation_only();

        CREATE FUNCTION ras_access_rights_revocation_consistent() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.revoked_at IS NOT NULL AND (SELECT publication_state FROM library_items
                                               WHERE id = NEW.item_id) IS DISTINCT FROM 'revoked' THEN
                RAISE EXCEPTION 'revoked access rights need their common copy revoked'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER trg_access_rights_revocation_consistent
            AFTER UPDATE ON access_rights DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION ras_access_rights_revocation_consistent();

        CREATE FUNCTION ras_common_publications_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'publication records are never deleted' USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL
                   OR ROW(NEW.item_id, NEW.item_visibility, NEW.access_rights_id, NEW.file_checksum_sha256,
                          NEW.verified_byte_size, NEW.published_at, NEW.published_by_user_id)
                      IS DISTINCT FROM
                      ROW(OLD.item_id, OLD.item_visibility, OLD.access_rights_id, OLD.file_checksum_sha256,
                          OLD.verified_byte_size, OLD.published_at, OLD.published_by_user_id) THEN
                    RAISE EXCEPTION 'the only update to a publication record is its revocation'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.published_by_user_id AND role = 'admin') THEN
                RAISE EXCEPTION 'only an admin publishes a common copy' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.revoked_at IS NOT NULL OR NOT EXISTS (
                SELECT 1 FROM access_rights r JOIN library_items i ON i.id = r.item_id
                WHERE r.id = NEW.access_rights_id AND r.revoked_at IS NULL
                  AND r.rights_epoch_at_grant = i.rights_epoch AND i.publication_state = 'listed') THEN
                RAISE EXCEPTION 'publication needs live access rights at the current epoch'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_common_publications_guard
            BEFORE INSERT OR UPDATE OR DELETE ON common_publications
            FOR EACH ROW EXECUTE FUNCTION ras_common_publications_guard();

        CREATE FUNCTION ras_common_publications_consistent() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE state text;
        BEGIN
            SELECT publication_state INTO state FROM library_items WHERE id = NEW.item_id;
            IF (NEW.revoked_at IS NULL AND state IS DISTINCT FROM 'listed')
               OR (NEW.revoked_at IS NOT NULL AND state IS DISTINCT FROM 'revoked') THEN
                RAISE EXCEPTION 'publication record and common copy state disagree'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER trg_common_publications_consistent
            AFTER INSERT OR UPDATE ON common_publications DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION ras_common_publications_consistent();

        CREATE FUNCTION ras_deletion_tombstones_guard() RETURNS trigger
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
        CREATE TRIGGER trg_deletion_tombstones_guard
            BEFORE UPDATE OR DELETE ON deletion_tombstones
            FOR EACH ROW EXECUTE FUNCTION ras_deletion_tombstones_guard();
        CREATE TRIGGER trg_deletion_tombstones_no_truncate
            BEFORE TRUNCATE ON deletion_tombstones
            FOR EACH STATEMENT EXECUTE FUNCTION ras_deletion_tombstones_guard();

        CREATE FUNCTION ras_deletion_tombstones_target_revoked() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            -- A tombstone replayed into an older snapshot may name an item that does not exist there.
            IF EXISTS (SELECT 1 FROM library_items WHERE id = NEW.item_id
                       AND publication_state IS DISTINCT FROM 'revoked') THEN
                RAISE EXCEPTION 'a tombstoned common copy must be revoked' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER trg_deletion_tombstones_target_revoked
            AFTER INSERT ON deletion_tombstones DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION ras_deletion_tombstones_target_revoked();
    """)


def downgrade() -> None:
    # 0021 has no representation for a published or revoked copy.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM library_items WHERE publication_state = 'revoked')
               OR EXISTS (SELECT 1 FROM common_publications)
               OR EXISTS (SELECT 1 FROM deletion_tombstones)
               OR EXISTS (SELECT 1 FROM access_rights WHERE revoked_at IS NOT NULL) THEN
                RAISE EXCEPTION 'published or revoked common copies exist; downgrade below 0022 would lose them';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER trg_deletion_tombstones_target_revoked ON deletion_tombstones")
    op.execute("DROP TRIGGER trg_deletion_tombstones_no_truncate ON deletion_tombstones")
    op.execute("DROP TRIGGER trg_deletion_tombstones_guard ON deletion_tombstones")
    op.drop_table("deletion_tombstones")
    op.drop_table("common_publications")  # drops its triggers
    for fn in ("ras_deletion_tombstones_target_revoked", "ras_deletion_tombstones_guard",
               "ras_common_publications_consistent", "ras_common_publications_guard"):
        op.execute(f"DROP FUNCTION {fn}()")
    op.execute("DROP TRIGGER trg_access_rights_revocation_consistent ON access_rights")
    op.execute("DROP FUNCTION ras_access_rights_revocation_consistent()")
    op.execute("DROP TRIGGER trg_access_rights_revocation_only ON access_rights")
    op.execute("DROP FUNCTION ras_access_rights_revocation_only()")
    op.drop_constraint("uq_access_rights_id_item_id_file_checksum_sha256", "access_rights", type_="unique")
    for name in ("revoke_bumps_epoch", "revocation_reason_valid", "revocation_fields"):
        op.drop_constraint(op.f(f"ck_access_rights_{name}"), "access_rights", type_="check")
    for column in ("rights_epoch_at_revoke", "revocation_reason_code", "revoked_by_user_id", "revoked_at"):
        op.drop_column("access_rights", column)
    op.execute("""
        CREATE FUNCTION ras_access_rights_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'access rights have no update/delete path yet' USING ERRCODE = 'check_violation';
        END $$;
        CREATE TRIGGER trg_access_rights_append_only
            BEFORE UPDATE OR DELETE ON access_rights
            FOR EACH ROW EXECUTE FUNCTION ras_access_rights_append_only();
    """)
    op.execute("DROP TRIGGER trg_library_items_publication_consistent ON library_items")
    op.execute("DROP FUNCTION ras_library_items_publication_consistent()")
    op.execute("DROP TRIGGER trg_library_items_publication_transition ON library_items")
    op.execute("DROP FUNCTION ras_library_items_publication_transition()")
    op.execute("""
        CREATE FUNCTION ras_library_items_publication_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.publication_state IS DISTINCT FROM OLD.publication_state THEN
                RAISE EXCEPTION 'publication state has no transition yet' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_library_items_publication_immutable
            BEFORE UPDATE ON library_items
            FOR EACH ROW EXECUTE FUNCTION ras_library_items_publication_immutable();
    """)
    op.drop_constraint(op.f("ck_library_items_publication_state_valid"), "library_items", type_="check")
    op.create_check_constraint(op.f("ck_library_items_publication_state_valid"), "library_items", OLD_STATE_CHECK)
