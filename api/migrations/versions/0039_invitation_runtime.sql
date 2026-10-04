-- Locks acquired by these routines always precede the journal advisory lock.
CREATE FUNCTION ras_auth_invitation_before_journal() RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF EXISTS (SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'
  AND classid=(32195295680032364::bigint >> 32)::oid
  AND objid=(32195295680032364::bigint & 4294967295)::oid) THEN
  RAISE EXCEPTION 'invitation locks must precede journal lock' USING ERRCODE='check_violation'; END IF;
END $$;

CREATE FUNCTION ras_auth_invitation_actor(p_user uuid,p_session uuid,p_csrf text,p_at timestamptz,p_idle interval)
RETURNS boolean LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT coalesce(isfinite(p_at) AND p_idle>interval '0' AND EXISTS (
  SELECT 1 FROM users u JOIN sessions s ON s.user_id=u.id
  WHERE u.id=p_user AND u.role='admin' AND s.id=p_session AND s.csrf_token_hash=p_csrf
   AND ras_auth_session_live(s.id,u.id,p_at,p_idle)),false)
$$;

CREATE FUNCTION ras_auth_invitation_lock(p_ids uuid[]) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 PERFORM ras_auth_invitation_before_journal();
 PERFORM id FROM invitations WHERE id=ANY(p_ids) ORDER BY id FOR UPDATE;
END $$;

CREATE FUNCTION ras_auth_invitation_create(p_id uuid,p_email text,p_name text,p_token text,
 p_actor uuid,p_session uuid,p_csrf text,p_at timestamptz,p_expires timestamptz,p_idle interval)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 PERFORM ras_auth_invitation_before_journal();
 IF NOT ras_auth_invitation_actor(p_actor,p_session,p_csrf,p_at,p_idle)
  OR p_id IS NULL OR p_id::text !~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
  OR p_email IS NULL OR p_email IS DISTINCT FROM lower(btrim(p_email)) OR p_email !~ '^[^@[:space:]]+@[^@[:space:]]+$'
  OR p_token IS NULL OR p_token !~ '^[0-9a-f]{64}$' OR p_expires IS NULL
  OR NOT isfinite(p_expires) OR p_expires<=p_at
  OR EXISTS (SELECT 1 FROM users WHERE email=p_email) THEN
  RAISE EXCEPTION 'invalid invitation creation' USING ERRCODE='check_violation'; END IF;
 INSERT INTO invitations(id,email,display_name,token_hash,invited_by_user_id,created_at,expires_at,creation_transaction_id)
 VALUES(p_id,p_email,p_name,p_token,p_actor,p_at,p_expires,txid_current());
 INSERT INTO audit_events(occurred_at,actor_type,actor_user_id,action,outcome,target_type,target_id)
 VALUES(p_at,'user',p_actor,'invitation.create','success','invitation',p_id::text);
EXCEPTION WHEN unique_violation THEN
 RAISE EXCEPTION 'invitation creation conflict' USING ERRCODE='unique_violation';
END $$;

CREATE FUNCTION ras_auth_invitation_revoke(p_id uuid,p_actor uuid,p_session uuid,p_csrf text,
 p_at timestamptz,p_idle interval,p_reason text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 PERFORM ras_auth_invitation_before_journal();
 IF NOT ras_auth_invitation_actor(p_actor,p_session,p_csrf,p_at,p_idle)
  OR p_reason IS NULL OR p_reason NOT IN ('admin_revoked','invitation_replaced') THEN
  RAISE EXCEPTION 'invalid invitation revocation' USING ERRCODE='check_violation'; END IF;
 UPDATE invitations SET revoked_at=p_at,closure_reason=p_reason WHERE id=p_id
  AND accepted_at IS NULL AND revoked_at IS NULL AND closed_by_recovery_at IS NULL;
 IF NOT FOUND THEN RETURN false; END IF;
 INSERT INTO audit_events(occurred_at,actor_type,actor_user_id,action,outcome,target_type,target_id,details)
 VALUES(p_at,'user',p_actor,'invitation.revoke','success','invitation',p_id::text,jsonb_build_object('reason_code',p_reason));
 RETURN true;
END $$;

-- Only the known committed issuance binding can be compensated. No actor authority
-- is required to restrict this link; this primitive cannot reopen/change a closure.
CREATE FUNCTION ras_auth_invitation_compensate(p_id uuid,p_token text,p_tx bigint,p_inviter uuid,p_at timestamptz)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 PERFORM ras_auth_invitation_before_journal();
 IF p_at IS NULL OR NOT isfinite(p_at) OR p_token IS NULL OR p_tx IS NULL OR p_inviter IS NULL THEN
  RAISE EXCEPTION 'invalid delivery compensation' USING ERRCODE='check_violation'; END IF;
 IF NOT EXISTS (SELECT 1 FROM invitations WHERE id=p_id AND token_hash=p_token
  AND creation_transaction_id=p_tx AND invited_by_user_id=p_inviter) THEN
  RAISE EXCEPTION 'delivery compensation binding mismatch' USING ERRCODE='check_violation'; END IF;
 UPDATE invitations SET revoked_at=p_at,closure_reason='delivery_failed' WHERE id=p_id
  AND accepted_at IS NULL AND revoked_at IS NULL AND closed_by_recovery_at IS NULL;
 RETURN FOUND;
END $$;

CREATE FUNCTION ras_auth_invitation_prepare(p_id uuid,p_token text,p_email text,p_user uuid,
 p_name text,p_verifier text,p_policy integer,p_at timestamptz) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE i invitations;
BEGIN
 PERFORM ras_auth_invitation_before_journal();
 SELECT * INTO i FROM invitations WHERE id=p_id FOR UPDATE;
 IF i.id IS NULL OR p_at IS NULL OR NOT isfinite(p_at) OR i.expires_at<=p_at
  OR num_nonnulls(i.accepted_at,i.revoked_at,i.closed_by_recovery_at)>0
  OR p_token IS NULL OR p_email IS NULL OR i.token_hash IS DISTINCT FROM p_token OR i.email IS DISTINCT FROM p_email
  OR p_user IS NULL OR p_user::text !~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
  OR EXISTS (SELECT 1 FROM users WHERE id=p_user OR email=i.email)
  OR EXISTS (SELECT 1 FROM revocation_journal_records WHERE auth_user_id=p_user
   OR auth_operation_id=p_user OR auth_generation=p_user OR invitation_id=i.id)
  OR NOT coalesce(ras_auth_phc_valid(p_verifier,p_policy),false)
  OR p_policy IS DISTINCT FROM (SELECT max(version) FROM auth_hash_policies) THEN
  RAISE EXCEPTION 'invalid invitation acceptance' USING ERRCODE='check_violation'; END IF;
 INSERT INTO users(id,email,display_name,role,status,password_verifier,password_hash_policy_version,
  created_at,updated_at,password_changed_at)
 VALUES(p_user,i.email,coalesce(nullif(p_name,''),i.display_name),'user','active',p_verifier,p_policy,p_at,p_at,p_at);
 UPDATE invitations SET accepted_at=p_at,accepted_user_id=p_user,closure_reason='invitation_accepted',
  acceptance_transaction_id=txid_current() WHERE id=i.id;
 INSERT INTO audit_events(occurred_at,actor_type,actor_user_id,action,outcome,target_type,target_id)
 VALUES(p_at,'user',p_user,'invitation.accept','success','invitation',i.id::text);
EXCEPTION WHEN unique_violation THEN
 RAISE EXCEPTION 'invitation acceptance conflict' USING ERRCODE='unique_violation';
END $$;

CREATE FUNCTION ras_auth_invitation_marker() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF TG_OP='UPDATE' AND (NEW.creation_transaction_id IS DISTINCT FROM OLD.creation_transaction_id
   OR (OLD.acceptance_transaction_id IS NOT NULL AND NEW.acceptance_transaction_id IS DISTINCT FROM OLD.acceptance_transaction_id)) THEN
  RAISE EXCEPTION 'invitation transaction binding is final' USING ERRCODE='check_violation'; END IF;
 IF NEW.accepted_at IS NOT NULL AND (TG_OP='INSERT' OR OLD.accepted_at IS NULL) THEN
  IF NEW.acceptance_transaction_id IS DISTINCT FROM txid_current() THEN
   RAISE EXCEPTION 'invitation acceptance needs transaction binding' USING ERRCODE='check_violation'; END IF;
 ELSIF NEW.accepted_at IS NULL AND NEW.acceptance_transaction_id IS NOT NULL THEN
  RAISE EXCEPTION 'invitation acceptance binding without acceptance' USING ERRCODE='check_violation'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_auth_invitation_marker BEFORE INSERT OR UPDATE ON invitations
 FOR EACH ROW EXECUTE FUNCTION ras_auth_invitation_marker();

CREATE FUNCTION ras_auth_invitation_pair() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF NEW.accepted_at IS NULL OR (TG_OP='UPDATE' AND OLD.accepted_at IS NOT NULL) THEN RETURN NULL; END IF;
 IF NOT EXISTS (
  SELECT 1 FROM invitations i JOIN users u ON u.id=i.accepted_user_id
  JOIN auth_change_receipts c ON c.operation_id=u.credential_operation_id
  JOIN auth_change_receipts s ON s.operation_id=u.status_operation_id
  JOIN revocation_journal_records cj ON cj.auth_operation_id=c.operation_id
  JOIN revocation_journal_records sj ON sj.auth_operation_id=s.operation_id
  JOIN revocation_journal_records ij ON ij.invitation_id=i.id
  WHERE i.id=NEW.id AND i.acceptance_transaction_id IS NOT DISTINCT FROM txid_current()
   AND i.accepted_user_id IS NOT DISTINCT FROM NEW.accepted_user_id
   AND i.accepted_at IS NOT DISTINCT FROM NEW.accepted_at AND i.closure_reason='invitation_accepted'
   AND u.email=i.email AND u.role='user' AND u.status='active' AND u.credential_state='usable' AND u.status_safety='proven'
   AND c.transaction_id=i.acceptance_transaction_id AND s.transaction_id=c.transaction_id
   AND ROW(c.user_id,c.domain,c.generation,c.decision,c.reason_code,c.recorded_at)
    IS NOT DISTINCT FROM ROW(u.id,'credential'::text,u.credential_generation,'changed'::text,'invitation_accept'::text,i.accepted_at)
   AND ROW(s.user_id,s.domain,s.generation,s.decision,s.reason_code,s.recorded_at)
    IS NOT DISTINCT FROM ROW(u.id,'status'::text,u.status_generation,'active'::text,'invitation_accept'::text,i.accepted_at)
   AND u.created_at=i.accepted_at AND u.password_changed_at=i.accepted_at AND u.updated_at=i.accepted_at
   AND u.credential_reason_code='invitation_accept' AND u.status_reason_code='invitation_accept'
   AND sj.seq=cj.seq+1 AND ij.seq=sj.seq+1
   AND ROW(ij.invitation_end_kind,ij.invitation_reason_code,ij.invitation_ended_at)
    IS NOT DISTINCT FROM ROW('accepted'::text,'invitation_accepted'::text,i.accepted_at)
   AND EXISTS (SELECT 1 FROM audit_events a WHERE a.action='invitation.accept' AND a.outcome='success'
    AND a.actor_type='user' AND a.actor_user_id=u.id AND a.target_type='invitation' AND a.target_id=i.id::text
    AND a.occurred_at=i.accepted_at AND a.details='{}'::jsonb)
 ) THEN RAISE EXCEPTION 'invitation needs initial receipt pair closure and audit' USING ERRCODE='check_violation'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_auth_invitation_pair AFTER INSERT OR UPDATE ON invitations
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ras_auth_invitation_pair();

CREATE FUNCTION ras_auth_invitation_recover(p_id uuid,p_kind text,p_reason text,p_at timestamptz)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF p_at IS NULL OR NOT isfinite(p_at) OR p_kind IS NULL OR p_reason IS NULL
  OR NOT ((p_kind='accepted' AND p_reason IN ('invitation_accepted','legacy_accepted'))
   OR (p_kind='revoked' AND p_reason IN ('admin_revoked','invitation_replaced','delivery_failed','legacy_revoked'))) THEN
  RAISE EXCEPTION 'invalid recovery invitation closure' USING ERRCODE='check_violation'; END IF;
 -- Rows have already been locked before journal; no new User/FK is acquired.
 UPDATE invitations SET closed_by_recovery_at=p_at,recovery_end_kind=p_kind,closure_reason=p_reason
 WHERE id=p_id AND accepted_at IS NULL AND revoked_at IS NULL AND closed_by_recovery_at IS NULL;
 RETURN FOUND;
END $$;

DO $$ DECLARE f record; df name; lr name; BEGIN
 SELECT pg_get_userbyid(proowner) INTO df FROM pg_proc
  WHERE oid='ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure;
 REVOKE ALL ON invitations FROM PUBLIC;
 FOR lr IN SELECT DISTINCT r.rolname FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a
  JOIN pg_roles r ON r.oid=a.grantee WHERE p.oid IN
   ('ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure,'ras_auth_restrict(uuid)'::regprocedure)
  AND a.privilege_type='EXECUTE' AND r.rolcanlogin AND r.rolname<>df
 LOOP EXECUTE format('REVOKE INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER ON invitations FROM %I',lr); END LOOP;
 EXECUTE format('GRANT SELECT,INSERT,UPDATE ON invitations TO %I',df);
 EXECUTE format('GRANT INSERT ON users,audit_events TO %I',df);
 FOR f IN SELECT oid::regprocedure signature,proname FROM pg_proc WHERE pronamespace='public'::regnamespace
  AND proname LIKE 'ras_auth_invitation_%'
 LOOP
  EXECUTE format('ALTER FUNCTION %s OWNER TO %I',f.signature,df);
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f.signature);
  FOR lr IN SELECT DISTINCT r.rolname FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a
   JOIN pg_roles r ON r.oid=a.grantee WHERE p.oid IN
    ('ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure,'ras_auth_restrict(uuid)'::regprocedure)
   AND a.privilege_type='EXECUTE' AND r.rolcanlogin AND r.rolname<>df
   AND (f.proname IN ('ras_auth_invitation_lock','ras_auth_invitation_recover') OR
    p.proname='ras_auth_session_create' AND f.proname IN
    ('ras_auth_invitation_create','ras_auth_invitation_revoke','ras_auth_invitation_compensate','ras_auth_invitation_prepare'))
  LOOP EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I',f.signature,lr); END LOOP;
 END LOOP;
END $$;
