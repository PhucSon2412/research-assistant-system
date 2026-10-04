CREATE FUNCTION ras_auth_status_live(p_user uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS (SELECT 1 FROM users u JOIN auth_safety_heads h ON h.user_id=u.id
  JOIN auth_change_receipts r ON r.operation_id=u.status_operation_id
  JOIN revocation_journal_records j ON j.seq=h.status_seq AND j.auth_user_id=u.id AND j.auth_domain='status'
  WHERE u.id=p_user AND u.status='active' AND u.status_safety='proven' AND h.status_decision='active'
   AND j.journal_confirmed AND NOT EXISTS (SELECT 1 FROM revocation_journal_records n
     WHERE n.auth_user_id=u.id AND n.auth_domain='status' AND n.seq>h.status_seq)
   AND ROW(u.status_operation_id,u.status_generation,u.status_reason_code)
     IS NOT DISTINCT FROM ROW(h.status_operation_id,h.status_generation,h.status_reason_code)
   AND ROW(r.user_id,r.domain,r.generation,r.decision,r.reason_code,r.recorded_at)
     IS NOT DISTINCT FROM ROW(u.id,'status'::text,h.status_generation,'active'::text,h.status_reason_code,h.status_recorded_at))
$$;
CREATE FUNCTION ras_auth_reset_eligible(p_user uuid,p_fence uuid,p_purpose text) RETURNS boolean
LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS (SELECT 1 FROM users u JOIN auth_safety_heads h ON h.user_id=u.id
  JOIN revocation_journal_records j ON j.seq=h.credential_seq AND j.auth_user_id=u.id AND j.auth_domain='credential'
  WHERE u.id=p_user AND p_fence IS NOT NULL AND p_fence=h.credential_operation_id AND j.journal_confirmed
   AND ras_auth_status_live(u.id) AND NOT EXISTS (SELECT 1 FROM revocation_journal_records n
     WHERE n.auth_user_id=u.id AND n.auth_domain='credential' AND n.seq>h.credential_seq)
   AND ((p_purpose='normal' AND ras_auth_account_live(u.id))
     OR (p_purpose='quarantine' AND u.credential_state='quarantined')))
$$;
CREATE FUNCTION ras_auth_reset_token_live(p_id uuid,p_user uuid,p_at timestamptz) RETURNS boolean
LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS (SELECT 1 FROM password_reset_tokens t WHERE t.id=p_id AND t.user_id=p_user
  AND t.used_at IS NULL AND t.revoked_at IS NULL AND t.expires_at>p_at AND isfinite(p_at)
  AND ras_auth_reset_eligible(t.user_id,t.credential_fence_operation_id,t.recovery_purpose))
$$;
CREATE OR REPLACE FUNCTION ras_auth_token_fence() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF TG_OP='UPDATE' AND ROW(NEW.credential_fence_operation_id,NEW.recovery_purpose)
   IS DISTINCT FROM ROW(OLD.credential_fence_operation_id,OLD.recovery_purpose) THEN
  RAISE EXCEPTION 'reset token fence is final' USING ERRCODE='check_violation'; END IF;
 IF TG_OP='INSERT' AND NEW.credential_fence_operation_id IS NOT NULL
  AND NOT ras_auth_reset_eligible(NEW.user_id,NEW.credential_fence_operation_id,NEW.recovery_purpose) THEN
  RAISE EXCEPTION 'reset token credential fence mismatch' USING ERRCODE='check_violation'; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION ras_auth_token_lock(p_user uuid,p_ids uuid[]) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 PERFORM id FROM password_reset_tokens WHERE user_id=p_user AND id=ANY(p_ids) ORDER BY id FOR UPDATE;
END $$;
CREATE FUNCTION ras_auth_token_close(p_id uuid,p_user uuid,p_kind text,p_reason text,p_at timestamptz) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF p_kind NOT IN ('consumed','revoked') OR p_kind IS NULL OR p_at IS NULL OR NOT isfinite(p_at) THEN
  RAISE EXCEPTION 'invalid reset closure' USING ERRCODE='check_violation'; END IF;
 UPDATE password_reset_tokens SET used_at=CASE WHEN p_kind='consumed' THEN p_at END,
  revoked_at=CASE WHEN p_kind='revoked' THEN p_at END,closure_reason=p_reason
 WHERE id=p_id AND user_id=p_user AND used_at IS NULL AND revoked_at IS NULL;
 RETURN FOUND;
END $$;
DO $$ DECLARE f record; df name; role_name name; BEGIN
 SELECT pg_get_userbyid(proowner) INTO df FROM pg_proc
  WHERE oid='ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure;
 FOR f IN SELECT oid::regprocedure signature FROM pg_proc WHERE pronamespace='public'::regnamespace
  AND proname IN ('ras_auth_status_live','ras_auth_reset_eligible','ras_auth_reset_token_live','ras_auth_token_lock','ras_auth_token_close')
 LOOP
  EXECUTE format('ALTER FUNCTION %s OWNER TO %I',f.signature,df);
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f.signature);
  FOR role_name IN SELECT DISTINCT r.rolname FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a
   JOIN pg_roles r ON r.oid=a.grantee WHERE p.oid IN
    ('ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure,'ras_auth_restrict(uuid)'::regprocedure)
   AND a.privilege_type='EXECUTE' AND r.rolcanlogin AND r.rolname<>df
  LOOP EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I',f.signature,role_name); END LOOP;
 END LOOP;
 EXECUTE format('GRANT UPDATE ON password_reset_tokens TO %I',df);
END $$;
