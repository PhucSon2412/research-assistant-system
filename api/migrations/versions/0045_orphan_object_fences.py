"""C2d-5e2: durable orphan original fences, completions and the listing cursor.

Manual orphan reconcile prepares a permanent per-key fence when no
source_files row names the key; the 0044 SourceFile INSERT trigger now also
refuses orphan-fenced keys under the same key lock. See the SQL file.
"""
from pathlib import Path
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

# The 0044 trigger body, restored on downgrade.
FENCE_0044 = """CREATE OR REPLACE FUNCTION ras_source_files_delete_fence() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF current_setting('transaction_isolation') IS DISTINCT FROM 'read committed' THEN
  RAISE EXCEPTION 'source file insert requires read committed' USING ERRCODE='check_violation'; END IF;
 PERFORM pg_advisory_xact_lock(1918989163, hashtext(NEW.storage_key));
 IF EXISTS (SELECT 1 FROM object_delete_fences WHERE storage_key = NEW.storage_key) THEN
  RAISE EXCEPTION 'object key is fenced for deletion' USING ERRCODE='check_violation'; END IF;
 RETURN NEW;
END $$;"""


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    # A used orphan fence is the only record that a key was authorised for
    # deletion; dropping it would let a new SourceFile name a deleted object.
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM orphan_object_fences) OR EXISTS (SELECT 1 FROM orphan_object_completions) THEN
       RAISE EXCEPTION 'orphan object fences exist; downgrade would lose them'; END IF;
    END $$;""")
    op.execute(FENCE_0044)
    op.execute("""DROP FUNCTION ras_auth_orphan_complete(uuid,text,uuid,text,timestamptz);
    DROP FUNCTION ras_auth_orphan_fence(uuid,text,bigint,timestamptz,timestamptz,timestamptz);
    DROP TABLE orphan_reconcile_cursors;
    DROP TRIGGER trg_orphan_object_completions_no_truncate ON orphan_object_completions;
    DROP TRIGGER trg_orphan_object_completions_guard ON orphan_object_completions;
    DROP TRIGGER trg_orphan_object_fences_no_truncate ON orphan_object_fences;
    DROP TRIGGER trg_orphan_object_fences_guard ON orphan_object_fences;
    DROP TABLE orphan_object_completions;
    DROP TABLE orphan_object_fences;
    DROP FUNCTION ras_orphan_fences_guard();""")
