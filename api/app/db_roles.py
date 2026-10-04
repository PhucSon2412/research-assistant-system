"""Provision the C1 role boundary using a migrator connection, never runtime."""
import re
from sqlalchemy import text

RUNTIME_ROUTINES = {"ras_auth_mirror", "ras_auth_confirm", "ras_auth_project", "ras_auth_change", "ras_auth_rehash",
                    "ras_auth_session_create", "ras_auth_token_create", "ras_auth_lock_users",
                    "ras_auth_session_lock", "ras_auth_session_activity", "ras_auth_session_close", "ras_auth_closure_mirror",
                    "ras_auth_account_live", "ras_auth_session_live", "ras_auth_status_live", "ras_auth_reset_eligible",
                    "ras_auth_reset_token_live", "ras_auth_token_lock", "ras_auth_token_close"}
RECOVERY_ROUTINES = {"ras_auth_mirror", "ras_auth_confirm", "ras_auth_project", "ras_auth_restrict", "ras_auth_progress", "ras_auth_lock_users",
                     "ras_auth_account_live", "ras_auth_session_live", "ras_auth_session_lock",
                     "ras_auth_session_close", "ras_auth_closure_mirror", "ras_auth_status_live", "ras_auth_reset_eligible",
                     "ras_auth_reset_token_live", "ras_auth_token_lock", "ras_auth_token_close"}
PROTECTED = {"users", "sessions", "password_reset_tokens", "auth_change_receipts", "auth_safety_heads", "auth_safety_state", "auth_hash_policies", "revocation_journal_records"}


def _identifier(name):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name):
        raise ValueError("invalid role identifier")
    return '"' + name + '"'


def provision(engine, *, runtime: str, recovery: str, definer: str):
    """Login roles/passwords are supplied by deployment; no default credentials."""
    identifiers = [_identifier(n) for n in (runtime, recovery, definer)]
    if len(set(identifiers)) != 3:
        raise ValueError("roles must be distinct")
    rt, rc, df = identifiers
    with engine.begin() as conn:
        for name in (runtime, recovery):
            row = conn.execute(text("SELECT rolsuper,rolcreaterole,rolcreatedb,rolbypassrls FROM pg_roles WHERE rolname=:n"), {"n": name}).first()
            if row is None or any(row):
                raise ValueError("login role missing or privileged")
            if conn.execute(text("SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=:n)"), {"n": name}).scalar():
                raise ValueError("login role has memberships")
            if conn.execute(text("""SELECT EXISTS(SELECT 1 FROM pg_class WHERE relowner=(SELECT oid FROM pg_roles WHERE rolname=:n))
                OR EXISTS(SELECT 1 FROM pg_namespace WHERE nspowner=(SELECT oid FROM pg_roles WHERE rolname=:n))
                OR EXISTS(SELECT 1 FROM pg_proc WHERE proowner=(SELECT oid FROM pg_roles WHERE rolname=:n))
                OR EXISTS(SELECT 1 FROM pg_database WHERE datname=current_database() AND datdba=(SELECT oid FROM pg_roles WHERE rolname=:n))"""), {"n":name}).scalar():
                raise ValueError("login role owns database objects")
        existing = conn.execute(text("SELECT rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,rolbypassrls FROM pg_roles WHERE rolname=:n"), {"n":definer}).first()
        if existing is None:
            conn.execute(text(f"CREATE ROLE {df} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"))
        elif any(existing):
            raise ValueError("definer must be an unprivileged NOLOGIN role")
        if conn.execute(text("SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=:n)"), {"n":definer}).scalar():
            raise ValueError("definer has memberships")
        if conn.execute(text("""SELECT EXISTS(SELECT 1 FROM pg_class WHERE relowner=(SELECT oid FROM pg_roles WHERE rolname=:n))
            OR EXISTS(SELECT 1 FROM pg_namespace WHERE nspowner=(SELECT oid FROM pg_roles WHERE rolname=:n))
            OR EXISTS(SELECT 1 FROM pg_database WHERE datname=current_database() AND datdba=(SELECT oid FROM pg_roles WHERE rolname=:n))"""), {"n":definer}).scalar():
            raise ValueError("definer owns tables or schemas")
        # PostgreSQL 16 public schema belongs to the database owner. No runtime DDL.
        conn.execute(text(f"REVOKE CREATE ON SCHEMA public FROM PUBLIC, {rt}, {rc}, {df}"))
        database = conn.dialect.identifier_preparer.quote(conn.scalar(text("SELECT current_database()")))
        conn.execute(text(f"REVOKE CREATE,TEMPORARY ON DATABASE {database} FROM PUBLIC, {rt}, {rc}, {df}"))
        for table in PROTECTED:
            conn.execute(text(f"REVOKE ALL ON {_identifier(table)} FROM PUBLIC"))
        conn.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {rt}, {rc}, {df}"))
        conn.execute(text(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {rt}, {rc}, {df}"))
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {rt}, {rc}, {df}"))
        conn.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {rt}, {rc}"))
        tables = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars().all()
        for table in tables:
            t = _identifier(table)
            if table not in PROTECTED and table != "alembic_version":
                conn.execute(text(f"GRANT INSERT,UPDATE,DELETE ON {t} TO {rt}, {rc}"))
        # Recovery writes old closure/mirror data, but cannot create receipts or write User auth state.
        for table in ("sessions", "password_reset_tokens", "revocation_journal_records"):
            conn.execute(text(f"GRANT INSERT,UPDATE ON {_identifier(table)} TO {rc}"))
        conn.execute(text(f"GRANT SELECT ON users,sessions,password_reset_tokens,auth_change_receipts,auth_safety_heads,auth_safety_state,auth_hash_policies,revocation_journal_records TO {df}"))
        conn.execute(text(f"GRANT INSERT ON auth_change_receipts,auth_safety_heads,sessions,password_reset_tokens,revocation_journal_records TO {df}"))
        conn.execute(text(f"GRANT UPDATE ON users,sessions,password_reset_tokens,auth_safety_heads,auth_safety_state,revocation_journal_records TO {df}"))
        conn.execute(text(f"GRANT EXECUTE ON FUNCTION ras_auth_phc_valid(text,integer) TO {df}"))
        # Triggers execute under the caller; SELECT is already granted. Trigger functions don't expose mutation.
        functions = conn.execute(text("SELECT oid::regprocedure::text signature,proname FROM pg_proc WHERE pronamespace='public'::regnamespace AND (prosecdef OR proname IN ('ras_auth_account_live','ras_auth_session_live','ras_auth_status_live','ras_auth_reset_eligible','ras_auth_reset_token_live')) AND proname LIKE 'ras_auth_%'"))
        for signature, name in functions:
            conn.execute(text(f"ALTER FUNCTION {signature} OWNER TO {df}"))
            conn.execute(text(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC"))
            for role, names in ((rt,RUNTIME_ROUTINES),(rc,RECOVERY_ROUTINES)):
                if name in names:
                    conn.execute(text(f"GRANT EXECUTE ON FUNCTION {signature} TO {role}"))
