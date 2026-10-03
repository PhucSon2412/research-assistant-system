-- Restore the frozen 0033 guard only on a pristine, unregistered database.
CREATE OR REPLACE FUNCTION ras_auth_token_fence() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF TG_OP='UPDATE' AND ROW(NEW.credential_fence_operation_id,NEW.recovery_purpose)
       IS DISTINCT FROM ROW(OLD.credential_fence_operation_id,OLD.recovery_purpose) THEN
        RAISE EXCEPTION 'reset token fence is final' USING ERRCODE='check_violation'; END IF;
    IF TG_OP='INSERT' AND NEW.credential_fence_operation_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM users u JOIN auth_safety_heads h ON h.user_id=u.id WHERE u.id=NEW.user_id
        AND u.status='active' AND u.status_safety='proven' AND h.credential_operation_id=NEW.credential_fence_operation_id
        AND ((NEW.recovery_purpose='normal' AND u.credential_state='usable' AND u.credential_operation_id=h.credential_operation_id)
             OR (NEW.recovery_purpose='quarantine' AND u.credential_state='quarantined'))) THEN
        RAISE EXCEPTION 'reset token credential fence mismatch' USING ERRCODE='check_violation'; END IF;
    RETURN NEW;
END $$;
