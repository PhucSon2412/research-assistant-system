"""C2c-2: invitation runtime boundary and atomic initial acceptance pair."""
from pathlib import Path
from alembic import op
import sqlalchemy as sa

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade():
    # NULL preserves historical rows without upgrading their evidence.
    op.add_column("invitations", sa.Column("creation_transaction_id", sa.BigInteger()))
    op.add_column("invitations", sa.Column("acceptance_transaction_id", sa.BigInteger()))
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM users) OR EXISTS (SELECT 1 FROM invitations)
       OR EXISTS (SELECT 1 FROM recovery_registrations) OR EXISTS (SELECT 1 FROM revocation_journal_records)
       OR EXISTS (SELECT 1 FROM auth_change_receipts) OR EXISTS (SELECT 1 FROM auth_safety_heads)
       OR EXISTS (SELECT 1 FROM auth_safety_state WHERE projection_seq>0 OR backfill_complete) THEN
       RAISE EXCEPTION 'auth safety history prevents downgrade'; END IF;
    END $$;""")
    op.execute("DROP TRIGGER trg_auth_invitation_pair ON invitations")
    op.execute("DROP TRIGGER trg_auth_invitation_marker ON invitations")
    for fn in ("ras_auth_invitation_pair()", "ras_auth_invitation_marker()",
               "ras_auth_invitation_lock(uuid[])",
               "ras_auth_invitation_create(uuid,text,text,text,uuid,uuid,text,timestamptz,timestamptz,interval)",
               "ras_auth_invitation_revoke(uuid,uuid,uuid,text,timestamptz,interval,text)",
               "ras_auth_invitation_compensate(uuid,text,bigint,uuid,timestamptz)",
               "ras_auth_invitation_prepare(uuid,text,text,uuid,text,text,integer,timestamptz)",
               "ras_auth_invitation_recover(uuid,text,text,timestamptz)",
               "ras_auth_invitation_before_journal()",
               "ras_auth_invitation_actor(uuid,uuid,text,timestamptz,interval)"):
        op.execute(f"DROP FUNCTION {fn}")
    op.drop_column("invitations", "acceptance_transaction_id")
    op.drop_column("invitations", "creation_transaction_id")
