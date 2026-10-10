"""C2d-5e1: durable object deletion fences and the SourceFile INSERT fence.

Revoked common cleanup prepares a permanent per-key fence before any S3
delete; every source_files INSERT serialises on the same key lock and is
refused for a fenced key. See the SQL file for the lock graph.
"""
from pathlib import Path
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    # A used fence is the only record that a key was authorised for deletion;
    # dropping it would let a new SourceFile name a deleted object silently.
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM object_delete_fences) THEN
       RAISE EXCEPTION 'object deletion fences exist; downgrade would lose them'; END IF;
    END $$;
    DROP FUNCTION ras_auth_cleanup_fence(uuid,uuid,uuid,text,bigint);
    DROP TRIGGER trg_source_files_delete_fence ON source_files;
    DROP FUNCTION ras_source_files_delete_fence();
    DROP TRIGGER trg_object_delete_fences_no_truncate ON object_delete_fences;
    DROP TRIGGER trg_object_delete_fences_guard ON object_delete_fences;
    DROP FUNCTION ras_object_delete_fences_guard();
    DROP TABLE object_delete_fences;""")
