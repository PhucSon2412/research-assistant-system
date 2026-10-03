-- Shared, invoker-read predicate: receipt is required independently of mirror confirmation.
CREATE FUNCTION ras_auth_account_live(p_user uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS (
  SELECT 1 FROM users u JOIN auth_safety_heads h ON h.user_id=u.id
  JOIN auth_change_receipts c ON c.operation_id=u.credential_operation_id
  JOIN auth_change_receipts s ON s.operation_id=u.status_operation_id
  JOIN revocation_journal_records cj ON cj.seq=h.credential_seq AND cj.auth_user_id=u.id AND cj.auth_domain='credential'
  JOIN revocation_journal_records sj ON sj.seq=h.status_seq AND sj.auth_user_id=u.id AND sj.auth_domain='status'
  WHERE u.id=p_user AND u.credential_state='usable' AND u.status='active' AND u.status_safety='proven'
   AND h.status_decision='active'
   AND cj.journal_confirmed AND sj.journal_confirmed
   AND NOT EXISTS (SELECT 1 FROM revocation_journal_records newer WHERE newer.auth_user_id=u.id
       AND ((newer.auth_domain='credential' AND newer.seq>h.credential_seq)
         OR (newer.auth_domain='status' AND newer.seq>h.status_seq)))
   AND ROW(u.credential_operation_id,u.credential_generation,u.credential_reason_code,u.password_changed_at)
       IS NOT DISTINCT FROM ROW(h.credential_operation_id,h.credential_generation,h.credential_reason_code,h.credential_recorded_at)
   AND ROW(u.status_operation_id,u.status_generation,u.status_reason_code)
       IS NOT DISTINCT FROM ROW(h.status_operation_id,h.status_generation,h.status_reason_code)
   AND ROW(c.user_id,c.domain,c.generation,c.decision,c.reason_code,c.recorded_at)
       IS NOT DISTINCT FROM ROW(u.id,'credential'::text,h.credential_generation,'changed'::text,h.credential_reason_code,h.credential_recorded_at)
   AND ROW(s.user_id,s.domain,s.generation,s.decision,s.reason_code,s.recorded_at)
       IS NOT DISTINCT FROM ROW(u.id,'status'::text,h.status_generation,'active'::text,h.status_reason_code,h.status_recorded_at)
 )
$$;
CREATE FUNCTION ras_auth_session_live(p_id uuid,p_user uuid,p_at timestamptz,p_idle interval) RETURNS boolean
LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS (SELECT 1 FROM sessions s JOIN users u ON u.id=s.user_id
  WHERE s.id=p_id AND s.user_id=p_user AND s.revoked_at IS NULL
   AND s.credential_generation=u.credential_generation AND s.credential_generation IS NOT NULL
   AND s.absolute_expires_at>p_at AND s.last_activity_at>p_at-p_idle
   AND p_idle>interval '0' AND isfinite(p_at) AND ras_auth_account_live(u.id))
$$;
CREATE OR REPLACE FUNCTION ras_auth_session_fence() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF TG_OP='UPDATE' AND NEW.credential_generation IS DISTINCT FROM OLD.credential_generation THEN
  RAISE EXCEPTION 'session generation is final' USING ERRCODE='check_violation'; END IF;
 IF TG_OP='INSERT' AND NEW.credential_generation IS NOT NULL AND NOT EXISTS (
   SELECT 1 FROM users u WHERE u.id=NEW.user_id AND u.credential_generation=NEW.credential_generation
    AND ras_auth_account_live(u.id)) THEN
  RAISE EXCEPTION 'session credential fence mismatch' USING ERRCODE='check_violation'; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION ras_auth_session_lock(p_user uuid,p_ids uuid[]) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 PERFORM id FROM sessions WHERE user_id=p_user AND id=ANY(p_ids) ORDER BY id FOR UPDATE;
END $$;
CREATE FUNCTION ras_auth_session_activity(p_id uuid,p_user uuid,p_at timestamptz,p_idle interval,p_csrf text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 UPDATE sessions SET last_activity_at=greatest(last_activity_at,p_at),
   csrf_token_hash=coalesce(p_csrf,csrf_token_hash)
 WHERE id=p_id AND user_id=p_user AND ras_auth_session_live(p_id,p_user,p_at,p_idle);
 RETURN FOUND;
END $$;
CREATE FUNCTION ras_auth_session_close(p_id uuid,p_user uuid,p_at timestamptz,p_reason text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 UPDATE sessions SET revoked_at=p_at,revoked_reason=p_reason
 WHERE id=p_id AND user_id=p_user AND revoked_at IS NULL;
 RETURN FOUND;
END $$;
-- Typed target validation remains in existing SQL triggers. No caller-supplied columns.
CREATE FUNCTION ras_auth_closure_mirror(p_line text,p_via text,p_result text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE e jsonb:=p_line::jsonb; b jsonb:=e->'body'; k text:=e->>'kind';
BEGIN
 IF k NOT IN ('session_revocation','reset_token_closure','invitation_closure') THEN
  RAISE EXCEPTION 'invalid auth closure kind' USING ERRCODE='check_violation'; END IF;
 INSERT INTO revocation_journal_records(seq,kind,record_hash,line,applied_via,result,journal_confirmed,
  session_id,session_user_id,reset_token_id,reset_token_user_id,reset_end_kind,reset_reason_code,reset_ended_at,
  invitation_id,invitation_end_kind,invitation_reason_code,invitation_ended_at)
 VALUES ((e->>'seq')::bigint,k,e->>'hash',p_line,p_via,p_result,false,
  CASE WHEN k='session_revocation' THEN (b->>'session_id')::uuid END,
  CASE WHEN k='session_revocation' THEN (b->>'user_id')::uuid END,
  CASE WHEN k='reset_token_closure' THEN (b->>'token_id')::uuid END,
  CASE WHEN k='reset_token_closure' THEN (b->>'user_id')::uuid END,
  CASE WHEN k='reset_token_closure' THEN b->>'end_kind' END,
  CASE WHEN k='reset_token_closure' THEN b->>'reason_code' END,
  CASE WHEN k='reset_token_closure' THEN (b->>'ended_at')::timestamptz END,
  CASE WHEN k='invitation_closure' THEN (b->>'invitation_id')::uuid END,
  CASE WHEN k='invitation_closure' THEN b->>'end_kind' END,
  CASE WHEN k='invitation_closure' THEN b->>'reason_code' END,
  CASE WHEN k='invitation_closure' THEN (b->>'ended_at')::timestamptz END);
END $$;

DO $$ DECLARE f record; df name; rt name; rc name; BEGIN
 SELECT pg_get_userbyid(proowner) INTO df FROM pg_proc WHERE oid='ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure;
 FOR f IN SELECT oid::regprocedure signature,proname FROM pg_proc WHERE pronamespace='public'::regnamespace
  AND proname IN ('ras_auth_account_live','ras_auth_session_live','ras_auth_session_lock','ras_auth_session_activity','ras_auth_session_close','ras_auth_closure_mirror')
 LOOP
  EXECUTE format('ALTER FUNCTION %s OWNER TO %I',f.signature,df);
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f.signature);
  -- Preserve provisioned roles by existing routine ACL, without guessing login names.
  FOR rt IN SELECT DISTINCT r.rolname FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a
    JOIN pg_roles r ON r.oid=a.grantee WHERE p.oid='ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure
    AND a.privilege_type='EXECUTE' AND r.rolcanlogin AND r.rolname<>df
  LOOP EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I',f.signature,rt); END LOOP;
  IF f.proname IN ('ras_auth_account_live','ras_auth_session_live','ras_auth_session_lock','ras_auth_session_close','ras_auth_closure_mirror') THEN
   FOR rc IN SELECT DISTINCT r.rolname FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a
    JOIN pg_roles r ON r.oid=a.grantee WHERE p.oid='ras_auth_restrict(uuid)'::regprocedure
    AND a.privilege_type='EXECUTE' AND r.rolcanlogin AND r.rolname<>df
   LOOP EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I',f.signature,rc); END LOOP;
  END IF;
 END LOOP;
 EXECUTE format('GRANT UPDATE ON sessions TO %I',df);
END $$;

CREATE OR REPLACE FUNCTION ras_auth_rehash(p_user uuid,p_old text,p_generation uuid,p_operation uuid,p_new text,p_policy integer,p_at timestamptz) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF NOT ras_auth_phc_valid(p_new,p_policy) OR p_at IS NULL OR NOT isfinite(p_at) THEN
        RAISE EXCEPTION 'invalid rehash PHC/policy' USING ERRCODE='check_violation'; END IF;
    UPDATE users u SET password_verifier=p_new,password_hash_policy_version=p_policy,updated_at=p_at
      FROM auth_safety_heads h WHERE u.id=p_user AND h.user_id=u.id AND u.password_verifier=p_old
      AND u.credential_generation=p_generation AND u.credential_operation_id=p_operation
      AND ras_auth_account_live(u.id)
      AND h.credential_operation_id=p_operation AND h.credential_generation=p_generation
      AND h.status_operation_id=u.status_operation_id AND h.status_generation=u.status_generation
      AND p_policy>u.password_hash_policy_version;
    RETURN FOUND;
END $$;
