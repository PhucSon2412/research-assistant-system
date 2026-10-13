-- Durable proof uses stored parent/receipt transaction IDs and exact events,
-- never historical xmin (which changes during pg_dump/pg_restore).
CREATE FUNCTION ras_auth_initial_pair(p_operation uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS (
  SELECT 1 FROM auth_change_receipts r
  JOIN auth_change_receipts c ON c.user_id=r.user_id AND c.domain='credential'
   AND c.reason_code=r.reason_code AND c.transaction_id=r.transaction_id AND c.recorded_at=r.recorded_at
  JOIN auth_change_receipts s ON s.user_id=c.user_id AND s.domain='status'
   AND s.reason_code=c.reason_code AND s.transaction_id=c.transaction_id AND s.recorded_at=c.recorded_at
  JOIN users u ON u.id=r.user_id
  JOIN revocation_journal_records cj ON
   ROW(cj.auth_operation_id,cj.auth_user_id,cj.auth_domain,cj.auth_generation,cj.auth_decision,cj.auth_reason_code,cj.auth_recorded_at)
    IS NOT DISTINCT FROM ROW(c.operation_id,c.user_id,c.domain,c.generation,c.decision,c.reason_code,c.recorded_at)
  JOIN revocation_journal_records sj ON
   ROW(sj.auth_operation_id,sj.auth_user_id,sj.auth_domain,sj.auth_generation,sj.auth_decision,sj.auth_reason_code,sj.auth_recorded_at)
    IS NOT DISTINCT FROM ROW(s.operation_id,s.user_id,s.domain,s.generation,s.decision,s.reason_code,s.recorded_at)
  JOIN recovery_registrations reg ON reg.id=1
  JOIN revocation_journal_records g ON g.seq=1 AND g.kind='genesis' AND g.journal_confirmed
  WHERE r.operation_id=p_operation AND r.operation_id IN (c.operation_id,s.operation_id)
   AND c.decision='changed' AND s.decision='active' AND c.transaction_id IS NOT NULL
   AND u.created_at=c.recorded_at AND sj.seq=cj.seq+1
   AND reg.journal_id IS NOT NULL AND reg.genesis_hash IS NOT NULL AND g.record_hash=reg.genesis_hash
   AND ROW(g.line::jsonb->'seq',g.line::jsonb->>'kind',g.line::jsonb->>'hash',g.line::jsonb->>'prev',
           g.line::jsonb#>>'{body,journal_id}',g.line::jsonb#>>'{body,mode}')
    IS NOT DISTINCT FROM ROW('1'::jsonb,'genesis'::text,reg.genesis_hash,repeat('0',64),reg.journal_id::text,reg.mode)
   AND NOT EXISTS (SELECT 1 FROM revocation_journal_records earlier WHERE earlier.auth_user_id=u.id AND earlier.seq<cj.seq)
   AND ((r.reason_code='bootstrap' AND reg.mode='new_install' AND u.role='admin' AND EXISTS (
     SELECT 1 FROM bootstrap_state b WHERE b.admin_user_id=u.id AND b.completed_at=c.recorded_at
      AND b.creation_transaction_id=c.transaction_id
      AND EXISTS (SELECT 1 FROM audit_events a WHERE a.action='admin.bootstrap' AND a.outcome='success'
       AND a.actor_type='system' AND a.target_type='user' AND a.target_id=u.id::text
       AND a.occurred_at=b.completed_at AND a.details='{"via":"cli"}'::jsonb)))
    OR (r.reason_code='invitation_accept' AND u.role='user' AND EXISTS (
     SELECT 1 FROM invitations i JOIN revocation_journal_records ij ON ij.invitation_id=i.id
     WHERE i.accepted_user_id=u.id AND i.email=u.email AND i.accepted_at=c.recorded_at
      AND i.acceptance_transaction_id=c.transaction_id AND i.closure_reason='invitation_accepted'
      AND ij.seq=sj.seq+1 AND ROW(ij.invitation_end_kind,ij.invitation_reason_code,ij.invitation_ended_at)
       IS NOT DISTINCT FROM ROW('accepted'::text,'invitation_accepted'::text,i.accepted_at)
      AND EXISTS (SELECT 1 FROM audit_events a WHERE a.action='invitation.accept' AND a.outcome='success'
       AND a.actor_type='user' AND a.actor_user_id=u.id AND a.target_type='invitation' AND a.target_id=i.id::text
       AND a.occurred_at=i.accepted_at AND a.details='{}'::jsonb))))
 )
$$;

CREATE FUNCTION ras_auth_operator_receipt_provenance(p_operation uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS (SELECT 1 FROM auth_change_receipts r
  JOIN revocation_journal_records k ON k.case_operation_id=r.operation_id AND k.case_user_id=r.user_id
   AND k.case_recorded_at=r.recorded_at AND k.kind='operator_recovery_case' AND k.applied_via='close' AND k.journal_confirmed
  JOIN auth_recovery_cases c ON c.case_id=k.case_id AND c.user_id=k.case_user_id
   AND c.operator_id=k.case_operator_id AND c.action=k.case_action
  JOIN recovery_registrations reg ON reg.id=1 AND reg.journal_id=c.journal_id
  WHERE r.operation_id=p_operation AND r.recorded_at>=c.approved_at AND r.recorded_at<c.expires_at
   AND ((r.domain='status' AND r.decision='active' AND r.reason_code='verified_initial_status' AND c.action='resolve_initial_status')
     OR (r.domain='credential' AND r.decision='changed' AND r.reason_code='verified_recovery' AND c.action='set_credential')))
$$;

CREATE FUNCTION ras_auth_initial_status_verified(p_user uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS (SELECT 1 FROM auth_change_receipts r JOIN revocation_journal_records j ON
  ROW(j.auth_operation_id,j.auth_user_id,j.auth_domain,j.auth_generation,j.auth_decision,j.auth_reason_code,j.auth_recorded_at)
   IS NOT DISTINCT FROM ROW(r.operation_id,r.user_id,r.domain,r.generation,r.decision,r.reason_code,r.recorded_at)
  JOIN auth_safety_heads h ON h.user_id=r.user_id
  WHERE r.user_id=p_user AND r.domain='status' AND r.decision='active' AND j.journal_confirmed AND j.seq<=h.status_seq
   AND ((r.reason_code='verified_initial_status' AND ras_auth_operator_receipt_provenance(r.operation_id)) OR
    (r.reason_code IN ('bootstrap','invitation_accept') AND ras_auth_initial_pair(r.operation_id)
     AND NOT EXISTS (SELECT 1 FROM auth_change_receipts c JOIN revocation_journal_records cj ON cj.auth_operation_id=c.operation_id
      WHERE c.user_id=r.user_id AND c.domain='credential' AND c.transaction_id=r.transaction_id AND NOT cj.journal_confirmed)
     AND (r.reason_code='bootstrap' OR EXISTS (SELECT 1 FROM invitations i JOIN revocation_journal_records ij ON ij.invitation_id=i.id
      WHERE i.accepted_user_id=r.user_id AND i.acceptance_transaction_id=r.transaction_id AND ij.journal_confirmed)))))
$$;

CREATE FUNCTION ras_auth_receipt_provenance(p_operation uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS (SELECT 1 FROM auth_change_receipts r WHERE r.operation_id=p_operation
  AND CASE WHEN r.reason_code IN ('bootstrap','invitation_accept')
            THEN ras_auth_initial_pair(r.operation_id) AND ras_auth_initial_status_verified(r.user_id)
           WHEN r.reason_code IN ('verified_recovery','verified_initial_status') THEN ras_auth_operator_receipt_provenance(r.operation_id)
           WHEN r.domain='status' AND r.decision='active' AND r.reason_code='admin_enable'
            THEN ras_auth_initial_status_verified(r.user_id)
           ELSE true END)
$$;

-- Transient freshness is additional evidence for NEW writes only. Parent rows
-- are minted by protected creation routines; GUCs/caller-supplied txids do not
-- create one. Both parent and User must be part of this live creation attempt.
-- Invitation prepare has a PL/pgSQL exception block (a subtransaction). Check
-- the creating xid's live status, not equality with the top xid; it is visible
-- to this transaction only. Extend the 32-bit xid around the current epoch.
CREATE FUNCTION ras_auth_creation_current(p_xmin xid) RETURNS boolean
LANGUAGE plpgsql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE top_xid bigint := txid_current(); delta bigint;
BEGIN
 delta := p_xmin::text::bigint - mod(top_xid,4294967296);
 IF delta > 2147483648 THEN delta := delta-4294967296; END IF;
 IF delta < -2147483648 THEN delta := delta+4294967296; END IF;
 RETURN coalesce(pg_xact_status((top_xid+delta)::text::xid8)='in progress',false);
EXCEPTION WHEN OTHERS THEN RETURN false;
END $$;
CREATE FUNCTION ras_auth_receipt_initial_parent() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF NEW.reason_code NOT IN ('bootstrap','invitation_accept') THEN RETURN NEW; END IF;
 IF NOT EXISTS (SELECT 1 FROM users u WHERE u.id=NEW.user_id AND ras_auth_creation_current(u.xmin)
  AND u.created_at=NEW.recorded_at AND (
   (NEW.reason_code='bootstrap' AND u.role='admin' AND EXISTS (
    SELECT 1 FROM bootstrap_state b WHERE b.admin_user_id=u.id AND b.creation_transaction_id=txid_current()
     AND b.completed_at=NEW.recorded_at AND ras_auth_creation_current(b.xmin))) OR
   (NEW.reason_code='invitation_accept' AND u.role='user' AND EXISTS (
    SELECT 1 FROM invitations i WHERE i.accepted_user_id=u.id AND i.acceptance_transaction_id=txid_current()
     AND i.accepted_at=NEW.recorded_at AND i.closure_reason='invitation_accepted' AND ras_auth_creation_current(i.xmin))))) THEN
  RAISE EXCEPTION 'initial receipt requires live account creation parent' USING ERRCODE='check_violation'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_auth_receipt_initial_parent BEFORE INSERT ON auth_change_receipts
 FOR EACH ROW EXECUTE FUNCTION ras_auth_receipt_initial_parent();

CREATE FUNCTION ras_auth_receipt_initial_pair() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF NEW.reason_code IN ('bootstrap','invitation_accept') AND NOT ras_auth_initial_pair(NEW.operation_id) THEN
  RAISE EXCEPTION 'initial receipt requires exact durable provenance pair' USING ERRCODE='check_violation'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_auth_receipt_initial_pair AFTER INSERT ON auth_change_receipts
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ras_auth_receipt_initial_pair();

-- Existing bad receipts are retained. Recovery only restricts their effective
-- state, allowing a separately authorized C3 decision to replace the pointer.
CREATE OR REPLACE FUNCTION ras_auth_restrict(p_user uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE u users; h auth_safety_heads;
BEGIN
 SELECT * INTO u FROM users WHERE id=p_user;
 IF u.id IS NULL THEN RETURN; END IF;
 SELECT * INTO h FROM auth_safety_heads WHERE user_id=p_user;
 UPDATE users SET credential_state='quarantined',credential_reason_code='journal_rollback'
  WHERE id=p_user AND (h.credential_operation_id IS NULL OR
   ROW(credential_operation_id,credential_generation) IS DISTINCT FROM ROW(h.credential_operation_id,h.credential_generation)
   OR NOT ras_auth_receipt_provenance(credential_operation_id));
 IF h.status_decision='disabled' THEN
  UPDATE users SET status='disabled',status_generation=h.status_generation,status_safety='proven',
   status_operation_id=CASE WHEN status_operation_id=h.status_operation_id THEN status_operation_id ELSE NULL END,
   status_reason_code=h.status_reason_code WHERE id=p_user;
 ELSIF h.status_operation_id IS NULL OR ROW(u.status_operation_id,u.status_generation,u.status)
   IS DISTINCT FROM ROW(h.status_operation_id,h.status_generation,h.status_decision)
   OR NOT ras_auth_receipt_provenance(u.status_operation_id) THEN
  UPDATE users SET status_safety='held',status_reason_code='journal_rollback' WHERE id=p_user;
 END IF;
END $$;
