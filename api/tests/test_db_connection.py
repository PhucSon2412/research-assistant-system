"""Regression: database passwords containing URL delimiters.

Previously Compose spliced POSTGRES_PASSWORD into RAS_DATABASE_URL, so a
password with ``@`` (or ``:``, ``/``, ``#`` ...) was parsed as part of the
host/port. Connection parts are now passed separately and assembled with
``URL.create``. These tests cover the settings layer and, against a real
PostgreSQL role, every M1a entry point: Alembic, the API and bootstrap.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

from app.config import DatabaseConnection, Settings
from app.main import app
from tests.conftest import HEAD_REVISION, create_empty_database, db_env, drop_database

API_DIR = Path(__file__).resolve().parents[1]

TRICKY = [
    "p@ss",
    "a:b",
    "a/b",
    "a?b=c&d",
    "a#b",
    "a%40b",  # a literal percent sequence must not be decoded
    "a b",
    "a+b",
    "[::1]",
    "@:/?#[]%",
    "a$b",
    "a'b\"c",
    "a\\b",
    "ünï©ødé",
]

PARTS = {"host": "db", "port": 5432, "user": "ras", "name": "ras"}


def _clear_ras_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("RAS_"):
            monkeypatch.delenv(key)


@pytest.mark.parametrize("password", TRICKY)
def test_connection_parts_keep_password_and_host_intact(password: str) -> None:
    url = DatabaseConnection(password=password, **PARTS).url()
    assert (url.password, url.host, url.port, url.database, url.username) == (password, "db", 5432, "ras", "ras")
    # Rendering to a driver string escapes the password; parsing it back is lossless.
    rendered = url.render_as_string(hide_password=False)
    assert make_url(rendered).password == password
    assert make_url(rendered).host == "db"


@pytest.mark.parametrize("password", ["p@ss", "a%40b", "x@y:z/w", "@:/?#[]%"])
def test_old_spliced_url_breaks_negative_control(password: str) -> None:
    """Documents the original bug: the previous Compose template spliced the
    raw password into the URL. ``@`` moves part of the password into the
    host, ``%XX`` is silently decoded, and mixes of delimiters fail to parse.
    (A lone ``:``/``/``/``#`` happens to survive SQLAlchemy's parser, which is
    not something to rely on.)"""
    spliced = f"postgresql+psycopg://ras:{password}@db:5432/ras"
    try:
        url = make_url(spliced)
    except ValueError:
        return
    assert (url.password, url.host, url.port, url.database) != (password, "db", 5432, "ras")


@pytest.mark.parametrize("password", TRICKY)
def test_settings_from_environment_parts(monkeypatch: pytest.MonkeyPatch, password: str) -> None:
    _clear_ras_env(monkeypatch)
    for key, value in {
        "RAS_DB__HOST": "db", "RAS_DB__PORT": "5432", "RAS_DB__USER": "ras",
        "RAS_DB__PASSWORD": password, "RAS_DB__NAME": "ras",
    }.items():
        monkeypatch.setenv(key, value)
    url = Settings().sqlalchemy_url()  # type: ignore[call-arg]
    assert (url.password, url.host, url.database) == (password, "db", "ras")


def test_percent_encoded_database_url_is_still_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ras_env(monkeypatch)
    monkeypatch.setenv("RAS_DATABASE_URL", "postgresql+psycopg://ras:p%40ss%3Aw%2Frd@db:5432/ras")
    url = Settings().sqlalchemy_url()  # type: ignore[call-arg]
    assert (url.password, url.host) == ("p@ss:w/rd", "db")


def test_exactly_one_connection_source_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ras_env(monkeypatch)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql+psycopg://u@h/d", db=DatabaseConnection(password="x", **PARTS))


def test_settings_repr_hides_password() -> None:
    s = Settings(db=DatabaseConnection(password="p@ss-visible?", **PARTS))
    assert "p@ss-visible?" not in repr(s) + str(s)
    assert "p@ss-visible?" not in repr(s.sqlalchemy_url())


# --- real PostgreSQL role with a delimiter-heavy password --------------------


@pytest.fixture
def tricky_role_db(admin_engine: Engine) -> Iterator[URL]:
    """Fresh DB owned by a throwaway role whose password contains every
    delimiter above. Yields a URL built from parts (never printed)."""
    role = f"ras_t_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(8) + "@:/?#[]%40 $'\\\"&+ü"
    with admin_engine.connect() as conn:
        literal = password.replace("'", "''")
        conn.execute(text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{literal}'"))
    db_url = create_empty_database(admin_engine, owner=role)
    # CREATE EXTENSION needs privileges the role lacks; pre-create it so the
    # migration's IF NOT EXISTS is a no-op.
    admin_db = create_engine(db_url, isolation_level="AUTOCOMMIT")
    with admin_db.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    admin_db.dispose()

    yield db_url.set(username=role, password=password)

    drop_database(admin_engine, db_url)
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))


def _subprocess_env(url: URL) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RAS_")}
    env.update(db_env(url))
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run(args: list[str], url: URL, stdin: str = "",
         extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args], input=stdin, capture_output=True, text=True, encoding="utf-8",
        cwd=API_DIR, env=_subprocess_env(url) | (extra or {}), timeout=120,
    )


def test_special_password_works_for_migration_api_and_bootstrap(tricky_role_db: URL, app_env,
                                                                  journal_path) -> None:
    password = tricky_role_db.password
    assert "@" in password and ":" in password and "/" in password

    # 1. Alembic, exactly as the Compose `migrate` service runs it.
    migrate = _run(["-m", "alembic", "upgrade", "head"], tricky_role_db)
    assert migrate.returncode == 0, migrate.stderr[-2000:].replace(password, "<pw>")

    # 1b. New-install registration, exactly as the Compose `recovery-init` service runs it (M6c-1).
    init = _run(["-m", "app.cli.recovery", "init", "--if-new"], tricky_role_db,
                extra={"RAS_RECOVERY__JOURNAL_PATH": str(journal_path)})
    assert init.returncode == 0 and "Registered a new install" in init.stdout, \
        init.stderr[-2000:].replace(password, "<pw>")

    # 2. API readiness through RAS_DB__* settings.
    app_env(tricky_role_db)
    assert TestClient(app).get("/readyz").status_code == 200

    # 3. Bootstrap CLI in a separate process: first succeeds, second refused.
    admin_pw = secrets.token_urlsafe(18)
    cmd = ["-m", "app.cli.bootstrap_admin", "--email", "admin@example.org", "--password-stdin"]
    first = _run(cmd, tricky_role_db, admin_pw + "\n")
    second = _run(cmd, tricky_role_db, admin_pw + "\n")
    assert first.returncode == 0, first.stderr.replace(password, "<pw>")
    assert second.returncode == 3

    for proc in (migrate, first, second):
        assert password not in proc.stdout + proc.stderr
        assert admin_pw not in proc.stdout + proc.stderr

    check = create_engine(tricky_role_db)
    with check.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        assert conn.scalar(text("SELECT count(*) FROM users WHERE role = 'admin'")) == 1
    check.dispose()
