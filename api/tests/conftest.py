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
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

from app import config as app_config
from app import db as app_db

API_DIR = Path(__file__).resolve().parents[1]
# Latest Alembic revision; bump when a migration is added.
HEAD_REVISION = "0028"
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


def _unquote(value: str) -> str:
    # Compose-style quoting: '...' is literal, "..." is also accepted.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _admin_url() -> URL:
    url = os.environ.get("RAS_TEST_DATABASE_ADMIN_URL")
    if url:
        return make_url(url)
    env = {k: _unquote(v) for k, v in _read_dotenv(REPO_DIR / ".env").items()}
    required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
    if not all(env.get(k) for k in required):
        pytest.fail("Set RAS_TEST_DATABASE_ADMIN_URL or create .env and start `docker compose up -d db`")
    # Parts, not an f-string: the password may contain URL delimiters.
    return URL.create(
        "postgresql+psycopg",
        username=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"],
        host="127.0.0.1",
        port=int(env.get("POSTGRES_HOST_PORT", "5432")),
        database=env["POSTGRES_DB"],
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


def create_empty_database(admin_engine: Engine, owner: str | None = None) -> URL:
    name = f"ras_test_{uuid.uuid4().hex[:12]}"
    owner_sql = f' OWNER "{owner}"' if owner else ""
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0{owner_sql}'))
    return admin_engine.url.set(database=name)


def drop_database(admin_engine: Engine, url: URL) -> None:
    name = url.database
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def alembic_config(url: str | URL | None, output: io.StringIO | None = None) -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"), output_buffer=output)
    cfg.set_main_option("script_location", str(API_DIR / "migrations"))
    if url is not None:
        cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


def upgrade(url: str | URL, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


@pytest.fixture
def empty_db_url(admin_engine: Engine) -> Iterator[URL]:
    url = create_empty_database(admin_engine)
    yield url
    drop_database(admin_engine, url)


@pytest.fixture
def journal_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This test's revocation journal (M6c-1): a file outside the database, as in a deployment."""
    path = tmp_path / "journal" / "revocations.jsonl"
    monkeypatch.setenv("RAS_RECOVERY__JOURNAL_PATH", str(path))
    app_config.get_settings.cache_clear()
    return path


def register_new_install(url: URL, path: Path) -> None:
    """Genesis + registration for a freshly migrated database, like `recovery init` on a new install."""
    from app.clock import utcnow
    from app.recovery import init_new

    eng = app_db.make_engine(url)
    try:
        init_new(eng, SimpleNamespace(recovery=SimpleNamespace(journal_path=path)), utcnow())
    finally:
        eng.dispose()


@pytest.fixture
def migrated_db_url(empty_db_url: URL, journal_path: Path) -> URL:
    upgrade(empty_db_url)
    register_new_install(empty_db_url, journal_path)
    return empty_db_url


@pytest.fixture
def engine(migrated_db_url: URL) -> Iterator[Engine]:
    eng = app_db.make_engine(migrated_db_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def shared_migrated_engine(admin_engine: Engine) -> Iterator[Engine]:
    """One migrated DB per module for constraint tests that roll back."""
    url = create_empty_database(admin_engine)
    upgrade(url)
    eng = app_db.make_engine(url)
    yield eng
    eng.dispose()
    drop_database(admin_engine, url)


def db_env(url: URL) -> dict[str, str]:
    """RAS_DB__* variables for ``url``, the same shape Compose provides."""
    return {
        "RAS_DB__HOST": url.host or "",
        "RAS_DB__PORT": str(url.port or 5432),
        "RAS_DB__USER": url.username or "",
        "RAS_DB__PASSWORD": url.password or "",
        "RAS_DB__NAME": url.database or "",
    }


@pytest.fixture
def api(app_env, migrated_db_url: URL, monkeypatch: pytest.MonkeyPatch, engine: Engine):
    """Migrated DB + development auth settings + a controllable clock."""
    from app.api.deps import get_clock, get_now
    from app.delivery import get_delivery
    from app.main import app
    from tests.helpers import DEV_ENV, FakeClock, Harness

    for key, value in DEV_ENV.items():
        monkeypatch.setenv(key, value)
    app_env(migrated_db_url)
    get_delivery.cache_clear()
    clock = FakeClock()
    app.dependency_overrides[get_now] = lambda: clock.now
    app.dependency_overrides[get_clock] = lambda: (lambda: clock.now)
    yield Harness(engine=engine, clock=clock)
    app.dependency_overrides.clear()
    get_delivery.cache_clear()


@pytest.fixture
def app_env(monkeypatch: pytest.MonkeyPatch):
    """Point application settings at a database through RAS_DB__* parts."""

    def _set(url: URL) -> None:
        monkeypatch.delenv("RAS_DATABASE_URL", raising=False)
        for key, value in db_env(url).items():
            monkeypatch.setenv(key, value)
        app_config.get_settings.cache_clear()
        app_db.get_engine.cache_clear()

    yield _set
    app_config.get_settings.cache_clear()
    app_db.get_engine.cache_clear()
