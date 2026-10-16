"""M6d irreversible owner deletion, journal batch and storage fences."""
from pathlib import Path
from alembic import op
from sqlalchemy import text

revision = '0052'
down_revision = '0051'
branch_labels = None
depends_on = None


def _rewrite_function(signature, replacements):
    # PostgreSQL evaluates the existing definition in both online migrations and
    # exported offline SQL. Preserve every prior check and function property.
    def literal(value):
        return "'" + value.replace("'", "''") + "'"
    assignments=[]
    for replacement in replacements:
        old,new,*limit=replacement
        # These migrations use either all occurrences or the first BEGIN only.
        if limit and limit != [1]:
            raise ValueError('unsupported rewrite limit')
        if limit:
            assignments.append('definition := overlay(definition placing '+literal(new)
                +' from strpos(definition, '+literal(old)+') for '+str(len(old))+');')
        else:
            assignments.append('definition := replace(definition, '+literal(old)+', '+literal(new)+');')
    op.execute('DO $m6d_migration$ DECLARE definition text; BEGIN '
        'SELECT pg_get_functiondef('+literal(signature)+'::regprocedure) INTO STRICT definition; '
        + ' '.join(assignments) + ' EXECUTE definition; END $m6d_migration$;')


def upgrade():
    op.execute(Path(__file__).with_suffix('.sql').read_text(encoding='utf-8'))
    source=[]
    source.append(('BEGIN', """BEGIN
      -- M6d source scrub begin
      IF EXISTS(SELECT 1 FROM library_items WHERE id=OLD.item_id AND deleted_at IS NOT NULL) THEN
       IF (to_jsonb(NEW)-ARRAY['original_filename','file_source']) IS NOT DISTINCT FROM
          (to_jsonb(OLD)-ARRAY['original_filename','file_source'])
        AND (NEW.original_filename IS NULL OR NEW.original_filename IS NOT DISTINCT FROM OLD.original_filename)
        AND (NEW.file_source IS NULL OR NEW.file_source IS NOT DISTINCT FROM OLD.file_source)
        THEN RETURN NEW; END IF;
       RAISE EXCEPTION 'deleted file only permits scrub' USING ERRCODE='check_violation';
      END IF;
      -- M6d source scrub end
    """, 1))
    source.append(("publication_state = 'revoked'", "(publication_state = 'revoked' OR deleted_at IS NOT NULL)"))
    source.append(('OR NEW.file_source IS DISTINCT FROM OLD.file_source', "OR (NEW.file_source IS DISTINCT FROM OLD.file_source AND NOT (NEW.file_source IS NULL AND "
        "EXISTS(SELECT 1 FROM library_items WHERE id=OLD.item_id AND deleted_at IS NOT NULL)))"))
    _rewrite_function('ras_source_files_immutable()', source)
    review=[]
    review.append(('BEGIN', """BEGIN
      -- M6d review scrub begin
      IF EXISTS(SELECT 1 FROM library_items WHERE id=OLD.item_id AND deleted_at IS NOT NULL)
       AND NEW.declared_source IS NULL AND NEW.declared_rights IS NULL
       AND NEW.declared_source_url IS NULL AND NEW.declared_license_url IS NULL AND NEW.rejection_note IS NULL
       AND (to_jsonb(NEW)-ARRAY['declared_source','declared_rights','declared_source_url','declared_license_url','rejection_note'])
        IS NOT DISTINCT FROM
        (to_jsonb(OLD)-ARRAY['declared_source','declared_rights','declared_source_url','declared_license_url','rejection_note'])
       THEN RETURN NEW; END IF;
      -- M6d review scrub end
    """, 1))
    _rewrite_function('ras_review_requests_transition()', review)
    op.execute('ALTER TABLE review_requests ALTER COLUMN declared_source DROP NOT NULL; '
               'ALTER TABLE review_requests ALTER COLUMN declared_rights DROP NOT NULL; '
               'ALTER TABLE upload_intents ALTER COLUMN filename DROP NOT NULL')
    guard=[]
    guard.append(('NEW.seq,', 'NEW.private_deletion_id, NEW.seq,'))
    guard.append(('OLD.seq,', 'OLD.private_deletion_id, OLD.seq,'))
    _rewrite_function('ras_revocation_journal_records_confirm_only()', guard)
    common_fence=[]
    common_fence.append(("li.publication_state IS DISTINCT FROM 'revoked'", "(li.publication_state IS DISTINCT FROM 'revoked' AND li.deleted_at IS NULL)"))
    _rewrite_function('ras_auth_cleanup_fence(uuid,uuid,uuid,text,bigint)', common_fence)


def downgrade():
    conn=op.get_bind()
    if conn.scalar(text("SELECT EXISTS(SELECT 1 FROM private_item_deletions) OR EXISTS(SELECT 1 FROM private_object_fences)"
        " OR EXISTS(SELECT 1 FROM revocation_journal_records WHERE kind='private_deletion')")):
        raise RuntimeError('private deletion history prevents downgrade')
    def uninject(signature,marker):
        definition=conn.scalar(text('SELECT pg_get_functiondef(to_regprocedure(:s))'),{'s':signature})
        start=definition.index('-- M6d '+marker+' scrub begin')
        end=definition.index('-- M6d '+marker+' scrub end')+len('-- M6d '+marker+' scrub end')
        return definition[:start]+definition[end:]
    source=uninject('ras_source_files_immutable()','source')
    source=source.replace("(publication_state = 'revoked' OR deleted_at IS NOT NULL)","publication_state = 'revoked'")
    source=source.replace("OR (NEW.file_source IS DISTINCT FROM OLD.file_source AND NOT (NEW.file_source IS NULL AND "
        "EXISTS(SELECT 1 FROM library_items WHERE id=OLD.item_id AND deleted_at IS NOT NULL)))",
        'OR NEW.file_source IS DISTINCT FROM OLD.file_source')
    op.execute(source)
    op.execute(uninject('ras_review_requests_transition()','review'))
    for signature,old,new in (
        ('ras_revocation_journal_records_confirm_only()','NEW.private_deletion_id, NEW.seq,','NEW.seq,'),
        ('ras_revocation_journal_records_confirm_only()','OLD.private_deletion_id, OLD.seq,','OLD.seq,'),
        ('ras_auth_cleanup_fence(uuid,uuid,uuid,text,bigint)',
         "(li.publication_state IS DISTINCT FROM 'revoked' AND li.deleted_at IS NULL)","li.publication_state IS DISTINCT FROM 'revoked'")):
        definition=conn.scalar(text('SELECT pg_get_functiondef(to_regprocedure(:s))'),{'s':signature})
        op.execute(definition.replace(old,new))
    old_sql=Path(__file__).with_name('0045_orphan_object_fences.sql').read_text(encoding='utf-8')
    start=old_sql.index('CREATE OR REPLACE FUNCTION ras_source_files_delete_fence()')
    op.execute(old_sql[start:old_sql.index('END $$;',start)+len('END $$;')])
    op.execute("""CREATE OR REPLACE FUNCTION ras_derived_target_closed(target uuid) RETURNS boolean
      LANGUAGE plpgsql AS $$ BEGIN
       IF target IS NULL OR NOT EXISTS(SELECT 1 FROM library_items WHERE id=target AND visibility='common') THEN RETURN false; END IF;
       PERFORM 1 FROM library_items WHERE id=target FOR KEY SHARE;
       RETURN EXISTS(SELECT 1 FROM library_items WHERE id=target AND publication_state='revoked')
        OR EXISTS(SELECT 1 FROM deletion_tombstones WHERE item_id=target);
      END $$;
      CREATE OR REPLACE FUNCTION ras_auth_confirm(p_seq bigint) RETURNS void LANGUAGE sql SECURITY DEFINER
       SET search_path=pg_catalog,public,pg_temp AS $$
       UPDATE revocation_journal_records SET journal_confirmed=true
        WHERE seq=p_seq AND kind<>'revocation' AND NOT journal_confirmed; $$;
      DROP TRIGGER trg_private_membership_fence ON collection_items;
      DO $$ DECLARE f record; BEGIN
       FOR f IN SELECT oid::regprocedure signature FROM pg_proc WHERE pronamespace='public'::regnamespace
        AND (proname LIKE 'ras_private_%' OR proname LIKE 'ras_auth_private_%') LOOP
        EXECUTE format('DROP FUNCTION %s CASCADE',f.signature);
       END LOOP; END $$;
      DROP TABLE private_object_fences,private_item_deletions;
      ALTER TABLE library_items DROP COLUMN deleted_at,DROP COLUMN deletion_transaction_id;
      ALTER TABLE revocation_journal_records DROP CONSTRAINT ck_revocation_journal_records_private_deletion_binding;
      ALTER TABLE revocation_journal_records DROP COLUMN private_deletion_id;
      ALTER TABLE revocation_journal_records DROP CONSTRAINT ck_revocation_journal_records_kind_valid;
      ALTER TABLE revocation_journal_records ADD CONSTRAINT ck_revocation_journal_records_kind_valid CHECK(kind IN
       ('genesis','revocation','recovery_begin','recovery_end','review_closure','session_revocation','reset_token_closure',
        'invitation_closure','credential_transition','account_status_decision','operator_recovery_case'));
      ALTER TABLE review_requests ALTER COLUMN declared_source SET NOT NULL,ALTER COLUMN declared_rights SET NOT NULL;
      ALTER TABLE upload_intents ALTER COLUMN filename SET NOT NULL;
    """)
