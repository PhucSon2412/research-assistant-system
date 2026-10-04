-- CREATE OR REPLACE keeps the existing NOLOGIN owner and runtime EXECUTE ACL.
CREATE OR REPLACE FUNCTION ras_auth_bootstrap_prepare(p_user uuid,p_email text,p_name text,p_verifier text,p_policy integer,p_at timestamptz)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF EXISTS (SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'
  AND classid=(32195295680032364::bigint >> 32)::oid
  AND objid=(32195295680032364::bigint & 4294967295)::oid) THEN
  RAISE EXCEPTION 'bootstrap must precede journal lock' USING ERRCODE='check_violation'; END IF;
 PERFORM pg_advisory_xact_lock(5927110128859829297::bigint);
 -- Installation evidence belongs to SQL creation, even for callers bypassing CLI.
 -- Missing/NULL/mismatched evidence cannot make NOT EXISTS evaluate to UNKNOWN.
 -- This checks the DB binding only; the writer still checks the independent file.
 IF NOT EXISTS (
  SELECT 1 FROM recovery_registrations r JOIN revocation_journal_records g ON g.seq=1
  WHERE r.id=1 AND r.mode IS NOT DISTINCT FROM 'new_install'::text
   AND r.journal_id IS NOT NULL AND r.genesis_hash IS NOT NULL
   AND g.kind IS NOT DISTINCT FROM 'genesis'::text AND g.journal_confirmed IS TRUE
   AND g.record_hash IS NOT DISTINCT FROM r.genesis_hash
   AND (g.line::jsonb -> 'seq') IS NOT DISTINCT FROM '1'::jsonb
   AND ROW(g.line::jsonb ->> 'kind', g.line::jsonb ->> 'hash', g.line::jsonb ->> 'prev',
           g.line::jsonb #>> '{body,journal_id}', g.line::jsonb #>> '{body,mode}')
    IS NOT DISTINCT FROM ROW('genesis'::text, r.genesis_hash, repeat('0',64),
                             r.journal_id::text, 'new_install'::text)
 ) THEN
  RAISE EXCEPTION 'bootstrap requires bound new installation' USING ERRCODE='check_violation'; END IF;
 IF EXISTS (SELECT 1 FROM users) OR EXISTS (SELECT 1 FROM bootstrap_state)
  OR EXISTS (SELECT 1 FROM auth_change_receipts) OR EXISTS (SELECT 1 FROM auth_safety_heads)
  OR EXISTS (SELECT 1 FROM revocation_journal_records WHERE kind NOT IN ('genesis','recovery_begin','recovery_end')) THEN
  RAISE EXCEPTION 'bootstrap already completed' USING ERRCODE='check_violation'; END IF;
 IF p_user IS NULL OR p_user::text !~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
  OR p_email IS NULL OR p_email IS DISTINCT FROM lower(btrim(p_email)) OR p_email !~ '^[^@[:space:]]+@[^@[:space:]]+$'
  OR p_at IS NULL OR NOT isfinite(p_at) OR NOT coalesce(ras_auth_phc_valid(p_verifier,p_policy),false) THEN
  RAISE EXCEPTION 'invalid bootstrap input' USING ERRCODE='check_violation'; END IF;
 INSERT INTO users(id,email,display_name,role,status,password_verifier,password_hash_policy_version,
  created_at,updated_at,password_changed_at)
 VALUES(p_user,p_email,p_name,'admin','active',p_verifier,p_policy,p_at,p_at,p_at);
 INSERT INTO bootstrap_state(id,admin_user_id,completed_at,creation_transaction_id)
 VALUES(true,p_user,p_at,txid_current());
 INSERT INTO audit_events(occurred_at,actor_type,action,outcome,target_type,target_id,details)
 VALUES(p_at,'system','admin.bootstrap','success','user',p_user::text,'{"via":"cli"}'::jsonb);
END $$;

-- Grant only the read needed by the replacement to its already provisioned owner.
DO $$ DECLARE df name; BEGIN
 SELECT pg_get_userbyid(proowner) INTO df FROM pg_proc
  WHERE oid='ras_auth_bootstrap_prepare(uuid,text,text,text,integer,timestamptz)'::regprocedure;
 EXECUTE format('GRANT SELECT ON recovery_registrations TO %I',df);
END $$;
