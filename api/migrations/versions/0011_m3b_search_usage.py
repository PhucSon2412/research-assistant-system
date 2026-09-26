"""M3b durable search rate limit and query/rerank reservations."""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("search_request_days",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"))
    op.create_table("search_usage",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("task_type", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("search_units", sa.Integer()),
        sa.Column("reserved_usd", sa.Numeric(14, 8), nullable=False),
        sa.Column("cost_usd", sa.Numeric(14, 8)),
        sa.Column("cost_status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("task_type IN ('embed_query', 'rerank')", name="ck_search_usage_task_type_valid"),
        sa.CheckConstraint("status IN ('reserved', 'completed', 'uncertain', 'rejected')", name="ck_search_usage_status_valid"),
        sa.CheckConstraint("reserved_usd >= 0 AND (cost_usd IS NULL OR cost_usd >= 0)", name="ck_search_usage_cost_non_negative"))
    op.create_index("ix_search_usage_day_user", "search_usage", ["day", "user_id"])


def downgrade() -> None:
    op.drop_index("ix_search_usage_day_user", table_name="search_usage")
    op.drop_table("search_usage")
    op.drop_table("search_request_days")
