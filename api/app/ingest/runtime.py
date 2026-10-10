"""Worker runtime identity and short transaction ownership. No provisioning/fallback."""
from dataclasses import dataclass

from sqlalchemy import text


class Halt(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass
class Outcome:
    commit_started: bool = False
    committed: bool = False


def require_runtime(db):
    """Inspect the actual login, including inherited and PUBLIC privileges."""
    try:
        forbidden = db.scalar(text("""
            SELECT current_user <> session_user OR r.rolsuper OR r.rolcreaterole
              OR r.rolcreatedb OR r.rolbypassrls OR NOT r.rolcanlogin
              OR EXISTS (SELECT 1 FROM pg_auth_members WHERE member=r.oid)
              OR EXISTS (SELECT 1 FROM pg_class WHERE relowner=r.oid)
              OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner=r.oid)
              OR EXISTS (SELECT 1 FROM pg_proc WHERE proowner=r.oid)
              OR EXISTS (SELECT 1 FROM pg_database WHERE datname=current_database() AND datdba=r.oid)
              OR has_database_privilege(current_database(),'CREATE')
              OR has_database_privilege(current_database(),'TEMP')
              OR has_schema_privilege('public','CREATE')
              OR has_table_privilege('users','UPDATE')
              OR has_table_privilege('revocation_journal_records','INSERT')
              OR EXISTS (SELECT 1 FROM unnest(ARRAY['users','invitations','bootstrap_state','sessions',
                 'password_reset_tokens','auth_change_receipts','auth_safety_heads','auth_safety_state',
                 'auth_hash_policies','revocation_journal_records','object_delete_fences']) AS t(name)
                 WHERE has_table_privilege(t.name,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER')
                   OR has_any_column_privilege(t.name,'INSERT,UPDATE,REFERENCES'))
              OR NOT has_function_privilege('ras_auth_lock_users(uuid[])','EXECUTE')
              OR NOT has_function_privilege('ras_auth_status_live(uuid)','EXECUTE')
              OR NOT has_function_privilege('ras_auth_session_create(uuid,uuid,uuid,text,text,timestamp with time zone,timestamp with time zone)','EXECUTE')
              OR NOT has_table_privilege('ingestion_jobs','UPDATE')
              OR EXISTS (SELECT 1 FROM unnest(ARRAY['embedding_calls','embedding_budget_days',
                 'embedding_user_budget_days','index_generations','index_entries']) AS t(name)
                 CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE']) AS p(name)
                 WHERE NOT has_table_privilege(t.name,p.name))
            FROM pg_roles r WHERE rolname=current_user
        """))
        if forbidden is not False:
            raise Halt('worker_runtime_required')
    except Halt:
        raise
    except Exception:
        raise Halt('worker_runtime_unavailable') from None


def release(db):
    failed = False
    for action in (db.rollback, db.close):
        try:
            action()
        except BaseException:
            failed = True
    if failed:
        try:
            db.invalidate()
        except BaseException:
            pass
        raise Halt('worker_release_unavailable') from None


def commit(db, outcome):
    outcome.commit_started = True
    try:
        db.commit()
    except BaseException:
        raise Halt('worker_commit_uncertain') from None
    outcome.committed = True


def short(db, operation):
    """Stop on every uncertain/release fault; teardown cannot erase commit facts."""
    outcome = Outcome()
    db.info['worker_outcome'] = outcome
    try:
        result = operation(outcome)
    except BaseException as exc:
        try:
            release(db)
        except Halt:
            pass
        if isinstance(exc, Halt) or type(exc).__name__ == 'StepError':
            raise
        raise Halt('worker_checkpoint_unavailable') from None
    release(db)
    return result
