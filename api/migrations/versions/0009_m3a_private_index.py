"""Durable private index generations and embedding reservations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("index_generations",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("item_id", sa.UUID(), nullable=False), sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("source_file_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("ingestion_jobs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False), sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer()), sa.Column("pipeline_version", sa.Text(), nullable=False),
        sa.Column("index_version", sa.Text(), nullable=False), sa.Column("expected_chunks", sa.Integer(), nullable=False),
        sa.Column("rights_epoch", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="building"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["version_id", "item_id"], ["paper_versions.id", "paper_versions.item_id"], name="fk_index_generation_version_item", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_file_id", "version_id"], ["source_files.id", "source_files.version_id"], name="fk_index_generation_file_version", ondelete="RESTRICT"),
        sa.CheckConstraint("state IN ('building', 'active', 'superseded', 'abandoned')", name=op.f("ck_index_generations_state_valid")),
        sa.CheckConstraint("expected_chunks > 0 AND rights_epoch >= 0 AND (dimensions IS NULL OR dimensions > 0)", name=op.f("ck_index_generations_counts_valid")),
        sa.UniqueConstraint("job_id", "model_id", "index_version", name="uq_index_generation_job_model_version"),
        sa.UniqueConstraint("id", "item_id", name="uq_index_generation_id_item"))
    op.create_index("ix_index_generations_item_id", "index_generations", ["item_id"])
    op.add_column("library_items", sa.Column("active_index_generation_id", sa.UUID()))
    op.create_foreign_key("fk_library_items_active_index_generation", "library_items", "index_generations",
                          ["active_index_generation_id", "id"], ["id", "item_id"], ondelete="RESTRICT")
    op.create_table("embedding_budget_days",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("committed_usd", sa.Numeric(14, 8), nullable=False, server_default="0"),
        sa.Column("reserved_usd", sa.Numeric(14, 8), nullable=False, server_default="0"))
    op.create_table("embedding_calls",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_file_id", sa.UUID(), sa.ForeignKey("source_files.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False), sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("pipeline_version", sa.Text(), nullable=False), sa.Column("chunk_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False), sa.Column("vector", Vector()),
        sa.Column("dimensions", sa.Integer()), sa.Column("input_tokens", sa.Integer()),
        sa.Column("reserved_usd", sa.Numeric(14, 8), nullable=False), sa.Column("cost_usd", sa.Numeric(14, 8)),
        sa.Column("cost_status", sa.Text(), nullable=False), sa.Column("reserved_day", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('reserved', 'completed', 'uncertain', 'rejected')", name=op.f("ck_embedding_calls_status_valid")),
        sa.CheckConstraint("chunk_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_embedding_calls_hash_hex")),
        sa.CheckConstraint("reserved_usd >= 0 AND (cost_usd IS NULL OR cost_usd >= 0)", name=op.f("ck_embedding_calls_cost_non_negative")),
        sa.CheckConstraint("dimensions IS NULL OR dimensions > 0", name=op.f("ck_embedding_calls_dimensions_positive")),
        sa.CheckConstraint("vector IS NULL OR (dimensions IS NOT NULL AND vector_dims(vector) = dimensions)", name=op.f("ck_embedding_calls_vector_dimensions_match")),
        sa.UniqueConstraint("source_file_id", "model_id", "pipeline_version", "chunk_hash", name="uq_embedding_call_content"))
    op.create_index("ix_embedding_calls_status", "embedding_calls", ["status"])
    op.create_table("index_entries",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("generation_id", sa.UUID(), sa.ForeignKey("index_generations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("chunk_id", sa.UUID(), sa.ForeignKey("chunks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("embedding_call_id", sa.UUID(), sa.ForeignKey("embedding_calls.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("chunk_hash", sa.Text(), nullable=False), sa.Column("text", sa.Text(), nullable=False),
        sa.Column("keywords", postgresql.TSVECTOR(), sa.Computed("to_tsvector('simple'::regconfig, text)", persisted=True)),
        sa.CheckConstraint("chunk_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_index_entries_hash_hex")),
        sa.UniqueConstraint("generation_id", "chunk_id", name="uq_index_entry_chunk"))
    op.create_index("ix_index_entries_keywords", "index_entries", ["keywords"], postgresql_using="gin")
    op.create_index("ix_index_entries_generation_id", "index_entries", ["generation_id"])
    op.drop_constraint(op.f("ck_ingest_step_attempts_step_valid"), "ingest_step_attempts", type_="check")
    op.create_check_constraint(op.f("ck_ingest_step_attempts_step_valid"), "ingest_step_attempts",
                               "step IN ('validate', 'extract', 'chunk', 'index')")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_ingest_step_attempts_step_valid"), "ingest_step_attempts", type_="check")
    op.create_check_constraint(op.f("ck_ingest_step_attempts_step_valid"), "ingest_step_attempts",
                               "step IN ('validate', 'extract', 'chunk')")
    op.drop_index("ix_index_entries_generation_id", table_name="index_entries")
    op.drop_index("ix_index_entries_keywords", table_name="index_entries")
    op.drop_table("index_entries")
    op.drop_index("ix_embedding_calls_status", table_name="embedding_calls")
    op.drop_table("embedding_calls")
    op.drop_table("embedding_budget_days")
    op.drop_constraint("fk_library_items_active_index_generation", "library_items", type_="foreignkey")
    op.drop_column("library_items", "active_index_generation_id")
    op.drop_index("ix_index_generations_item_id", table_name="index_generations")
    op.drop_table("index_generations")
