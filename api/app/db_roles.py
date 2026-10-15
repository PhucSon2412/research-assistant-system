"""Provision the C1 role boundary using a migrator connection, never runtime."""
import re
import uuid
from sqlalchemy import text

RUNTIME_ROUTINES = {"ras_auth_review_closure_mirror", "ras_auth_review_closure_confirm",
                    "ras_auth_common_revocation_mirror", "ras_auth_common_revocation_confirm",
                    "ras_auth_review_approval_mirror", "ras_auth_review_approval_confirm",
                    "ras_auth_mirror", "ras_auth_confirm", "ras_auth_project", "ras_auth_change", "ras_auth_rehash",
                    "ras_auth_session_create", "ras_auth_token_create", "ras_auth_lock_users",
                    "ras_auth_session_lock", "ras_auth_session_activity", "ras_auth_session_close", "ras_auth_closure_mirror",
                    "ras_auth_account_live", "ras_auth_session_live", "ras_auth_status_live", "ras_auth_reset_eligible",
                    "ras_auth_reset_token_live", "ras_auth_token_lock", "ras_auth_token_close", "ras_auth_bootstrap_prepare",
                    "ras_auth_invitation_lock", "ras_auth_invitation_create", "ras_auth_invitation_revoke",
                    "ras_auth_invitation_prepare", "ras_auth_invitation_compensate", "ras_auth_invitation_recover",
                    "ras_auth_cleanup_fence", "ras_auth_orphan_fence", "ras_auth_orphan_complete",
                    "ras_auth_initial_pair", "ras_auth_initial_status_verified", "ras_auth_receipt_provenance", "ras_auth_creation_current", "ras_auth_operator_receipt_provenance"}
RECOVERY_ROUTINES = {"ras_auth_complete_contract", "ras_auth_mirror", "ras_auth_confirm", "ras_auth_project", "ras_auth_restrict", "ras_auth_progress", "ras_auth_lock_users",
                     "ras_auth_account_live", "ras_auth_session_live", "ras_auth_session_lock",
                     "ras_auth_session_close", "ras_auth_closure_mirror", "ras_auth_status_live", "ras_auth_reset_eligible",
                     "ras_auth_reset_token_live", "ras_auth_token_lock", "ras_auth_token_close",
                     "ras_auth_invitation_lock", "ras_auth_invitation_recover",
                     "ras_auth_initial_pair", "ras_auth_initial_status_verified", "ras_auth_receipt_provenance", "ras_auth_creation_current", "ras_auth_operator_receipt_provenance"}
# C3: operator logins get these and nothing else; identity is resolved from session_user in SQL.
OPERATOR_ROUTINES = {"ras_auth_operator_whoami", "ras_auth_operator_gate", "ras_auth_operator_inspect",
                     "ras_auth_operator_case", "ras_auth_operator_snapshot", "ras_auth_operator_stage",
                     "ras_auth_operator_confirm", "ras_auth_operator_outcome"}
PROTECTED = {"users", "invitations", "bootstrap_state", "sessions", "password_reset_tokens", "auth_change_receipts", "auth_safety_heads", "auth_safety_state", "auth_hash_policies", "revocation_journal_records", "object_delete_fences", "orphan_object_fences", "orphan_object_completions",
             "auth_recovery_operators", "auth_recovery_cases", "auth_operational_state", "recovery_registrations"}


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
            row = conn.execute(text("SELECT NOT rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,rolbypassrls,rolreplication FROM pg_roles WHERE rolname=:n"), {"n": name}).first()
            if row is None or any(row):
                raise ValueError("login role missing or privileged")
            if conn.execute(text("SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=:n)"), {"n": name}).scalar():
                raise ValueError("login role has memberships")
            if conn.execute(text("""SELECT EXISTS(SELECT 1 FROM pg_class WHERE relowner=(SELECT oid FROM pg_roles WHERE rolname=:n))
                OR EXISTS(SELECT 1 FROM pg_namespace WHERE nspowner=(SELECT oid FROM pg_roles WHERE rolname=:n))
                OR EXISTS(SELECT 1 FROM pg_proc WHERE proowner=(SELECT oid FROM pg_roles WHERE rolname=:n))
                OR EXISTS(SELECT 1 FROM pg_database WHERE datname=current_database() AND datdba=(SELECT oid FROM pg_roles WHERE rolname=:n))"""), {"n":name}).scalar():
                raise ValueError("login role owns database objects")
        existing = conn.execute(text("SELECT rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,rolbypassrls,rolreplication FROM pg_roles WHERE rolname=:n"), {"n":definer}).first()
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
            # Older revisions are provisioned by migration tests before the fence table exists (0044).
            if conn.scalar(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table}):
                conn.execute(text(f"REVOKE ALL ON {_identifier(table)} FROM PUBLIC"))
                # Table REVOKE does not remove independent column ACLs. Strip
                # those too before rebuilding this deployment's protected ACLs.
                columns = conn.execute(text("SELECT attname FROM pg_attribute WHERE attrelid=to_regclass(:t)"
                    " AND attnum>0 AND NOT attisdropped ORDER BY attnum"), {"t": table}).scalars()
                column_list = ','.join(conn.dialect.identifier_preparer.quote(name) for name in columns)
                if column_list:
                    conn.execute(text(f"REVOKE ALL ({column_list}) ON {_identifier(table)} FROM PUBLIC, {rt}, {rc}, {df}"))
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
        conn.execute(text(f"GRANT INSERT ON recovery_registrations TO {rc}"))
        conn.execute(text(f"GRANT SELECT ON users,sessions,password_reset_tokens,auth_change_receipts,auth_safety_heads,auth_safety_state,auth_hash_policies,revocation_journal_records TO {df}"))
        conn.execute(text(f"GRANT INSERT ON auth_change_receipts,auth_safety_heads,sessions,password_reset_tokens,revocation_journal_records TO {df}"))
        conn.execute(text(f"GRANT UPDATE ON users,sessions,password_reset_tokens,auth_safety_heads,auth_safety_state,revocation_journal_records TO {df}"))
        conn.execute(text(f"GRANT SELECT,INSERT ON bootstrap_state TO {df}"))
        conn.execute(text(f"GRANT SELECT ON recovery_registrations TO {df}"))
        if conn.scalar(text("SELECT to_regclass('auth_operational_state') IS NOT NULL")):
            conn.execute(text(f"GRANT SELECT,INSERT,UPDATE ON auth_operational_state TO {df}"))
        conn.execute(text(f"GRANT INSERT ON users,audit_events TO {df}"))
        conn.execute(text(f"GRANT SELECT,INSERT,UPDATE ON invitations TO {df}"))
        conn.execute(text(f"GRANT SELECT ON review_requests TO {df}"))
        # Approval mirror binds the exact private tuple and new common tuple/rights (C2d-5b).
        conn.execute(text(f"GRANT SELECT ON library_items,paper_versions,source_files,access_rights TO {df}"))
        conn.execute(text(f"GRANT SELECT ON common_publications,ingestion_jobs,deletion_tombstones,revocation_metadata_retentions,audit_events TO {df}"))
        conn.execute(text(f"GRANT EXECUTE ON FUNCTION ras_auth_phc_valid(text,integer) TO {df}"))
        if conn.scalar(text("SELECT to_regclass('object_delete_fences') IS NOT NULL")):
            # Only the definer routine writes fences; logins read them (SourceFile INSERT trigger runs as invoker).
            conn.execute(text(f"GRANT SELECT,INSERT ON object_delete_fences TO {df}"))
        if conn.scalar(text("SELECT to_regclass('orphan_object_fences') IS NOT NULL")):
            # Only the definer routines write orphan fences/completions (C2d-5e2, 0045).
            conn.execute(text(f"GRANT SELECT,INSERT ON orphan_object_fences,orphan_object_completions TO {df}"))
        if conn.scalar(text("SELECT to_regclass('auth_recovery_cases') IS NOT NULL")):
            # C3 (0049): definer routines read the operator map/cases; only the migrator writes them.
            conn.execute(text(f"GRANT SELECT ON auth_recovery_operators,auth_recovery_cases TO {df}"))
            for name in (runtime, recovery):
                if conn.scalar(text("SELECT EXISTS(SELECT 1 FROM auth_recovery_operators WHERE login_role=:n)"), {"n": name}):
                    raise ValueError("runtime/recovery login is mapped as an operator")
        if conn.scalar(text("SELECT to_regprocedure('ras_auth_invitation_before_journal()') IS NOT NULL")):
            conn.execute(text(f"GRANT EXECUTE ON FUNCTION ras_auth_invitation_before_journal(),ras_auth_invitation_actor(uuid,uuid,text,timestamptz,interval) TO {df}"))
        # Triggers execute under the caller; SELECT is already granted. Trigger functions don't expose mutation.
        functions = conn.execute(text("SELECT oid::regprocedure::text signature,proname FROM pg_proc WHERE pronamespace='public'::regnamespace AND (prosecdef OR proname IN ('ras_auth_account_live','ras_auth_session_live','ras_auth_status_live','ras_auth_reset_eligible','ras_auth_reset_token_live','ras_auth_initial_pair','ras_auth_initial_status_verified','ras_auth_receipt_provenance','ras_auth_creation_current','ras_auth_operator_receipt_provenance')) AND proname LIKE 'ras_auth_%'"))
        for signature, name in functions:
            conn.execute(text(f"ALTER FUNCTION {signature} OWNER TO {df}"))
            conn.execute(text(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC"))
            # REVOKE ALL above also revokes an existing owner's ordinary
            # EXECUTE ACL. ALTER OWNER to the same role does not restore it.
            # Definer-to-helper calls must keep working after reprovision.
            conn.execute(text(f"GRANT EXECUTE ON FUNCTION {signature} TO {df}"))
            for role, names in ((rt,RUNTIME_ROUTINES),(rc,RECOVERY_ROUTINES)):
                if name in names:
                    conn.execute(text(f"GRANT EXECUTE ON FUNCTION {signature} TO {role}"))


_SESSION_CREATE = "ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)"


def provision_operator(engine, *, login: str, operator_id: uuid.UUID):
    """Map one real LOGIN to an operator UUID and grant only operator routines (idempotent).

    Trusted provisioning (migrator connection) is the authority for this map;
    the operator CLI never registers itself. Refuses privileged/member/owning
    logins and logins that already act as runtime/recovery/definer/migrator.
    """
    ident = _identifier(login)
    operator_id = uuid.UUID(str(operator_id))
    with engine.begin() as conn:
        definer = conn.scalar(text(f"SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE oid='{_SESSION_CREATE}'::regprocedure"))
        if login in (definer, conn.scalar(text("SELECT current_user")), conn.scalar(text("SELECT session_user"))):
            raise ValueError("operator login must be distinct from definer/migrator")
        row = conn.execute(text("SELECT oid,rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,rolbypassrls,rolreplication"
                                " FROM pg_roles WHERE rolname=:n"), {"n": login}).first()
        if row is None or not row.rolcanlogin or any(row[2:]):
            raise ValueError("operator login missing or privileged")
        if conn.scalar(text("SELECT count(*) FROM pg_auth_members WHERE member=:o OR roleid=:o"), {"o": row.oid}):
            raise ValueError("operator login has memberships")
        if conn.scalar(text("""SELECT EXISTS(SELECT 1 FROM pg_class WHERE relowner=:o)
            OR EXISTS(SELECT 1 FROM pg_namespace WHERE nspowner=:o) OR EXISTS(SELECT 1 FROM pg_proc WHERE proowner=:o)
            OR EXISTS(SELECT 1 FROM pg_database WHERE datdba=:o)"""), {"o": row.oid}):
            raise ValueError("operator login owns database objects")
        if conn.scalar(text(f"""SELECT has_table_privilege(:n,'users','SELECT,INSERT,UPDATE,DELETE')
            OR has_table_privilege(:n,'revocation_journal_records','SELECT,INSERT,UPDATE,DELETE')
            OR has_function_privilege(:n,'{_SESSION_CREATE}','EXECUTE')
            OR has_function_privilege(:n,'ras_auth_restrict(uuid)','EXECUTE')"""), {"n": login}):
            raise ValueError("runtime/recovery login cannot be an operator")
        existing = conn.execute(text("SELECT operator_id,login_role,revoked_at FROM auth_recovery_operators"
                                     " WHERE operator_id=:id OR login_role=:n"), {"id": operator_id, "n": login}).all()
        if existing and [tuple(r) for r in existing] != [(operator_id, login, None)]:
            raise ValueError("operator mapping conflict")
        if not existing:
            conn.execute(text("INSERT INTO auth_recovery_operators(operator_id,login_role) VALUES(:id,:n)"),
                         {"id": operator_id, "n": login})
        database = conn.dialect.identifier_preparer.quote(conn.scalar(text("SELECT current_database()")))
        conn.execute(text(f"REVOKE CREATE,TEMPORARY ON DATABASE {database} FROM {ident}"))
        conn.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ident}"))
        conn.execute(text(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {ident}"))
        conn.execute(text(f"REVOKE CREATE ON SCHEMA public FROM {ident}"))
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {ident}"))
        signatures = conn.execute(text("SELECT oid::regprocedure::text FROM pg_proc WHERE pronamespace='public'::regnamespace"
                                       " AND proname = ANY(:names)"), {"names": sorted(OPERATOR_ROUTINES)}).scalars().all()
        if len(signatures) != len(OPERATOR_ROUTINES):
            raise ValueError("operator routines missing; migrate first")
        for signature in signatures:
            conn.execute(text(f"GRANT EXECUTE ON FUNCTION {signature} TO {ident}"))


def revoke_operator(engine, *, operator_id: uuid.UUID, now):
    """End an operator mapping (once); its login keeps no routine grant."""
    with engine.begin() as conn:
        login = conn.scalar(text("UPDATE auth_recovery_operators SET revoked_at=:at WHERE operator_id=:id"
                                 " AND revoked_at IS NULL RETURNING login_role"), {"id": uuid.UUID(str(operator_id)), "at": now})
        if login is None:
            raise ValueError("operator not active")
        if conn.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:n)"), {"n": login}):
            conn.execute(text(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {_identifier(login)}"))


CASE_COLUMNS = ("case_id", "action", "operator_id", "approver_id", "journal_id", "user_id", "expected_role",
                "expected_credential_operation_id", "expected_status_operation_id", "verification_method",
                "approved_at", "expires_at")


def register_case(engine, case, *, now, max_lifetime):
    """Trusted provisioning of one approved case (same values as the restricted case file); idempotent."""
    from app.operator_cases import RecoveryCase
    if case.expires_at - case.approved_at > max_lifetime or case.expires_at <= now:
        raise ValueError("case window outside policy")
    with engine.begin() as conn:
        if conn.scalar(text("SELECT journal_id FROM recovery_registrations WHERE id=1")) != case.journal_id:
            raise ValueError("case installation does not match this database registration")
        if not conn.scalar(text("SELECT EXISTS(SELECT 1 FROM auth_recovery_operators WHERE operator_id=:o AND revoked_at IS NULL)"),
                           {"o": case.operator_id}):
            raise ValueError("case operator not active")
        row = conn.execute(text("SELECT * FROM auth_recovery_cases WHERE case_id=:c"), {"c": case.case_id}).first()
        if row is not None:
            if RecoveryCase.from_row(row) != case:
                raise ValueError("case conflict")
            return
        conn.execute(text(f"INSERT INTO auth_recovery_cases({','.join(CASE_COLUMNS)})"
                          f" VALUES({','.join(':' + c for c in CASE_COLUMNS)})"),
                     {c: getattr(case, c) for c in CASE_COLUMNS})
