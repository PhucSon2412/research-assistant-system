"""Use composite FKs to close concurrent source-file link races."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint("uq_ingestion_jobs_id_item_id", "ingestion_jobs", ["id", "item_id"])
    op.add_column("source_files", sa.Column("item_id", sa.UUID(), nullable=True))
    # Existing M1c rows (including those created on a dev DB after 0003)
    # must be preserved. If both links disagree, fail the migration instead
    # of silently choosing one.
    op.execute("""
        UPDATE source_files AS f SET item_id = COALESCE(v.item_id, j.item_id)
        FROM paper_versions AS v, ingestion_jobs AS j
        WHERE f.version_id = v.id AND f.job_id = j.id
          AND v.item_id = j.item_id
    """)
    op.execute("""
        UPDATE source_files AS f SET item_id = v.item_id
        FROM paper_versions AS v WHERE f.version_id = v.id AND f.item_id IS NULL AND f.job_id IS NULL
    """)
    op.execute("""
        UPDATE source_files AS f SET item_id = j.item_id
        FROM ingestion_jobs AS j WHERE f.job_id = j.id AND f.item_id IS NULL AND f.version_id IS NULL
    """)
    op.create_check_constraint("ck_source_files_both_links_require_item", "source_files",
                               "version_id IS NULL OR job_id IS NULL OR item_id IS NOT NULL")
    op.create_foreign_key("fk_source_files_version_item_paper_versions", "source_files", "paper_versions",
                          ["version_id", "item_id"], ["id", "item_id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_source_files_job_item_ingestion_jobs", "source_files", "ingestion_jobs",
                          ["job_id", "item_id"], ["id", "item_id"], ondelete="RESTRICT")


def downgrade() -> None:
    op.drop_constraint("fk_source_files_job_item_ingestion_jobs", "source_files", type_="foreignkey")
    op.drop_constraint("fk_source_files_version_item_paper_versions", "source_files", type_="foreignkey")
    op.drop_constraint("ck_source_files_both_links_require_item", "source_files", type_="check")
    op.drop_column("source_files", "item_id")
    op.drop_constraint("uq_ingestion_jobs_id_item_id", "ingestion_jobs", type_="unique")
