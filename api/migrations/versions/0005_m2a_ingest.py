"""M2a durable claims and Markdown derivatives.

No existing received job is changed; the poller discovers it after upgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name, typ in (
        ("lease_token", sa.UUID()), ("lease_until", sa.DateTime(timezone=True)),
        ("next_attempt_at", sa.DateTime(timezone=True)), ("started_at", sa.DateTime(timezone=True)),
        ("resume_state", sa.Text()),
    ):
        op.add_column("ingestion_jobs", sa.Column(name, typ, nullable=True))
    op.drop_constraint(op.f("ck_ingestion_jobs_state_valid"), "ingestion_jobs", type_="check")
    op.create_check_constraint(op.f("ck_ingestion_jobs_state_valid"), "ingestion_jobs",
        "state IN ('received', 'validating', 'duplicate', 'stored', 'extracting', 'chunking',"
        " 'awaiting_index', 'indexing', 'ready', 'fulltext_rejected', 'needs_review',"
        " 'retryable_failed', 'terminal_failed', 'cancelled')")

    op.create_table("ingest_step_attempts",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("ingestion_jobs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("step", sa.Text(), nullable=False), sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False), sa.Column("error_code", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("step IN ('validate', 'extract', 'chunk')", name="step_valid"),
        sa.CheckConstraint("outcome IN ('running', 'success', 'retry', 'failed', 'cancelled')", name="outcome_valid"),
        sa.CheckConstraint("attempt_no > 0", name="attempt_no_positive"),
        sa.UniqueConstraint("job_id", "step", "attempt_no", name="uq_ingest_step_attempt"),
    )
    op.create_index("ix_ingest_step_attempts_job_id", "ingest_step_attempts", ["job_id"])

    def identity_columns() -> list[sa.Column]:
        return [sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
                sa.Column("item_id", sa.UUID(), nullable=False), sa.Column("version_id", sa.UUID(), nullable=False),
                sa.Column("source_file_id", sa.UUID(), nullable=False)]

    def identity_fks(prefix: str) -> list[sa.ForeignKeyConstraint]:
        return [
            sa.ForeignKeyConstraint(["version_id", "item_id"], ["paper_versions.id", "paper_versions.item_id"],
                                    name=f"fk_{prefix}_version_item", ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["source_file_id", "version_id"], ["source_files.id", "source_files.version_id"],
                                    name=f"fk_{prefix}_file_version", ondelete="RESTRICT"),
        ]

    op.create_table("metadata_candidates", *identity_columns(),
        sa.Column("field_name", sa.Text(), nullable=False), sa.Column("value", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False), sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("pipeline_version", sa.Text(), nullable=False), *identity_fks("metadata_candidate"),
        sa.CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="offsets_valid"),
        sa.UniqueConstraint("source_file_id", "field_name", "pipeline_version", name="uq_metadata_candidate_field"))

    op.create_table("sections", *identity_columns(),
        sa.Column("kind", sa.Text(), nullable=False), sa.Column("heading", sa.Text()),
        sa.Column("ordinal", sa.Integer(), nullable=False), sa.Column("text", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False), sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False), sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("pipeline_version", sa.Text(), nullable=False), *identity_fks("section"),
        sa.CheckConstraint("kind IN ('body', 'abstract', 'references')", name="kind_valid"),
        sa.CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        sa.CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="offsets_valid"),
        sa.CheckConstraint("start_line > 0 AND end_line >= start_line", name="lines_valid"),
        sa.UniqueConstraint("source_file_id", "pipeline_version", "ordinal", name="uq_section_ordinal"),
        sa.UniqueConstraint("id", "item_id", "version_id", "source_file_id", name="uq_section_identity"))
    op.create_index("ix_sections_item_id", "sections", ["item_id"])

    op.create_table("chunks", *identity_columns(),
        sa.Column("section_id", sa.UUID(), nullable=False), sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False), sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False), sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False), sa.Column("chunk_hash", sa.Text(), nullable=False),
        sa.Column("pipeline_version", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["section_id", "item_id", "version_id", "source_file_id"],
                                ["sections.id", "sections.item_id", "sections.version_id", "sections.source_file_id"],
                                name="fk_chunk_section_identity", ondelete="RESTRICT"),
        sa.CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        sa.CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="offsets_valid"),
        sa.CheckConstraint("start_line > 0 AND end_line >= start_line", name="lines_valid"),
        sa.CheckConstraint("chunk_hash ~ '^[0-9a-f]{64}$'", name="hash_hex"),
        sa.UniqueConstraint("section_id", "pipeline_version", "ordinal", name="uq_chunk_ordinal"))
    op.create_index("ix_chunks_item_id", "chunks", ["item_id"])


def downgrade() -> None:
    op.drop_table("chunks")
    op.drop_table("sections")
    op.drop_table("metadata_candidates")
    op.drop_table("ingest_step_attempts")
    op.drop_constraint(op.f("ck_ingestion_jobs_state_valid"), "ingestion_jobs", type_="check")
    op.create_check_constraint(op.f("ck_ingestion_jobs_state_valid"), "ingestion_jobs",
        "state IN ('received', 'validating', 'duplicate', 'stored', 'extracting', 'chunking',"
        " 'indexing', 'ready', 'fulltext_rejected', 'needs_review',"
        " 'retryable_failed', 'terminal_failed', 'cancelled')")
    for name in ("resume_state", "started_at", "next_attempt_at", "lease_until", "lease_token"):
        op.drop_column("ingestion_jobs", name)
