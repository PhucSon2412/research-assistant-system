"""Per-owner embedding ledger, backfilled from durable calls."""

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("embedding_calls", sa.Column("owner_user_id", sa.UUID()))
    op.execute("""UPDATE embedding_calls AS c SET owner_user_id = i.owner_user_id
        FROM source_files AS f JOIN library_items AS i ON i.id = f.item_id
        WHERE c.source_file_id = f.id""")
    op.alter_column("embedding_calls", "owner_user_id", nullable=False)
    op.create_foreign_key("fk_embedding_calls_owner_user_id", "embedding_calls", "users",
                          ["owner_user_id"], ["id"], ondelete="RESTRICT")
    op.create_table("embedding_user_budget_days",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("committed_usd", sa.Numeric(14, 8), nullable=False, server_default="0"),
        sa.Column("reserved_usd", sa.Numeric(14, 8), nullable=False, server_default="0"))
    op.execute("""INSERT INTO embedding_user_budget_days(day, user_id, committed_usd, reserved_usd)
        SELECT reserved_day, owner_user_id,
          COALESCE(SUM(CASE WHEN status='completed' THEN COALESCE(cost_usd, reserved_usd) ELSE 0 END), 0),
          COALESCE(SUM(CASE WHEN status IN ('reserved', 'uncertain') THEN reserved_usd ELSE 0 END), 0)
        FROM embedding_calls GROUP BY reserved_day, owner_user_id""")


def downgrade() -> None:
    op.drop_table("embedding_user_budget_days")
    op.drop_constraint("fk_embedding_calls_owner_user_id", "embedding_calls", type_="foreignkey")
    op.drop_column("embedding_calls", "owner_user_id")
