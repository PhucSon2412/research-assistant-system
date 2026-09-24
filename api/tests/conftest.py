"""Test fixtures against a real PostgreSQL (the Compose `db` service).

Each DB fixture creates a brand-new empty database, so "empty DB -> migrate"
is exercised for real. The admin connection URL comes from
RAS_TEST_DATABASE_ADMIN_URL, or is derived from the repo's .env (host port
POSTGRES_HOST_PORT on 127.0.0.1).
"""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app import config as app_config
from app import db as app_db

API_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = API_DIR.parent


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _admin_url() -> str:
    url = os.environ.get("RAS_TEST_DATABASE_ADMIN_URL")
    if url:
        return url
    env = _read_dotenv(REPO_DIR / ".env")
    required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
    if not all(env.get(k) for k in required):
        pytest.fail("Set RAS_TEST_DATABASE_ADMIN_URL or create .env and start `docker compose up -d db`")
    port = env.get("POSTGRES_HOST_PORT", "5432")
    return (
        f"postgresql+psycopg://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}"
        f"@127.0.0.1:{port}/{env['POSTGRES_DB']}"
    )


@pytest.fixture(scope="session")
def admin_engine() -> Iterator[Engine]:
    engine = create_engine(_admin_url(), isolation_level="AUTOCOMMIT", hide_parameters=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"PostgreSQL not reachable for tests ({type(exc).__name__}); run `docker compose up -d db`")
    yield engine
    engine.dispose()


def _create_empty_database(admin_engine: Engine) -> str:
    name = f"ras_test_{uuid.uuid4().hex[:12]}"
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
    return admin_engine.url.set(database=name).render_as_string(hide_password=False)


def _drop_database(admin_engine: Engine, url: str) -> None:
    name = make_url(url).database
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def alembic_config(url: str, output: io.StringIO | None = None) -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"), output_buffer=output)
    cfg.set_main_option("script_location", str(API_DIR / "migrations"))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


@pytest.fixture
def empty_db_url(admin_engine: Engine) -> Iterator[str]:
    url = _create_empty_database(admin_engine)
    yield url
    _drop_database(admin_engine, url)


@pytest.fixture
def migrated_db_url(empty_db_url: str) -> str:
    upgrade(empty_db_url)
    return empty_db_url


@pytest.fixture
def engine(migrated_db_url: str) -> Iterator[Engine]:
    eng = app_db.make_engine(migrated_db_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def shared_migrated_engine(admin_engine: Engine) -> Iterator[Engine]:
    """One migrated DB per module for constraint tests that roll back."""
    url = _create_empty_database(admin_engine)
    upgrade(url)
    eng = app_db.make_engine(url)
    yield eng
    eng.dispose()
    _drop_database(admin_engine, url)


@pytest.fixture
def app_env(monkeypatch: pytest.MonkeyPatch):
    """Point application settings at a given database URL."""

    def _set(url: str) -> None:
        monkeypatch.setenv("RAS_DATABASE_URL", url)
        app_config.get_settings.cache_clear()
        app_db.get_engine.cache_clear()

    yield _set
    app_config.get_settings.cache_clear()
    app_db.get_engine.cache_clear()
