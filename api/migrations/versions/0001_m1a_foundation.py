"""M1a foundation: users, bootstrap marker, library item/version/file, ingestion job, audit.

Revision ID: 0001
Revises:
Create Date: 2026-09-24

Deferred on purpose (later revisions, not this one):
- M1b: invitations, sessions, password_reset_tokens (reference users.id;
  users.status / users.password_changed_at already exist).
- M1c: processing_acknowledgements, upload_intents (reference users.id and,
  once consumed, library_items/ingestion_jobs).
- M2+: step_attempts, sections, chunks, references; M3: embeddings/pgvector
  columns and active index pointer; M5/M6: access_rights (targets the
  (paper_versions.id, item_id) and (source_files.id, version_id) keys),
  deletion tombstones, promotion requests, discovery tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
UUID_DEFAULT = sa.text("gen_random_uuid()")


def _ts(name: str, nullable: bool = False, default: bool = True) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=NOW if default else None, nullable=nullable
    )


def upgrade() -> None:
    # Prepare pgvector for M3; no vector columns yet (model dimensions must be
    # verified at integration time, not hardcoded here).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), server_default=UUID_DEFAULT, nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), server_default="user", nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("password_verifier", sa.Text(), nullable=False),
        _ts("password_changed_at"),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "email = lower(btrim(email)) AND email LIKE '_%%@_%%'", name=op.f("ck_users_email_normalised")
        ),
        sa.CheckConstraint(
            "password_verifier LIKE '$argon2id$%%'", name=op.f("ck_users_password_verifier_argon2id")
        ),
        sa.CheckConstraint("role IN ('admin', 'user')", name=op.f("ck_users_role_valid")),
        sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f("ck_users_status_valid")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        _ts("occurred_at"),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(actor_type = 'user') = (actor_user_id IS NOT NULL)",
            name=op.f("ck_audit_events_actor_user_matches_type"),
        ),
        sa.CheckConstraint("actor_type IN ('user', 'system')", name=op.f("ck_audit_events_actor_type_valid")),
        sa.CheckConstraint(
            "outcome IN ('success', 'denied', 'failure')", name=op.f("ck_audit_events_outcome_valid")
        ),
        sa.CheckConstraint("length(action) > 0", name=op.f("ck_audit_events_action_not_empty")),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], name=op.f("fk_audit_events_actor_user_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"])
    op.create_index("ix_audit_events_target", "audit_events", ["target_type", "target_id"])

    op.create_table(
        "bootstrap_state",
        sa.Column("id", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("admin_user_id", sa.UUID(), nullable=False),
        _ts("completed_at"),
        sa.CheckConstraint("id", name=op.f("ck_bootstrap_state_singleton")),
        sa.ForeignKeyConstraint(
            ["admin_user_id"], ["users.id"], name=op.f("fk_bootstrap_state_admin_user_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bootstrap_state")),
        sa.UniqueConstraint("admin_user_id", name=op.f("uq_bootstrap_state_admin_user_id")),
    )

    op.create_table(
        "library_items",
        sa.Column("id", sa.UUID(), server_default=UUID_DEFAULT, nullable=False),
        sa.Column("visibility", sa.Text(), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=True),
        sa.Column("content_status", sa.Text(), server_default="metadata_only", nullable=False),
        sa.Column("active_version_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("rights_epoch", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "(visibility = 'private' AND owner_user_id IS NOT NULL)"
            " OR (visibility = 'common' AND owner_user_id IS NULL)",
            name=op.f("ck_library_items_owner_matches_visibility"),
        ),
        sa.CheckConstraint(
            "content_status <> 'indexed' OR active_version_id IS NOT NULL",
            name=op.f("ck_library_items_indexed_requires_active_version"),
        ),
        sa.CheckConstraint(
            "content_status IN ('metadata_only', 'indexed')", name=op.f("ck_library_items_content_status_valid")
        ),
        sa.CheckConstraint("visibility IN ('common', 'private')", name=op.f("ck_library_items_visibility_valid")),
        sa.CheckConstraint("rights_epoch >= 0", name=op.f("ck_library_items_rights_epoch_non_negative")),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_library_items_created_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], name=op.f("fk_library_items_owner_user_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_library_items")),
    )
    op.create_index("ix_library_items_owner_user_id", "library_items", ["owner_user_id"])

    op.create_table(
        "paper_versions",
        sa.Column("id", sa.UUID(), server_default=UUID_DEFAULT, nullable=False),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("version_label", sa.Text(), nullable=True),
        sa.Column("source_identifier", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("coverage", sa.Text(), nullable=True),
        _ts("received_at"),
        _ts("created_at"),
        sa.CheckConstraint(
            "coverage IS NULL OR coverage IN ('complete', 'partial', 'none')",
            name=op.f("ck_paper_versions_coverage_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["library_items.id"], name=op.f("fk_paper_versions_item_id_library_items"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_paper_versions")),
        sa.UniqueConstraint("id", "item_id", name="uq_paper_versions_id_item_id"),
    )
    op.create_index("ix_paper_versions_item_id", "paper_versions", ["item_id"])

    # Circular reference: added after both tables exist. The composite key
    # guarantees the active version belongs to this item.
    op.create_foreign_key(
        "fk_library_items_active_version_paper_versions",
        "library_items",
        "paper_versions",
        ["active_version_id", "id"],
        ["id", "item_id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.UUID(), server_default=UUID_DEFAULT, nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("source_label", sa.Text(), nullable=True),
        sa.Column("scope_visibility", sa.Text(), nullable=False),
        sa.Column("scope_owner_user_id", sa.UUID(), nullable=True),
        sa.Column("item_id", sa.UUID(), nullable=True),
        sa.Column("version_id", sa.UUID(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="received", nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        _ts("cancel_requested_at", nullable=True, default=False),
        sa.Column("rights_epoch_at_start", sa.BigInteger(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("finished_at", nullable=True, default=False),
        sa.CheckConstraint(
            "(scope_visibility = 'private' AND scope_owner_user_id IS NOT NULL)"
            " OR (scope_visibility = 'common' AND scope_owner_user_id IS NULL)",
            name=op.f("ck_ingestion_jobs_scope_owner_matches_visibility"),
        ),
        sa.CheckConstraint(
            "origin <> 'upload' OR scope_visibility = 'private'", name=op.f("ck_ingestion_jobs_upload_is_private")
        ),
        sa.CheckConstraint(
            "origin IN ('discovery', 'upload', 'promotion')", name=op.f("ck_ingestion_jobs_origin_valid")
        ),
        sa.CheckConstraint(
            "scope_visibility IN ('common', 'private')", name=op.f("ck_ingestion_jobs_scope_visibility_valid")
        ),
        sa.CheckConstraint(
            "state IN ('received', 'validating', 'duplicate', 'stored', 'extracting', 'chunking', 'indexing',"
            " 'ready', 'fulltext_rejected', 'needs_review', 'retryable_failed', 'terminal_failed', 'cancelled')",
            name=op.f("ck_ingestion_jobs_state_valid"),
        ),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_ingestion_jobs_attempt_count_non_negative")),
        sa.CheckConstraint(
            "cancel_requested = (cancel_requested_at IS NOT NULL)",
            name=op.f("ck_ingestion_jobs_cancel_requested_at_consistent"),
        ),
        sa.CheckConstraint(
            "length(idempotency_key) > 0", name=op.f("ck_ingestion_jobs_idempotency_key_not_empty")
        ),
        sa.CheckConstraint(
            "version_id IS NULL OR item_id IS NOT NULL", name=op.f("ck_ingestion_jobs_version_requires_item")
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], name=op.f("fk_ingestion_jobs_actor_user_id_users"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["library_items.id"], name=op.f("fk_ingestion_jobs_item_id_library_items"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["scope_owner_user_id"],
            ["users.id"],
            name=op.f("fk_ingestion_jobs_scope_owner_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "item_id"],
            ["paper_versions.id", "paper_versions.item_id"],
            name="fk_ingestion_jobs_version_paper_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_jobs")),
        sa.UniqueConstraint(
            "scope_visibility",
            "scope_owner_user_id",
            "idempotency_key",
            name="uq_ingestion_jobs_scope_idempotency_key",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index("ix_ingestion_jobs_item_id", "ingestion_jobs", ["item_id"])
    op.create_index("ix_ingestion_jobs_state", "ingestion_jobs", ["state"])

    op.create_table(
        "source_files",
        sa.Column("id", sa.UUID(), server_default=UUID_DEFAULT, nullable=False),
        sa.Column("version_id", sa.UUID(), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("verified_mime", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("checksum_sha256", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=True),
        sa.Column("file_source", sa.Text(), nullable=True),
        _ts("created_at"),
        sa.CheckConstraint(
            "checksum_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_source_files_checksum_sha256_hex")
        ),
        sa.CheckConstraint(
            "verified_mime IN ('application/pdf', 'text/markdown')", name=op.f("ck_source_files_verified_mime_valid")
        ),
        sa.CheckConstraint("byte_size > 0", name=op.f("ck_source_files_byte_size_positive")),
        sa.CheckConstraint("length(storage_key) > 0", name=op.f("ck_source_files_storage_key_not_empty")),
        sa.CheckConstraint(
            "page_count IS NULL OR page_count > 0", name=op.f("ck_source_files_page_count_positive")
        ),
        sa.CheckConstraint(
            "version_id IS NOT NULL OR job_id IS NOT NULL", name=op.f("ck_source_files_has_owner_record")
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["ingestion_jobs.id"], name=op.f("fk_source_files_job_id_ingestion_jobs"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["paper_versions.id"],
            name=op.f("fk_source_files_version_id_paper_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_files")),
        sa.UniqueConstraint("id", "version_id", name="uq_source_files_id_version_id"),
    )
    op.create_index("ix_source_files_checksum_sha256", "source_files", ["checksum_sha256"])
    op.create_index("ix_source_files_job_id", "source_files", ["job_id"])
    op.create_index("ix_source_files_storage_key", "source_files", ["storage_key"])
    op.create_index("ix_source_files_version_id", "source_files", ["version_id"])

    _create_triggers()


def _create_triggers() -> None:
    # Visibility/owner never flip: promotion creates a separate common item.
    op.execute(
        """
        CREATE FUNCTION ras_library_items_scope_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.visibility IS DISTINCT FROM OLD.visibility
               OR NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id THEN
                RAISE EXCEPTION 'library item visibility/owner is immutable'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_library_items_scope_immutable
            BEFORE UPDATE ON library_items
            FOR EACH ROW EXECUTE FUNCTION ras_library_items_scope_immutable();
        """
    )

    # A job's scope must equal its item's scope, so job listings filtered by
    # scope cannot expose another owner's item.
    op.execute(
        """
        CREATE FUNCTION ras_ingestion_jobs_scope_matches_item() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            item_visibility text;
            item_owner uuid;
        BEGIN
            IF NEW.item_id IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT visibility, owner_user_id INTO item_visibility, item_owner
              FROM library_items WHERE id = NEW.item_id;
            IF item_visibility IS DISTINCT FROM NEW.scope_visibility
               OR item_owner IS DISTINCT FROM NEW.scope_owner_user_id THEN
                RAISE EXCEPTION 'ingestion job scope does not match its library item'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_ingestion_jobs_scope_matches_item
            BEFORE INSERT OR UPDATE OF item_id, scope_visibility, scope_owner_user_id ON ingestion_jobs
            FOR EACH ROW EXECUTE FUNCTION ras_ingestion_jobs_scope_matches_item();
        """
    )

    # Original files are immutable; links/page count may be filled once.
    op.execute(
        """
        CREATE FUNCTION ras_source_files_immutable() RETURNS trigger
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
        CREATE TRIGGER trg_source_files_immutable
            BEFORE UPDATE ON source_files
            FOR EACH ROW EXECUTE FUNCTION ras_source_files_immutable();
        """
    )

    # Audit log is append-only. A retention purge (M6) must be introduced by
    # an explicit, reviewed migration.
    op.execute(
        """
        CREATE FUNCTION ras_audit_events_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only'
                USING ERRCODE = 'insufficient_privilege';
        END $$;
        CREATE TRIGGER trg_audit_events_append_only
            BEFORE UPDATE OR DELETE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION ras_audit_events_append_only();
        CREATE TRIGGER trg_audit_events_no_truncate
            BEFORE TRUNCATE ON audit_events
            FOR EACH STATEMENT EXECUTE FUNCTION ras_audit_events_append_only();
        """
    )


def downgrade() -> None:
    op.drop_table("source_files")
    op.drop_table("ingestion_jobs")
    op.drop_constraint("fk_library_items_active_version_paper_versions", "library_items", type_="foreignkey")
    op.drop_table("paper_versions")
    op.drop_table("library_items")
    op.drop_table("bootstrap_state")
    op.drop_table("audit_events")
    op.drop_table("users")
    for fn in (
        "ras_audit_events_append_only",
        "ras_source_files_immutable",
        "ras_ingestion_jobs_scope_matches_item",
        "ras_library_items_scope_immutable",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {fn}()")
    # The vector extension is left installed: other databases objects may
    # depend on it and it holds no data.
