-- Frozen with revision 0033. Application roles are provisioned separately.
CREATE FUNCTION ras_auth_record_valid() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE b jsonb; e jsonb; fields text[]; stamp timestamptz; domain text; decision text;
BEGIN
    IF NEW.kind NOT IN ('credential_transition','account_status_decision') THEN
        IF num_nonnulls(NEW.auth_user_id,NEW.auth_operation_id,NEW.auth_generation,NEW.auth_domain,
                       NEW.auth_decision,NEW.auth_reason_code,NEW.auth_recorded_at) <> 0 THEN
            RAISE EXCEPTION 'auth fields belong only to auth events' USING ERRCODE='check_violation';
        END IF;
        RETURN NEW;
    END IF;
    e := NEW.line::jsonb; b := e->'body';
    fields := ARRAY['user_id','operation_id','generation','reason_code','recorded_at'];
    domain := 'credential'; decision := 'changed';
    IF NEW.kind = 'account_status_decision' THEN
        fields := array_append(fields, 'decision'); domain := 'status'; decision := b->>'decision';
    END IF;
    IF jsonb_typeof(e) IS DISTINCT FROM 'object' OR jsonb_typeof(b) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'invalid auth envelope' USING ERRCODE='check_violation';
    END IF;
    IF (SELECT count(*) FROM jsonb_object_keys(e)) <> 5 OR NOT e ?& ARRAY['body','hash','kind','prev','seq']
       OR (SELECT count(*) FROM jsonb_object_keys(b)) <> cardinality(fields) OR NOT b ?& fields
       OR EXISTS (SELECT 1 FROM jsonb_each(b) v WHERE jsonb_typeof(v.value) IS DISTINCT FROM 'string')
       OR jsonb_typeof(e->'kind') IS DISTINCT FROM 'string' OR jsonb_typeof(e->'hash') IS DISTINCT FROM 'string'
       OR jsonb_typeof(e->'prev') IS DISTINCT FROM 'string' OR jsonb_typeof(e->'seq') IS DISTINCT FROM 'number'
       OR COALESCE(e->>'prev' ~ '^[0-9a-f]{64}$',false) = false
       OR (e->>'seq') IS DISTINCT FROM NEW.seq::text OR (e->>'hash') IS DISTINCT FROM NEW.record_hash
       OR (e->>'kind') IS DISTINCT FROM NEW.kind
       OR COALESCE(b->>'user_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',false) = false
       OR COALESCE(b->>'operation_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',false) = false
       OR COALESCE(b->>'generation' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',false) = false
       OR COALESCE(b->>'recorded_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$',false) = false THEN
        RAISE EXCEPTION 'invalid auth fields' USING ERRCODE='check_violation';
    END IF;
    stamp := (b->>'recorded_at')::timestamptz;
    IF NOT isfinite(stamp) OR substring(b->>'recorded_at',12,2)::int > 23
       OR substring(b->>'recorded_at',15,2)::int > 59 OR substring(b->>'recorded_at',18,2)::int > 59
       OR ROW(NEW.auth_user_id,NEW.auth_operation_id,NEW.auth_generation,NEW.auth_domain,NEW.auth_decision,NEW.auth_reason_code,NEW.auth_recorded_at)
          IS DISTINCT FROM ROW((b->>'user_id')::uuid,(b->>'operation_id')::uuid,(b->>'generation')::uuid,domain,decision,b->>'reason_code',stamp)
       OR NEW.auth_operation_id = NEW.auth_generation
       OR COALESCE((domain='credential' AND b->>'reason_code' IN ('bootstrap','invitation_accept','password_reset','verified_recovery'))
          OR (domain='status' AND decision='active' AND b->>'reason_code' IN ('bootstrap','invitation_accept','verified_initial_status','admin_enable'))
          OR (domain='status' AND decision='disabled' AND b->>'reason_code' IN ('admin_disable','legacy_disabled')),false) = false THEN
        RAISE EXCEPTION 'auth binding or decision mismatch' USING ERRCODE='check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM revocation_journal_records j WHERE
          j.auth_operation_id IN (NEW.auth_operation_id,NEW.auth_generation)
          OR j.auth_generation IN (NEW.auth_operation_id,NEW.auth_generation)) THEN
        RAISE EXCEPTION 'auth UUID already used' USING ERRCODE='unique_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_auth_record_valid BEFORE INSERT ON revocation_journal_records FOR EACH ROW EXECUTE FUNCTION ras_auth_record_valid();

-- Generic comparison covers every new typed column as well as the previous kinds.
CREATE OR REPLACE FUNCTION ras_revocation_journal_records_confirm_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='UPDATE' AND NOT OLD.journal_confirmed AND NEW.journal_confirmed
       AND (to_jsonb(NEW)-'journal_confirmed') IS NOT DISTINCT FROM (to_jsonb(OLD)-'journal_confirmed') THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'journal is append-only' USING ERRCODE='check_violation';
END $$;

CREATE FUNCTION ras_auth_receipt_final() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'receipt is append-only' USING ERRCODE='check_violation'; END IF;
    NEW.transaction_id := txid_current();
    IF NOT EXISTS (SELECT 1 FROM revocation_journal_records j WHERE
        ROW(j.auth_operation_id,j.auth_user_id,j.auth_domain,j.auth_generation,j.auth_decision,j.auth_reason_code,j.auth_recorded_at)
        IS NOT DISTINCT FROM ROW(NEW.operation_id,NEW.user_id,NEW.domain,NEW.generation,NEW.decision,NEW.reason_code,NEW.recorded_at)) THEN
        RAISE EXCEPTION 'receipt needs its exact event' USING ERRCODE='check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_auth_receipt_final BEFORE INSERT OR UPDATE OR DELETE ON auth_change_receipts FOR EACH ROW EXECUTE FUNCTION ras_auth_receipt_final();
CREATE FUNCTION ras_auth_receipt_binding() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM users u WHERE u.id=NEW.user_id AND
         ((NEW.domain='credential' AND ROW(u.credential_operation_id,u.credential_generation,u.credential_reason_code,u.password_changed_at)
            IS NOT DISTINCT FROM ROW(NEW.operation_id,NEW.generation,NEW.reason_code,NEW.recorded_at)) OR
          (NEW.domain='status' AND ROW(u.status_operation_id,u.status_generation,u.status_reason_code,u.status,u.updated_at)
            IS NOT DISTINCT FROM ROW(NEW.operation_id,NEW.generation,NEW.reason_code,NEW.decision,NEW.recorded_at)))) THEN
        RAISE EXCEPTION 'receipt needs its same-transaction mutation' USING ERRCODE='check_violation';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_auth_receipt_binding AFTER INSERT ON auth_change_receipts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ras_auth_receipt_binding();

CREATE FUNCTION ras_auth_head_final() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE c revocation_journal_records; s revocation_journal_records;
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'auth projection cannot be deleted' USING ERRCODE='check_violation'; END IF;
    SELECT * INTO c FROM revocation_journal_records WHERE auth_user_id=NEW.user_id AND auth_domain='credential' ORDER BY seq DESC LIMIT 1;
    SELECT * INTO s FROM revocation_journal_records WHERE auth_user_id=NEW.user_id AND auth_domain='status' ORDER BY seq DESC LIMIT 1;
    IF (c.seq IS NULL AND s.seq IS NULL)
       OR ROW(NEW.credential_seq,NEW.credential_operation_id,NEW.credential_generation,NEW.credential_reason_code,NEW.credential_recorded_at)
          IS DISTINCT FROM ROW(c.seq,c.auth_operation_id,c.auth_generation,c.auth_reason_code,c.auth_recorded_at)
       OR ROW(NEW.status_seq,NEW.status_operation_id,NEW.status_generation,NEW.status_decision,NEW.status_reason_code,NEW.status_recorded_at)
          IS DISTINCT FROM ROW(s.seq,s.auth_operation_id,s.auth_generation,s.auth_decision,s.auth_reason_code,s.auth_recorded_at)
       OR NEW.projection_head_seq IS DISTINCT FROM greatest(c.seq,s.seq)
       OR (TG_OP='UPDATE' AND (NEW.user_id <> OLD.user_id OR NEW.projection_head_seq < OLD.projection_head_seq)) THEN
        RAISE EXCEPTION 'auth projection must match latest events' USING ERRCODE='check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_auth_head_final BEFORE INSERT OR UPDATE OR DELETE ON auth_safety_heads FOR EACH ROW EXECUTE FUNCTION ras_auth_head_final();

CREATE FUNCTION ras_auth_user_final() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        IF EXISTS (SELECT 1 FROM revocation_journal_records WHERE auth_user_id=OLD.id) THEN
            RAISE EXCEPTION 'auth User history is final' USING ERRCODE='check_violation'; END IF;
        RETURN OLD;
    END IF;
    IF TG_OP='INSERT' AND EXISTS (SELECT 1 FROM revocation_journal_records WHERE auth_user_id=NEW.id) THEN
        RAISE EXCEPTION 'cannot recreate a journaled User' USING ERRCODE='check_violation';
    END IF;
    IF TG_OP='UPDATE' AND NEW.id <> OLD.id THEN RAISE EXCEPTION 'User UUID is final' USING ERRCODE='check_violation'; END IF;
    IF TG_OP='UPDATE' AND NEW.password_verifier IS DISTINCT FROM OLD.password_verifier
       AND NEW.credential_generation IS NOT DISTINCT FROM OLD.credential_generation
       AND (NEW.password_hash_policy_version <= OLD.password_hash_policy_version
            OR NEW.password_changed_at IS DISTINCT FROM OLD.password_changed_at
            OR NEW.credential_state <> 'usable' OR OLD.credential_state <> 'usable') THEN
        RAISE EXCEPTION 'verifier change needs credential transition or policy CAS rehash' USING ERRCODE='check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_auth_user_final BEFORE INSERT OR UPDATE OR DELETE ON users FOR EACH ROW EXECUTE FUNCTION ras_auth_user_final();

CREATE FUNCTION ras_auth_user_binding() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE u users; h auth_safety_heads; r auth_change_receipts;
BEGIN
    SELECT * INTO u FROM users WHERE id=NEW.id;
    SELECT * INTO h FROM auth_safety_heads WHERE user_id=NEW.id;
    -- Deferred triggers see final row state, not intermediate writes.
    IF u.credential_state='usable' THEN
        SELECT * INTO r FROM auth_change_receipts WHERE operation_id=u.credential_operation_id;
        IF r.operation_id IS NULL OR ROW(u.id,'credential'::text,u.credential_generation,u.credential_reason_code,u.password_changed_at)
             IS DISTINCT FROM ROW(r.user_id,r.domain,r.generation,r.reason_code,r.recorded_at)
           OR ROW(u.credential_operation_id,u.credential_generation) IS DISTINCT FROM ROW(h.credential_operation_id,h.credential_generation) THEN
            RAISE EXCEPTION 'usable credential needs latest receipt and head' USING ERRCODE='check_violation'; END IF;
    END IF;
    IF u.status_safety='proven' THEN
        IF ROW(u.status_generation,u.status) IS DISTINCT FROM ROW(h.status_generation,h.status_decision) THEN
            RAISE EXCEPTION 'proven status needs latest head' USING ERRCODE='check_violation'; END IF;
        IF u.status='active' OR u.status_operation_id IS NOT NULL THEN
            SELECT * INTO r FROM auth_change_receipts WHERE operation_id=u.status_operation_id;
            IF r.operation_id IS NULL OR ROW(u.id,'status'::text,u.status_generation,u.status,u.status_reason_code)
               IS DISTINCT FROM ROW(r.user_id,r.domain,r.generation,r.decision,r.reason_code)
               OR u.status_operation_id IS DISTINCT FROM h.status_operation_id THEN
                RAISE EXCEPTION 'active status needs latest receipt' USING ERRCODE='check_violation'; END IF;
        END IF;
    END IF;
    IF TG_OP='UPDATE' THEN
        IF (NEW.credential_generation IS DISTINCT FROM OLD.credential_generation OR
            (OLD.credential_state='quarantined' AND NEW.credential_state='usable')) AND NOT EXISTS (
            SELECT 1 FROM auth_change_receipts WHERE operation_id=u.credential_operation_id AND transaction_id=txid_current()) THEN
            RAISE EXCEPTION 'credential release needs a new receipt' USING ERRCODE='check_violation'; END IF;
        IF ((NEW.status_safety='proven' AND OLD.status_safety='held' AND NEW.status='active')
             OR (OLD.status <> NEW.status AND NEW.status='active')) AND NOT EXISTS (
            SELECT 1 FROM auth_change_receipts WHERE operation_id=u.status_operation_id AND transaction_id=txid_current()) THEN
            RAISE EXCEPTION 'active release needs a new receipt' USING ERRCODE='check_violation'; END IF;
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_auth_user_binding AFTER INSERT OR UPDATE ON users DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ras_auth_user_binding();

CREATE FUNCTION ras_auth_progress_final() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF TG_OP='DELETE' OR NEW.projection_seq < OLD.projection_seq OR (OLD.backfill_complete AND NOT NEW.backfill_complete)
       OR ROW(NEW.id,NEW.version,NEW.cutover_state) IS DISTINCT FROM ROW(OLD.id,OLD.version,OLD.cutover_state) THEN
        RAISE EXCEPTION 'auth safety progress is monotonic and pending' USING ERRCODE='check_violation'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_auth_progress_final BEFORE UPDATE OR DELETE ON auth_safety_state FOR EACH ROW EXECUTE FUNCTION ras_auth_progress_final();

CREATE FUNCTION ras_auth_session_fence() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
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
CREATE TRIGGER trg_auth_session_fence BEFORE INSERT OR UPDATE ON sessions FOR EACH ROW EXECUTE FUNCTION ras_auth_session_fence();
CREATE FUNCTION ras_auth_token_fence() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
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
CREATE TRIGGER trg_auth_token_fence BEFORE INSERT OR UPDATE ON password_reset_tokens FOR EACH ROW EXECUTE FUNCTION ras_auth_token_fence();

CREATE FUNCTION ras_auth_mirror(p_line text,p_via text,p_result text,p_confirmed boolean) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE e jsonb; b jsonb; k text;
BEGIN
    BEGIN e:=p_line::jsonb; EXCEPTION WHEN OTHERS THEN RAISE EXCEPTION 'invalid auth JSON' USING ERRCODE='check_violation'; END;
    b:=e->'body'; k:=e->>'kind';
    IF k NOT IN ('credential_transition','account_status_decision') THEN RAISE EXCEPTION 'not an auth event'; END IF;
    -- Reject untrusted scalar values before casts can echo them in a PostgreSQL error.
    IF jsonb_typeof(b) IS DISTINCT FROM 'object'
       OR COALESCE(b->>'user_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',false)=false
       OR COALESCE(b->>'operation_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',false)=false
       OR COALESCE(b->>'generation' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',false)=false
       OR COALESCE(b->>'recorded_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$',false)=false THEN
        RAISE EXCEPTION 'invalid auth scalar fields' USING ERRCODE='check_violation';
    END IF;
    INSERT INTO revocation_journal_records(seq,kind,record_hash,line,applied_via,result,recorded_at,journal_confirmed,
        auth_user_id,auth_operation_id,auth_generation,auth_domain,auth_decision,auth_reason_code,auth_recorded_at)
    VALUES ((e->>'seq')::bigint,k,e->>'hash',p_line,p_via,p_result,(b->>'recorded_at')::timestamptz,p_confirmed,
        (b->>'user_id')::uuid,(b->>'operation_id')::uuid,(b->>'generation')::uuid,
        CASE WHEN k='credential_transition' THEN 'credential' ELSE 'status' END,
        CASE WHEN k='credential_transition' THEN 'changed' ELSE b->>'decision' END,b->>'reason_code',(b->>'recorded_at')::timestamptz);
END $$;
CREATE FUNCTION ras_auth_confirm(p_seq bigint) RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
    UPDATE revocation_journal_records SET journal_confirmed=true WHERE seq=p_seq AND NOT journal_confirmed;
$$;
CREATE FUNCTION ras_auth_project(p_user uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE c revocation_journal_records; s revocation_journal_records;
BEGIN
    SELECT * INTO c FROM revocation_journal_records WHERE auth_user_id=p_user AND auth_domain='credential' ORDER BY seq DESC LIMIT 1;
    SELECT * INTO s FROM revocation_journal_records WHERE auth_user_id=p_user AND auth_domain='status' ORDER BY seq DESC LIMIT 1;
    IF c.seq IS NULL AND s.seq IS NULL THEN RETURN; END IF;
    INSERT INTO auth_safety_heads VALUES(p_user,greatest(c.seq,s.seq),c.seq,c.auth_operation_id,c.auth_generation,
        c.auth_reason_code,c.auth_recorded_at,s.seq,s.auth_operation_id,s.auth_generation,s.auth_reason_code,s.auth_recorded_at,s.auth_decision)
    ON CONFLICT(user_id) DO UPDATE SET projection_head_seq=excluded.projection_head_seq,
        credential_seq=excluded.credential_seq,credential_operation_id=excluded.credential_operation_id,
        credential_generation=excluded.credential_generation,credential_reason_code=excluded.credential_reason_code,
        credential_recorded_at=excluded.credential_recorded_at,status_seq=excluded.status_seq,
        status_operation_id=excluded.status_operation_id,status_generation=excluded.status_generation,
        status_reason_code=excluded.status_reason_code,status_recorded_at=excluded.status_recorded_at,status_decision=excluded.status_decision;
END $$;

CREATE FUNCTION ras_auth_phc_valid(p_phc text,p_version integer) RETURNS boolean
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE parts text[]; policy auth_hash_policies;
BEGIN
    SELECT * INTO policy FROM auth_hash_policies WHERE version=p_version;
    parts := regexp_match(p_phc,'^\$argon2id\$v=19\$m=([0-9]+),t=([0-9]+),p=([0-9]+)\$([A-Za-z0-9+/]+)\$([A-Za-z0-9+/]+)$');
    IF policy.version IS NULL OR parts IS NULL THEN RETURN false; END IF;
    RETURN (parts[1]::integer,parts[2]::integer,parts[3]::integer) = (policy.memory_kib,policy.time_cost,policy.parallelism)
       AND octet_length(decode(parts[4] || repeat('=',(4-length(parts[4])%4)%4),'base64'))=policy.salt_bytes
       AND octet_length(decode(parts[5] || repeat('=',(4-length(parts[5])%4)%4),'base64'))=policy.hash_bytes;
EXCEPTION WHEN OTHERS THEN RETURN false;
END $$;

-- These routines are the trusted identity boundary. Neither a caller reason nor a GUC bypasses guards.
CREATE FUNCTION ras_auth_change(p_user uuid,p_op uuid,p_verifier text,p_policy integer) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j revocation_journal_records;
BEGIN
    SELECT * INTO j FROM revocation_journal_records WHERE auth_operation_id=p_op AND auth_user_id=p_user;
    IF j.seq IS NULL OR NOT EXISTS (SELECT 1 FROM auth_safety_heads h WHERE h.user_id=p_user AND
        ((j.auth_domain='credential' AND h.credential_operation_id=p_op) OR (j.auth_domain='status' AND h.status_operation_id=p_op))) THEN
        RAISE EXCEPTION 'mutation needs latest event and projection' USING ERRCODE='check_violation'; END IF;
    INSERT INTO auth_change_receipts(operation_id,user_id,domain,generation,decision,reason_code,recorded_at)
        VALUES(p_op,p_user,j.auth_domain,j.auth_generation,j.auth_decision,j.auth_reason_code,j.auth_recorded_at);
    IF j.auth_domain='credential' THEN
        IF NOT ras_auth_phc_valid(p_verifier,p_policy) THEN
            RAISE EXCEPTION 'invalid credential PHC/policy' USING ERRCODE='check_violation'; END IF;
        UPDATE users SET credential_generation=j.auth_generation,credential_operation_id=p_op,credential_state='usable',
            credential_reason_code=j.auth_reason_code,password_verifier=p_verifier,password_hash_policy_version=p_policy,
            password_changed_at=j.auth_recorded_at,updated_at=j.auth_recorded_at WHERE id=p_user;
    ELSE
        IF p_verifier IS NOT NULL OR p_policy IS NOT NULL THEN RAISE EXCEPTION 'status has no credential payload'; END IF;
        UPDATE users SET status_generation=j.auth_generation,status_operation_id=p_op,status=j.auth_decision,
            status_safety='proven',status_reason_code=j.auth_reason_code,updated_at=j.auth_recorded_at WHERE id=p_user;
    END IF;
    IF NOT FOUND THEN RAISE EXCEPTION 'User absent' USING ERRCODE='check_violation'; END IF;
END $$;
CREATE FUNCTION ras_auth_rehash(p_user uuid,p_old text,p_generation uuid,p_operation uuid,p_new text,p_policy integer,p_at timestamptz) RETURNS boolean
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
CREATE FUNCTION ras_auth_restrict(p_user uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE u users; h auth_safety_heads;
BEGIN
    SELECT * INTO u FROM users WHERE id=p_user;
    IF u.id IS NULL THEN RETURN; END IF;
    SELECT * INTO h FROM auth_safety_heads WHERE user_id=p_user;
    UPDATE users SET credential_state='quarantined',credential_reason_code='journal_rollback'
      WHERE id=p_user AND (h.credential_operation_id IS NULL OR
         ROW(credential_operation_id,credential_generation) IS DISTINCT FROM ROW(h.credential_operation_id,h.credential_generation));
    IF h.status_decision='disabled' THEN
        UPDATE users SET status='disabled',status_generation=h.status_generation,status_safety='proven',
            status_operation_id=CASE WHEN status_operation_id=h.status_operation_id THEN status_operation_id ELSE NULL END,
            status_reason_code=h.status_reason_code WHERE id=p_user;
    ELSIF h.status_operation_id IS NULL OR ROW(u.status_operation_id,u.status_generation,u.status)
           IS DISTINCT FROM ROW(h.status_operation_id,h.status_generation,h.status_decision) THEN
        UPDATE users SET status_safety='held',status_reason_code='journal_rollback' WHERE id=p_user;
    END IF;
END $$;
CREATE FUNCTION ras_auth_progress(p_seq bigint) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF p_seq IS DISTINCT FROM (SELECT max(seq) FROM revocation_journal_records) OR EXISTS (
       SELECT 1 FROM sessions WHERE credential_generation IS NULL AND revoked_at IS NULL) OR EXISTS (
       SELECT 1 FROM password_reset_tokens WHERE credential_fence_operation_id IS NULL AND used_at IS NULL AND revoked_at IS NULL) THEN
        RAISE EXCEPTION 'auth backfill incomplete' USING ERRCODE='check_violation'; END IF;
    UPDATE auth_safety_state SET projection_seq=p_seq,backfill_complete=true WHERE id=1;
END $$;

CREATE FUNCTION ras_auth_session_create(p_id uuid,p_user uuid,p_generation uuid,p_token text,p_csrf text,p_at timestamptz,p_expires timestamptz)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF p_generation IS NULL THEN RAISE EXCEPTION 'new session requires generation'; END IF;
    INSERT INTO sessions(id,user_id,credential_generation,token_hash,csrf_token_hash,created_at,last_activity_at,absolute_expires_at)
    VALUES(p_id,p_user,p_generation,p_token,p_csrf,p_at,p_at,p_expires);
END $$;
CREATE FUNCTION ras_auth_token_create(p_id uuid,p_user uuid,p_fence uuid,p_purpose text,p_token text,p_at timestamptz,p_expires timestamptz)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF p_fence IS NULL THEN RAISE EXCEPTION 'new reset token requires fence'; END IF;
    INSERT INTO password_reset_tokens(id,user_id,credential_fence_operation_id,recovery_purpose,token_hash,created_at,expires_at)
    VALUES(p_id,p_user,p_fence,p_purpose,p_token,p_at,p_expires);
END $$;

CREATE FUNCTION ras_auth_lock_users(p_users uuid[]) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'
        AND classid=(32195295680032364::bigint >> 32)::oid
        AND objid=(32195295680032364::bigint & 4294967295)::oid) THEN
        RAISE EXCEPTION 'User locks must precede journal lock' USING ERRCODE='check_violation';
    END IF;
    PERFORM id FROM users WHERE id=ANY(p_users) ORDER BY id FOR NO KEY UPDATE;
END $$;

-- Every SECURITY DEFINER routine is private until explicitly granted by the provisioning CLI.
DO $$ DECLARE f record; BEGIN
    FOR f IN SELECT oid::regprocedure signature FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname LIKE 'ras_auth_%'
    LOOP EXECUTE 'REVOKE ALL ON FUNCTION ' || f.signature || ' FROM PUBLIC'; END LOOP;
END $$;
