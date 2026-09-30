"""Index a published reviewed common copy through the shared pipeline (M6b-2b2b).

- ``ingestion_jobs.source_file_id`` pins the exact file a job reads. A common
  file keeps ``source_files.job_id`` NULL (it is filled once and would bind
  the file to one job forever), so the job names the file instead, through a
  composite FK ``(source_file_id, version_id) -> source_files(id, version_id)``
  that, with the existing ``(version_id, item_id)`` FK, keeps item, version and
  file of the job consistent. A ``promotion`` job must pin its file, and at
  insert must name the file of its copy's AccessRights and carry
  ``rights_epoch_at_start``; the pinned columns never change afterwards. At
  most one unfinished promotion job exists per file (partial unique index), so
  repeated or concurrent starts cannot run two jobs; later reindex jobs are
  new rows once the previous one finished.
- ``embedding_calls.owner_user_id`` becomes nullable: NULL means the call is
  charged to the application (common/system), never to the User who uploaded
  the private copy or to the Admin. A trigger keeps the owner equal to the
  item's scope: NULL for a common file, the owner for a private one.
- The 0022 rule "a reviewed common copy never leaves ``metadata_only``" is
  replaced by a gate: such a copy is ``indexed`` (or names an active
  generation) only while ``listed``, and at commit only when its active
  generation, the promotion job that built it, exactly one live AccessRights
  at the current epoch for the same version/file that permit full-text search
  *and* external processing to ``instance_users`` without conditions, and the
  live publication of that file all agree. The 0024 revoked-copy fence stays.
"""
from alembic import op
import sqlalchemy as sa

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

TRANSITION_0022 = """
    CREATE OR REPLACE FUNCTION ras_library_items_publication_transition() RETURNS trigger
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
"""
# The one change: leaving metadata_only is allowed while listed; the deferred gate checks the rest at commit.
TRANSITION_0025 = TRANSITION_0022.replace(
    """        IF NEW.content_status <> 'metadata_only'
           AND EXISTS (SELECT 1 FROM access_rights WHERE item_id = NEW.id) THEN
            RAISE EXCEPTION 'a reviewed common copy has no full-text path yet' USING ERRCODE = 'check_violation';
        END IF;
""",
    # A revoked copy is left to the 0024 fence here; the deferred gate refuses it at commit either way.
    """        IF (NEW.content_status <> 'metadata_only' OR NEW.active_index_generation_id IS NOT NULL)
           AND NEW.publication_state = 'pending'
           AND EXISTS (SELECT 1 FROM access_rights WHERE item_id = NEW.id) THEN
            RAISE EXCEPTION 'a reviewed common copy is indexed only while listed' USING ERRCODE = 'check_violation';
        END IF;
""")
assert TRANSITION_0025 != TRANSITION_0022


def upgrade() -> None:
    # --- ingestion_jobs: a job pins the exact file it reads ------------------
    op.add_column("ingestion_jobs", sa.Column("source_file_id", sa.UUID()))
    op.create_foreign_key("fk_ingestion_jobs_source_file_version", "ingestion_jobs", "source_files",
                          ["source_file_id", "version_id"], ["id", "version_id"], ondelete="RESTRICT")
    op.create_index("ix_ingestion_jobs_source_file_id", "ingestion_jobs", ["source_file_id"])
    # Promotion jobs could only come from fixtures or out-of-band SQL before this revision:
    # pin them to their copy's reviewed file, and refuse the upgrade if one cannot be.
    op.execute("""
        UPDATE ingestion_jobs j SET source_file_id = r.source_file_id, version_id = r.version_id
          FROM access_rights r
         WHERE j.origin = 'promotion' AND j.source_file_id IS NULL AND r.item_id = j.item_id
           AND (j.version_id IS NULL OR j.version_id = r.version_id);
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM ingestion_jobs WHERE origin = 'promotion' AND source_file_id IS NULL) THEN
                RAISE EXCEPTION 'promotion jobs without a reviewed common file exist; resolve them before 0025';
            END IF;
        END $$;
    """)
    op.create_check_constraint(op.f("ck_ingestion_jobs_source_file_requires_version"), "ingestion_jobs",
                               "source_file_id IS NULL OR version_id IS NOT NULL")
    op.create_check_constraint(op.f("ck_ingestion_jobs_promotion_pins_file"), "ingestion_jobs",
                               "origin <> 'promotion' OR (scope_visibility = 'common' AND item_id IS NOT NULL"
                               " AND version_id IS NOT NULL AND source_file_id IS NOT NULL)")
    # Repeated or concurrent starts cannot run two jobs on the same file.
    op.create_index("uq_ingestion_jobs_live_promotion_file", "ingestion_jobs", ["source_file_id"], unique=True,
                    postgresql_where=sa.text("origin = 'promotion' AND finished_at IS NULL"))
    op.execute("""
        CREATE FUNCTION ras_ingestion_jobs_file_pinned() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF (OLD.source_file_id IS NOT NULL OR NEW.source_file_id IS NOT NULL)
                   AND ROW(NEW.origin, NEW.scope_visibility, NEW.item_id, NEW.version_id, NEW.source_file_id,
                           NEW.rights_epoch_at_start, NEW.idempotency_key)
                       IS DISTINCT FROM
                       ROW(OLD.origin, OLD.scope_visibility, OLD.item_id, OLD.version_id, OLD.source_file_id,
                           OLD.rights_epoch_at_start, OLD.idempotency_key) THEN
                    RAISE EXCEPTION 'an ingestion job keeps its pinned item, version, file and rights epoch'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.origin = 'promotion' THEN
                IF NEW.rights_epoch_at_start IS NULL THEN
                    RAISE EXCEPTION 'a promotion job records the rights epoch it started at'
                        USING ERRCODE = 'check_violation';
                END IF;
                -- A missing file is ck_ingestion_jobs_promotion_pins_file's to refuse.
                IF NEW.source_file_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM access_rights r WHERE r.item_id = NEW.item_id
                               AND r.version_id = NEW.version_id AND r.source_file_id = NEW.source_file_id) THEN
                    RAISE EXCEPTION 'a promotion job reads the reviewed file of its common copy'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_ingestion_jobs_file_pinned
            BEFORE INSERT OR UPDATE ON ingestion_jobs
            FOR EACH ROW EXECUTE FUNCTION ras_ingestion_jobs_file_pinned();
    """)

    # --- embedding_calls: NULL owner = charged to the application ------------
    op.alter_column("embedding_calls", "owner_user_id", nullable=True)
    op.execute("""
        CREATE FUNCTION ras_embedding_calls_budget_owner() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE vis text; owner uuid;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id THEN
                    RAISE EXCEPTION 'an embedding call keeps its budget owner' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            SELECT i.visibility, i.owner_user_id INTO vis, owner
              FROM source_files f JOIN library_items i ON i.id = f.item_id WHERE f.id = NEW.source_file_id;
            IF vis = 'common' THEN
                IF NEW.owner_user_id IS NOT NULL THEN
                    RAISE EXCEPTION 'a common copy''s embedding is charged to the application, not a user'
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSIF vis = 'private' THEN
                IF NEW.owner_user_id IS DISTINCT FROM owner THEN
                    RAISE EXCEPTION 'a private embedding is charged to the item owner' USING ERRCODE = 'check_violation';
                END IF;
            ELSIF NEW.owner_user_id IS NULL THEN
                RAISE EXCEPTION 'an embedding call outside a library item needs an owner'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_embedding_calls_budget_owner
            BEFORE INSERT OR UPDATE OF owner_user_id ON embedding_calls
            FOR EACH ROW EXECUTE FUNCTION ras_embedding_calls_budget_owner();
    """)

    # --- library_items: the full-text gate of a reviewed copy ----------------
    op.execute(TRANSITION_0025)
    op.execute("""
        CREATE FUNCTION ras_common_copy_index_block(target uuid) RETURNS text
        LANGUAGE plpgsql STABLE AS $$
        DECLARE it library_items%ROWTYPE; g index_generations%ROWTYPE; j ingestion_jobs%ROWTYPE;
                r access_rights%ROWTYPE; f source_files%ROWTYPE; n integer;
        BEGIN
            SELECT * INTO it FROM library_items WHERE id = target;
            IF it.id IS NULL THEN
                RETURN 'missing';
            END IF;
            IF it.publication_state IS DISTINCT FROM 'listed' THEN
                RETURN 'not_listed';
            END IF;
            IF EXISTS (SELECT 1 FROM deletion_tombstones WHERE item_id = target) THEN
                RETURN 'tombstoned';
            END IF;
            IF it.content_status IS DISTINCT FROM 'indexed' OR it.active_index_generation_id IS NULL THEN
                RETURN 'indexed_without_generation';
            END IF;
            SELECT * INTO g FROM index_generations WHERE id = it.active_index_generation_id;
            IF g.item_id IS DISTINCT FROM target OR g.state IS DISTINCT FROM 'active' OR g.dimensions IS NULL
               OR g.version_id IS DISTINCT FROM it.active_version_id OR g.rights_epoch <> it.rights_epoch THEN
                RETURN 'generation_mismatch';
            END IF;
            SELECT * INTO j FROM ingestion_jobs WHERE id = g.job_id;
            IF j.origin IS DISTINCT FROM 'promotion' OR j.source_file_id IS DISTINCT FROM g.source_file_id
               OR j.version_id IS DISTINCT FROM g.version_id
               OR j.rights_epoch_at_start IS DISTINCT FROM it.rights_epoch THEN
                RETURN 'job_mismatch';
            END IF;
            SELECT count(*) INTO n FROM access_rights WHERE item_id = target;
            SELECT * INTO r FROM access_rights WHERE item_id = target AND revoked_at IS NULL
               AND rights_epoch_at_grant = it.rights_epoch AND version_id = g.version_id
               AND source_file_id = g.source_file_id;
            IF n <> 1 OR r.id IS NULL THEN
                RETURN 'rights_not_current';
            END IF;
            IF r.scope_audience IS DISTINCT FROM 'instance_users' OR NOT r.permits_fulltext_search
               OR NOT r.permits_external_processing OR r.scope_conditions IS NOT NULL THEN
                RETURN 'rights_not_indexable';
            END IF;
            SELECT * INTO f FROM source_files WHERE id = r.source_file_id;
            IF f.checksum_sha256 IS DISTINCT FROM r.file_checksum_sha256 OR NOT EXISTS (
                SELECT 1 FROM common_publications p WHERE p.item_id = target AND p.access_rights_id = r.id
                   AND p.revoked_at IS NULL AND p.file_checksum_sha256 = r.file_checksum_sha256
                   AND p.verified_byte_size = f.byte_size) THEN
                RETURN 'publication_mismatch';
            END IF;
            RETURN NULL;
        END $$;

        -- Checked at commit on the row as it is then, so a revoke in the same transaction that
        -- clears the index passes, and one that leaves it set does not.
        CREATE FUNCTION ras_common_copy_index_gate() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE cur library_items%ROWTYPE; reason text;
        BEGIN
            SELECT * INTO cur FROM library_items WHERE id = NEW.id;
            IF cur.id IS NULL OR cur.visibility <> 'common'
               OR NOT EXISTS (SELECT 1 FROM access_rights WHERE item_id = cur.id)
               OR (cur.content_status = 'metadata_only' AND cur.active_index_generation_id IS NULL) THEN
                RETURN NULL;
            END IF;
            reason := ras_common_copy_index_block(cur.id);
            IF reason IS NOT NULL THEN
                RAISE EXCEPTION 'a reviewed common copy is indexed only under its live full-text rights, publication and active generation (%)',
                    reason USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER trg_common_copy_index_gate
            AFTER UPDATE OF content_status, active_index_generation_id, active_version_id, publication_state,
                rights_epoch ON library_items DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION ras_common_copy_index_gate();
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            -- Only what 0024 cannot represent: application-charged calls and an indexed reviewed copy.
            IF EXISTS (SELECT 1 FROM embedding_calls WHERE owner_user_id IS NULL)
               OR EXISTS (SELECT 1 FROM library_items i WHERE i.content_status <> 'metadata_only'
                          AND EXISTS (SELECT 1 FROM access_rights r WHERE r.item_id = i.id)) THEN
                RAISE EXCEPTION 'common copies were indexed; downgrade below 0025 would lose their accounting and gate';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER trg_common_copy_index_gate ON library_items")
    op.execute("DROP FUNCTION ras_common_copy_index_gate()")
    op.execute("DROP FUNCTION ras_common_copy_index_block(uuid)")
    op.execute(TRANSITION_0022)
    op.execute("DROP TRIGGER trg_embedding_calls_budget_owner ON embedding_calls")
    op.execute("DROP FUNCTION ras_embedding_calls_budget_owner()")
    op.alter_column("embedding_calls", "owner_user_id", nullable=False)
    op.execute("DROP TRIGGER trg_ingestion_jobs_file_pinned ON ingestion_jobs")
    op.execute("DROP FUNCTION ras_ingestion_jobs_file_pinned()")
    op.drop_index("uq_ingestion_jobs_live_promotion_file", table_name="ingestion_jobs")
    op.drop_constraint(op.f("ck_ingestion_jobs_promotion_pins_file"), "ingestion_jobs", type_="check")
    op.drop_constraint(op.f("ck_ingestion_jobs_source_file_requires_version"), "ingestion_jobs", type_="check")
    op.drop_index("ix_ingestion_jobs_source_file_id", table_name="ingestion_jobs")
    op.drop_constraint("fk_ingestion_jobs_source_file_version", "ingestion_jobs", type_="foreignkey")
    op.drop_column("ingestion_jobs", "source_file_id")
