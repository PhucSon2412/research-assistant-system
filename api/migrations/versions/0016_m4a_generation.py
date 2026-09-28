"""Generation accounting and per-user RAG request limits."""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_search_usage_task_type_valid", "search_usage", type_="check")
    op.create_check_constraint("ck_search_usage_task_type_valid", "search_usage",
                               "task_type IN ('embed_query', 'rerank', 'generation')")
    op.add_column("search_usage", sa.Column("output_tokens", sa.Integer(), nullable=True))
    op.create_table("rag_request_days",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_table("rag_request_days")
    op.drop_column("search_usage", "output_tokens")
    op.execute("DELETE FROM search_usage WHERE task_type='generation'")
    op.drop_constraint("ck_search_usage_task_type_valid", "search_usage", type_="check")
    op.create_check_constraint("ck_search_usage_task_type_valid", "search_usage",
                               "task_type IN ('embed_query', 'rerank')")
