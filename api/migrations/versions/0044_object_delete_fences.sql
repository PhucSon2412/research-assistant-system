-- C2d-5e1: durable per-key object deletion fence for revoked common cleanup.
--
-- A fence row authorises deleting ONE object key. It holds the key, the
-- tombstone tuple that authorised it and a DB time only: no content, file
-- name, URL or rights note. It is permanent (no UPDATE/DELETE/TRUNCATE), is
-- never removed with derived rows and survives purged_at.
--
-- Serialisation: the prepare routine and every source_files INSERT take the
-- SAME transaction advisory lock (two-int namespace below, hashtext(key)).
-- Prepare takes only that lock and scalar reads (no row lock), so the lock
-- graph is rows -> key -> journal for inserters and key -> journal for
-- prepare; cleanup finalisation never takes the key lock. A hash collision
-- only serialises two keys conservatively; it never makes them one object.
-- READ COMMITTED is required on both sides: a later statement snapshot after
-- the lock wait sees the winner's committed row. Other isolation levels are
-- refused instead of trusting an older snapshot.

CREATE TABLE object_delete_fences (
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 storage_key text NOT NULL,
 tombstone_id uuid NOT NULL,
 item_id uuid NOT NULL,
 source_file_id uuid,
 rights_epoch bigint NOT NULL,
 fenced_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT pk_object_delete_fences PRIMARY KEY (id),
 CONSTRAINT uq_object_delete_fences_storage_key UNIQUE (storage_key),
 CONSTRAINT ck_object_delete_fences_storage_key_not_empty CHECK (length(storage_key) > 0),
 CONSTRAINT ck_object_delete_fences_epoch_positive CHECK (rights_epoch > 0)
);
REVOKE ALL ON object_delete_fences FROM PUBLIC;

CREATE FUNCTION ras_object_delete_fences_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 RAISE EXCEPTION 'object deletion fences are permanent' USING ERRCODE='check_violation';
END $$;
CREATE TRIGGER trg_object_delete_fences_guard BEFORE UPDATE OR DELETE ON object_delete_fences
 FOR EACH ROW EXECUTE FUNCTION ras_object_delete_fences_guard();
CREATE TRIGGER trg_object_delete_fences_no_truncate BEFORE TRUNCATE ON object_delete_fences
 FOR EACH STATEMENT EXECUTE FUNCTION ras_object_delete_fences_guard();

-- Invoker trigger: every inserter, including raw runtime SQL and the owner.
CREATE FUNCTION ras_source_files_delete_fence() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF current_setting('transaction_isolation') IS DISTINCT FROM 'read committed' THEN
  RAISE EXCEPTION 'source file insert requires read committed' USING ERRCODE='check_violation'; END IF;
 PERFORM pg_advisory_xact_lock(1918989163, hashtext(NEW.storage_key));
 IF EXISTS (SELECT 1 FROM object_delete_fences WHERE storage_key = NEW.storage_key) THEN
  RAISE EXCEPTION 'object key is fenced for deletion' USING ERRCODE='check_violation'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_source_files_delete_fence BEFORE INSERT ON source_files
 FOR EACH ROW EXECUTE FUNCTION ras_source_files_delete_fence();

-- The only fence writer. Exact confirmed tombstone binding, NULL-safe. Returns
-- 'fenced' (new row), 'already_fenced' or 'shared_kept' (a protected reference
-- exists: no deletion authority). Any mismatch raises; nothing is inserted.
CREATE FUNCTION ras_auth_cleanup_fence(p_tombstone uuid, p_item uuid, p_source_file uuid, p_key text, p_epoch bigint)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE t deletion_tombstones; i library_items; f source_files;
BEGIN
 IF p_tombstone IS NULL OR p_item IS NULL OR p_key IS NULL OR p_epoch IS NULL OR length(p_key) = 0 THEN
  RAISE EXCEPTION 'invalid fence binding' USING ERRCODE='check_violation'; END IF;
 IF current_setting('transaction_isolation') IS DISTINCT FROM 'read committed' THEN
  RAISE EXCEPTION 'fence prepare requires read committed' USING ERRCODE='check_violation'; END IF;
 PERFORM pg_advisory_xact_lock(1918989163, hashtext(p_key));
 SELECT * INTO t FROM deletion_tombstones WHERE id = p_tombstone;
 IF NOT FOUND OR t.target_kind IS DISTINCT FROM 'common_copy' OR t.item_id IS DISTINCT FROM p_item
  OR t.source_file_id IS DISTINCT FROM p_source_file OR t.storage_key IS DISTINCT FROM p_key
  OR t.rights_epoch IS DISTINCT FROM p_epoch OR t.purged_at IS NOT NULL
  OR NOT EXISTS (SELECT 1 FROM revocation_journal_records r
                 WHERE r.tombstone_id = t.id AND r.kind = 'revocation' AND r.journal_confirmed) THEN
  RAISE EXCEPTION 'fence binding mismatch' USING ERRCODE='check_violation'; END IF;
 SELECT * INTO i FROM library_items WHERE id = p_item;
 -- An absent target is allowed only through the confirmed tombstone above.
 IF FOUND AND (i.visibility IS DISTINCT FROM 'common' OR i.publication_state IS DISTINCT FROM 'revoked'
               OR i.rights_epoch IS DISTINCT FROM p_epoch) THEN
  RAISE EXCEPTION 'fence target not revoked' USING ERRCODE='check_violation'; END IF;
 IF p_source_file IS NOT NULL THEN
  SELECT * INTO f FROM source_files WHERE id = p_source_file;
  IF FOUND AND (f.storage_key IS DISTINCT FROM p_key OR f.item_id IS DISTINCT FROM p_item) THEN
   RAISE EXCEPTION 'fence file binding mismatch' USING ERRCODE='check_violation'; END IF;
 END IF;
 -- Every row naming the key that is not a revoked copy protects the object:
 -- a private file, a live/pending common copy, a file without item or with
 -- an absent item.
 IF EXISTS (SELECT 1 FROM source_files sf LEFT JOIN library_items li ON li.id = sf.item_id
            WHERE sf.storage_key = p_key AND (li.id IS NULL OR li.publication_state IS DISTINCT FROM 'revoked')) THEN
  IF EXISTS (SELECT 1 FROM object_delete_fences WHERE storage_key = p_key) THEN
   RAISE EXCEPTION 'fenced key has a protected reference' USING ERRCODE='check_violation'; END IF;
  RETURN 'shared_kept';
 END IF;
 IF EXISTS (SELECT 1 FROM object_delete_fences WHERE storage_key = p_key) THEN
  RETURN 'already_fenced';
 END IF;
 INSERT INTO object_delete_fences(storage_key, tombstone_id, item_id, source_file_id, rights_epoch)
  VALUES (p_key, p_tombstone, p_item, p_source_file, p_epoch);
 RETURN 'fenced';
END $$;
REVOKE ALL ON FUNCTION ras_auth_cleanup_fence(uuid,uuid,uuid,text,bigint) FROM PUBLIC;

-- Already provisioned installs: same definer/login discovery as 0039. Login
-- roles get SELECT only (the INSERT trigger reads fences as invoker); the
-- definer gets SELECT/INSERT; runtime (session-create login) gets EXECUTE.
DO $$ DECLARE df name; lr name; BEGIN
 SELECT pg_get_userbyid(proowner) INTO df FROM pg_proc
  WHERE oid='ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure;
 EXECUTE format('GRANT SELECT,INSERT ON object_delete_fences TO %I',df);
 EXECUTE format('ALTER FUNCTION ras_auth_cleanup_fence(uuid,uuid,uuid,text,bigint) OWNER TO %I',df);
 FOR lr IN SELECT DISTINCT r.rolname FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a
  JOIN pg_roles r ON r.oid=a.grantee WHERE p.oid IN
   ('ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure,'ras_auth_restrict(uuid)'::regprocedure)
  AND a.privilege_type='EXECUTE' AND r.rolcanlogin AND r.rolname<>df
 LOOP EXECUTE format('GRANT SELECT ON object_delete_fences TO %I',lr); END LOOP;
 FOR lr IN SELECT DISTINCT r.rolname FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a
  JOIN pg_roles r ON r.oid=a.grantee WHERE p.oid=
   'ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure
  AND a.privilege_type='EXECUTE' AND r.rolcanlogin AND r.rolname<>df
 LOOP EXECUTE format('GRANT EXECUTE ON FUNCTION ras_auth_cleanup_fence(uuid,uuid,uuid,text,bigint) TO %I',lr); END LOOP;
END $$;
