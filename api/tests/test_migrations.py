from __future__ import annotations

import io

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

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
    return {
        t: [tuple(r) for r in conn.execute(text(f"SELECT * FROM {t} ORDER BY 1")).all()]
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
