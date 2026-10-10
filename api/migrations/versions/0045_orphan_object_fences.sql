-- C2d-5e2: durable per-key fence for manual orphan original reconcile.
--
-- An orphan fence authorises deleting ONE original object key that NO
-- source_files row names anywhere in the database (private, common in any
-- state, item absent, odd binding). It is separate from the revoked-common
-- fence of 0044 (object_delete_fences, bound to a journal-confirmed
-- tombstone): there is no tombstone, item or rights epoch here and none is
-- faked. Columns: the exact key, the reconcile run that fenced it, the HEAD
-- size/Last-Modified it was observed with, the grace cutoff used and an
-- application time. No content, file name, URL, checksum/ETag or note.
-- Permanent: no UPDATE/DELETE/TRUNCATE, also not by the owner.
--
-- Completion (deleted/absent/kept) is a separate append-only row per fence so
-- the fence itself stays immutable; at most one completion per fence.
--
-- Serialisation: the orphan routine and every source_files INSERT take the
-- SAME transaction advisory lock as 0044 (namespace 1918989163,
-- hashtext(key)). Prepare takes only that lock and plain reads (no row lock):
-- inserter rows -> key -> journal, prepare key -> journal, completion
-- (fence row KEY SHARE via FK) -> journal; nothing takes a row or key lock
-- after the journal. READ COMMITTED on both sides: the statement after the
-- lock wait sees the winner's committed row; other isolation levels are
-- refused instead of trusting an older snapshot.

CREATE TABLE orphan_object_fences (
 id uuid NOT NULL DEFAULT gen_random_uuid(),
 storage_key text NOT NULL,
 run_id uuid NOT NULL,
 observed_size bigint NOT NULL,
 observed_last_modified timestamptz NOT NULL,
 grace_cutoff timestamptz NOT NULL,
 fenced_at timestamptz NOT NULL,
 CONSTRAINT pk_orphan_object_fences PRIMARY KEY (id),
 CONSTRAINT uq_orphan_object_fences_storage_key UNIQUE (storage_key),
 CONSTRAINT ck_orphan_object_fences_original_key CHECK (storage_key ~ '^originals/[0-9a-f]{32}$'),
 CONSTRAINT ck_orphan_object_fences_size_not_negative CHECK (observed_size >= 0),
 CONSTRAINT ck_orphan_object_fences_older_than_cutoff CHECK (observed_last_modified < grace_cutoff),
 CONSTRAINT ck_orphan_object_fences_cutoff_before_fence CHECK (grace_cutoff < fenced_at)
);
REVOKE ALL ON orphan_object_fences FROM PUBLIC;

CREATE TABLE orphan_object_completions (
 fence_id uuid NOT NULL,
 outcome text NOT NULL,
 run_id uuid NOT NULL,
 completed_at timestamptz NOT NULL,
 CONSTRAINT pk_orphan_object_completions PRIMARY KEY (fence_id),
 CONSTRAINT fk_orphan_object_completions_fence_id_orphan_object_fences FOREIGN KEY (fence_id)
  REFERENCES orphan_object_fences (id),
 CONSTRAINT ck_orphan_object_completions_outcome_valid
  CHECK (outcome IN ('deleted', 'already_absent', 'kept_identity_changed'))
);
REVOKE ALL ON orphan_object_completions FROM PUBLIC;

-- Listing position hint only (never deletion authority). Runtime DML like
-- other operational tables; one row.
CREATE TABLE orphan_reconcile_cursors (
 id smallint NOT NULL,
 start_after text,
 updated_at timestamptz NOT NULL,
 CONSTRAINT pk_orphan_reconcile_cursors PRIMARY KEY (id),
 CONSTRAINT ck_orphan_reconcile_cursors_singleton CHECK (id = 1),
 CONSTRAINT ck_orphan_reconcile_cursors_original_prefix
  CHECK (start_after IS NULL OR (start_after LIKE 'originals/%' AND length(start_after) <= 1024))
);

CREATE FUNCTION ras_orphan_fences_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 RAISE EXCEPTION 'orphan object fences are permanent' USING ERRCODE='check_violation';
END $$;
CREATE TRIGGER trg_orphan_object_fences_guard BEFORE UPDATE OR DELETE ON orphan_object_fences
 FOR EACH ROW EXECUTE FUNCTION ras_orphan_fences_guard();
CREATE TRIGGER trg_orphan_object_fences_no_truncate BEFORE TRUNCATE ON orphan_object_fences
 FOR EACH STATEMENT EXECUTE FUNCTION ras_orphan_fences_guard();
CREATE TRIGGER trg_orphan_object_completions_guard BEFORE UPDATE OR DELETE ON orphan_object_completions
 FOR EACH ROW EXECUTE FUNCTION ras_orphan_fences_guard();
CREATE TRIGGER trg_orphan_object_completions_no_truncate BEFORE TRUNCATE ON orphan_object_completions
 FOR EACH STATEMENT EXECUTE FUNCTION ras_orphan_fences_guard();

-- The 0044 invoker trigger now refuses a key fenced by EITHER authority.
-- Same lock, same isolation rule, same message; 0044's file is unchanged.
CREATE OR REPLACE FUNCTION ras_source_files_delete_fence() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF current_setting('transaction_isolation') IS DISTINCT FROM 'read committed' THEN
  RAISE EXCEPTION 'source file insert requires read committed' USING ERRCODE='check_violation'; END IF;
 PERFORM pg_advisory_xact_lock(1918989163, hashtext(NEW.storage_key));
 IF EXISTS (SELECT 1 FROM object_delete_fences WHERE storage_key = NEW.storage_key)
    OR EXISTS (SELECT 1 FROM orphan_object_fences WHERE storage_key = NEW.storage_key) THEN
  RAISE EXCEPTION 'object key is fenced for deletion' USING ERRCODE='check_violation'; END IF;
 RETURN NEW;
END $$;

-- Orphan fence writer. NULL-safe, exact original key format. Returns
-- 'fenced' (new row), 'already_fenced' or 'referenced' (some source_files row
-- names the key: no authority, nothing written). Any invalid input raises.
CREATE FUNCTION ras_auth_orphan_fence(p_run uuid, p_key text, p_size bigint, p_last_modified timestamptz,
                                      p_cutoff timestamptz, p_at timestamptz)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF p_run IS NULL OR p_key IS NULL OR p_size IS NULL OR p_last_modified IS NULL OR p_cutoff IS NULL OR p_at IS NULL
    OR p_key !~ '^originals/[0-9a-f]{32}$' OR p_size < 0
    OR NOT (p_last_modified < p_cutoff) OR NOT (p_cutoff < p_at) THEN
  RAISE EXCEPTION 'invalid orphan fence binding' USING ERRCODE='check_violation'; END IF;
 IF current_setting('transaction_isolation') IS DISTINCT FROM 'read committed' THEN
  RAISE EXCEPTION 'orphan fence requires read committed' USING ERRCODE='check_violation'; END IF;
 PERFORM pg_advisory_xact_lock(1918989163, hashtext(p_key));
 -- Zero references in the whole database, whatever the item/visibility/state.
 IF EXISTS (SELECT 1 FROM source_files WHERE storage_key = p_key) THEN
  IF EXISTS (SELECT 1 FROM orphan_object_fences WHERE storage_key = p_key) THEN
   RAISE EXCEPTION 'orphan fenced key has a reference' USING ERRCODE='check_violation'; END IF;
  RETURN 'referenced';
 END IF;
 IF EXISTS (SELECT 1 FROM orphan_object_fences WHERE storage_key = p_key) THEN
  RETURN 'already_fenced';
 END IF;
 INSERT INTO orphan_object_fences(storage_key, run_id, observed_size, observed_last_modified, grace_cutoff, fenced_at)
  VALUES (p_key, p_run, p_size, p_last_modified, p_cutoff, p_at);
 RETURN 'fenced';
END $$;
REVOKE ALL ON FUNCTION ras_auth_orphan_fence(uuid,text,bigint,timestamptz,timestamptz,timestamptz) FROM PUBLIC;

-- One completion per fence after storage verification. Returns 'completed'
-- or 'already_completed'. A reference to the key (only possible if the INSERT
-- trigger was bypassed) or a mismatched binding raises.
CREATE FUNCTION ras_auth_orphan_complete(p_fence uuid, p_key text, p_run uuid, p_outcome text, p_at timestamptz)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE f orphan_object_fences;
BEGIN
 IF p_fence IS NULL OR p_key IS NULL OR p_run IS NULL OR p_outcome IS NULL OR p_at IS NULL
    OR p_outcome NOT IN ('deleted', 'already_absent', 'kept_identity_changed') THEN
  RAISE EXCEPTION 'invalid orphan completion binding' USING ERRCODE='check_violation'; END IF;
 SELECT * INTO f FROM orphan_object_fences WHERE id = p_fence;
 IF NOT FOUND OR f.storage_key IS DISTINCT FROM p_key OR p_at < f.fenced_at THEN
  RAISE EXCEPTION 'orphan completion binding mismatch' USING ERRCODE='check_violation'; END IF;
 IF EXISTS (SELECT 1 FROM source_files WHERE storage_key = p_key) THEN
  RAISE EXCEPTION 'orphan fenced key has a reference' USING ERRCODE='check_violation'; END IF;
 INSERT INTO orphan_object_completions(fence_id, outcome, run_id, completed_at)
  VALUES (p_fence, p_outcome, p_run, p_at) ON CONFLICT (fence_id) DO NOTHING;
 IF NOT FOUND THEN
  RETURN 'already_completed';
 END IF;
 RETURN 'completed';
END $$;
REVOKE ALL ON FUNCTION ras_auth_orphan_complete(uuid,text,uuid,text,timestamptz) FROM PUBLIC;

-- Already provisioned installs: same definer/login discovery as 0039/0044.
-- Login roles read fences (the INSERT trigger runs as invoker) and use the
-- cursor like any operational table; the definer reads source_files (0041)
-- and writes fences/completions; only runtime (session-create login)
-- executes the two routines.
DO $$ DECLARE df name; lr name; BEGIN
 SELECT pg_get_userbyid(proowner) INTO df FROM pg_proc
  WHERE oid='ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure;
 EXECUTE format('GRANT SELECT,INSERT ON orphan_object_fences,orphan_object_completions TO %I',df);
 EXECUTE format('GRANT SELECT ON source_files TO %I',df);
 EXECUTE format('ALTER FUNCTION ras_auth_orphan_fence(uuid,text,bigint,timestamptz,timestamptz,timestamptz) OWNER TO %I',df);
 EXECUTE format('ALTER FUNCTION ras_auth_orphan_complete(uuid,text,uuid,text,timestamptz) OWNER TO %I',df);
 FOR lr IN SELECT DISTINCT r.rolname FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a
  JOIN pg_roles r ON r.oid=a.grantee WHERE p.oid IN
   ('ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure,'ras_auth_restrict(uuid)'::regprocedure)
  AND a.privilege_type='EXECUTE' AND r.rolcanlogin AND r.rolname<>df
 LOOP
  EXECUTE format('GRANT SELECT ON orphan_object_fences,orphan_object_completions TO %I',lr);
  EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON orphan_reconcile_cursors TO %I',lr);
 END LOOP;
 FOR lr IN SELECT DISTINCT r.rolname FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a
  JOIN pg_roles r ON r.oid=a.grantee WHERE p.oid=
   'ras_auth_session_create(uuid,uuid,uuid,text,text,timestamptz,timestamptz)'::regprocedure
  AND a.privilege_type='EXECUTE' AND r.rolcanlogin AND r.rolname<>df
 LOOP
  EXECUTE format('GRANT EXECUTE ON FUNCTION ras_auth_orphan_fence(uuid,text,bigint,timestamptz,timestamptz,timestamptz) TO %I',lr);
  EXECUTE format('GRANT EXECUTE ON FUNCTION ras_auth_orphan_complete(uuid,text,uuid,text,timestamptz) TO %I',lr);
 END LOOP;
END $$;
