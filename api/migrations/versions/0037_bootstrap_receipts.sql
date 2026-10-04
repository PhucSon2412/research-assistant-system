-- Existing singleton/account/receipt/history rows stay untouched.
CREATE FUNCTION ras_auth_bootstrap_final() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF TG_OP <> 'INSERT' THEN
  RAISE EXCEPTION 'bootstrap singleton is final' USING ERRCODE='check_violation'; END IF;
 IF NEW.creation_transaction_id IS DISTINCT FROM txid_current()
  OR NEW.completed_at IS NULL OR NOT isfinite(NEW.completed_at) THEN
  RAISE EXCEPTION 'bootstrap needs its creation transaction' USING ERRCODE='check_violation'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_auth_bootstrap_final BEFORE INSERT OR UPDATE OR DELETE ON bootstrap_state
 FOR EACH ROW EXECUTE FUNCTION ras_auth_bootstrap_final();

CREATE FUNCTION ras_auth_bootstrap_pair() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF NOT EXISTS (
  SELECT 1 FROM users u
  JOIN auth_change_receipts c ON c.operation_id=u.credential_operation_id
  JOIN auth_change_receipts s ON s.operation_id=u.status_operation_id
  JOIN revocation_journal_records cj ON cj.auth_operation_id=c.operation_id
  JOIN revocation_journal_records sj ON sj.auth_operation_id=s.operation_id
  WHERE u.id=NEW.admin_user_id AND u.role='admin' AND u.status='active'
   AND u.credential_state='usable' AND u.status_safety='proven'
   AND NEW.creation_transaction_id=txid_current()
   AND c.transaction_id=NEW.creation_transaction_id AND s.transaction_id=c.transaction_id
   AND ROW(c.user_id,c.domain,c.generation,c.decision,c.reason_code,c.recorded_at)
    IS NOT DISTINCT FROM ROW(u.id,'credential'::text,u.credential_generation,'changed'::text,'bootstrap'::text,NEW.completed_at)
   AND ROW(s.user_id,s.domain,s.generation,s.decision,s.reason_code,s.recorded_at)
    IS NOT DISTINCT FROM ROW(u.id,'status'::text,u.status_generation,'active'::text,'bootstrap'::text,NEW.completed_at)
   AND u.created_at=NEW.completed_at AND u.password_changed_at=NEW.completed_at AND u.updated_at=NEW.completed_at
   AND u.credential_reason_code='bootstrap' AND u.status_reason_code='bootstrap'
   AND sj.seq=cj.seq+1
   AND EXISTS (SELECT 1 FROM audit_events a WHERE a.action='admin.bootstrap' AND a.outcome='success'
    AND a.actor_type='system' AND a.target_type='user' AND a.target_id=u.id::text
    AND a.occurred_at=NEW.completed_at AND a.details='{"via":"cli"}'::jsonb)
 ) THEN
  RAISE EXCEPTION 'bootstrap needs its initial receipt pair and audit' USING ERRCODE='check_violation'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_auth_bootstrap_pair AFTER INSERT ON bootstrap_state
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ras_auth_bootstrap_pair();

CREATE FUNCTION ras_auth_bootstrap_prepare(p_user uuid,p_email text,p_name text,p_verifier text,p_policy integer,p_at timestamptz)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 -- Never acquire account/FK/audit locks after journal, including a caller's shared lock.
 IF EXISTS (SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'
  AND classid=(32195295680032364::bigint >> 32)::oid
  AND objid=(32195295680032364::bigint & 4294967295)::oid) THEN
  RAISE EXCEPTION 'bootstrap must precede journal lock' USING ERRCODE='check_violation'; END IF;
 PERFORM pg_advisory_xact_lock(5927110128859829297::bigint);
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

-- Carry the provisioned runtime ACL forward, never grant creation to recovery.
DO $$ DECLARE df name; login_role name; BEGIN
 SELECT pg_get_userbyid(proowner) INTO df FROM pg_proc
  WHERE oid='ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure;
 REVOKE ALL ON bootstrap_state FROM PUBLIC;
 FOR login_role IN SELECT DISTINCT r.rolname FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a
  JOIN pg_roles r ON r.oid=a.grantee WHERE p.oid IN
   ('ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure,'ras_auth_restrict(uuid)'::regprocedure)
  AND a.privilege_type='EXECUTE' AND r.rolcanlogin AND r.rolname<>df
 LOOP EXECUTE format('REVOKE INSERT,UPDATE,DELETE ON bootstrap_state FROM %I',login_role); END LOOP;
 EXECUTE format('GRANT SELECT,INSERT ON bootstrap_state TO %I',df);
 EXECUTE format('GRANT INSERT ON users,audit_events TO %I',df);
 EXECUTE format('ALTER FUNCTION ras_auth_bootstrap_prepare(uuid,text,text,text,integer,timestamptz) OWNER TO %I',df);
 REVOKE ALL ON FUNCTION ras_auth_bootstrap_prepare(uuid,text,text,text,integer,timestamptz) FROM PUBLIC;
 REVOKE ALL ON FUNCTION ras_auth_bootstrap_final(),ras_auth_bootstrap_pair() FROM PUBLIC;
 FOR login_role IN SELECT DISTINCT r.rolname FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a
  JOIN pg_roles r ON r.oid=a.grantee
  WHERE p.oid='ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure
   AND a.privilege_type='EXECUTE' AND r.rolcanlogin AND r.rolname<>df
 LOOP EXECUTE format('GRANT EXECUTE ON FUNCTION ras_auth_bootstrap_prepare(uuid,text,text,text,integer,timestamptz) TO %I',login_role); END LOOP;
END $$;
