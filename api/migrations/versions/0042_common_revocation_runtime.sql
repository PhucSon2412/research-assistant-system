-- Live transition evidence minted by PostgreSQL. Old revoked rows remain NULL.
-- Runtime already executes the shared auth confirmer. Exclude this newly
-- isolated domain there too; otherwise it could confirm a previous revoke by
-- seq and bypass the transaction-bound routine below. Other domains unchanged.
CREATE OR REPLACE FUNCTION ras_auth_confirm(p_seq bigint) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 UPDATE revocation_journal_records SET journal_confirmed=true
 WHERE seq=p_seq AND kind<>'revocation' AND NOT journal_confirmed;
$$;
ALTER TABLE library_items ADD COLUMN revocation_transaction_id bigint,
 ADD COLUMN revocation_prior_state text, ADD COLUMN revocation_prior_epoch bigint;
CREATE FUNCTION ras_common_revocation_transaction() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF TG_OP='INSERT' THEN
  IF num_nonnulls(NEW.revocation_transaction_id,NEW.revocation_prior_state,NEW.revocation_prior_epoch)<>0 THEN
   RAISE EXCEPTION 'revocation evidence is database minted' USING ERRCODE='check_violation'; END IF;
 ELSE
  IF ROW(NEW.revocation_transaction_id,NEW.revocation_prior_state,NEW.revocation_prior_epoch)
   IS DISTINCT FROM ROW(OLD.revocation_transaction_id,OLD.revocation_prior_state,OLD.revocation_prior_epoch) THEN
   RAISE EXCEPTION 'revocation evidence is immutable' USING ERRCODE='check_violation'; END IF;
  IF OLD.visibility='common' AND OLD.publication_state IN ('pending','listed') AND NEW.publication_state='revoked' THEN
   NEW.revocation_transaction_id:=txid_current();
   NEW.revocation_prior_state:=OLD.publication_state;
   NEW.revocation_prior_epoch:=OLD.rights_epoch;
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_library_items_z_revocation_transaction BEFORE INSERT OR UPDATE ON library_items
 FOR EACH ROW EXECUTE FUNCTION ras_common_revocation_transaction();

CREATE FUNCTION ras_auth_common_revocation_mirror(p_line text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE e jsonb; b jsonb; r jsonb; n text; at timestamptz; due timestamptz; x xid; t deletion_tombstones;
BEGIN
 IF p_line IS NULL THEN RAISE EXCEPTION 'invalid revocation envelope' USING ERRCODE='check_violation'; END IF;
 e:=p_line::jsonb; b:=e->'body';
 IF jsonb_typeof(e) IS DISTINCT FROM 'object'
  OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(e) key)
    IS DISTINCT FROM ARRAY['body','hash','kind','prev','seq']::text[]
  OR jsonb_typeof(e->'kind') IS DISTINCT FROM 'string' OR e->>'kind' IS DISTINCT FROM 'revocation'
  OR jsonb_typeof(e->'seq') IS DISTINCT FROM 'number' OR (e->>'seq') !~ '^[1-9][0-9]*$'
  OR jsonb_typeof(e->'hash') IS DISTINCT FROM 'string' OR (e->>'hash') !~ '^[0-9a-f]{64}$'
  OR jsonb_typeof(e->'prev') IS DISTINCT FROM 'string' OR (e->>'prev') !~ '^[0-9a-f]{64}$'
  OR jsonb_typeof(b) IS DISTINCT FROM 'object' THEN
  RAISE EXCEPTION 'invalid revocation envelope' USING ERRCODE='check_violation'; END IF;
 IF encode(sha256(convert_to(regexp_replace(p_line,',"hash":"[0-9a-f]{64}"',''),'UTF8')),'hex')
    IS DISTINCT FROM e->>'hash'
  OR NOT EXISTS (SELECT 1 FROM revocation_journal_records j WHERE j.seq=(e->>'seq')::bigint-1
     AND j.record_hash=e->>'prev' AND j.journal_confirmed
     AND j.seq=(SELECT max(seq) FROM revocation_journal_records)) THEN
  RAISE EXCEPTION 'invalid revocation chain' USING ERRCODE='check_violation'; END IF;
 IF (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(b) key) IS DISTINCT FROM
  ARRAY['access_rights_id','id','item_id','prior_publication_state','purge_due_at','reason_code',
   'recorded_at','recorded_by_user_id','retentions','rights_epoch','source_file_id','storage_key','target_kind','version_id']::text[] THEN
  RAISE EXCEPTION 'invalid revocation fields' USING ERRCODE='check_violation'; END IF;
 FOREACH n IN ARRAY ARRAY['id','item_id','version_id','source_file_id','access_rights_id','recorded_by_user_id'] LOOP
  IF jsonb_typeof(b->n) IS DISTINCT FROM 'string' OR (b->>n) !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
   RAISE EXCEPTION 'invalid revocation UUID' USING ERRCODE='check_violation'; END IF;
 END LOOP;
 IF jsonb_typeof(b->'target_kind') IS DISTINCT FROM 'string' OR b->>'target_kind' IS DISTINCT FROM 'common_copy'
  OR jsonb_typeof(b->'prior_publication_state') IS DISTINCT FROM 'string' OR b->>'prior_publication_state' NOT IN ('pending','listed')
  OR jsonb_typeof(b->'reason_code') IS DISTINCT FROM 'string' OR b->>'reason_code' NOT IN
   ('rights_recorded_in_error','rights_withdrawn','rights_expired','wrong_file_or_version','duplicate_copy','other')
  OR jsonb_typeof(b->'rights_epoch') IS DISTINCT FROM 'number' OR (b->>'rights_epoch') !~ '^[1-9][0-9]*$'
  OR jsonb_typeof(b->'storage_key') IS DISTINCT FROM 'string' OR (b->>'storage_key') !~ '^originals/[0-9a-f]{32}$'
  OR jsonb_typeof(b->'retentions') IS DISTINCT FROM 'array' THEN
  RAISE EXCEPTION 'invalid revocation decision' USING ERRCODE='check_violation'; END IF;
 FOREACH n IN ARRAY ARRAY['recorded_at','purge_due_at'] LOOP
  IF jsonb_typeof(b->n) IS DISTINCT FROM 'string' OR (b->>n) !~ '^\d{4}-\d{2}-\d{2}T(0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.\d{1,6})?(Z|[+-](0[0-9]|1[0-9]|2[0-3]):[0-5][0-9])$' THEN
   RAISE EXCEPTION 'invalid revocation time' USING ERRCODE='check_violation'; END IF;
 END LOOP;
 at:=(b->>'recorded_at')::timestamptz; due:=(b->>'purge_due_at')::timestamptz;
 IF NOT isfinite(at) OR NOT isfinite(due) OR due<=at THEN
  RAISE EXCEPTION 'invalid revocation deadline' USING ERRCODE='check_violation'; END IF;
 x:=(txid_current() % 4294967296)::text::xid;
 SELECT * INTO t FROM deletion_tombstones d WHERE d.id=(b->>'id')::uuid AND d.xmin=x;
 IF NOT FOUND OR ROW(t.target_kind,t.item_id,t.version_id,t.source_file_id,t.storage_key,t.access_rights_id,
    t.reason_code,t.prior_publication_state,t.rights_epoch,t.recorded_at,t.recorded_by_user_id,t.purge_due_at)
  IS DISTINCT FROM ROW('common_copy',(b->>'item_id')::uuid,(b->>'version_id')::uuid,(b->>'source_file_id')::uuid,
    b->>'storage_key',(b->>'access_rights_id')::uuid,b->>'reason_code',b->>'prior_publication_state',
    (b->>'rights_epoch')::bigint,at,(b->>'recorded_by_user_id')::uuid,due)
  OR t.purged_at IS NOT NULL OR t.cleanup_attempts<>0 THEN
  RAISE EXCEPTION 'revocation tombstone does not bind this transaction' USING ERRCODE='check_violation'; END IF;
 IF NOT EXISTS (SELECT 1 FROM library_items i JOIN access_rights a ON a.item_id=i.id
  JOIN paper_versions v ON v.id=a.version_id AND v.item_id=i.id
  JOIN source_files f ON f.id=a.source_file_id AND f.item_id=i.id AND f.version_id=v.id
  JOIN users u ON u.id=t.recorded_by_user_id AND u.role='admin'
  WHERE i.id=t.item_id AND i.visibility='common' AND i.owner_user_id IS NULL AND i.publication_state='revoked'
   AND i.content_status='metadata_only' AND i.active_index_generation_id IS NULL
   AND i.revocation_transaction_id=txid_current() AND i.revocation_prior_state=t.prior_publication_state
   AND i.revocation_prior_epoch+1=t.rights_epoch AND i.rights_epoch=t.rights_epoch AND i.updated_at=at AND i.xmin=x
   AND a.id=t.access_rights_id AND a.version_id=t.version_id AND a.source_file_id=t.source_file_id
   AND a.revoked_at=at AND a.revoked_by_user_id=t.recorded_by_user_id AND a.revocation_reason_code=t.reason_code
   AND a.rights_epoch_at_revoke=t.rights_epoch AND a.rights_epoch_at_grant=i.revocation_prior_epoch AND a.xmin=x
   AND f.storage_key=t.storage_key AND f.checksum_sha256=a.file_checksum_sha256
   AND (SELECT count(*) FROM access_rights ar WHERE ar.item_id=i.id)=1
   AND (SELECT count(*) FROM paper_versions pv WHERE pv.item_id=i.id)=1
   AND (SELECT count(*) FROM source_files sf WHERE sf.item_id=i.id)=1)
  OR EXISTS (SELECT 1 FROM common_publications p WHERE p.item_id=t.item_id AND
    (p.access_rights_id<>t.access_rights_id OR p.revoked_at IS DISTINCT FROM greatest(at,p.published_at) OR p.xmin<>x))
  OR (t.prior_publication_state='listed' AND NOT EXISTS (SELECT 1 FROM common_publications p WHERE p.item_id=t.item_id))
  OR (t.prior_publication_state='pending' AND EXISTS (SELECT 1 FROM common_publications p WHERE p.item_id=t.item_id))
  OR EXISTS (SELECT 1 FROM ingestion_jobs j WHERE j.item_id=t.item_id AND j.finished_at IS NULL AND NOT j.cancel_requested) THEN
  RAISE EXCEPTION 'revocation resource binding failed' USING ERRCODE='check_violation'; END IF;
 IF jsonb_array_length(b->'retentions')<>(SELECT count(*) FROM revocation_metadata_retentions m WHERE m.tombstone_id=t.id)
  OR (SELECT count(DISTINCT value->>'field') FROM jsonb_array_elements(b->'retentions'))<>jsonb_array_length(b->'retentions') THEN
  RAISE EXCEPTION 'revocation retention set differs' USING ERRCODE='check_violation'; END IF;
 FOR r IN SELECT value FROM jsonb_array_elements(b->'retentions') LOOP
  IF jsonb_typeof(r) IS DISTINCT FROM 'object' OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(r) key)
   IS DISTINCT FROM ARRAY['confirmed_by_user_id','field','policy_source','reason','recorded_at','scope']::text[] THEN
   RAISE EXCEPTION 'invalid retention fields' USING ERRCODE='check_violation'; END IF;
  FOREACH n IN ARRAY ARRAY['confirmed_by_user_id','field','policy_source','reason','recorded_at','scope'] LOOP
   IF jsonb_typeof(r->n) IS DISTINCT FROM 'string' THEN
    RAISE EXCEPTION 'invalid retention type' USING ERRCODE='check_violation'; END IF;
  END LOOP;
  IF NOT EXISTS (SELECT 1 FROM revocation_metadata_retentions m WHERE m.tombstone_id=t.id AND m.item_id=t.item_id
    AND m.field=r->>'field' AND m.policy_source=r->>'policy_source' AND m.scope=r->>'scope' AND m.reason=r->>'reason'
    AND m.confirmed_by_user_id=t.recorded_by_user_id AND m.confirmed_by_user_id::text=r->>'confirmed_by_user_id'
    AND m.recorded_at=at AND m.recorded_at=(r->>'recorded_at')::timestamptz AND m.xmin=x) THEN
   RAISE EXCEPTION 'retention does not bind this transaction' USING ERRCODE='check_violation'; END IF;
 END LOOP;
 IF NOT EXISTS (SELECT 1 FROM audit_events a WHERE a.action='common_copy.revoke' AND a.outcome='success'
   AND a.actor_user_id=t.recorded_by_user_id AND a.target_type='library_item' AND a.target_id=t.item_id::text AND a.xmin=x
   AND a.details->>'tombstone_id'=t.id::text AND a.details->>'reason_code'=t.reason_code
   AND a.details->>'from_state'=t.prior_publication_state
   AND a.details->'retained_fields'=COALESCE((SELECT jsonb_agg(m.field ORDER BY m.field)
      FROM revocation_metadata_retentions m WHERE m.tombstone_id=t.id),'[]'::jsonb)
   AND a.details->'cancelled_jobs'=to_jsonb((SELECT count(*) FROM ingestion_jobs j WHERE j.item_id=t.item_id
      AND j.finished_at IS NULL AND j.cancel_requested AND j.cancel_requested_at=at AND j.xmin=x))) THEN
  RAISE EXCEPTION 'revocation audit binding failed' USING ERRCODE='check_violation'; END IF;
 INSERT INTO revocation_journal_records(seq,kind,record_hash,line,tombstone_id,applied_via,result,journal_confirmed)
 VALUES ((e->>'seq')::bigint,'revocation',e->>'hash',p_line,t.id,'revoke','revoked',false);
 -- All business, retention, audit and mirror constraints precede the first byte.
 SET CONSTRAINTS ALL IMMEDIATE;
 SET CONSTRAINTS ALL DEFERRED;
END $$;

CREATE FUNCTION ras_auth_common_revocation_confirm(p_line text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE x xid:=(txid_current() % 4294967296)::text::xid;
BEGIN
 UPDATE revocation_journal_records j SET journal_confirmed=true
 FROM deletion_tombstones t JOIN library_items i ON i.id=t.item_id
 WHERE j.line=p_line AND j.kind='revocation' AND j.applied_via='revoke' AND j.result='revoked'
  AND NOT j.journal_confirmed AND j.xmin=x AND t.xmin=x AND j.tombstone_id=t.id
  AND i.revocation_transaction_id=txid_current() AND i.publication_state='revoked'
  AND i.rights_epoch=t.rights_epoch AND i.xmin=x;
 IF NOT FOUND THEN RAISE EXCEPTION 'revocation confirmation does not bind this transaction'
  USING ERRCODE='check_violation'; END IF;
END $$;

DO $$ DECLARE df name; lr name; f record; BEGIN
 SELECT pg_get_userbyid(proowner) INTO df FROM pg_proc
  WHERE oid='ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure;
 EXECUTE format('GRANT SELECT ON library_items,paper_versions,source_files,access_rights,users,common_publications,ingestion_jobs,deletion_tombstones,revocation_metadata_retentions,audit_events TO %I',df);
 FOR f IN SELECT oid::regprocedure signature FROM pg_proc WHERE pronamespace='public'::regnamespace
  AND proname IN ('ras_auth_common_revocation_mirror','ras_auth_common_revocation_confirm') LOOP
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
