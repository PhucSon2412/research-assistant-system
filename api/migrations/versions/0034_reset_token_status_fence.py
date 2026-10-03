"""Require latest active status receipt when issuing reset tokens; keep C1 pending."""
from pathlib import Path

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade():
    # Replace in place to retain the deployed function's OID, owner and ACL.
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    # Removing a safety guard is permitted only before any installation/history.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM recovery_registrations) OR EXISTS (SELECT 1 FROM revocation_journal_records)
           OR EXISTS (SELECT 1 FROM users) OR EXISTS (SELECT 1 FROM auth_change_receipts)
           OR EXISTS (SELECT 1 FROM auth_safety_heads)
           OR EXISTS (SELECT 1 FROM auth_safety_state WHERE projection_seq > 0 OR backfill_complete) THEN
            RAISE EXCEPTION 'auth safety history prevents downgrade';
        END IF;
    END $$;""")
    op.execute(Path(__file__).with_name("0034_reset_token_status_fence_downgrade.sql").read_text(encoding="utf-8"))
