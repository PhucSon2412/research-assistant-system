"""Test fixtures against a real PostgreSQL (the Compose `db` service).

``empty_db_url`` is a brand-new empty database, so "empty DB -> migrate" is
exercised for real by the migration tests. ``migrated_db_url`` (M12) clones a
template that was migrated once per session/worker; the template is named by
the hash of ``api/migrations`` and checked once per session against a fresh
``upgrade head``. The admin connection URL comes from
RAS_TEST_DATABASE_ADMIN_URL, or is derived from the repo's .env (host port
POSTGRES_HOST_PORT on 127.0.0.1).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace


def _apply_worker_env() -> None:
    """M12: under pytest-xdist each worker gets its own PostgreSQL/Redis/MinIO.

    scripts/m8a_test_host.py writes one env block per worker (cluster roles,
    limiter keys and buckets never cross workers); it must run before any test
    module reads the environment, hence at conftest import."""
    path = os.environ.get("RAS_TEST_WORKER_ENV_FILE")
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not path or not worker:
        return
    blocks = json.loads(Path(path).read_text(encoding="utf-8"))
    if worker not in blocks:
        raise RuntimeError(f"no isolated test stack for xdist worker {worker}; set -n to the harness worker count")
    os.environ.update(blocks[worker])


_apply_worker_env()

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import Engine, create_engine, text  # noqa: E402
from sqlalchemy.engine import URL, make_url  # noqa: E402

from app import config as app_config  # noqa: E402
from app import db as app_db  # noqa: E402
from app.rate_limit import make_rate_limiter as REAL_MAKE_RATE_LIMITER  # noqa: E402

API_DIR = Path(__file__).resolve().parents[1]
# Latest Alembic revision; bump when a migration is added.
HEAD_REVISION = "0052"
REPO_DIR = API_DIR.parent


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--ras-shuffle", type=int, default=None, metavar="SEED",
                     help="M12: run the collected tests in a seeded random order (same order on every xdist worker)")
    parser.addoption("--ras-shard", default=None, metavar="INDEX/COUNT",
                     help="M12: 1-based node shard, applied after marker deselection")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    shard = config.getoption("--ras-shard")
    if shard:
        try:
            index, count = map(int, shard.split("/"))
            assert 1 <= index <= count
        except (ValueError, AssertionError):
            raise pytest.UsageError("--ras-shard must be INDEX/COUNT with 1 <= INDEX <= COUNT") from None
        import sys
        sys.path.insert(0, str(REPO_DIR / "scripts"))
        from ci_tiers import durations, shard_nodes
        # The workflow sets the tier explicitly; local full is the conservative default.
        ordered = shard_nodes([item.nodeid for item in items], index - 1, count,
                              durations(os.environ.get("TIER", "full")))
        selected = set(ordered)
        deselected = [item for item in items if item.nodeid not in selected]
        by_id = {item.nodeid: item for item in items}
        # Start long cases first; otherwise a late 28-minute case can dominate
        # wall time even when total shard weights are balanced.
        items[:] = [by_id[node] for node in ordered]
        config.hook.pytest_deselected(items=deselected)
    seed = config.getoption("--ras-shuffle")
    if seed is not None:
        random.Random(seed).shuffle(items)


_previous_node = ""


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """JUnit can reconstruct the exact order of each worker, including setup failures."""
    global _previous_node
    item.user_properties.extend((("ras_worker", os.environ.get("PYTEST_XDIST_WORKER", "serial")),
                                 ("ras_previous", _previous_node)))
    _previous_node = item.nodeid


def pytest_report_header(config: pytest.Config) -> str | None:
    seed = config.getoption("--ras-shuffle")
    return None if seed is None else f"ras-shuffle seed: {seed}"


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


def migrations_digest() -> str:
    """Hash of everything ``upgrade head`` executes: alembic.ini and api/migrations (no caches)."""
    digest = hashlib.sha256()
    files = [API_DIR / "alembic.ini", *(API_DIR / "migrations").rglob("*")]
    for path in sorted(p for p in files if p.is_file() and "__pycache__" not in p.parts):
        digest.update(path.relative_to(API_DIR).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def _terminate(admin_engine: Engine, name: str) -> None:
    with admin_engine.connect() as conn:
        conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                          " WHERE datname=:d AND pid <> pg_backend_pid()"), {"d": name})


# Catalog fingerprint of one database: every object, definition, owner and ACL
# outside the system schemas, plus the rows migrations insert. Names, never OIDs.
_SCHEMA_QUERIES = {
    "extensions": "SELECT e.extname, e.extversion, n.nspname FROM pg_extension e"
                  " JOIN pg_namespace n ON n.oid=e.extnamespace ORDER BY 1",
    "schemas": "SELECT nspname, pg_get_userbyid(nspowner), nspacl::text FROM pg_namespace"
               " WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema' ORDER BY 1",
    "relations": "SELECT n.nspname, c.relname, c.relkind, pg_get_userbyid(c.relowner), c.relacl::text,"
                 " c.reloptions::text, c.relpersistence, c.relrowsecurity, c.relforcerowsecurity,"
                 " obj_description(c.oid, 'pg_class') FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
                 " WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema' ORDER BY 1, 2",
    "columns": "SELECT c.relname, a.attname, a.attnum, format_type(a.atttypid, a.atttypmod), a.attnotnull,"
               " pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated, a.attacl::text, a.attcollation::regcollation::text,"
               " col_description(c.oid, a.attnum) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid"
               " JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum"
               " WHERE a.attnum > 0 AND NOT a.attisdropped AND n.nspname NOT LIKE 'pg\\_%'"
               " AND n.nspname <> 'information_schema' ORDER BY 1, 3",
    "constraints": "SELECT c.conrelid::regclass::text, c.contypid::regtype::text, c.conname, pg_get_constraintdef(c.oid),"
                   " c.condeferrable, c.condeferred, c.convalidated FROM pg_constraint c JOIN pg_namespace n"
                   " ON n.oid=c.connamespace WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'"
                   " ORDER BY 1, 2, 3",
    "indexes": "SELECT schemaname, tablename, indexname, indexdef FROM pg_indexes"
               " WHERE schemaname NOT LIKE 'pg\\_%' AND schemaname <> 'information_schema' ORDER BY 1, 2, 3",
    "triggers": "SELECT t.tgrelid::regclass::text, t.tgname, pg_get_triggerdef(t.oid), t.tgenabled FROM pg_trigger t"
                " WHERE NOT t.tgisinternal ORDER BY 1, 2",
    "functions": "SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid), p.prokind,"
                 " pg_get_function_result(p.oid), md5(p.prosrc), p.prosecdef, p.provolatile, p.proconfig::text,"
                 " pg_get_userbyid(p.proowner), p.proacl::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace"
                 " WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema' ORDER BY 1, 2, 3",
    "views": "SELECT schemaname, viewname, md5(definition) FROM pg_views"
             " WHERE schemaname NOT LIKE 'pg\\_%' AND schemaname <> 'information_schema' ORDER BY 1, 2",
    "policies": "SELECT schemaname, tablename, policyname, permissive, roles::text, cmd, qual, with_check"
                " FROM pg_policies ORDER BY 1, 2, 3",
    "types": "SELECT n.nspname, t.typname, t.typtype, pg_get_userbyid(t.typowner), t.typacl::text,"
             " (SELECT string_agg(enumlabel, ',' ORDER BY enumsortorder) FROM pg_enum e WHERE e.enumtypid=t.oid)"
             " FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname NOT LIKE 'pg\\_%'"
             " AND n.nspname <> 'information_schema' AND t.typtype IN ('e', 'd', 'r', 'm') ORDER BY 1, 2",
    "sequences": "SELECT schemaname, sequencename, data_type::text, start_value, increment_by, last_value"
                 " FROM pg_sequences ORDER BY 1, 2",
    "default_acl": "SELECT pg_get_userbyid(defaclrole), defaclnamespace::regnamespace::text, defaclobjtype,"
                   " defaclacl::text FROM pg_default_acl ORDER BY 1, 2, 3",
    "database": "SELECT d.datacl::text, d.encoding, d.datcollate, d.datctype,"
                " (SELECT setconfig::text FROM pg_db_role_setting s WHERE s.setdatabase=d.oid AND s.setrole=0)"
                " FROM pg_database d WHERE d.datname=current_database()",
}


def schema_fingerprint(url: URL) -> dict[str, list[tuple]]:
    eng = create_engine(url, hide_parameters=True)
    try:
        with eng.connect() as conn:
            found = {k: [tuple(r) for r in conn.execute(text(q))] for k, q in _SCHEMA_QUERIES.items()}
            tables = conn.execute(text("SELECT format('%I.%I', schemaname, tablename) FROM pg_tables"
                                       " WHERE schemaname NOT LIKE 'pg\\_%' AND schemaname <> 'information_schema'"
                                       " ORDER BY 1")).scalars().all()
            found["rows"] = [(t, *conn.execute(text(
                f"SELECT count(*), md5(coalesce(string_agg(r::text, '|' ORDER BY r::text), '')) FROM {t} r")).one())
                for t in tables]
        return found
    finally:
        eng.dispose()


def _compare_fingerprints(clone: dict[str, list[tuple]], fresh: dict[str, list[tuple]]) -> None:
    diffs = []
    for key in fresh:
        extra, missing = set(clone[key]) - set(fresh[key]), set(fresh[key]) - set(clone[key])
        if extra or missing or clone[key] != fresh[key]:
            diffs.append(f"{key}: clone-only {sorted(map(str, extra))[:3]}, upgrade-only {sorted(map(str, missing))[:3]}")
    if diffs:
        pytest.fail("migrated template differs from a fresh `upgrade head`:\n" + "\n".join(diffs))


@pytest.fixture(scope="session")
def migrated_template(admin_engine: Engine) -> str:
    """One database migrated to head per session/worker; tests clone it (M12).

    Built under an advisory lock on the admin DB, renamed into place only once
    complete and then closed to connections, so a clone never copies a partial
    migration or races another worker. The genesis/registration is not in it:
    ``register_new_install`` still runs per test with that test's journal. Once
    per session a clone is compared with a fresh ``upgrade head``."""
    name = f"ras_tpl_{migrations_digest()}"
    with admin_engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(hashtext(:n))"), {"n": name})
        try:
            if not conn.scalar(text("SELECT datistemplate FROM pg_database WHERE datname=:n"), {"n": name}):
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
                build = create_empty_database(admin_engine)
                try:
                    upgrade(build)
                    _terminate(admin_engine, build.database)
                    conn.execute(text(f'ALTER DATABASE "{build.database}" RENAME TO "{name}"'))
                except BaseException:
                    drop_database(admin_engine, build)
                    raise
                conn.execute(text(f'ALTER DATABASE "{name}" WITH IS_TEMPLATE true ALLOW_CONNECTIONS false'))
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(hashtext(:n))"), {"n": name})
    clone, fresh = clone_migrated_database(admin_engine, name), create_empty_database(admin_engine)
    try:
        upgrade(fresh)
        _compare_fingerprints(schema_fingerprint(clone), schema_fingerprint(fresh))
    finally:
        drop_database(admin_engine, clone)
        drop_database(admin_engine, fresh)
    return name


def clone_migrated_database(admin_engine: Engine, template: str) -> URL:
    name = f"ras_test_{uuid.uuid4().hex[:12]}"
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
    return admin_engine.url.set(database=name)


@pytest.fixture
def migrated_db_url(admin_engine: Engine, migrated_template: str, journal_path: Path) -> Iterator[URL]:
    url = clone_migrated_database(admin_engine, migrated_template)
    register_new_install(url, journal_path)
    yield url
    drop_database(admin_engine, url)


@pytest.fixture
def engine(migrated_db_url: URL) -> Iterator[Engine]:
    eng = app_db.make_engine(migrated_db_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def shared_migrated_engine(admin_engine: Engine, migrated_template: str) -> Iterator[Engine]:
    """One migrated DB per module for constraint tests that roll back."""
    url = clone_migrated_database(admin_engine, migrated_template)
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
    from tests.test_auth_safety_foundation import runtime_engine
    from app import recovery
    from app.config import get_settings
    from app.storage import get_storage
    from app.clock import utcnow

    for key, value in DEV_ENV.items():
        monkeypatch.setenv(key, value)
    app_env(migrated_db_url)
    runtime = runtime_engine(SimpleNamespace(engine=engine))
    app_env(runtime.url)
    recovery.recover(engine, get_storage(get_settings()), get_settings(), utcnow())
    get_delivery.cache_clear()
    clock = FakeClock()
    app.dependency_overrides[get_now] = lambda: clock.now
    app.dependency_overrides[get_clock] = lambda: (lambda: clock.now)
    harness = Harness(engine=runtime, clock=clock)
    harness.setup_engine = engine  # privileged negative/snapshot setup only
    yield harness
    app.dependency_overrides.clear()
    get_delivery.cache_clear()
    runtime.dispose()


@pytest.fixture(autouse=True)
def _auth_rate_limit_default(monkeypatch: pytest.MonkeyPatch):
    """M8a: suites that do not target the auth limiter keep the pre-M8a request
    flow. The settings are left untouched (production still refuses
    ``disabled``); only the route's limiter factory is replaced here. Limiter
    tests restore the real factory with ``real_rate_limiter``."""
    from app import rate_limit

    monkeypatch.setattr(rate_limit, "make_rate_limiter", lambda settings: rate_limit.DisabledRateLimiter())
    rate_limit.get_rate_limiter.cache_clear()
    yield
    rate_limit.get_rate_limiter.cache_clear()


@pytest.fixture
def real_rate_limiter(monkeypatch: pytest.MonkeyPatch):
    from app import rate_limit

    monkeypatch.setattr(rate_limit, "make_rate_limiter", REAL_MAKE_RATE_LIMITER)
    rate_limit.get_rate_limiter.cache_clear()
    yield
    rate_limit.get_rate_limiter.cache_clear()


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


# Reuse the installation fixture for legacy modules that import upload_api
# without importing that fixture's dependencies themselves.
@pytest.fixture
def installation(engine,app_env,migrated_db_url,journal_path):
    from tests.test_c2c1_bootstrap import installation as original
    yield from original.__wrapped__(engine,app_env,migrated_db_url,journal_path)
