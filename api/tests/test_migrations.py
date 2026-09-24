from __future__ import annotations

import io

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from app.models import Base
from tests.conftest import alembic_config, upgrade

EXPECTED_TABLES = {
    "alembic_version",
    "users",
    "bootstrap_state",
    "library_items",
    "paper_versions",
    "source_files",
    "ingestion_jobs",
    "audit_events",
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
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0001"
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
