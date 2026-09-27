"""Personal collections and item membership; neither grants content access."""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("collections",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 1 AND 200", name="ck_collections_name_length"))
    op.create_index("ix_collections_owner_user_id", "collections", ["owner_user_id"])
    op.create_table("collection_items",
        sa.Column("collection_id", sa.UUID(), sa.ForeignKey("collections.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("item_id", sa.UUID(), sa.ForeignKey("library_items.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_collection_items_item_id", "collection_items", ["item_id"])


def downgrade() -> None:
    op.drop_index("ix_collection_items_item_id", table_name="collection_items")
    op.drop_table("collection_items")
    op.drop_index("ix_collections_owner_user_id", table_name="collections")
    op.drop_table("collections")
