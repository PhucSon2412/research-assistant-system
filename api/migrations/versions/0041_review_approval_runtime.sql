-- Approval-only mirror. 0040 withdraw/reject routines stay unchanged.
-- The common tuple must be inserted by THIS transaction: PostgreSQL mints
-- xmin, so no caller GUC, boolean or arbitrary UUID can qualify old rows.
-- Acceptance uses no savepoint (a subtransaction xmin fails closed).
CREATE FUNCTION ras_auth_review_approval_mirror(p_line text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE e jsonb; b jsonb; n text; at timestamptz; x xid;
BEGIN
 IF p_line IS NULL THEN
  RAISE EXCEPTION 'invalid review approval envelope' USING ERRCODE='check_violation'; END IF;
 e:=p_line::jsonb; b:=e->'body';
 IF jsonb_typeof(e) IS DISTINCT FROM 'object'
  OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(e) key)
     IS DISTINCT FROM ARRAY['body','hash','kind','prev','seq']::text[]
  OR jsonb_typeof(e->'kind') IS DISTINCT FROM 'string' OR e->>'kind' IS DISTINCT FROM 'review_closure'
  OR jsonb_typeof(e->'seq') IS DISTINCT FROM 'number' OR (e->>'seq') !~ '^[1-9][0-9]*$'
  OR jsonb_typeof(e->'hash') IS DISTINCT FROM 'string' OR (e->>'hash') !~ '^[0-9a-f]{64}$'
  OR jsonb_typeof(e->'prev') IS DISTINCT FROM 'string' OR (e->>'prev') !~ '^[0-9a-f]{64}$'
  OR jsonb_typeof(b) IS DISTINCT FROM 'object' THEN
  RAISE EXCEPTION 'invalid review approval envelope' USING ERRCODE='check_violation'; END IF;
 IF (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(b) key)
    IS DISTINCT FROM ARRAY['closed_at','item_id','reason_code','request_id','source_file_id','status','version_id']::text[] THEN
  RAISE EXCEPTION 'invalid review approval fields' USING ERRCODE='check_violation'; END IF;
 FOREACH n IN ARRAY ARRAY['request_id','item_id','version_id','source_file_id'] LOOP
  IF jsonb_typeof(b->n) IS DISTINCT FROM 'string'
   OR (b->>n) !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
   RAISE EXCEPTION 'invalid review approval UUID' USING ERRCODE='check_violation'; END IF;
 END LOOP;
 IF jsonb_typeof(b->'status') IS DISTINCT FROM 'string' OR b->>'status' IS DISTINCT FROM 'approved'
  OR jsonb_typeof(b->'reason_code') IS DISTINCT FROM 'string' OR b->>'reason_code' IS DISTINCT FROM 'approved_by_admin'
  OR jsonb_typeof(b->'closed_at') IS DISTINCT FROM 'string'
  OR (b->>'closed_at') !~ '^\d{4}-\d{2}-\d{2}T(0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.\d{1,6})?(Z|[+-](0[0-9]|1[0-9]|2[0-3]):[0-5][0-9])$' THEN
  RAISE EXCEPTION 'invalid review approval decision/time' USING ERRCODE='check_violation'; END IF;
 at:=(b->>'closed_at')::timestamptz;
 x:=(txid_current() % 4294967296)::text::xid;
 IF NOT isfinite(at) OR NOT EXISTS (
  SELECT 1 FROM review_requests r
  JOIN users u ON u.id=r.closed_by_user_id AND u.role='admin'
  JOIN library_items pi ON pi.id=r.item_id AND pi.visibility='private'
   AND pi.owner_user_id=r.requester_user_id AND pi.rights_epoch=r.rights_epoch_at_submit
  JOIN paper_versions pv ON pv.id=r.version_id AND pv.item_id=pi.id
  JOIN source_files pf ON pf.id=r.source_file_id AND pf.version_id=pv.id AND pf.item_id=pi.id
   AND pf.checksum_sha256=r.file_checksum_sha256
  JOIN access_rights ar ON ar.origin_review_request_id=r.id
  JOIN library_items ci ON ci.id=ar.item_id
  JOIN paper_versions cv ON cv.id=ar.version_id AND cv.item_id=ci.id
  JOIN source_files cf ON cf.id=ar.source_file_id AND cf.version_id=cv.id AND cf.item_id=ci.id
  WHERE ROW(r.id,r.item_id,r.version_id,r.source_file_id,r.status,r.close_reason_code,r.closed_at,r.closure_transaction_id)
   IS NOT DISTINCT FROM ROW((b->>'request_id')::uuid,(b->>'item_id')::uuid,(b->>'version_id')::uuid,
    (b->>'source_file_id')::uuid,'approved','approved_by_admin',at,txid_current())
   AND r.rejection_note IS NULL
   AND ar.origin_kind='review_request' AND ar.reviewer_user_id=r.closed_by_user_id AND ar.recorded_at=r.closed_at
   AND ar.file_checksum_sha256=r.file_checksum_sha256 AND ar.rights_epoch_at_grant=0
   AND ci.visibility='common' AND ci.publication_state='pending' AND ci.owner_user_id IS NULL
   AND ci.rights_epoch=0 AND ci.created_by_user_id=r.closed_by_user_id AND ci.content_status='metadata_only'
   AND ci.active_version_id IS NULL AND ci.active_index_generation_id IS NULL
   AND cf.checksum_sha256=r.file_checksum_sha256 AND cf.byte_size=pf.byte_size AND cf.verified_mime=pf.verified_mime
   AND cf.storage_key<>pf.storage_key AND cf.file_source='review_approval' AND cf.job_id IS NULL
   AND ci.xmin=x AND cv.xmin=x AND cf.xmin=x AND ar.xmin=x
   AND (SELECT count(*) FROM paper_versions v WHERE v.item_id=ci.id)=1
   AND (SELECT count(*) FROM source_files f WHERE f.item_id=ci.id)=1) THEN
  RAISE EXCEPTION 'review approval does not bind this transaction' USING ERRCODE='check_violation'; END IF;
 INSERT INTO revocation_journal_records(seq,kind,record_hash,line,applied_via,result,journal_confirmed,review_request_id)
 VALUES ((e->>'seq')::bigint,'review_closure',e->>'hash',p_line,'close','closed',false,(b->>'request_id')::uuid);
 -- Approval/rights/pending/closure/FK/audit deferred checks BEFORE the first journal byte.
 SET CONSTRAINTS ALL IMMEDIATE;
 SET CONSTRAINTS ALL DEFERRED;
END $$;

CREATE FUNCTION ras_auth_review_approval_confirm(p_line text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 UPDATE revocation_journal_records j SET journal_confirmed=true
 FROM review_requests r WHERE j.line=p_line AND j.kind='review_closure'
  AND j.applied_via='close' AND j.result='closed' AND NOT j.journal_confirmed
  AND j.review_request_id=r.id AND r.closure_transaction_id=txid_current()
  AND r.status='approved' AND r.close_reason_code='approved_by_admin';
 IF NOT FOUND THEN RAISE EXCEPTION 'review approval confirmation does not bind this transaction'
  USING ERRCODE='check_violation'; END IF;
END $$;

-- Upgrade already-provisioned installations, using the existing domain ACL.
DO $$ DECLARE df name; lr name; f record; BEGIN
 SELECT pg_get_userbyid(proowner) INTO df FROM pg_proc
  WHERE oid='ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure;
 EXECUTE format('GRANT SELECT ON review_requests,users,library_items,paper_versions,source_files,access_rights TO %I',df);
 FOR f IN SELECT oid::regprocedure signature FROM pg_proc WHERE pronamespace='public'::regnamespace
  AND proname IN ('ras_auth_review_approval_mirror','ras_auth_review_approval_confirm') LOOP
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
