"""Exact arXiv links from private reference entries to discovery metadata.

Existing references get rows from the bounded reconcile task, not here: the
matcher is versioned Python code and must not be frozen into a migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

EXACT = "'linked', 'no_discovery_record', 'discovery_record_deleted'"
UNLINKED = "'no_arxiv_id', 'bare_identifier', 'invalid_arxiv_id', 'multiple_arxiv_ids', 'conflicting_versions'"


def upgrade() -> None:
    op.create_table("reference_citation_links",
        sa.Column("reference_id", sa.UUID(), sa.ForeignKey("reference_entries.id", ondelete="RESTRICT"),
                  primary_key=True),
        sa.Column("matcher_version", sa.Text(), nullable=False),
        sa.Column("raw_citation_sha256", sa.Text(), nullable=False),
        sa.Column("match_status", sa.Text(), nullable=False),
        sa.Column("match_method", sa.Text()),
        sa.Column("id_form", sa.Text()),
        sa.Column("arxiv_id", sa.Text()),
        sa.Column("cited_version", sa.Text()),
        sa.Column("id_start_offset", sa.Integer()),
        sa.Column("id_end_offset", sa.Integer()),
        sa.Column("discovery_paper_id", sa.UUID(), sa.ForeignKey("discovery_papers.id", ondelete="RESTRICT")),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(f"match_status IN ({EXACT}, {UNLINKED})",
                           name="ck_reference_citation_links_status_valid"),
        sa.CheckConstraint(f"(match_status IN ({EXACT})) = "
                           "(arxiv_id IS NOT NULL AND match_method IS NOT NULL AND id_form IS NOT NULL"
                           " AND id_start_offset IS NOT NULL AND id_end_offset IS NOT NULL)",
                           name="ck_reference_citation_links_exact_fields"),
        sa.CheckConstraint("(match_status = 'linked') = (discovery_paper_id IS NOT NULL)",
                           name="ck_reference_citation_links_target_iff_linked"),
        sa.CheckConstraint("match_method IS NULL OR match_method = 'arxiv_id_exact'",
                           name="ck_reference_citation_links_method_valid"),
        sa.CheckConstraint("id_form IS NULL OR id_form IN ('arxiv_prefix', 'arxiv_url', 'arxiv_doi', 'abs_path')",
                           name="ck_reference_citation_links_id_form_valid"),
        sa.CheckConstraint("cited_version IS NULL OR (arxiv_id IS NOT NULL AND cited_version ~ '^v[1-9][0-9]*$')",
                           name="ck_reference_citation_links_cited_version_valid"),
        sa.CheckConstraint("id_start_offset IS NULL OR (id_start_offset >= 0 AND id_end_offset > id_start_offset)",
                           name="ck_reference_citation_links_id_span_valid"),
        sa.CheckConstraint("raw_citation_sha256 ~ '^[0-9a-f]{64}$'",
                           name="ck_reference_citation_links_sha256_valid"))
    op.create_index("ix_reference_citation_links_discovery_paper_id", "reference_citation_links",
                    ["discovery_paper_id"])
    op.create_index("ix_reference_citation_links_arxiv_id", "reference_citation_links", ["arxiv_id"])


def downgrade() -> None:
    op.drop_index("ix_reference_citation_links_arxiv_id", table_name="reference_citation_links")
    op.drop_index("ix_reference_citation_links_discovery_paper_id", table_name="reference_citation_links")
    op.drop_table("reference_citation_links")
