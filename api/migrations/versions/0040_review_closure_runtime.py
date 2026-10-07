"""C2d-5a: transaction-bound review closure mirror for runtime."""
from pathlib import Path
from alembic import op
import sqlalchemy as sa

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("review_requests", sa.Column("closure_transaction_id", sa.BigInteger()))
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM users) OR EXISTS (SELECT 1 FROM recovery_registrations)
       OR EXISTS (SELECT 1 FROM revocation_journal_records) THEN
       RAISE EXCEPTION 'auth safety history prevents downgrade'; END IF;
    END $$;
    DROP FUNCTION ras_auth_review_closure_confirm(text);
    DROP FUNCTION ras_auth_review_closure_mirror(text);
    DROP TRIGGER trg_review_closure_transaction ON review_requests;
    DROP FUNCTION ras_review_closure_transaction();""")
    op.drop_column("review_requests", "closure_transaction_id")
