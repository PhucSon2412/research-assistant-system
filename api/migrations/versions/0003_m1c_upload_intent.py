"""M1c: per-upload acknowledgement/intent and source-file item integrity."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "processing_acknowledgements",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("notice_version", sa.Text(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(notice_version) > 0", name=op.f("ck_processing_acknowledgements_notice_version_not_empty")),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT", name=op.f("fk_processing_acknowledgements_actor_user_id_users")),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="RESTRICT", name=op.f("fk_processing_acknowledgements_session_id_sessions")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_acknowledgements")),
    )
    op.create_table(
        "upload_intents",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("acknowledgement_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("notice_version", sa.Text(), nullable=False),
        sa.Column("file_kind", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("declared_size", sa.BigInteger(), nullable=False),
        sa.Column("declared_sha256", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("item_id", sa.UUID(), nullable=True),
        sa.CheckConstraint("declared_size > 0", name=op.f("ck_upload_intents_declared_size_positive")),
        sa.CheckConstraint("declared_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_upload_intents_declared_sha256_hex")),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_upload_intents_token_hash_hex")),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_upload_intents_expires_after_created")),
        sa.ForeignKeyConstraint(["acknowledgement_id"], ["processing_acknowledgements.id"], ondelete="RESTRICT", name=op.f("fk_upload_intents_acknowledgement_id_processing_acknowledgements")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT", name=op.f("fk_upload_intents_user_id_users")),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="RESTRICT", name=op.f("fk_upload_intents_session_id_sessions")),
        sa.ForeignKeyConstraint(["item_id"], ["library_items.id"], ondelete="RESTRICT", name=op.f("fk_upload_intents_item_id_library_items")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_upload_intents")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_upload_intents_token_hash")),
        sa.UniqueConstraint("acknowledgement_id", name=op.f("uq_upload_intents_acknowledgement_id")),
        sa.UniqueConstraint("item_id", name=op.f("uq_upload_intents_item_id")),
    )
    op.create_index("ix_upload_intents_user_id", "upload_intents", ["user_id"])
    # A SourceFile may reference either record alone during ingest. Once both
    # links are present, they must resolve to the same item, including scope.
    op.execute("""
        CREATE FUNCTION ras_source_file_links_same_item() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE job_item uuid; version_item uuid;
        BEGIN
            IF NEW.job_id IS NOT NULL AND NEW.version_id IS NOT NULL THEN
                SELECT item_id INTO job_item FROM ingestion_jobs WHERE id = NEW.job_id;
                SELECT item_id INTO version_item FROM paper_versions WHERE id = NEW.version_id;
                IF job_item IS NULL OR version_item IS NULL OR job_item IS DISTINCT FROM version_item THEN
                    RAISE EXCEPTION 'source file job and version must belong to the same item'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_source_file_links_same_item
            BEFORE INSERT OR UPDATE OF job_id, version_id ON source_files
            FOR EACH ROW EXECUTE FUNCTION ras_source_file_links_same_item();
    """)
    # Prevent changing an already-linked job's item underneath the file.
    op.execute("""
        CREATE FUNCTION ras_job_item_file_link_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.item_id IS DISTINCT FROM OLD.item_id AND EXISTS (
                SELECT 1 FROM source_files WHERE job_id = OLD.id AND version_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'job item with source file is immutable' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_job_item_file_link_immutable
            BEFORE UPDATE OF item_id ON ingestion_jobs
            FOR EACH ROW EXECUTE FUNCTION ras_job_item_file_link_immutable();
    """)
    op.execute("""
        CREATE FUNCTION ras_version_item_file_link_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.item_id IS DISTINCT FROM OLD.item_id AND EXISTS (
                SELECT 1 FROM source_files WHERE version_id = OLD.id AND job_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'version item with source file is immutable' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_version_item_file_link_immutable
            BEFORE UPDATE OF item_id ON paper_versions
            FOR EACH ROW EXECUTE FUNCTION ras_version_item_file_link_immutable();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_version_item_file_link_immutable ON paper_versions")
    op.execute("DROP FUNCTION ras_version_item_file_link_immutable()")
    op.execute("DROP TRIGGER trg_job_item_file_link_immutable ON ingestion_jobs")
    op.execute("DROP FUNCTION ras_job_item_file_link_immutable()")
    op.execute("DROP TRIGGER trg_source_file_links_same_item ON source_files")
    op.execute("DROP FUNCTION ras_source_file_links_same_item()")
    op.drop_table("upload_intents")
    op.drop_table("processing_acknowledgements")
