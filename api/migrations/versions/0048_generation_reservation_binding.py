"""Allow nonce-bound generation without adopting historical all-NULL rows."""
from alembic import op

revision = '0048'
down_revision = '0047'
branch_labels = None
depends_on = None


def binding(tasks):
    return ('(reservation_token IS NULL AND operation_id IS NULL AND session_id IS NULL) OR '
        '(reservation_token IS NOT NULL AND operation_id IS NOT NULL AND session_id IS NOT NULL '
        f'AND task_type IN ({tasks}))')


def upgrade():
    op.drop_constraint('ck_search_usage_reservation_binding', 'search_usage', type_='check')
    op.create_check_constraint('ck_search_usage_reservation_binding', 'search_usage',
        binding("'embed_query','rerank','generation'"))


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM search_usage WHERE task_type='generation' AND reservation_token IS NOT NULL) THEN
            RAISE EXCEPTION 'generation reservation bindings exist';
        END IF;
    END $$""")
    op.drop_constraint('ck_search_usage_reservation_binding', 'search_usage', type_='check')
    op.create_check_constraint('ck_search_usage_reservation_binding', 'search_usage',
        binding("'embed_query','rerank'"))
