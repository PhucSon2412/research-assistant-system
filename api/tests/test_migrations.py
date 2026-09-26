from __future__ import annotations

import io

import pytest

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.models import Base
from tests.conftest import HEAD_REVISION, alembic_config, upgrade

EXPECTED_TABLES = {
    "alembic_version",
    "users",
    "bootstrap_state",
    "library_items",
    "paper_versions",
    "source_files",
    "ingestion_jobs",
    "audit_events",
    # M1b (0002)
    "invitations",
    "sessions",
    "password_reset_tokens",
    "processing_acknowledgements",
    "upload_intents",
    "ingest_step_attempts",
    "metadata_candidates",
    "sections",
    "chunks",
    "pdf_pages",
    "reference_entries",
    "index_generations", "embedding_calls", "index_entries", "embedding_budget_days",
}


def _tables(url: str) -> set[str]:
    eng = create_engine(url)
    try:
        return set(inspect(eng).get_table_names())
    finally:
        eng.dispose()


def test_empty_database_upgrades_to_head(empty_db_url: str) -> None:
    assert _tables(empty_db_url) == set()

    upgrade(empty_db_url)

    assert _tables(empty_db_url) == EXPECTED_TABLES
    eng = create_engine(empty_db_url)
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        assert conn.scalar(text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")) == 1
        # No seeded/default accounts or credentials.
        assert conn.scalar(text("SELECT count(*) FROM users")) == 0
        assert conn.scalar(text("SELECT count(*) FROM bootstrap_state")) == 0
        assert conn.scalar(text("SELECT count(*) FROM audit_events")) == 0
    eng.dispose()


def test_rerunning_upgrade_is_a_noop(migrated_db_url: str) -> None:
    upgrade(migrated_db_url)
    assert _tables(migrated_db_url) == EXPECTED_TABLES


def test_downgrade_to_base_and_upgrade_again(migrated_db_url: str) -> None:
    command.downgrade(alembic_config(migrated_db_url), "base")
    assert _tables(migrated_db_url) == {"alembic_version"}
    eng = create_engine(migrated_db_url)
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM pg_proc WHERE proname LIKE 'ras\\_%'")) == 0
    eng.dispose()

    upgrade(migrated_db_url)
    assert _tables(migrated_db_url) == EXPECTED_TABLES


def test_orm_models_match_migrated_schema(migrated_db_url: str) -> None:
    eng = create_engine(migrated_db_url)
    with eng.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": True})
        diff = compare_metadata(ctx, Base.metadata)
    eng.dispose()
    assert diff == []


def test_0009_upgrades_database_with_m2b_pdf_data(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0008")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        owner = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('m3a@example.org',:p) RETURNING id"),
                            {"p": hash_password("synthetic-password-long")})
        item = conn.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:o,'M2b fixture') RETURNING id"), {"o": owner})
        version = conn.scalar(text("INSERT INTO paper_versions(item_id,coverage) VALUES(:i,'partial') RETURNING id"), {"i": item})
        job = conn.scalar(text("INSERT INTO ingestion_jobs(origin,scope_visibility,scope_owner_user_id,item_id,version_id,idempotency_key,state,rights_epoch_at_start,extraction_finished_at) VALUES('upload','private',:o,:i,:v,'m2b-existing','awaiting_index',0,now()) RETURNING id"),
                          {"o": owner, "i": item, "v": version})
        source = conn.scalar(text("INSERT INTO source_files(item_id,version_id,job_id,storage_key,verified_mime,byte_size,page_count,checksum_sha256) VALUES(:i,:v,:j,'existing-original','application/pdf',100,1,:h) RETURNING id"),
                             {"i": item, "v": version, "j": job, "h": "a" * 64})
        conn.execute(text("INSERT INTO pdf_pages(item_id,version_id,source_file_id,page_number,text,width,height,role,body_chars,body_page,qualifies,layout_mode,pipeline_version) VALUES(:i,:v,:f,1,'Introduction body',612,792,'body',16,true,true,'single','pypdf-6.19.0-layout-v2')"),
                     {"i": item, "v": version, "f": source})
    upgrade(empty_db_url)
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT state FROM ingestion_jobs WHERE id=:j"), {"j": job}) == "awaiting_index"
        assert conn.scalar(text("SELECT count(*) FROM pdf_pages WHERE source_file_id=:f"), {"f": source}) == 1
        assert conn.scalar(text("SELECT content_status FROM library_items WHERE id=:i"), {"i": item}) == "metadata_only"
        assert conn.scalar(text("SELECT active_index_generation_id FROM library_items WHERE id=:i"), {"i": item}) is None
        assert conn.scalar(text("SELECT count(*) FROM index_generations")) == 0
    eng.dispose()


def test_migration_sql_seeds_no_rows() -> None:
    """Offline SQL: the migration creates schema only, never INSERTs data
    such as a default account (the alembic_version bookkeeping aside)."""
    out = io.StringIO()
    cfg = alembic_config("postgresql+psycopg://offline@localhost/offline", output=out)
    command.upgrade(cfg, "head", sql=True)
    inserts = [
        line for line in out.getvalue().splitlines()
        if line.strip().upper().startswith("INSERT") and "alembic_version" not in line
    ]
    assert inserts == []


# --- M1b: 0001 -> 0002 on a database that already holds M1a data -------------


def _snapshot(conn) -> dict[str, list]:
    # Keep the 0001 projection stable as later migrations add nullable columns.
    columns = {"library_items": "id, visibility, owner_user_id, content_status, active_version_id, title, rights_epoch, created_by_user_id, created_at, updated_at"}
    return {
        t: [tuple(r) for r in conn.execute(text(f"SELECT {columns.get(t, '*')} FROM {t} ORDER BY 1")).all()]
        for t in ("users", "bootstrap_state", "library_items", "paper_versions", "audit_events")
    }


def test_upgrade_from_0001_keeps_existing_data(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0001")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        admin = conn.scalar(
            text(
                "INSERT INTO users (email, role, password_verifier)"
                " VALUES ('a@example.org', 'admin', :v) RETURNING id"
            ),
            {"v": hash_password("x" * 12)},
        )
        conn.execute(text("INSERT INTO bootstrap_state (admin_user_id) VALUES (:a)"), {"a": admin})
        item = conn.scalar(text("INSERT INTO library_items (visibility, title) VALUES ('common', 't') RETURNING id"))
        conn.execute(text("INSERT INTO paper_versions (item_id) VALUES (:i)"), {"i": item})
        conn.execute(text("INSERT INTO audit_events (actor_type, action, outcome) VALUES ('system', 'x', 'success')"))
    with eng.connect() as conn:
        before = _snapshot(conn)

    upgrade(empty_db_url)
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        after_first = _snapshot(conn)
    upgrade(empty_db_url)  # re-run: no-op
    with eng.connect() as conn:
        after_second = _snapshot(conn)
        for table in ("invitations", "sessions", "password_reset_tokens"):
            assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    assert before == after_first == after_second

    # Downgrading only M1b keeps the M1a data too.
    command.downgrade(alembic_config(empty_db_url), "0001")
    with eng.connect() as conn:
        assert _snapshot(conn) == before
    assert "sessions" not in _tables(empty_db_url)
    eng.dispose()


def test_0005_preserves_received_m1c_job_and_original_metadata(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0004")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        owner = conn.scalar(text("INSERT INTO users (email, password_verifier) VALUES ('m2a@example.org', :v) RETURNING id"),
                            {"v": hash_password("test-password-long")})
        item = conn.scalar(text("INSERT INTO library_items (visibility, owner_user_id, title)"
                                " VALUES ('private', :o, 'User title') RETURNING id"), {"o": owner})
        version = conn.scalar(text("INSERT INTO paper_versions (item_id) VALUES (:i) RETURNING id"), {"i": item})
        job = conn.scalar(text("INSERT INTO ingestion_jobs (origin, actor_user_id, scope_visibility,"
                               " scope_owner_user_id, item_id, version_id, idempotency_key, rights_epoch_at_start)"
                               " VALUES ('upload', :o, 'private', :o, :i, :v, 'm1c-existing', 0) RETURNING id"),
                          {"o": owner, "i": item, "v": version})
        source = conn.scalar(text("INSERT INTO source_files (item_id, version_id, job_id, storage_key, verified_mime,"
                                  " byte_size, checksum_sha256) VALUES (:i, :v, :j, 'originals/old', 'text/markdown',"
                                  " 4, :h) RETURNING id"), {"i": item, "v": version, "j": job, "h": "a" * 64})
    upgrade(empty_db_url, "0005")
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO metadata_candidates (item_id, version_id, source_file_id, field_name,"
                          " value, start_offset, end_offset, pipeline_version)"
                          " VALUES (:i, :v, :s, 'title', 'Test', 2, 6, 'markdown-lines-v1')"),
                     {"i": item, "v": version, "s": source})
        section = conn.scalar(text("INSERT INTO sections (item_id, version_id, source_file_id, kind, ordinal,"
                                   " text, start_offset, end_offset, start_line, end_line, pipeline_version)"
                                   " VALUES (:i, :v, :s, 'body', 0, '# Test', 0, 6, 1, 1, 'markdown-lines-v1') RETURNING id"),
                              {"i": item, "v": version, "s": source})
        conn.execute(text("INSERT INTO chunks (section_id, item_id, version_id, source_file_id, ordinal, text,"
                          " start_offset, end_offset, start_line, end_line, chunk_hash, pipeline_version)"
                          " VALUES (:section, :i, :v, :s, 0, '# Test', 0, 6, 1, 1, :h, 'markdown-lines-v1')"),
                     {"section": section, "i": item, "v": version, "s": source, "h": "b" * 64})
    upgrade(empty_db_url)
    with eng.connect() as conn:
        row = conn.execute(text("SELECT state, attempt_count, lease_token FROM ingestion_jobs WHERE id=:j"),
                           {"j": job}).one()
        assert row == ("received", 0, None)
        assert conn.scalar(text("SELECT id FROM source_files WHERE id=:s"), {"s": source}) == source
        assert conn.scalar(text("SELECT title FROM library_items WHERE id=:i"), {"i": item}) == "User title"
        assert conn.scalar(text("SELECT locator_kind FROM sections WHERE id=:s"), {"s": section}) == "markdown"
        assert conn.scalar(text("SELECT locator_kind FROM chunks WHERE section_id=:s"), {"s": section}) == "markdown"
        assert conn.scalar(text("SELECT locator_kind FROM metadata_candidates WHERE source_file_id=:s"),
                           {"s": source}) == "markdown"
    with pytest.raises(IntegrityError):
        with eng.begin() as conn:
            conn.execute(text("INSERT INTO chunks (section_id, item_id, version_id, source_file_id, ordinal, text,"
                              " locator_kind, start_offset, start_line, end_line, chunk_hash, pipeline_version)"
                              " VALUES (:section, :i, :v, :s, 1, 'bad', 'markdown', 0, 1, 1, :h, 'markdown-lines-v1')"),
                         {"section": section, "i": item, "v": version, "s": source, "h": "c" * 64})
    eng.dispose()


def test_0007_backfills_handoff_without_completing_job(empty_db_url: str) -> None:
    upgrade(empty_db_url, "0006")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        handoff = conn.scalar(text("INSERT INTO ingestion_jobs (origin, scope_visibility, idempotency_key,"
                                   " state, started_at, finished_at) VALUES ('discovery', 'common', 'handoff',"
                                   " 'awaiting_index', now() - interval '1 minute', now()) RETURNING id"))
        terminal = conn.scalar(text("INSERT INTO ingestion_jobs (origin, scope_visibility, idempotency_key,"
                                    " state, started_at, finished_at) VALUES ('discovery', 'common', 'terminal',"
                                    " 'terminal_failed', now() - interval '1 minute', now()) RETURNING id"))
    upgrade(empty_db_url)
    with eng.connect() as conn:
        row = conn.execute(text("SELECT extraction_finished_at, finished_at FROM ingestion_jobs WHERE id=:id"),
                           {"id": handoff}).one()
        assert row.extraction_finished_at is not None
        assert row.finished_at is None
        other = conn.execute(text("SELECT extraction_finished_at, finished_at FROM ingestion_jobs WHERE id=:id"),
                             {"id": terminal}).one()
        assert other.extraction_finished_at is None
        assert other.finished_at is not None
    with pytest.raises(IntegrityError):
        with eng.begin() as conn:
            conn.execute(text("UPDATE ingestion_jobs SET finished_at=now() WHERE id=:id"), {"id": handoff})
    eng.dispose()
