-- Frozen with revision 0049 (C3). Operator logins are mapped/granted only by trusted provisioning.
-- Identity comes from session_user (the real login), never from a caller UUID, role name or GUC.

CREATE FUNCTION ras_auth_recovery_operator_final() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'operator mapping is final' USING ERRCODE='check_violation'; END IF;
 IF ROW(NEW.operator_id,NEW.login_role,NEW.provisioned_at) IS DISTINCT FROM ROW(OLD.operator_id,OLD.login_role,OLD.provisioned_at)
  OR OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR NOT isfinite(NEW.revoked_at) THEN
  RAISE EXCEPTION 'operator mapping can only be revoked once' USING ERRCODE='check_violation'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_auth_recovery_operator_final BEFORE UPDATE OR DELETE ON auth_recovery_operators
 FOR EACH ROW EXECUTE FUNCTION ras_auth_recovery_operator_final();

CREATE FUNCTION ras_auth_recovery_case_final() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 RAISE EXCEPTION 'registered recovery case is final' USING ERRCODE='check_violation';
END $$;
CREATE TRIGGER trg_auth_recovery_case_final BEFORE UPDATE OR DELETE ON auth_recovery_cases
 FOR EACH ROW EXECUTE FUNCTION ras_auth_recovery_case_final();

-- Independent of the Python parser: exact envelope/body, typed binding, live use only by the case operator.
CREATE FUNCTION ras_auth_case_record_valid() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE e jsonb; b jsonb; stamp timestamptz;
BEGIN
 IF NEW.kind IS DISTINCT FROM 'operator_recovery_case' THEN RETURN NEW; END IF;
 e:=NEW.line::jsonb; b:=e->'body';
 IF jsonb_typeof(e) IS DISTINCT FROM 'object' OR jsonb_typeof(b) IS DISTINCT FROM 'object'
  OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(e) k) IS DISTINCT FROM ARRAY['body','hash','kind','prev','seq']::text[]
  OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(b) k)
     IS DISTINCT FROM ARRAY['action','case_id','operation_id','operator_id','recorded_at','user_id']::text[]
  OR EXISTS (SELECT 1 FROM jsonb_each(b) v WHERE jsonb_typeof(v.value) IS DISTINCT FROM 'string')
  OR jsonb_typeof(e->'seq') IS DISTINCT FROM 'number' OR (e->>'seq') IS DISTINCT FROM NEW.seq::text
  OR jsonb_typeof(e->'hash') IS DISTINCT FROM 'string' OR (e->>'hash') IS DISTINCT FROM NEW.record_hash
  OR (e->>'kind') IS DISTINCT FROM NEW.kind OR COALESCE((e->>'prev') ~ '^[0-9a-f]{64}$',false)=false
  OR COALESCE(b->>'case_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',false)=false
  OR COALESCE(b->>'operation_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',false)=false
  OR COALESCE(b->>'operator_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',false)=false
  OR COALESCE(b->>'user_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',false)=false
  OR COALESCE(b->>'action' IN ('set_credential','resolve_initial_status'),false)=false
  OR COALESCE(b->>'recorded_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]([.][0-9]{1,6})?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$',false)=false THEN
  RAISE EXCEPTION 'invalid operator case record' USING ERRCODE='check_violation'; END IF;
 stamp:=(b->>'recorded_at')::timestamptz;
 IF NOT isfinite(stamp)
  OR ROW(NEW.case_id,NEW.case_operator_id,NEW.case_user_id,NEW.case_action,NEW.case_operation_id,NEW.case_recorded_at)
     IS DISTINCT FROM ROW((b->>'case_id')::uuid,(b->>'operator_id')::uuid,(b->>'user_id')::uuid,b->>'action',
                          (b->>'operation_id')::uuid,stamp) THEN
  RAISE EXCEPTION 'operator case binding mismatch' USING ERRCODE='check_violation'; END IF;
 IF NEW.applied_via IS DISTINCT FROM 'replay' THEN
  -- A live use: unconfirmed, by the mapped login of THIS case/installation, inside its window.
  IF NEW.applied_via IS DISTINCT FROM 'close' OR NEW.journal_confirmed IS DISTINCT FROM false OR NOT EXISTS (
    SELECT 1 FROM auth_recovery_operators o JOIN auth_recovery_cases c ON c.operator_id=o.operator_id
    JOIN recovery_registrations r ON r.id=1 AND r.journal_id=c.journal_id
    WHERE o.login_role=session_user::text AND o.revoked_at IS NULL AND o.operator_id=NEW.case_operator_id
     AND c.case_id=NEW.case_id AND c.user_id=NEW.case_user_id AND c.action=NEW.case_action
     AND c.approved_at<=stamp AND stamp<c.expires_at) THEN
   RAISE EXCEPTION 'operator case not authorised for this login' USING ERRCODE='insufficient_privilege'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_auth_case_record_valid BEFORE INSERT ON revocation_journal_records
 FOR EACH ROW EXECUTE FUNCTION ras_auth_case_record_valid();

-- Operator-only reasons: no generic runtime/recovery/owner path can mint them without a live case use
-- of THIS transaction by the mapped login. Historical receipts are untouched.
CREATE FUNCTION ras_auth_receipt_operator_case() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE x xid:=(txid_current() % 4294967296)::text::xid;
BEGIN
 IF NEW.reason_code IS DISTINCT FROM 'verified_recovery' AND NEW.reason_code IS DISTINCT FROM 'verified_initial_status' THEN
  RETURN NEW; END IF;
 IF NOT EXISTS (SELECT 1 FROM revocation_journal_records j JOIN auth_recovery_operators o ON o.operator_id=j.case_operator_id
   WHERE j.kind='operator_recovery_case' AND j.case_operation_id=NEW.operation_id AND j.case_user_id=NEW.user_id
    AND j.case_recorded_at=NEW.recorded_at AND j.applied_via='close' AND NOT j.journal_confirmed AND j.xmin=x
    AND o.login_role=session_user::text AND o.revoked_at IS NULL
    AND ((j.case_action='set_credential' AND NEW.domain='credential' AND NEW.reason_code='verified_recovery')
      OR (j.case_action='resolve_initial_status' AND NEW.domain='status' AND NEW.decision='active'
          AND NEW.reason_code='verified_initial_status'))) THEN
  RAISE EXCEPTION 'operator recovery receipt needs its case use' USING ERRCODE='insufficient_privilege'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_auth_receipt_operator_case BEFORE INSERT ON auth_change_receipts
 FOR EACH ROW EXECUTE FUNCTION ras_auth_receipt_operator_case();

-- Positive login evidence; a probe failure or an unmapped/privileged/member login is refused.
CREATE FUNCTION ras_auth_operator_assert() RETURNS uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE v uuid;
BEGIN
 SELECT o.operator_id INTO v FROM auth_recovery_operators o JOIN pg_roles r ON r.rolname=o.login_role
  WHERE o.login_role=session_user::text AND o.revoked_at IS NULL
   AND r.rolcanlogin AND NOT r.rolsuper AND NOT r.rolcreaterole AND NOT r.rolcreatedb
   AND NOT r.rolbypassrls AND NOT r.rolreplication
   AND NOT EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid OR m.roleid=r.oid)
   AND NOT has_table_privilege(r.oid,'users','SELECT,INSERT,UPDATE,DELETE')
   AND NOT has_table_privilege(r.oid,'revocation_journal_records','SELECT,INSERT,UPDATE,DELETE')
   AND NOT has_table_privilege(r.oid,'auth_recovery_cases','SELECT,INSERT,UPDATE,DELETE')
   AND NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.relowner=r.oid)
   AND NOT EXISTS (SELECT 1 FROM pg_proc p WHERE p.proowner=r.oid)
   AND NOT EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspowner=r.oid);
 IF v IS NULL THEN RAISE EXCEPTION 'operator authority required' USING ERRCODE='insufficient_privilege'; END IF;
 RETURN v;
END $$;

CREATE FUNCTION ras_auth_operator_whoami() RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT ras_auth_operator_assert()
$$;

-- Same view as the recovery gate (app.recovery._db_view) plus reconciliation progress.
CREATE FUNCTION ras_auth_operator_gate() RETURNS TABLE(journal_id uuid, genesis_hash text, record_count bigint,
 head_seq bigint, head_hash text, unconfirmed bigint, unjournaled_closures bigint, unjournaled_sessions bigint,
 unjournaled_tokens bigint, unjournaled_invitations bigint, backfill_complete boolean, projection_seq bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
#variable_conflict use_column
BEGIN
 PERFORM ras_auth_operator_assert();
 RETURN QUERY SELECT r.journal_id, r.genesis_hash, (SELECT count(*) FROM revocation_journal_records), h.seq, h.record_hash,
  (SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed),
  (SELECT count(*) FROM review_requests rr WHERE rr.status<>'open' AND NOT EXISTS
     (SELECT 1 FROM revocation_journal_records j WHERE j.review_request_id=rr.id)),
  (SELECT count(*) FROM sessions s WHERE s.revoked_at IS NOT NULL AND NOT EXISTS
     (SELECT 1 FROM revocation_journal_records j WHERE j.session_id=s.id)),
  (SELECT count(*) FROM password_reset_tokens t WHERE (t.used_at IS NOT NULL OR t.revoked_at IS NOT NULL) AND NOT EXISTS
     (SELECT 1 FROM revocation_journal_records j WHERE j.reset_token_id=t.id)),
  (SELECT count(*) FROM invitations i WHERE num_nonnulls(i.accepted_at,i.revoked_at,i.closed_by_recovery_at)>0 AND NOT EXISTS
     (SELECT 1 FROM revocation_journal_records j WHERE j.invitation_id=i.id)),
  st.backfill_complete, st.projection_seq
 FROM (SELECT 1) one
 LEFT JOIN recovery_registrations r ON r.id=1
 LEFT JOIN LATERAL (SELECT seq,record_hash FROM revocation_journal_records ORDER BY seq DESC LIMIT 1) h ON true
 LEFT JOIN auth_safety_state st ON st.id=1;
END $$;

-- IDs/codes/flags only: no email, verifier, token or session secret.
CREATE FUNCTION ras_auth_operator_inspect(p_user uuid) RETURNS TABLE(present boolean, role text, status text,
 status_safety text, credential_state text, credential_operation_id uuid, credential_generation uuid,
 status_operation_id uuid, status_generation uuid, head_credential_seq bigint, head_credential_operation_id uuid,
 head_status_seq bigint, head_status_operation_id uuid, head_status_decision text, credential_receipt boolean,
 status_live boolean, account_live boolean, initial_status_verified boolean, open_sessions bigint, open_tokens bigint,
 bootstrap_admin text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
#variable_conflict use_column
BEGIN
 PERFORM ras_auth_operator_assert();
 RETURN QUERY SELECT u.id IS NOT NULL, u.role, u.status, u.status_safety, u.credential_state,
  u.credential_operation_id, u.credential_generation, u.status_operation_id, u.status_generation,
  h.credential_seq, h.credential_operation_id, h.status_seq, h.status_operation_id, h.status_decision,
  COALESCE(u.credential_state='usable' AND EXISTS (SELECT 1 FROM auth_change_receipts c
    JOIN revocation_journal_records cj ON cj.seq=h.credential_seq AND cj.auth_user_id=u.id
     AND cj.auth_domain='credential' AND cj.journal_confirmed
    WHERE c.operation_id=u.credential_operation_id
     AND ROW(c.user_id,c.domain,c.generation,c.decision,c.reason_code,c.recorded_at) IS NOT DISTINCT FROM
         ROW(u.id,'credential'::text,h.credential_generation,'changed'::text,h.credential_reason_code,h.credential_recorded_at)
     AND ROW(u.credential_operation_id,u.credential_generation,u.credential_reason_code,u.password_changed_at)
         IS NOT DISTINCT FROM ROW(h.credential_operation_id,h.credential_generation,h.credential_reason_code,h.credential_recorded_at)
     AND NOT EXISTS (SELECT 1 FROM revocation_journal_records n WHERE n.auth_user_id=u.id
       AND n.auth_domain='credential' AND n.seq>h.credential_seq)),false),
  COALESCE(ras_auth_status_live(u.id),false), COALESCE(ras_auth_account_live(u.id),false),
  COALESCE(EXISTS (SELECT 1 FROM auth_change_receipts r JOIN revocation_journal_records j
    ON j.auth_operation_id=r.operation_id AND j.auth_user_id=r.user_id AND j.auth_domain=r.domain
     AND j.auth_generation=r.generation AND j.auth_decision=r.decision AND j.auth_reason_code=r.reason_code
     AND j.auth_recorded_at=r.recorded_at
    WHERE r.user_id=u.id AND r.domain='status' AND r.decision='active'
     AND r.reason_code IN ('bootstrap','invitation_accept','verified_initial_status')
     AND j.journal_confirmed AND j.seq<=h.status_seq),false),
  (SELECT count(*) FROM sessions s WHERE s.user_id=p_user AND s.revoked_at IS NULL),
  (SELECT count(*) FROM password_reset_tokens t WHERE t.user_id=p_user AND t.used_at IS NULL AND t.revoked_at IS NULL),
  CASE WHEN NOT EXISTS (SELECT 1 FROM bootstrap_state) THEN 'absent'
       WHEN EXISTS (SELECT 1 FROM bootstrap_state b WHERE b.admin_user_id=p_user) THEN 'yes' ELSE 'other' END
 FROM (SELECT 1) one LEFT JOIN users u ON u.id=p_user LEFT JOIN auth_safety_heads h ON h.user_id=p_user;
END $$;

CREATE FUNCTION ras_auth_operator_case(p_case uuid) RETURNS SETOF auth_recovery_cases
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE me uuid:=ras_auth_operator_assert();
BEGIN
 RETURN QUERY SELECT * FROM auth_recovery_cases c WHERE c.case_id=p_case AND c.operator_id=me;
END $$;

-- Case-bound snapshot. The verifier is returned only for set_credential, to refuse the quarantined password
-- outside the DB. With p_lock: User NO KEY UPDATE -> open tokens -> open sessions (UUID order), before journal.
CREATE FUNCTION ras_auth_operator_snapshot(p_case uuid,p_at timestamptz,p_lock boolean) RETURNS TABLE(user_id uuid,
 role text, status text, status_safety text, credential_state text, credential_operation_id uuid,
 credential_generation uuid, status_operation_id uuid, status_generation uuid, password_verifier text,
 password_hash_policy_version integer, email_digest text, head_credential_operation_id uuid,
 head_status_operation_id uuid, head_status_decision text, status_live boolean, policy_version integer,
 memory_kib integer, time_cost integer, parallelism integer, salt_bytes integer, hash_bytes integer,
 token_ids uuid[], session_ids uuid[])
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
#variable_conflict use_column
DECLARE me uuid:=ras_auth_operator_assert(); c auth_recovery_cases;
BEGIN
 SELECT * INTO c FROM auth_recovery_cases k WHERE k.case_id=p_case AND k.operator_id=me;
 IF c.case_id IS NULL OR p_at IS NULL OR NOT isfinite(p_at) OR p_at<c.approved_at OR p_at>=c.expires_at
  OR NOT EXISTS (SELECT 1 FROM recovery_registrations r WHERE r.id=1 AND r.journal_id=c.journal_id)
  OR EXISTS (SELECT 1 FROM revocation_journal_records j WHERE j.case_id=p_case) THEN
  RAISE EXCEPTION 'operator case unavailable' USING ERRCODE='check_violation'; END IF;
 IF p_lock THEN
  IF EXISTS (SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'
     AND classid=(32195295680032364::bigint >> 32)::oid AND objid=(32195295680032364::bigint & 4294967295)::oid) THEN
   RAISE EXCEPTION 'User locks must precede journal lock' USING ERRCODE='check_violation'; END IF;
  PERFORM 1 FROM users x WHERE x.id=c.user_id FOR NO KEY UPDATE;
  PERFORM 1 FROM password_reset_tokens t WHERE t.user_id=c.user_id AND t.used_at IS NULL AND t.revoked_at IS NULL
   ORDER BY t.id FOR UPDATE;
  PERFORM 1 FROM sessions s WHERE s.user_id=c.user_id AND s.revoked_at IS NULL ORDER BY s.id FOR UPDATE;
 END IF;
 RETURN QUERY SELECT u.id, u.role, u.status, u.status_safety, u.credential_state, u.credential_operation_id,
  u.credential_generation, u.status_operation_id, u.status_generation,
  CASE WHEN c.action='set_credential' THEN u.password_verifier END, u.password_hash_policy_version,
  encode(sha256(convert_to(u.email,'UTF8')),'hex'), h.credential_operation_id, h.status_operation_id,
  h.status_decision, COALESCE(ras_auth_status_live(u.id),false), p.version, p.memory_kib, p.time_cost,
  p.parallelism, p.salt_bytes, p.hash_bytes,
  ARRAY(SELECT t.id FROM password_reset_tokens t WHERE t.user_id=u.id AND t.used_at IS NULL AND t.revoked_at IS NULL ORDER BY t.id),
  ARRAY(SELECT s.id FROM sessions s WHERE s.user_id=u.id AND s.revoked_at IS NULL ORDER BY s.id)
 FROM users u LEFT JOIN auth_safety_heads h ON h.user_id=u.id
 CROSS JOIN LATERAL (SELECT * FROM auth_hash_policies ORDER BY version DESC LIMIT 1) p
 WHERE u.id=c.user_id;
END $$;

-- Under the exclusive journal lock, after all row locks: case record -> auth event/receipt/mutation
-- -> token closures -> session closures -> audit, all mirrors false, deferred checks forced before append.
CREATE FUNCTION ras_auth_operator_stage(p_case uuid,p_at timestamptz,p_lines text[],p_verifier text,p_policy integer)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE me uuid:=ras_auth_operator_assert(); c auth_recovery_cases; u users; h auth_safety_heads;
 e jsonb; b jsonb; i integer; n integer; prev_seq bigint; prev_hash text; op uuid; k text; phase integer:=2;
 tokens integer:=0; closed_sessions integer:=0;
BEGIN
 SELECT * INTO c FROM auth_recovery_cases x WHERE x.case_id=p_case AND x.operator_id=me;
 IF c.case_id IS NULL OR p_at IS NULL OR NOT isfinite(p_at) OR p_at<c.approved_at OR p_at>=c.expires_at
  OR NOT EXISTS (SELECT 1 FROM recovery_registrations r WHERE r.id=1 AND r.journal_id=c.journal_id) THEN
  RAISE EXCEPTION 'operator case unavailable' USING ERRCODE='check_violation'; END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory' AND mode='ExclusiveLock'
   AND granted AND classid=(32195295680032364::bigint >> 32)::oid AND objid=(32195295680032364::bigint & 4294967295)::oid) THEN
  RAISE EXCEPTION 'operator stage needs the exclusive journal lock' USING ERRCODE='check_violation'; END IF;
 -- Already held by this transaction; NOWAIT never waits for a User after the journal lock.
 SELECT * INTO u FROM users x WHERE x.id=c.user_id FOR NO KEY UPDATE NOWAIT;
 IF u.id IS NULL THEN RAISE EXCEPTION 'operator target absent' USING ERRCODE='check_violation'; END IF;
 SELECT * INTO h FROM auth_safety_heads x WHERE x.user_id=c.user_id;
 IF u.role IS DISTINCT FROM c.expected_role
  OR h.credential_operation_id IS DISTINCT FROM c.expected_credential_operation_id
  OR h.status_operation_id IS DISTINCT FROM c.expected_status_operation_id THEN
  RAISE EXCEPTION 'operator case is stale' USING ERRCODE='check_violation'; END IF;
 IF c.action='resolve_initial_status' AND (u.status IS DISTINCT FROM 'active' OR u.status_safety IS DISTINCT FROM 'held'
   OR h.status_decision IS NOT DISTINCT FROM 'disabled' OR ras_auth_status_live(u.id)
   OR p_verifier IS NOT NULL OR p_policy IS NOT NULL) THEN
  RAISE EXCEPTION 'initial status decision not eligible' USING ERRCODE='check_violation'; END IF;
 IF c.action='set_credential' AND (p_verifier IS NULL OR p_policy IS NULL) THEN
  RAISE EXCEPTION 'credential payload required' USING ERRCODE='check_violation'; END IF;
 IF EXISTS (SELECT 1 FROM revocation_journal_records WHERE NOT journal_confirmed)
  OR EXISTS (SELECT 1 FROM revocation_journal_records j WHERE j.case_id=p_case) THEN
  RAISE EXCEPTION 'operator journal state not clean' USING ERRCODE='check_violation'; END IF;
 SELECT seq, record_hash INTO prev_seq, prev_hash FROM revocation_journal_records ORDER BY seq DESC LIMIT 1;
 n:=COALESCE(array_length(p_lines,1),0);
 IF n<2 OR prev_seq IS NULL THEN RAISE EXCEPTION 'operator batch incomplete' USING ERRCODE='check_violation'; END IF;
 FOR i IN 1..n LOOP
  e:=p_lines[i]::jsonb; b:=e->'body'; k:=e->>'kind';
  IF jsonb_typeof(e) IS DISTINCT FROM 'object' OR jsonb_typeof(b) IS DISTINCT FROM 'object'
   OR jsonb_typeof(e->'seq') IS DISTINCT FROM 'number' OR (e->>'seq') IS DISTINCT FROM (prev_seq+1)::text
   OR (e->>'prev') IS DISTINCT FROM prev_hash
   OR encode(sha256(convert_to(regexp_replace(p_lines[i],',"hash":"[0-9a-f]{64}"',''),'UTF8')),'hex')
      IS DISTINCT FROM (e->>'hash') THEN
   RAISE EXCEPTION 'operator batch chain invalid' USING ERRCODE='check_violation'; END IF;
  IF i=1 THEN
   IF k IS DISTINCT FROM 'operator_recovery_case' OR (b->>'case_id') IS DISTINCT FROM p_case::text
    OR (b->>'operator_id') IS DISTINCT FROM me::text OR (b->>'user_id') IS DISTINCT FROM c.user_id::text
    OR (b->>'action') IS DISTINCT FROM c.action OR (b->>'recorded_at')::timestamptz IS DISTINCT FROM p_at THEN
    RAISE EXCEPTION 'operator case record mismatch' USING ERRCODE='check_violation'; END IF;
   INSERT INTO revocation_journal_records(seq,kind,record_hash,line,applied_via,result,recorded_at,journal_confirmed,
    case_id,case_operator_id,case_user_id,case_action,case_operation_id,case_recorded_at)
   VALUES ((e->>'seq')::bigint,k,e->>'hash',p_lines[i],'close','case_used',p_at,false,
    p_case,me,c.user_id,c.action,(b->>'operation_id')::uuid,(b->>'recorded_at')::timestamptz);
   op:=(b->>'operation_id')::uuid;
  ELSIF i=2 THEN
   IF (b->>'user_id') IS DISTINCT FROM c.user_id::text OR (b->>'operation_id') IS DISTINCT FROM op::text
    OR (b->>'recorded_at')::timestamptz IS DISTINCT FROM p_at
    OR NOT ((c.action='set_credential' AND k='credential_transition' AND b->>'reason_code'='verified_recovery')
         OR (c.action='resolve_initial_status' AND k='account_status_decision' AND b->>'decision'='active'
             AND b->>'reason_code'='verified_initial_status')) THEN
    RAISE EXCEPTION 'operator auth event mismatch' USING ERRCODE='check_violation'; END IF;
   PERFORM ras_auth_mirror(p_lines[i],'close','auth_changed',false);
   PERFORM ras_auth_project(c.user_id);
   PERFORM ras_auth_change(c.user_id,op,p_verifier,p_policy);
  ELSIF k='reset_token_closure' AND phase<=3 THEN
   phase:=3;
   IF (b->>'user_id') IS DISTINCT FROM c.user_id::text OR (b->>'end_kind') IS DISTINCT FROM 'revoked'
    OR (b->>'reason_code') IS DISTINCT FROM 'operator_recovery' OR (b->>'ended_at')::timestamptz IS DISTINCT FROM p_at THEN
    RAISE EXCEPTION 'operator token closure mismatch' USING ERRCODE='check_violation'; END IF;
   IF NOT ras_auth_token_close((b->>'token_id')::uuid,c.user_id,'revoked','operator_recovery',p_at) THEN
    RAISE EXCEPTION 'operator token already ended' USING ERRCODE='check_violation'; END IF;
   PERFORM ras_auth_closure_mirror(p_lines[i],'close','reset_token_ended');
   tokens:=tokens+1;
  ELSIF k='session_revocation' THEN
   phase:=4;
   IF (b->>'user_id') IS DISTINCT FROM c.user_id::text OR (b->>'reason_code') IS DISTINCT FROM 'operator_recovery'
    OR (b->>'revoked_at')::timestamptz IS DISTINCT FROM p_at THEN
    RAISE EXCEPTION 'operator session closure mismatch' USING ERRCODE='check_violation'; END IF;
   IF NOT ras_auth_session_close((b->>'session_id')::uuid,c.user_id,p_at,'operator_recovery') THEN
    RAISE EXCEPTION 'operator session already ended' USING ERRCODE='check_violation'; END IF;
   PERFORM ras_auth_closure_mirror(p_lines[i],'revoke','session_revoked');
   closed_sessions:=closed_sessions+1;
  ELSE
   RAISE EXCEPTION 'operator batch kind invalid' USING ERRCODE='check_violation';
  END IF;
  prev_seq:=prev_seq+1; prev_hash:=e->>'hash';
 END LOOP;
 -- Nothing issued before this decision may come back to life with it.
 IF EXISTS (SELECT 1 FROM password_reset_tokens t WHERE t.user_id=c.user_id AND t.used_at IS NULL AND t.revoked_at IS NULL)
  OR EXISTS (SELECT 1 FROM sessions s WHERE s.user_id=c.user_id AND s.revoked_at IS NULL) THEN
  RAISE EXCEPTION 'operator batch must close every token and session' USING ERRCODE='check_violation'; END IF;
 INSERT INTO audit_events(occurred_at,actor_type,action,outcome,target_type,target_id,details)
 VALUES (p_at,'system',CASE c.action WHEN 'set_credential' THEN 'auth.operator_recovery.credential'
   ELSE 'auth.operator_recovery.initial_status' END,'success','user',c.user_id::text,
   jsonb_build_object('case_id',c.case_id,'operator_id',me,'operation_id',op,'method',c.verification_method,
                      'tokens_revoked',tokens,'sessions_revoked',closed_sessions));
 -- Every deferred receipt/binding/closure/FK check BEFORE the first journal byte.
 SET CONSTRAINTS ALL IMMEDIATE;
 SET CONSTRAINTS ALL DEFERRED;
END $$;

-- Confirm-only for exactly this transaction's staged lines, after append + fsync returned.
CREATE FUNCTION ras_auth_operator_confirm(p_case uuid,p_lines text[]) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE me uuid:=ras_auth_operator_assert(); c auth_recovery_cases; x xid:=(txid_current() % 4294967296)::text::xid; n integer;
BEGIN
 SELECT * INTO c FROM auth_recovery_cases k WHERE k.case_id=p_case AND k.operator_id=me;
 IF c.case_id IS NULL OR NOT EXISTS (SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'
   AND mode='ExclusiveLock' AND granted AND classid=(32195295680032364::bigint >> 32)::oid
   AND objid=(32195295680032364::bigint & 4294967295)::oid)
  OR NOT EXISTS (SELECT 1 FROM revocation_journal_records j WHERE j.case_id=p_case AND j.line=p_lines[1]
   AND j.xmin=x AND NOT j.journal_confirmed) THEN
  RAISE EXCEPTION 'operator confirmation does not bind this transaction' USING ERRCODE='check_violation'; END IF;
 UPDATE revocation_journal_records j SET journal_confirmed=true
  WHERE j.line=ANY(p_lines) AND NOT j.journal_confirmed AND j.xmin=x
   AND (j.case_id=p_case OR j.auth_user_id=c.user_id OR j.session_user_id=c.user_id OR j.reset_token_user_id=c.user_id);
 GET DIAGNOSTICS n=ROW_COUNT;
 IF n IS DISTINCT FROM array_length(p_lines,1) THEN
  RAISE EXCEPTION 'operator confirmation incomplete' USING ERRCODE='check_violation'; END IF;
END $$;

-- Read-only outcome by the case record: receipt, pointer and head of its exact operation.
CREATE FUNCTION ras_auth_operator_outcome(p_case uuid) RETURNS TABLE(case_operator_id uuid, case_seq bigint,
 case_confirmed boolean, operation_id uuid, action text, user_id uuid, recorded_at timestamptz, event_seq bigint,
 event_confirmed boolean, generation uuid, receipt boolean, current_pointer boolean, head_current boolean,
 credential_state text, status text, status_safety text, account_live boolean)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
#variable_conflict use_column
BEGIN
 PERFORM ras_auth_operator_assert();
 RETURN QUERY SELECT k.case_operator_id, k.seq, k.journal_confirmed, k.case_operation_id, k.case_action,
  k.case_user_id, k.case_recorded_at, j.seq, j.journal_confirmed, j.auth_generation,
  COALESCE(EXISTS (SELECT 1 FROM auth_change_receipts r WHERE r.operation_id=k.case_operation_id
   AND ROW(r.user_id,r.domain,r.generation,r.decision,r.reason_code,r.recorded_at) IS NOT DISTINCT FROM
       ROW(j.auth_user_id,j.auth_domain,j.auth_generation,j.auth_decision,j.auth_reason_code,j.auth_recorded_at)),false),
  COALESCE(CASE WHEN k.case_action='set_credential' THEN u.credential_operation_id=k.case_operation_id
   ELSE u.status_operation_id=k.case_operation_id END,false),
  COALESCE(CASE WHEN k.case_action='set_credential' THEN hd.credential_operation_id=k.case_operation_id
   ELSE hd.status_operation_id=k.case_operation_id END,false),
  u.credential_state, u.status, u.status_safety, COALESCE(ras_auth_account_live(u.id),false)
 FROM revocation_journal_records k
 LEFT JOIN revocation_journal_records j ON j.auth_operation_id=k.case_operation_id
 LEFT JOIN users u ON u.id=k.case_user_id
 LEFT JOIN auth_safety_heads hd ON hd.user_id=k.case_user_id
 WHERE k.case_id=p_case;
END $$;

-- Private until provisioning grants operator logins; existing deployments keep their definer owner.
DO $$ DECLARE f record; df name; BEGIN
 SELECT pg_get_userbyid(proowner) INTO df FROM pg_proc
  WHERE oid='ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure;
 EXECUTE format('GRANT SELECT ON auth_recovery_operators,auth_recovery_cases TO %I',df);
 FOR f IN SELECT oid::regprocedure signature,prosecdef FROM pg_proc WHERE pronamespace='public'::regnamespace
  AND proname IN ('ras_auth_operator_assert','ras_auth_operator_whoami','ras_auth_operator_gate','ras_auth_operator_inspect',
   'ras_auth_operator_case','ras_auth_operator_snapshot','ras_auth_operator_stage','ras_auth_operator_confirm',
   'ras_auth_operator_outcome','ras_auth_case_record_valid','ras_auth_receipt_operator_case',
   'ras_auth_recovery_operator_final','ras_auth_recovery_case_final') LOOP
  IF f.prosecdef THEN EXECUTE format('ALTER FUNCTION %s OWNER TO %I',f.signature,df); END IF;
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f.signature);
 END LOOP;
END $$;
