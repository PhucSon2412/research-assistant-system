"""C4 reconciled operational checkpoint, advanced only by confirmed journal writes."""
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE auth_operational_state (
      id smallint CONSTRAINT pk_auth_operational_state PRIMARY KEY CONSTRAINT ck_auth_operational_state_singleton CHECK(id=1),
      contract_version smallint NOT NULL CONSTRAINT ck_auth_operational_state_contract_version CHECK(contract_version=1),
      journal_id uuid NOT NULL,
      genesis_hash text NOT NULL,
      verified_seq bigint NOT NULL CONSTRAINT ck_auth_operational_state_verified_seq_positive CHECK(verified_seq>0),
      verified_hash text NOT NULL
    );
    REVOKE ALL ON auth_operational_state FROM PUBLIC;
    CREATE FUNCTION ras_auth_complete_contract(p_seq bigint,p_hash text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
    DECLARE reg recovery_registrations;
    BEGIN
      SELECT * INTO STRICT reg FROM recovery_registrations WHERE id=1;
      IF NOT EXISTS(SELECT 1 FROM auth_safety_state WHERE id=1 AND version=1
                    AND backfill_complete AND projection_seq=p_seq)
        OR NOT EXISTS(SELECT 1 FROM revocation_journal_records WHERE seq=p_seq
                      AND record_hash=p_hash AND journal_confirmed)
        OR p_seq IS DISTINCT FROM (SELECT max(seq) FROM revocation_journal_records)
        OR EXISTS(SELECT 1 FROM revocation_journal_records WHERE NOT journal_confirmed)
      THEN RAISE EXCEPTION 'operational reconciliation incomplete'; END IF;
      INSERT INTO auth_operational_state VALUES(1,1,reg.journal_id,reg.genesis_hash,p_seq,p_hash)
      ON CONFLICT(id) DO UPDATE SET journal_id=excluded.journal_id,genesis_hash=excluded.genesis_hash,
        verified_seq=excluded.verified_seq,verified_hash=excluded.verified_hash;
    END $$;
    REVOKE ALL ON FUNCTION ras_auth_complete_contract(bigint,text) FROM PUBLIC;
    CREATE FUNCTION ras_auth_advance_contract() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
    DECLARE next_row revocation_journal_records; checkpoint auth_operational_state;
    BEGIN
      IF NOT NEW.journal_confirmed THEN RETURN NEW; END IF;
      SELECT * INTO checkpoint FROM auth_operational_state WHERE id=1 FOR UPDATE;
      IF NOT FOUND THEN RETURN NEW; END IF;
      LOOP
        SELECT * INTO next_row FROM revocation_journal_records
         WHERE seq=checkpoint.verified_seq+1 AND journal_confirmed;
        EXIT WHEN NOT FOUND;
        IF next_row.line::jsonb->>'prev' <> checkpoint.verified_hash THEN
          RAISE EXCEPTION 'operational checkpoint diverged'; END IF;
        checkpoint.verified_seq=next_row.seq; checkpoint.verified_hash=next_row.record_hash;
      END LOOP;
      UPDATE auth_operational_state SET verified_seq=checkpoint.verified_seq,
        verified_hash=checkpoint.verified_hash WHERE id=1;
      -- C3 progress is evidence of reconciliation past a hold's begin. A
      -- mirrored begin alone must never supply that evidence. Only recover's
      -- explicit ras_auth_progress may cross it after the full postconditions.
      UPDATE auth_safety_state SET projection_seq=checkpoint.verified_seq WHERE id=1
       AND NOT EXISTS(SELECT 1 FROM revocation_journal_records j WHERE j.kind='recovery_begin'
                      AND j.seq>auth_safety_state.projection_seq AND j.seq<=checkpoint.verified_seq);
      RETURN NEW;
    END $$;
    REVOKE ALL ON FUNCTION ras_auth_advance_contract() FROM PUBLIC;
    CREATE TRIGGER trg_auth_advance_contract AFTER INSERT OR UPDATE OF journal_confirmed
      ON revocation_journal_records FOR EACH ROW EXECUTE FUNCTION ras_auth_advance_contract();
    """)


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM recovery_registrations) OR EXISTS(SELECT 1 FROM auth_operational_state)
      THEN RAISE EXCEPTION 'auth safety history prevents downgrade of registered operational contract'; END IF;
    END $$;
    DROP TRIGGER trg_auth_advance_contract ON revocation_journal_records;
    DROP FUNCTION ras_auth_advance_contract();
    DROP FUNCTION ras_auth_complete_contract(bigint,text);
    DROP TABLE auth_operational_state;
    """)
