"""Bind initial receipts in both directions; reject unproven historical evidence."""
from pathlib import Path

from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def _definition(filename, name, next_name):
    source = Path(__file__).with_name(filename).read_text(encoding="utf-8")
    start = source.index(f"CREATE FUNCTION {name}(")
    end = source.index(f"CREATE FUNCTION {next_name}(", start)
    return source[start:end].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))
    # Keep the predecessor's exact binding/TTL/authority checks. Only add the
    # shared provenance predicate; CREATE OR REPLACE preserves OID/owner/ACL.
    account = _definition("0035_login_session_predicates.sql", "ras_auth_account_live", "ras_auth_session_live")
    account = account.replace("AND h.status_decision='active'", "AND h.status_decision='active'\n"
        "   AND ras_auth_receipt_provenance(c.operation_id) AND ras_auth_receipt_provenance(s.operation_id)")
    op.execute(account)
    status = _definition("0036_reset_status_writers.sql", "ras_auth_status_live", "ras_auth_reset_eligible")
    status = status.replace("AND j.journal_confirmed", "AND ras_auth_receipt_provenance(r.operation_id) AND j.journal_confirmed")
    op.execute(status)
    inspect = _definition("0049_operator_auth_recovery.sql", "ras_auth_operator_inspect", "ras_auth_operator_case")
    inspect = inspect.replace("WHERE c.operation_id=u.credential_operation_id", "WHERE c.operation_id=u.credential_operation_id AND ras_auth_receipt_provenance(c.operation_id)")
    start = inspect.index("  COALESCE(EXISTS (SELECT 1 FROM auth_change_receipts r JOIN")
    end = inspect.index("  (SELECT count(*) FROM sessions", start)
    inspect = inspect[:start] + "  ras_auth_initial_status_verified(u.id),\n" + inspect[end:]
    op.execute(inspect)
    # Existing CAS rehash (0035) already calls ras_auth_account_live.
    # Newly added read routines inherit existing runtime/recovery ACLs by
    # capability, not role names. A provisioned NOLOGIN definer owns them.
    op.execute("""DO $$ DECLARE df name; f text; lr name; BEGIN
      SELECT pg_get_userbyid(proowner) INTO df FROM pg_proc
       WHERE oid='ras_auth_change(uuid,uuid,text,integer)'::regprocedure;
      FOREACH f IN ARRAY ARRAY['ras_auth_creation_current(xid)', 'ras_auth_initial_pair(uuid)', 'ras_auth_operator_receipt_provenance(uuid)', 'ras_auth_initial_status_verified(uuid)',
                              'ras_auth_receipt_provenance(uuid)'] LOOP
       EXECUTE format('ALTER FUNCTION %s OWNER TO %I',f,df);
       EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f);
       FOR lr IN SELECT DISTINCT r.rolname FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a
        JOIN pg_roles r ON r.oid=a.grantee WHERE p.oid='ras_auth_account_live(uuid)'::regprocedure
         AND a.privilege_type='EXECUTE' AND r.rolcanlogin AND r.rolname<>df
       LOOP EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I',f,lr); END LOOP;
      END LOOP;
      FOR f IN SELECT oid::regprocedure::text FROM pg_proc WHERE pronamespace='public'::regnamespace
       AND proname LIKE 'ras_auth_%' AND proowner=(SELECT oid FROM pg_roles WHERE rolname=df)
      LOOP EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I',f,df); END LOOP;
      REVOKE ALL ON FUNCTION ras_auth_receipt_initial_parent(),ras_auth_receipt_initial_pair() FROM PUBLIC;
    END $$;""")


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM users) OR EXISTS (SELECT 1 FROM bootstrap_state)
       OR EXISTS (SELECT 1 FROM recovery_registrations) OR EXISTS (SELECT 1 FROM revocation_journal_records)
       OR EXISTS (SELECT 1 FROM auth_change_receipts) OR EXISTS (SELECT 1 FROM auth_recovery_operators)
       OR EXISTS (SELECT 1 FROM auth_recovery_cases)
       OR EXISTS (SELECT 1 FROM auth_safety_state WHERE projection_seq>0 OR backfill_complete) THEN
       RAISE EXCEPTION 'auth safety history prevents downgrade'; END IF;
    END $$;""")
    for filename, name, next_name in (
        ("0035_login_session_predicates.sql", "ras_auth_account_live", "ras_auth_session_live"),
        ("0036_reset_status_writers.sql", "ras_auth_status_live", "ras_auth_reset_eligible"),
        ("0049_operator_auth_recovery.sql", "ras_auth_operator_inspect", "ras_auth_operator_case"),
        ("0033_auth_safety_foundation.sql", "ras_auth_restrict", "ras_auth_progress"),
    ):
        op.execute(_definition(filename, name, next_name))
    op.execute("DROP TRIGGER trg_auth_receipt_initial_parent ON auth_change_receipts")
    op.execute("DROP TRIGGER trg_auth_receipt_initial_pair ON auth_change_receipts")
    for signature in ("ras_auth_receipt_initial_parent()", "ras_auth_receipt_initial_pair()",
                      "ras_auth_receipt_provenance(uuid)", "ras_auth_initial_status_verified(uuid)",
                      "ras_auth_initial_pair(uuid)", "ras_auth_creation_current(xid)", "ras_auth_operator_receipt_provenance(uuid)"):
        op.execute(f"DROP FUNCTION {signature}")
