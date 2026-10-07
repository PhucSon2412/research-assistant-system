-- Only a real open -> closed transition can mint a pending closure binding.
-- Historical closures keep NULL; replay can still name an absent request.
CREATE FUNCTION ras_review_closure_transaction() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF TG_OP='INSERT' THEN
  IF NEW.closure_transaction_id IS NOT NULL THEN
   RAISE EXCEPTION 'closure transaction is derived' USING ERRCODE='check_violation'; END IF;
 ELSIF OLD.status='open' AND NEW.status<>'open' THEN
  NEW.closure_transaction_id:=txid_current();
 ELSIF NEW.closure_transaction_id IS DISTINCT FROM OLD.closure_transaction_id THEN
  RAISE EXCEPTION 'closure transaction is final' USING ERRCODE='check_violation';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_review_closure_transaction BEFORE INSERT OR UPDATE ON review_requests
 FOR EACH ROW EXECUTE FUNCTION ras_review_closure_transaction();

CREATE FUNCTION ras_auth_review_closure_mirror(p_line text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE e jsonb:=p_line::jsonb; b jsonb:=e->'body'; n text; at timestamptz;
BEGIN
 IF p_line IS NULL OR jsonb_typeof(e) IS DISTINCT FROM 'object'
  OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(e) key)
     IS DISTINCT FROM ARRAY['body','hash','kind','prev','seq']::text[]
  OR e->>'kind' IS DISTINCT FROM 'review_closure'
  OR jsonb_typeof(e->'kind') IS DISTINCT FROM 'string'
  OR jsonb_typeof(e->'seq') IS DISTINCT FROM 'number'
  OR (e->>'seq') !~ '^[1-9][0-9]*$'
  OR jsonb_typeof(e->'hash') IS DISTINCT FROM 'string' OR (e->>'hash') !~ '^[0-9a-f]{64}$'
  OR jsonb_typeof(e->'prev') IS DISTINCT FROM 'string' OR (e->>'prev') !~ '^[0-9a-f]{64}$'
  OR jsonb_typeof(b) IS DISTINCT FROM 'object' THEN
  RAISE EXCEPTION 'invalid review closure envelope' USING ERRCODE='check_violation'; END IF;
 IF (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(b) key)
    IS DISTINCT FROM ARRAY['closed_at','item_id','reason_code','request_id','source_file_id','status','version_id']::text[] THEN
  RAISE EXCEPTION 'invalid review closure fields' USING ERRCODE='check_violation'; END IF;
 FOREACH n IN ARRAY ARRAY['request_id','item_id','version_id','source_file_id'] LOOP
  IF jsonb_typeof(b->n) IS DISTINCT FROM 'string'
   OR (b->>n) !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
   RAISE EXCEPTION 'invalid review closure UUID' USING ERRCODE='check_violation'; END IF;
 END LOOP;
 IF jsonb_typeof(b->'status') IS DISTINCT FROM 'string'
  OR jsonb_typeof(b->'reason_code') IS DISTINCT FROM 'string'
  OR NOT ((b->>'status'='withdrawn' AND b->>'reason_code'='withdrawn_by_requester')
   OR (b->>'status'='rejected' AND b->>'reason_code' IN
    ('rights_not_established','insufficient_evidence','wrong_file_or_version','out_of_scope','other')))
  OR jsonb_typeof(b->'closed_at') IS DISTINCT FROM 'string'
  OR (b->>'closed_at') !~ '^\d{4}-\d{2}-\d{2}T(0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.\d{1,6})?(Z|[+-](0[0-9]|1[0-9]|2[0-3]):[0-5][0-9])$' THEN
  RAISE EXCEPTION 'invalid review closure decision/time' USING ERRCODE='check_violation'; END IF;
 at:=(b->>'closed_at')::timestamptz;
 IF NOT isfinite(at) OR NOT EXISTS (SELECT 1 FROM review_requests r WHERE
  ROW(r.id,r.item_id,r.version_id,r.source_file_id,r.status,r.close_reason_code,r.closed_at,r.closure_transaction_id)
  IS NOT DISTINCT FROM ROW((b->>'request_id')::uuid,(b->>'item_id')::uuid,(b->>'version_id')::uuid,
    (b->>'source_file_id')::uuid,b->>'status',b->>'reason_code',at,txid_current())
  AND r.closed_by_user_id IS NOT NULL) THEN
  RAISE EXCEPTION 'review closure does not bind this transaction' USING ERRCODE='check_violation'; END IF;
 INSERT INTO revocation_journal_records(seq,kind,record_hash,line,applied_via,result,journal_confirmed,review_request_id)
 VALUES ((e->>'seq')::bigint,'review_closure',e->>'hash',p_line,'close','closed',false,(b->>'request_id')::uuid);
 -- All deferred target/FK/audit/closure checks BEFORE the first journal byte.
 SET CONSTRAINTS ALL IMMEDIATE;
 SET CONSTRAINTS ALL DEFERRED;
END $$;

CREATE FUNCTION ras_auth_review_closure_confirm(p_line text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 UPDATE revocation_journal_records j SET journal_confirmed=true
 FROM review_requests r WHERE j.line=p_line AND j.kind='review_closure'
  AND j.applied_via='close' AND j.result='closed' AND NOT j.journal_confirmed
  AND j.review_request_id=r.id AND r.closure_transaction_id=txid_current();
 IF NOT FOUND THEN RAISE EXCEPTION 'review confirmation does not bind this transaction'
  USING ERRCODE='check_violation'; END IF;
END $$;

-- Upgrade already-provisioned installations, using the existing domain ACL.
DO $$ DECLARE df name; lr name; f record; BEGIN
 SELECT pg_get_userbyid(proowner) INTO df FROM pg_proc
  WHERE oid='ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure;
 EXECUTE format('GRANT SELECT ON review_requests TO %I',df);
 FOR f IN SELECT oid::regprocedure signature FROM pg_proc WHERE pronamespace='public'::regnamespace
  AND proname IN ('ras_auth_review_closure_mirror','ras_auth_review_closure_confirm') LOOP
  EXECUTE format('ALTER FUNCTION %s OWNER TO %I',f.signature,df);
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f.signature);
  FOR lr IN SELECT DISTINCT r.rolname FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a
   JOIN pg_roles r ON r.oid=a.grantee
   WHERE p.oid='ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure
    AND a.privilege_type='EXECUTE' AND r.rolcanlogin AND r.rolname<>df LOOP
   EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I',f.signature,lr);
  END LOOP;
 END LOOP;
END $$;
