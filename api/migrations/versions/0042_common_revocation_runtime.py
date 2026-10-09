"""C2d-5c2: transaction-bound common revocation mirror/confirm."""
from pathlib import Path
from alembic import op

revision = "0042"
down_revision = "0041"
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
    DROP FUNCTION ras_auth_common_revocation_confirm(text);
    DROP FUNCTION ras_auth_common_revocation_mirror(text);
    DROP TRIGGER trg_library_items_z_revocation_transaction ON library_items;
    DROP FUNCTION ras_common_revocation_transaction();
    ALTER TABLE library_items DROP COLUMN revocation_transaction_id,
      DROP COLUMN revocation_prior_state, DROP COLUMN revocation_prior_epoch;
    CREATE OR REPLACE FUNCTION ras_auth_confirm(p_seq bigint) RETURNS void
    LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
      UPDATE revocation_journal_records SET journal_confirmed=true WHERE seq=p_seq AND NOT journal_confirmed;
    $$;""")
