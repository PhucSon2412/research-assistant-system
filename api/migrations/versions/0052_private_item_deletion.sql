ALTER TABLE library_items ADD COLUMN deleted_at timestamptz;
ALTER TABLE library_items ADD COLUMN deletion_transaction_id bigint;
CREATE OR REPLACE FUNCTION ras_auth_confirm(p_seq bigint) RETURNS void LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,public,pg_temp AS $$
 UPDATE revocation_journal_records SET journal_confirmed=true WHERE seq=p_seq
 AND kind NOT IN ('revocation','private_deletion') AND NOT journal_confirmed;
$$;
CREATE TABLE private_item_deletions (
 id uuid NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
 item_id uuid NOT NULL CONSTRAINT uq_private_item_deletions_item_id UNIQUE,
 owner_user_id uuid NOT NULL,
 rights_epoch bigint NOT NULL,
 deleted_at timestamptz NOT NULL,
 purge_due_at timestamptz NOT NULL,
 manifest jsonb NOT NULL,
 purged_at timestamptz,
 cleanup_attempts integer NOT NULL DEFAULT 0,
 last_cleanup_error text,
 CONSTRAINT ck_private_item_deletions_epoch_positive CHECK(rights_epoch>0),
 CONSTRAINT ck_private_item_deletions_deadline_valid CHECK(purge_due_at>=deleted_at),
 CONSTRAINT ck_private_item_deletions_attempts_non_negative CHECK(cleanup_attempts>=0)
);
CREATE TABLE private_object_fences (
 id uuid NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
 deletion_id uuid NOT NULL,
 storage_key text NOT NULL CONSTRAINT uq_private_object_fences_storage_key UNIQUE
);
REVOKE ALL ON private_object_fences FROM PUBLIC;
CREATE TRIGGER trg_private_object_fences_guard BEFORE UPDATE OR DELETE ON private_object_fences
 FOR EACH ROW EXECUTE FUNCTION ras_object_delete_fences_guard();
CREATE TRIGGER trg_private_object_fences_no_truncate BEFORE TRUNCATE ON private_object_fences
 FOR EACH STATEMENT EXECUTE FUNCTION ras_object_delete_fences_guard();

ALTER TABLE revocation_journal_records ADD COLUMN private_deletion_id uuid CONSTRAINT uq_revocation_journal_records_private_deletion_id UNIQUE;
ALTER TABLE revocation_journal_records DROP CONSTRAINT ck_revocation_journal_records_kind_valid;
ALTER TABLE revocation_journal_records ADD CONSTRAINT ck_revocation_journal_records_kind_valid CHECK
 (kind IN ('genesis','revocation','recovery_begin','recovery_end','review_closure','session_revocation',
 'reset_token_closure','invitation_closure','credential_transition','account_status_decision','operator_recovery_case','private_deletion'));
ALTER TABLE revocation_journal_records ADD CONSTRAINT ck_revocation_journal_records_private_deletion_binding CHECK
 (CASE WHEN kind='private_deletion' THEN private_deletion_id IS NOT NULL
  AND private_deletion_id::text IS NOT DISTINCT FROM (line::jsonb #>> '{body,id}')
  ELSE private_deletion_id IS NULL END);

-- Canonical manifest independently reconstructed from all rows, including unversioned files.
CREATE FUNCTION ras_private_manifest(p_item uuid) RETURNS jsonb LANGUAGE sql STABLE
 SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('versions',COALESCE((SELECT jsonb_agg(id::text ORDER BY id)
  FROM paper_versions WHERE item_id=p_item),'[]'::jsonb),
 'files',COALESCE((SELECT jsonb_agg(jsonb_build_object('id',id::text,'version_id',version_id::text,
  'job_id',job_id::text,'storage_key',storage_key,'checksum_sha256',checksum_sha256) ORDER BY id)
  FROM source_files WHERE item_id=p_item),'[]'::jsonb))
$$;
CREATE FUNCTION ras_private_deletion_body(p_id uuid) RETURNS jsonb LANGUAGE sql STABLE
 SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('id',id::text,'item_id',item_id::text,'owner_user_id',owner_user_id::text,
 'rights_epoch',rights_epoch,'deleted_at',to_char(deleted_at,'YYYY-MM-DD"T"HH24:MI:SS.USOF'),
 'purge_due_at',to_char(purge_due_at,'YYYY-MM-DD"T"HH24:MI:SS.USOF'),'manifest',manifest)
 FROM private_item_deletions WHERE id=p_id
$$;
CREATE FUNCTION ras_private_deletion_matches(p_body jsonb,p_id uuid) RETURNS boolean LANGUAGE sql STABLE
 SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT COALESCE(EXISTS(SELECT 1 FROM private_item_deletions d WHERE d.id=p_id
  AND (p_body - 'deleted_at' - 'purge_due_at') IS NOT DISTINCT FROM
      (ras_private_deletion_body(p_id) - 'deleted_at' - 'purge_due_at')
  AND (p_body->>'deleted_at')::timestamptz IS NOT DISTINCT FROM d.deleted_at
  AND (p_body->>'purge_due_at')::timestamptz IS NOT DISTINCT FROM d.purge_due_at),false)
$$;
CREATE FUNCTION ras_private_snapshot_matches(p_id uuid) RETURNS boolean LANGUAGE sql STABLE
 SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT COALESCE((SELECT
  NOT EXISTS(SELECT 1 FROM jsonb_array_elements_text(d.manifest->'versions') x
   JOIN paper_versions v ON v.id=x::uuid WHERE v.item_id IS DISTINCT FROM d.item_id)
  AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(d.manifest->'files') x
   JOIN source_files f ON f.id=(x->>'id')::uuid WHERE f.item_id IS DISTINCT FROM d.item_id OR
    jsonb_build_object('id',f.id::text,'version_id',f.version_id::text,'job_id',f.job_id::text,
     'storage_key',f.storage_key,'checksum_sha256',f.checksum_sha256) IS DISTINCT FROM x)
  AND NOT EXISTS(SELECT 1 FROM paper_versions v WHERE v.item_id=d.item_id AND
   NOT (d.manifest->'versions') @> jsonb_build_array(v.id::text))
  AND NOT EXISTS(SELECT 1 FROM source_files f WHERE f.item_id=d.item_id AND
   NOT EXISTS(SELECT 1 FROM jsonb_array_elements(d.manifest->'files') x WHERE x->>'id'=f.id::text))
  FROM private_item_deletions d WHERE d.id=p_id),false)
$$;
CREATE FUNCTION ras_private_item_guard() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF TG_OP='INSERT' THEN
  IF NEW.deleted_at IS NOT NULL OR NEW.deletion_transaction_id IS NOT NULL
   OR EXISTS(SELECT 1 FROM private_item_deletions WHERE item_id=NEW.id) THEN
   RAISE EXCEPTION 'deleted item ID cannot be reused' USING ERRCODE='check_violation'; END IF;
 ELSE
  IF OLD.deleted_at IS NOT NULL THEN
   IF NEW.deleted_at IS DISTINCT FROM OLD.deleted_at OR NEW.rights_epoch IS DISTINCT FROM OLD.rights_epoch
    OR NEW.deletion_transaction_id IS DISTINCT FROM OLD.deletion_transaction_id
    OR NEW.active_version_id IS NOT NULL OR NEW.active_index_generation_id IS NOT NULL
    OR NEW.content_status IS DISTINCT FROM 'metadata_only'
    OR (NEW.title IS NOT NULL AND NEW.title IS DISTINCT FROM OLD.title) THEN
    RAISE EXCEPTION 'private deletion is final' USING ERRCODE='check_violation'; END IF;
  ELSIF NEW.deleted_at IS NOT NULL THEN
   IF OLD.visibility IS DISTINCT FROM 'private' OR NEW.rights_epoch IS DISTINCT FROM OLD.rights_epoch+1
    OR NEW.active_version_id IS NOT NULL OR NEW.active_index_generation_id IS NOT NULL
    OR NEW.content_status IS DISTINCT FROM 'metadata_only' THEN
    RAISE EXCEPTION 'invalid private deletion transition' USING ERRCODE='check_violation'; END IF;
   NEW.deletion_transaction_id:=txid_current();
  ELSIF NEW.deletion_transaction_id IS DISTINCT FROM OLD.deletion_transaction_id THEN
   RAISE EXCEPTION 'deletion transaction is derived' USING ERRCODE='check_violation'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_private_item_guard BEFORE INSERT OR UPDATE ON library_items
 FOR EACH ROW EXECUTE FUNCTION ras_private_item_guard();

CREATE FUNCTION ras_private_deletion_journaled() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE target uuid; d private_item_deletions;
BEGIN
 target:=(to_jsonb(NEW)->>(CASE WHEN TG_TABLE_NAME='library_items' THEN 'id' ELSE 'item_id' END))::uuid;
 SELECT * INTO d FROM private_item_deletions WHERE item_id=target;
 IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM revocation_journal_records j
  WHERE j.kind='private_deletion' AND j.private_deletion_id=d.id
   AND ras_private_deletion_matches(j.line::jsonb->'body',d.id)) THEN
  RAISE EXCEPTION 'private deletion requires exact journal decision' USING ERRCODE='check_violation'; END IF;
 IF EXISTS(SELECT 1 FROM library_items i WHERE i.id=target AND
  ROW(i.visibility,i.owner_user_id,i.deleted_at,i.rights_epoch) IS DISTINCT FROM
  ROW('private'::text,d.owner_user_id,d.deleted_at,d.rights_epoch)) THEN
  RAISE EXCEPTION 'private deletion target mismatch' USING ERRCODE='check_violation'; END IF;
 RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER trg_private_item_journaled AFTER UPDATE ON library_items
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN(OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL)
 EXECUTE FUNCTION ras_private_deletion_journaled();
CREATE CONSTRAINT TRIGGER trg_private_deletion_journaled AFTER INSERT ON private_item_deletions
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ras_private_deletion_journaled();

-- Existing derived triggers now fence both domains. Item lock precedes journal/citation lock.
CREATE OR REPLACE FUNCTION ras_derived_target_closed(target uuid) RETURNS boolean LANGUAGE plpgsql
 SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 PERFORM 1 FROM library_items WHERE id=target FOR KEY SHARE;
 RETURN EXISTS(SELECT 1 FROM library_items WHERE id=target AND
  (publication_state='revoked' OR deleted_at IS NOT NULL))
  OR EXISTS(SELECT 1 FROM deletion_tombstones WHERE item_id=target)
  OR EXISTS(SELECT 1 FROM private_item_deletions WHERE item_id=target);
END $$;

CREATE OR REPLACE FUNCTION ras_source_files_delete_fence() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF current_setting('transaction_isolation') IS DISTINCT FROM 'read committed' THEN
  RAISE EXCEPTION 'source file insert requires read committed' USING ERRCODE='check_violation'; END IF;
 IF ras_derived_target_closed(NEW.item_id) THEN
  RAISE EXCEPTION 'deleted source target' USING ERRCODE='check_violation'; END IF;
 PERFORM pg_advisory_xact_lock(1918989163,hashtext(NEW.storage_key));
 IF EXISTS(SELECT 1 FROM object_delete_fences WHERE storage_key=NEW.storage_key)
  OR EXISTS(SELECT 1 FROM orphan_object_fences WHERE storage_key=NEW.storage_key)
  OR EXISTS(SELECT 1 FROM private_object_fences WHERE storage_key=NEW.storage_key) THEN
  RAISE EXCEPTION 'object key is fenced for deletion' USING ERRCODE='check_violation'; END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION ras_private_resource_fence() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF ras_derived_target_closed(NEW.item_id) AND EXISTS(SELECT 1 FROM private_item_deletions WHERE item_id=NEW.item_id) THEN
  IF TG_OP='INSERT' THEN
   RAISE EXCEPTION 'private target deleted' USING ERRCODE='check_violation'; END IF;
  IF TG_TABLE_NAME='ingestion_jobs' THEN
   IF NOT NEW.cancel_requested OR NEW.lease_token IS NOT NULL
    OR NEW.state NOT IN ('cancelled','ready','duplicate','needs_review','terminal_failed','fulltext_rejected')
    OR (to_jsonb(NEW)-ARRAY['state','cancel_requested','cancel_requested_at','finished_at','updated_at',
     'lease_token','lease_until','next_attempt_at','last_error_code','source_label','warnings']) IS DISTINCT FROM
       (to_jsonb(OLD)-ARRAY['state','cancel_requested','cancel_requested_at','finished_at','updated_at',
     'lease_token','lease_until','next_attempt_at','last_error_code','source_label','warnings'])
    OR (NEW.last_error_code IS DISTINCT FROM OLD.last_error_code AND NEW.last_error_code IS NOT NULL AND NEW.last_error_code<>'cancelled')
    OR (NEW.source_label IS NOT NULL AND NEW.source_label IS DISTINCT FROM OLD.source_label)
    OR (NEW.warnings <> '[]'::jsonb AND NEW.warnings IS DISTINCT FROM OLD.warnings) THEN
    RAISE EXCEPTION 'deleted job cannot restart' USING ERRCODE='check_violation'; END IF;
  ELSIF (to_jsonb(NEW)-ARRAY['source_identifier','source_url','version_label','coverage']) IS DISTINCT FROM
        (to_jsonb(OLD)-ARRAY['source_identifier','source_url','version_label','coverage'])
    OR NEW.source_identifier IS NOT NULL OR NEW.source_url IS NOT NULL OR NEW.version_label IS NOT NULL
    OR NEW.coverage IS NOT NULL THEN
   RAISE EXCEPTION 'deleted version only permits scrub' USING ERRCODE='check_violation';
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_private_jobs_fence BEFORE INSERT OR UPDATE ON ingestion_jobs
 FOR EACH ROW EXECUTE FUNCTION ras_private_resource_fence();
CREATE TRIGGER trg_private_versions_fence BEFORE INSERT OR UPDATE ON paper_versions
 FOR EACH ROW EXECUTE FUNCTION ras_private_resource_fence();
CREATE TRIGGER trg_private_membership_fence BEFORE INSERT OR UPDATE ON collection_items
 FOR EACH ROW EXECUTE FUNCTION ras_item_derived_fence();
CREATE FUNCTION ras_private_review_insert_guard() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF NEW.declared_source IS NULL OR NEW.declared_rights IS NULL OR ras_derived_target_closed(NEW.item_id) THEN
  RAISE EXCEPTION 'review requires live target and declaration' USING ERRCODE='check_violation'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_private_review_insert_fence BEFORE INSERT ON review_requests
 FOR EACH ROW EXECUTE FUNCTION ras_private_review_insert_guard();

CREATE FUNCTION ras_private_intent_fence() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF NEW.filename IS NULL AND (TG_OP='INSERT' OR NEW.item_id IS NULL OR
  NOT EXISTS(SELECT 1 FROM private_item_deletions WHERE item_id=NEW.item_id)) THEN
  RAISE EXCEPTION 'live intent requires filename' USING ERRCODE='check_violation'; END IF;
 IF NEW.item_id IS NOT NULL AND EXISTS(SELECT 1 FROM private_item_deletions WHERE item_id=NEW.item_id) THEN
  IF TG_OP='INSERT' THEN RAISE EXCEPTION 'deleted intent target' USING ERRCODE='check_violation'; END IF;
  IF (to_jsonb(NEW)-'filename') IS DISTINCT FROM (to_jsonb(OLD)-'filename')
   OR (NEW.filename IS NOT NULL AND NEW.filename IS DISTINCT FROM OLD.filename) THEN
   RAISE EXCEPTION 'deleted intent only permits scrub' USING ERRCODE='check_violation'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_private_intent_fence BEFORE INSERT OR UPDATE ON upload_intents
 FOR EACH ROW EXECUTE FUNCTION ras_private_intent_fence();

CREATE FUNCTION ras_auth_private_delete_mirror(p_lines text[]) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE line text; e jsonb; b jsonb; d private_item_deletions; r review_requests; i library_items; last_seq bigint;
BEGIN
 IF p_lines IS NULL OR cardinality(p_lines) NOT BETWEEN 1 AND 129 THEN
  RAISE EXCEPTION 'invalid deletion batch' USING ERRCODE='check_violation'; END IF;
 b:=(p_lines[cardinality(p_lines)]::jsonb)->'body';
 SELECT * INTO d FROM private_item_deletions WHERE id=(b->>'id')::uuid;
 SELECT * INTO i FROM library_items WHERE id=d.item_id;
 IF d.id IS NULL OR NOT ras_private_deletion_matches(b,d.id) OR i.id IS NULL
  OR ROW(i.visibility,i.owner_user_id,i.deleted_at,i.rights_epoch,i.deletion_transaction_id)
   IS DISTINCT FROM ROW('private'::text,d.owner_user_id,d.deleted_at,d.rights_epoch,txid_current())
  OR NOT EXISTS(SELECT 1 FROM library_items WHERE id=i.id AND xmin=(txid_current()%4294967296)::text::xid)
  OR NOT EXISTS(SELECT 1 FROM private_item_deletions WHERE id=d.id AND xmin=(txid_current()%4294967296)::text::xid)
  OR d.manifest IS DISTINCT FROM ras_private_manifest(d.item_id)
  OR EXISTS(SELECT 1 FROM source_files WHERE item_id=d.item_id AND storage_key !~ '^originals/[0-9a-f]{32}$')
  OR jsonb_array_length(d.manifest->'versions')>128 OR jsonb_array_length(d.manifest->'files')>128
  OR EXISTS(SELECT 1 FROM ingestion_jobs WHERE item_id=d.item_id AND NOT cancel_requested)
  OR EXISTS(SELECT 1 FROM review_requests WHERE item_id=d.item_id AND status='open') THEN
  RAISE EXCEPTION 'deletion does not bind transaction' USING ERRCODE='check_violation'; END IF;
 SELECT seq INTO last_seq FROM revocation_journal_records ORDER BY seq DESC LIMIT 1;
 FOREACH line IN ARRAY p_lines LOOP
  e:=line::jsonb; b:=e->'body';
  IF (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(e) key)
    IS DISTINCT FROM ARRAY['body','hash','kind','prev','seq']::text[]
   OR jsonb_typeof(e->'seq') IS DISTINCT FROM 'number' OR (e->>'seq') !~ '^[1-9][0-9]*$'
   OR (e->>'seq')::bigint IS DISTINCT FROM last_seq+1
   OR (e->>'prev') IS DISTINCT FROM (SELECT record_hash FROM revocation_journal_records WHERE seq=last_seq)
   OR jsonb_typeof(e->'hash') IS DISTINCT FROM 'string' OR (e->>'hash') !~ '^[0-9a-f]{64}$'
   OR length(line)>65535 THEN
   RAISE EXCEPTION 'invalid deletion envelope' USING ERRCODE='check_violation'; END IF;
  IF line=p_lines[cardinality(p_lines)] THEN
   IF e->>'kind' IS DISTINCT FROM 'private_deletion' OR NOT ras_private_deletion_matches(b,d.id) THEN
    RAISE EXCEPTION 'invalid deletion decision' USING ERRCODE='check_violation'; END IF;
   INSERT INTO revocation_journal_records(seq,kind,record_hash,line,applied_via,result,journal_confirmed,private_deletion_id)
    VALUES((e->>'seq')::bigint,'private_deletion',e->>'hash',line,'close','deleted',false,d.id);
  ELSE
   SELECT * INTO r FROM review_requests WHERE id=(b->>'request_id')::uuid;
   IF e->>'kind' IS DISTINCT FROM 'review_closure' OR r.id IS NULL
    OR ROW(r.item_id,r.requester_user_id,r.closed_by_user_id,r.closed_at,r.status,r.close_reason_code,r.closure_transaction_id)
     IS DISTINCT FROM ROW(d.item_id,d.owner_user_id,d.owner_user_id,d.deleted_at,'withdrawn'::text,'withdrawn_by_requester'::text,txid_current())
    OR (b-'closed_at') IS DISTINCT FROM jsonb_build_object('request_id',r.id::text,'item_id',r.item_id::text,
     'version_id',r.version_id::text,'source_file_id',r.source_file_id::text,'status',r.status,'reason_code',r.close_reason_code)
    OR (b->>'closed_at')::timestamptz IS DISTINCT FROM r.closed_at THEN
    RAISE EXCEPTION 'invalid deletion review closure' USING ERRCODE='check_violation'; END IF;
   INSERT INTO revocation_journal_records(seq,kind,record_hash,line,applied_via,result,journal_confirmed,review_request_id)
    VALUES((e->>'seq')::bigint,'review_closure',e->>'hash',line,'close','closed',false,r.id);
  END IF;
  last_seq:=(e->>'seq')::bigint;
 END LOOP;
 SET CONSTRAINTS ALL IMMEDIATE;
 SET CONSTRAINTS ALL DEFERRED;
END $$;
CREATE FUNCTION ras_auth_private_delete_confirm(p_lines text[]) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE v_line text;
BEGIN
 FOREACH v_line IN ARRAY p_lines LOOP
  UPDATE revocation_journal_records j SET journal_confirmed=true WHERE j.line=v_line AND NOT j.journal_confirmed
   AND j.xmin=(txid_current()%4294967296)::text::xid AND j.applied_via='close'
   AND (j.kind='private_deletion' OR (j.kind='review_closure' AND EXISTS(SELECT 1 FROM review_requests r
     WHERE r.id=j.review_request_id AND r.closure_transaction_id=txid_current())));
  IF NOT FOUND THEN RAISE EXCEPTION 'deletion confirm mismatch' USING ERRCODE='check_violation'; END IF;
 END LOOP;
END $$;

CREATE FUNCTION ras_auth_private_cleanup_fence(p_id uuid,p_key text) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE d private_item_deletions;
BEGIN
 IF p_id IS NULL OR p_key IS NULL OR current_setting('transaction_isolation') IS DISTINCT FROM 'read committed' THEN
  RAISE EXCEPTION 'invalid private fence' USING ERRCODE='check_violation'; END IF;
 PERFORM pg_advisory_xact_lock(1918989163,hashtext(p_key));
 SELECT * INTO d FROM private_item_deletions WHERE id=p_id;
 IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM jsonb_array_elements(d.manifest->'files') f WHERE f->>'storage_key'=p_key)
  OR NOT ras_private_snapshot_matches(p_id)
  OR NOT EXISTS(SELECT 1 FROM revocation_journal_records j WHERE j.private_deletion_id=d.id
   AND j.kind='private_deletion' AND j.journal_confirmed AND ras_private_deletion_matches(j.line::jsonb->'body',d.id))
  OR EXISTS(SELECT 1 FROM library_items i WHERE i.id=d.item_id AND
   ROW(i.visibility,i.owner_user_id,i.deleted_at,i.rights_epoch) IS DISTINCT FROM
    ROW('private'::text,d.owner_user_id,d.deleted_at,d.rights_epoch)) THEN
  RAISE EXCEPTION 'private fence binding mismatch' USING ERRCODE='check_violation'; END IF;
 IF EXISTS(SELECT 1 FROM source_files f LEFT JOIN library_items i ON i.id=f.item_id
  WHERE f.storage_key=p_key AND (i.id IS NULL OR
   (i.deleted_at IS NULL AND i.publication_state IS DISTINCT FROM 'revoked'))) THEN
  IF EXISTS(SELECT 1 FROM private_object_fences WHERE storage_key=p_key) THEN
   RAISE EXCEPTION 'private fence reference conflict' USING ERRCODE='check_violation'; END IF;
  RETURN 'shared_kept'; END IF;
 IF EXISTS(SELECT 1 FROM private_object_fences WHERE storage_key=p_key) THEN RETURN 'already_fenced'; END IF;
 INSERT INTO private_object_fences(deletion_id,storage_key) VALUES(p_id,p_key);
 RETURN 'fenced';
END $$;
REVOKE ALL ON FUNCTION ras_auth_private_delete_mirror(text[]),ras_auth_private_delete_confirm(text[]),
 ras_auth_private_cleanup_fence(uuid,text) FROM PUBLIC;

CREATE FUNCTION ras_private_deletion_guard() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
  RAISE EXCEPTION 'private deletion decisions are permanent' USING ERRCODE='check_violation'; END IF;
 IF (to_jsonb(NEW)-ARRAY['purged_at','cleanup_attempts','last_cleanup_error']) IS DISTINCT FROM
    (to_jsonb(OLD)-ARRAY['purged_at','cleanup_attempts','last_cleanup_error'])
  OR NEW.cleanup_attempts<OLD.cleanup_attempts OR (OLD.purged_at IS NOT NULL AND NEW.purged_at IS DISTINCT FROM OLD.purged_at) THEN
  RAISE EXCEPTION 'private deletion only records cleanup progress' USING ERRCODE='check_violation'; END IF;
 IF NEW.purged_at IS NOT NULL AND (
  cardinality(ras_common_copy_derived_left(NEW.item_id))>0
  OR EXISTS(SELECT 1 FROM library_items WHERE id=NEW.item_id AND
    (title IS NOT NULL OR active_version_id IS NOT NULL OR deleted_at IS DISTINCT FROM NEW.deleted_at))
  OR EXISTS(SELECT 1 FROM paper_versions WHERE item_id=NEW.item_id AND
    num_nonnulls(version_label,source_identifier,source_url,coverage)>0)
  OR EXISTS(SELECT 1 FROM source_files WHERE item_id=NEW.item_id AND num_nonnulls(original_filename,file_source)>0)
  OR EXISTS(SELECT 1 FROM review_requests WHERE item_id=NEW.item_id AND
    num_nonnulls(declared_source,declared_rights,declared_source_url,declared_license_url,rejection_note)>0)
  OR EXISTS(SELECT 1 FROM upload_intents WHERE item_id=NEW.item_id AND filename IS NOT NULL)
  OR EXISTS(SELECT 1 FROM ingestion_jobs WHERE item_id=NEW.item_id AND
    (source_label IS NOT NULL OR warnings<>'[]'::jsonb OR NOT cancel_requested OR lease_token IS NOT NULL))
  OR EXISTS(SELECT 1 FROM collection_items WHERE item_id=NEW.item_id)) THEN
  RAISE EXCEPTION 'private cleanup incomplete' USING ERRCODE='check_violation'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_private_deletion_guard BEFORE UPDATE OR DELETE ON private_item_deletions
 FOR EACH ROW EXECUTE FUNCTION ras_private_deletion_guard();
CREATE TRIGGER trg_private_deletion_no_truncate BEFORE TRUNCATE ON private_item_deletions
 FOR EACH STATEMENT EXECUTE FUNCTION ras_private_deletion_guard();
