"""Remove a revoked common copy's derived full-text rows at cleanup, and fence late writes (M6b-2b2a).

Cleanup used to refuse a revoked copy with extraction/index rows
(``derived_rows_present``). It now deletes them in foreign-key order, and the
DB makes sure nothing is written back:

- ``embedding_calls`` gains ``content_removed_at``; ``chunk_hash`` becomes
  nullable. Cleanup nulls the vector and the chunk hash of the copy's calls and
  keeps the cost ledger (model, provider, tokens, cost, status, day). A removed
  call never gets content again, and only a revoked copy's calls can be scrubbed.
- Fence triggers on ``sections``, ``chunks``, ``pdf_pages``,
  ``reference_entries``, ``metadata_candidates``, ``index_generations``,
  ``index_entries``, ``reference_citation_links`` and ``embedding_calls``
  refuse INSERT, and UPDATE that writes content, for a common item that is
  ``revoked`` or has a tombstone. For a common item the check first takes the
  item row ``FOR KEY SHARE``, which conflicts with the revoke's ``FOR UPDATE``:
  a writer that got the lock first commits before ``t_rev`` (so cleanup, which
  runs after ``t_rev``, sees its rows); a later one waits and then sees
  ``t_rev``. Private items are never locked (their visibility never changes).
  DELETE is always allowed.
- A revoked item cannot get an active generation or leave ``metadata_only``.
- A tombstone cannot be marked ``purged_at`` while any derived row of the copy
  or embedding content of its files is left.

Backfill: a copy purged under 0023 could only have derived rows written out of
band after its purge (0023 refused to purge with them). They are removed here
the same way. Unpurged tombstones are left to cleanup.
"""
from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

# Tables whose rows name the item directly (composite FKs keep item/version/file consistent).
ITEM_TABLES = ("sections", "chunks", "pdf_pages", "reference_entries", "metadata_candidates", "index_generations")
CLOSED = "derived rows cannot be written for a revoked common copy"
ITEM_ROWS_LEFT = " ".join(f"UNION ALL SELECT '{t}' WHERE EXISTS (SELECT 1 FROM {t} WHERE item_id = target)"
                          for t in ITEM_TABLES)


def upgrade() -> None:
    op.add_column("embedding_calls", sa.Column("content_removed_at", sa.DateTime(timezone=True)))
    op.alter_column("embedding_calls", "chunk_hash", nullable=True)
    op.create_check_constraint(op.f("ck_embedding_calls_content_present"), "embedding_calls",
                               "chunk_hash IS NOT NULL OR content_removed_at IS NOT NULL")
    op.create_check_constraint(op.f("ck_embedding_calls_content_removed"), "embedding_calls",
                               "content_removed_at IS NULL OR (vector IS NULL AND chunk_hash IS NULL)")

    op.execute(f"""
        CREATE FUNCTION ras_derived_target_closed(target uuid) RETURNS boolean
        LANGUAGE plpgsql AS $$
        BEGIN
            -- Visibility never changes, so a private item needs no lock and is never closed here.
            IF target IS NULL OR NOT EXISTS (SELECT 1 FROM library_items WHERE id = target
                                             AND visibility = 'common') THEN
                RETURN false;
            END IF;
            -- Conflicts with the revoke's FOR UPDATE: wait for it, or make it wait for this writer.
            PERFORM 1 FROM library_items WHERE id = target FOR KEY SHARE;
            RETURN EXISTS (SELECT 1 FROM library_items WHERE id = target AND publication_state = 'revoked')
                OR EXISTS (SELECT 1 FROM deletion_tombstones WHERE item_id = target);
        END $$;

        CREATE FUNCTION ras_item_derived_fence() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF ras_derived_target_closed(NEW.item_id)
               OR (TG_OP = 'UPDATE' AND OLD.item_id IS DISTINCT FROM NEW.item_id
                   AND ras_derived_target_closed(OLD.item_id)) THEN
                RAISE EXCEPTION '{CLOSED}' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;

        CREATE FUNCTION ras_index_entries_fence() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF ras_derived_target_closed((SELECT item_id FROM index_generations WHERE id = NEW.generation_id))
               OR ras_derived_target_closed((SELECT item_id FROM chunks WHERE id = NEW.chunk_id))
               OR ras_derived_target_closed((SELECT f.item_id FROM embedding_calls c
                                             JOIN source_files f ON f.id = c.source_file_id
                                             WHERE c.id = NEW.embedding_call_id)) THEN
                RAISE EXCEPTION '{CLOSED}' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;

        CREATE FUNCTION ras_citation_links_fence() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF ras_derived_target_closed((SELECT item_id FROM reference_entries WHERE id = NEW.reference_id)) THEN
                RAISE EXCEPTION '{CLOSED}' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;

        -- Cost and status stay updatable (late reconciliation); content does not.
        CREATE FUNCTION ras_embedding_calls_fence() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE writes_content boolean; removes_content boolean;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                writes_content := true;
                removes_content := NEW.content_removed_at IS NOT NULL;
            ELSE
                IF OLD.content_removed_at IS NOT NULL
                   AND NEW.content_removed_at IS DISTINCT FROM OLD.content_removed_at THEN
                    RAISE EXCEPTION 'removed embedding content stays removed' USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.source_file_id IS DISTINCT FROM OLD.source_file_id THEN
                    RAISE EXCEPTION 'an embedding call keeps its source file' USING ERRCODE = 'check_violation';
                END IF;
                writes_content := (NEW.vector IS NOT NULL AND NEW.vector IS DISTINCT FROM OLD.vector)
                    OR (NEW.chunk_hash IS NOT NULL AND NEW.chunk_hash IS DISTINCT FROM OLD.chunk_hash);
                removes_content := OLD.content_removed_at IS NULL AND NEW.content_removed_at IS NOT NULL;
            END IF;
            IF writes_content OR removes_content THEN
                IF ras_derived_target_closed((SELECT item_id FROM source_files WHERE id = NEW.source_file_id)) THEN
                    IF TG_OP = 'INSERT' OR writes_content THEN
                        RAISE EXCEPTION '{CLOSED}' USING ERRCODE = 'check_violation';
                    END IF;
                ELSIF removes_content THEN
                    RAISE EXCEPTION 'only a revoked common copy''s embedding content is removed'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END $$;

        CREATE FUNCTION ras_library_items_revoked_index_fence() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.publication_state = 'revoked'
               AND ((NEW.active_index_generation_id IS NOT NULL
                     AND NEW.active_index_generation_id IS DISTINCT FROM OLD.active_index_generation_id)
                    OR (NEW.content_status <> 'metadata_only'
                        AND NEW.content_status IS DISTINCT FROM OLD.content_status)) THEN
                RAISE EXCEPTION 'a revoked common copy is never indexed again' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;

        CREATE FUNCTION ras_common_copy_derived_left(target uuid) RETURNS text[]
        LANGUAGE sql STABLE AS $$
            SELECT coalesce(array_agg(t ORDER BY t), '{{}}') FROM (
                SELECT 'active_index_generation' AS t FROM library_items
                    WHERE id = target AND active_index_generation_id IS NOT NULL
                {ITEM_ROWS_LEFT}
                UNION ALL SELECT 'index_entries' WHERE EXISTS (
                    SELECT 1 FROM index_entries e LEFT JOIN embedding_calls c ON c.id = e.embedding_call_id
                    LEFT JOIN source_files f ON f.id = c.source_file_id
                    WHERE f.item_id = target
                       OR e.generation_id IN (SELECT id FROM index_generations WHERE item_id = target)
                       OR e.chunk_id IN (SELECT id FROM chunks WHERE item_id = target))
                UNION ALL SELECT 'reference_citation_links' WHERE EXISTS (
                    SELECT 1 FROM reference_citation_links l JOIN reference_entries r ON r.id = l.reference_id
                    WHERE r.item_id = target)
                UNION ALL SELECT 'embedding_content' WHERE EXISTS (
                    SELECT 1 FROM embedding_calls c JOIN source_files f ON f.id = c.source_file_id
                    WHERE f.item_id = target AND c.content_removed_at IS NULL)
            ) x
        $$;

        CREATE FUNCTION ras_deletion_tombstones_purge_derived() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE left_over text[];
        BEGIN
            IF OLD.purged_at IS NULL AND NEW.purged_at IS NOT NULL THEN
                left_over := ras_common_copy_derived_left(NEW.item_id);
                IF cardinality(left_over) > 0 THEN
                    RAISE EXCEPTION 'cleanup must remove derived % before purging', array_to_string(left_over, ', ')
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
    """)

    # Backfill before the fences exist: rows written after a purge under 0023.
    op.execute("""
        CREATE TEMP TABLE purged_copies ON COMMIT DROP AS
            SELECT item_id FROM deletion_tombstones WHERE purged_at IS NOT NULL;
        UPDATE library_items SET active_index_generation_id = NULL
            WHERE id IN (SELECT item_id FROM purged_copies) AND active_index_generation_id IS NOT NULL;
        DELETE FROM reference_citation_links WHERE reference_id IN
            (SELECT id FROM reference_entries WHERE item_id IN (SELECT item_id FROM purged_copies));
        DELETE FROM reference_entries WHERE item_id IN (SELECT item_id FROM purged_copies);
        DELETE FROM index_entries WHERE generation_id IN
            (SELECT id FROM index_generations WHERE item_id IN (SELECT item_id FROM purged_copies));
        UPDATE embedding_calls SET vector = NULL, chunk_hash = NULL, content_removed_at = now()
            WHERE content_removed_at IS NULL AND source_file_id IN
                (SELECT id FROM source_files WHERE item_id IN (SELECT item_id FROM purged_copies));
        DELETE FROM index_generations WHERE item_id IN (SELECT item_id FROM purged_copies);
        DELETE FROM chunks WHERE item_id IN (SELECT item_id FROM purged_copies);
        DELETE FROM sections WHERE item_id IN (SELECT item_id FROM purged_copies);
        DELETE FROM pdf_pages WHERE item_id IN (SELECT item_id FROM purged_copies);
        DELETE FROM metadata_candidates WHERE item_id IN (SELECT item_id FROM purged_copies);
        DROP TABLE purged_copies;
    """)

    for table in ITEM_TABLES:
        op.execute(f"""CREATE TRIGGER trg_{table}_revoked_fence BEFORE INSERT OR UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION ras_item_derived_fence()""")
    op.execute("""
        CREATE TRIGGER trg_index_entries_revoked_fence BEFORE INSERT OR UPDATE ON index_entries
            FOR EACH ROW EXECUTE FUNCTION ras_index_entries_fence();
        CREATE TRIGGER trg_reference_citation_links_revoked_fence BEFORE INSERT OR UPDATE ON reference_citation_links
            FOR EACH ROW EXECUTE FUNCTION ras_citation_links_fence();
        CREATE TRIGGER trg_embedding_calls_revoked_fence BEFORE INSERT OR UPDATE ON embedding_calls
            FOR EACH ROW EXECUTE FUNCTION ras_embedding_calls_fence();
        CREATE TRIGGER trg_library_items_revoked_index_fence
            BEFORE UPDATE OF active_index_generation_id, content_status ON library_items
            FOR EACH ROW EXECUTE FUNCTION ras_library_items_revoked_index_fence();
        CREATE TRIGGER trg_deletion_tombstones_purge_derived BEFORE UPDATE OF purged_at ON deletion_tombstones
            FOR EACH ROW EXECUTE FUNCTION ras_deletion_tombstones_purge_derived();
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM embedding_calls WHERE content_removed_at IS NOT NULL) THEN
                RAISE EXCEPTION 'removed embedding content exists; downgrade below 0024 would lose that record';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER trg_deletion_tombstones_purge_derived ON deletion_tombstones")
    op.execute("DROP TRIGGER trg_library_items_revoked_index_fence ON library_items")
    op.execute("DROP TRIGGER trg_embedding_calls_revoked_fence ON embedding_calls")
    op.execute("DROP TRIGGER trg_reference_citation_links_revoked_fence ON reference_citation_links")
    op.execute("DROP TRIGGER trg_index_entries_revoked_fence ON index_entries")
    for table in ITEM_TABLES:
        op.execute(f"DROP TRIGGER trg_{table}_revoked_fence ON {table}")
    for fn in ("ras_deletion_tombstones_purge_derived()", "ras_common_copy_derived_left(uuid)",
               "ras_library_items_revoked_index_fence()", "ras_embedding_calls_fence()",
               "ras_citation_links_fence()", "ras_index_entries_fence()", "ras_item_derived_fence()",
               "ras_derived_target_closed(uuid)"):
        op.execute(f"DROP FUNCTION {fn}")
    op.drop_constraint(op.f("ck_embedding_calls_content_removed"), "embedding_calls", type_="check")
    op.drop_constraint(op.f("ck_embedding_calls_content_present"), "embedding_calls", type_="check")
    op.alter_column("embedding_calls", "chunk_hash", nullable=False)
    op.drop_column("embedding_calls", "content_removed_at")
