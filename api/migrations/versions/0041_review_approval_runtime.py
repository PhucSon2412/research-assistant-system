"""C2d-5b: approval-only review closure mirror/confirm for runtime."""
from pathlib import Path
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM users) OR EXISTS (SELECT 1 FROM recovery_registrations)
       OR EXISTS (SELECT 1 FROM revocation_journal_records) THEN
       RAISE EXCEPTION 'auth safety history prevents downgrade'; END IF;
    END $$;
    DROP FUNCTION ras_auth_review_approval_confirm(text);
    DROP FUNCTION ras_auth_review_approval_mirror(text);""")
