"""C2a runtime session writers and shared receipt/head predicates; pending remains."""
from pathlib import Path
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM recovery_registrations) OR EXISTS (SELECT 1 FROM users)
           OR EXISTS (SELECT 1 FROM revocation_journal_records) OR EXISTS (SELECT 1 FROM auth_change_receipts)
           OR EXISTS (SELECT 1 FROM auth_safety_heads)
           OR EXISTS (SELECT 1 FROM auth_safety_state WHERE projection_seq > 0 OR backfill_complete) THEN
            RAISE EXCEPTION 'auth safety history prevents downgrade'; END IF;
    END $$;""")
    op.execute(Path(__file__).with_name("0035_login_session_predicates_downgrade.sql").read_text(encoding="utf-8"))
