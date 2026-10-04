CREATE OR REPLACE FUNCTION ras_auth_rehash(p_user uuid,p_old text,p_generation uuid,p_operation uuid,p_new text,p_policy integer,p_at timestamptz) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF NOT ras_auth_phc_valid(p_new,p_policy) OR p_at IS NULL OR NOT isfinite(p_at) THEN
        RAISE EXCEPTION 'invalid rehash PHC/policy' USING ERRCODE='check_violation'; END IF;
    UPDATE users u SET password_verifier=p_new,password_hash_policy_version=p_policy,updated_at=p_at
      FROM auth_safety_heads h WHERE u.id=p_user AND h.user_id=u.id AND u.password_verifier=p_old
      AND u.credential_generation=p_generation AND u.credential_operation_id=p_operation
      AND u.credential_state='usable' AND u.status='active' AND u.status_safety='proven'
      AND h.credential_operation_id=p_operation AND h.credential_generation=p_generation
      AND h.status_operation_id=u.status_operation_id AND h.status_generation=u.status_generation
      AND p_policy>u.password_hash_policy_version;
    RETURN FOUND;
END $$;

CREATE OR REPLACE FUNCTION ras_auth_session_fence() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF TG_OP='UPDATE' AND NEW.credential_generation IS DISTINCT FROM OLD.credential_generation THEN
        RAISE EXCEPTION 'session generation is final' USING ERRCODE='check_violation'; END IF;
    IF TG_OP='INSERT' AND NEW.credential_generation IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM users u JOIN auth_safety_heads h ON h.user_id=u.id
        WHERE u.id=NEW.user_id AND u.credential_state='usable' AND u.status='active' AND u.status_safety='proven'
          AND NEW.credential_generation=u.credential_generation AND u.credential_generation=h.credential_generation
          AND u.credential_operation_id=h.credential_operation_id AND u.status_operation_id=h.status_operation_id
          AND u.status_generation=h.status_generation) THEN
        RAISE EXCEPTION 'session credential fence mismatch' USING ERRCODE='check_violation'; END IF;
    RETURN NEW;
END $$;

DROP FUNCTION ras_auth_session_activity(uuid,uuid,timestamptz,interval,text);
DROP FUNCTION ras_auth_session_close(uuid,uuid,timestamptz,text);
DROP FUNCTION ras_auth_session_lock(uuid,uuid[]);
DROP FUNCTION ras_auth_closure_mirror(text,text,text);
DROP FUNCTION ras_auth_session_live(uuid,uuid,timestamptz,interval);
DROP FUNCTION ras_auth_account_live(uuid);
