"""M6c-1 smoke: a restore from before a revocation serves nothing until `recover`, on an *isolated* Compose project.

User A and User B upload the same synthetic Markdown through the web (B also a
private file of its own); the Compose worker extracts and this host process
indexes with a fake embedding (the containers have no key). The Admin approves
A's file into a common copy whose rights permit download, full-text search and
provider processing, publishes and indexes it. Then:

1. a backup of the database (``CREATE DATABASE ... TEMPLATE``, API/worker/beat
   stopped) and of every object in the bucket; one private upload is left
   ``received`` with the worker stopped, to see whether a worker processes it;
2. the Admin revokes the copy through the web and cleanup runs (object and
   derived rows gone);
3. restore: the old database replaces the live one and the objects are put
   back; the journal volume (a host directory here) is kept;
4. before replay: every request through the web proxy and straight to the API
   answers 503 ``service_recovering``, ``/readyz`` 503, the pages carry no
   data, and the worker leaves the job untouched;
5. ``python -m app.cli.recovery recover`` in a one-off container with
   API/worker/beat stopped; then the copy stays closed for everyone, its
   derived rows and object are gone, and B's private copy of the same bytes
   and A's files are intact; the left-over job is processed.

Needs ``SMOKE_COMPOSE_PROJECT`` (never the dev project ``ras``) and
``RAS_JOURNAL_SOURCE`` (an absolute host directory, also used by ``up``). Reads
only ``POSTGRES_*``/``MINIO_ROOT_*`` from ``.env`` and refuses a database
holding any account it did not create. Prints step results and counts only.
"""

from __future__ import annotations

import hashlib
import io
import os
import secrets
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
ALLOWED = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD")
PREFIX = "m6c1-smoke-"
PROJECT = os.environ.get("SMOKE_COMPOSE_PROJECT", "")
JOURNAL_DIR = Path(os.environ.get("RAS_JOURNAL_SOURCE", ""))
if not PROJECT or PROJECT == "ras" or not JOURNAL_DIR.is_absolute():
    raise SystemExit("set SMOKE_COMPOSE_PROJECT (not 'ras') and an absolute RAS_JOURNAL_SOURCE directory")


def env_subset() -> dict[str, str]:
    """Only the whitelisted names; every other line of .env (keys included) is skipped unread."""
    values = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() in ALLOWED:
            value = value.strip()
            values[name.strip()] = value[1:-1] if len(value) >= 2 and value[0] == value[-1] in "'\"" else value
    return values


ENV = env_subset()
PORTS = {name: os.environ.get(name, default) for name, default in (
    ("POSTGRES_HOST_PORT", "5432"), ("WEB_HOST_PORT", "3000"), ("API_HOST_PORT", "8000"),
    ("MINIO_API_HOST_PORT", "9000"))}
for name, value in {
    "RAS_DB__HOST": "127.0.0.1", "RAS_DB__PORT": PORTS["POSTGRES_HOST_PORT"], "RAS_DB__USER": ENV["POSTGRES_USER"],
    "RAS_DB__PASSWORD": ENV["POSTGRES_PASSWORD"], "RAS_DB__NAME": ENV["POSTGRES_DB"],
    "RAS_STORAGE__ENDPOINT_URL": f"http://127.0.0.1:{PORTS['MINIO_API_HOST_PORT']}",
    "RAS_STORAGE__ACCESS_KEY": ENV["MINIO_ROOT_USER"], "RAS_STORAGE__SECRET_KEY": ENV["MINIO_ROOT_PASSWORD"],
    "RAS_STORAGE__BUCKET": "ras-originals", "RAS_EMBEDDING__API_KEY": "", "OPENROUTER_API_KEY": "",
    "RAS_RECOVERY__JOURNAL_PATH": str(JOURNAL_DIR / "revocations.jsonl"),
}.items():
    os.environ[name] = str(value)

from app.config import get_settings  # noqa: E402
from app.identity.passwords import hash_password  # noqa: E402
from app.ingest import indexer  # noqa: E402
from app.ingest.embedding import EmbeddingResult  # noqa: E402
from app.ingest.worker import process_job  # noqa: E402
from app.storage import OriginalStorage, is_missing_object  # noqa: E402

get_settings.cache_clear()
BASE = get_settings()
assert BASE.embedding.api_key is None or not BASE.embedding.api_key.get_secret_value()
FAKE = BASE.model_copy(deep=True)
FAKE.embedding.api_key = SecretStr("synthetic-fake-key")
FAKE.embedding.pricing_verified = True  # fake transport below; nothing reaches a provider
ENGINE = create_engine(URL.create("postgresql+psycopg", username=ENV["POSTGRES_USER"],
                                  password=ENV["POSTGRES_PASSWORD"], host="127.0.0.1",
                                  port=int(PORTS["POSTGRES_HOST_PORT"]), database=ENV["POSTGRES_DB"]),
                       hide_parameters=True)
WEB = f"http://127.0.0.1:{PORTS['WEB_HOST_PORT']}"
API = f"http://127.0.0.1:{PORTS['API_HOST_PORT']}"
SCOPE = {"audience": "instance_users", "original_download": True, "fulltext_search": True,
         "external_processing": True, "conditions": None}
RECOVERING = {"error": "service_recovering"}
RESULTS: list[tuple[str, bool]] = []
CALLS: list[str] = []


def step(name: str, ok: bool) -> None:
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        raise SystemExit(1)


def scalar(sql: str, **params):
    with ENGINE.connect() as conn:
        return conn.scalar(text(sql), params)


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", "-p", PROJECT, *args], cwd=ROOT, capture_output=True, text=True,
                          check=check)


def psql(sql: str) -> None:
    """Run SQL as the Postgres superuser inside the db container, connected to the maintenance database."""
    compose("exec", "-T", "db", "sh", "-c", f'psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -c "{sql}"')


def fake_embed(value: str, selected):
    CALLS.append(value)
    return EmbeddingResult([0.1, 0.2, 0.3], selected.embedding.model_id, len(value.split()))


class Person:
    """A signed-in account; ``web`` goes through the Next.js origin, ``api`` straight to FastAPI (same cookies)."""

    def __init__(self, email: str, password: str):
        self.web = httpx.Client(base_url=WEB, timeout=60)
        assert self.web.get("/api/auth/csrf").status_code == 200
        response = self.web.post("/api/auth/login", json={"email": email, "password": password},
                                 headers=self.headers())
        assert response.status_code == 200, response.status_code
        self.api = httpx.Client(base_url=API, timeout=60, cookies=self.web.cookies)

    def headers(self) -> dict:
        return {"origin": WEB, "x-csrf-token": self.web.cookies.get("ras_csrf", "")}

    def get(self, path: str, direct: bool = False):
        return (self.api.get(path.removeprefix("/api")) if direct else self.web.get(path))

    def post(self, path: str, body: dict, direct: bool = False):
        client, target = (self.api, path.removeprefix("/api")) if direct else (self.web, path)
        return client.post(target, json=body, headers=self.headers())

    def upload(self, body: bytes) -> dict:
        notice = self.get("/api/uploads/notice").json()["version"]
        intent = self.post("/api/uploads/intents", {"acknowledged": True, "notice_version": notice,
                                                    "file_kind": "markdown", "filename": "smoke.md",
                                                    "byte_size": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        assert intent.status_code == 201, intent.status_code
        sent = self.web.put("/api/uploads/original", content=body, headers={
            **self.headers(), "x-upload-intent": intent.json()["intent_token"],
            "content-type": "application/octet-stream"})
        assert sent.status_code == 201, sent.status_code
        return sent.json()


def wait_state(job_id: uuid.UUID, wanted: str, seconds: int = 120) -> None:
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        if scalar("SELECT state FROM ingestion_jobs WHERE id=:j", j=job_id) == wanted:
            return
        time.sleep(0.5)
    raise AssertionError(f"job did not reach {wanted}")


def wait_http(url: str, status: int, seconds: int = 120) -> bool:
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        try:
            if httpx.get(url, timeout=3).status_code == status:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False


def indexed(person: Person, storage: OriginalStorage, body: bytes) -> dict:
    ids = person.upload(body)
    wait_state(uuid.UUID(ids["job_id"]), "awaiting_index")
    assert process_job(uuid.UUID(ids["job_id"]), FAKE, storage) == "ready"
    return ids


def present(storage: OriginalStorage, key: str) -> bool:
    try:
        storage.size(key)
        return True
    except Exception as exc:  # noqa: BLE001
        if is_missing_object(exc):
            return False
        raise


def hits(response: httpx.Response) -> set[str]:
    assert response.status_code == 200, response.status_code
    return {hit["item_id"] for hit in response.json()["hits"]}


def main() -> None:
    others = scalar("SELECT count(*) FROM users WHERE email NOT LIKE :p", p=f"{PREFIX}%")
    step("isolated database (no account other than this smoke's)", others == 0)
    step("recovery-init registered the new database: /readyz 200", httpx.get(f"{API}/readyz").status_code == 200)
    run = uuid.uuid4().hex[:8]
    password = secrets.token_urlsafe(24)
    with ENGINE.begin() as conn:
        for who, role in (("admin", "admin"), ("a", "user"), ("b", "user")):
            conn.execute(text("INSERT INTO users(email, password_verifier, role) VALUES (:e, :p, :r)"),
                         {"e": f"{PREFIX}{run}-{who}@example.org", "p": hash_password(password), "r": role})
    admin, a, b = (Person(f"{PREFIX}{run}-{who}@example.org", password) for who in ("admin", "a", "b"))
    people = {"a": a, "b": b, "admin": admin}
    indexer.embed = fake_embed
    storage = OriginalStorage(FAKE)
    term = f"Restore{run}"
    title = f"Restore common {run}"
    main_md = (f"# Restore {run}\n\n## Abstract\n{term} abstract of the shared method.\n\n## Methods\n"
               f"{term} result is nine units.\n").encode()
    a_main = indexed(a, storage, main_md)
    b_same = indexed(b, storage, main_md)  # same bytes and checksum as A's file and the common copy
    b_only = indexed(b, storage, f"## Methods\n{term} result Bsecret{run} private body.\n".encode())
    step("A and B indexed private Markdown through the web (Compose worker + host fake embedding)", True)

    rid = a.post("/api/review-requests", {"acknowledged": True, "item_id": a_main["item_id"],
                                          "version_id": a_main["version_id"], "source_file_id": a_main["file_id"],
                                          "notice_version": a.get("/api/review-requests/notice").json()["version"],
                                          "declared_source": "Synthetic smoke file", "declared_rights": "Smoke"})
    checksum = hashlib.sha256(main_md).hexdigest()
    approved = admin.post(f"/api/admin/review-requests/{rid.json()['request']['id']}/approve", {
        "confirm_verified": True, "file_checksum_sha256": checksum, "title": title,
        "verified_source": "Smoke Admin check: synthetic file by the smoke script",
        "evidence_note": "Smoke Admin check: synthetic permission for this checksum",
        "checked_on": datetime.now(UTC).date().isoformat(), "scope": SCOPE})
    copy = approved.json()["request"]["pending_publication_id"]
    assert admin.post(f"/api/admin/common-copies/{copy}/publish",
                      {"confirm_publish": True, "file_checksum_sha256": checksum}).status_code == 200
    job = uuid.UUID(admin.post(f"/api/admin/common-copies/{copy}/index",
                               {"confirm_external_processing": True}).json()["job"]["id"])
    wait_state(job, "awaiting_index")
    step("common copy (download + search + provider) approved, published and indexed",
         process_job(job, FAKE, storage) == "ready")
    common_hits = [h for h in b.post("/api/search", {"query": term, "mode": "keyword"}).json()["hits"]
                   if h["item_id"] == copy]
    chunk, common_file = common_hits[0]["chunk_id"], common_hits[0]["source_file_id"]
    common_key = scalar("SELECT storage_key FROM source_files WHERE id=:f", f=uuid.UUID(common_file))
    b_key = scalar("SELECT storage_key FROM source_files WHERE id=:f", f=uuid.UUID(b_same["file_id"]))
    step("before the backup, B finds the copy and downloads its original through the web",
         hashlib.sha256(b.get(f"/api/files/{common_file}/original").content).hexdigest() == checksum)

    # 1. Backup, with one private upload left unprocessed (worker stopped).
    compose("stop", "worker", "beat")
    left = a.upload(f"## Methods\n{term} left-over upload.\n".encode())
    left_job = uuid.UUID(left["job_id"])
    compose("stop", "api")
    ENGINE.dispose()
    db = ENV["POSTGRES_DB"]
    psql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{db}' AND pid<>pg_backend_pid()")
    psql(f'CREATE DATABASE {db}_m6c1_backup TEMPLATE {db}')
    saved = {key: storage.open(key).read() for key, _ in storage.orphan_keys("")}
    compose("start", "api")
    step("backup taken (database template + every object); API back",
         wait_http(f"{API}/readyz", 200) and scalar("SELECT state FROM ingestion_jobs WHERE id=:j", j=left_job)
         == "received")

    # 2. Revoke through the web; cleanup.
    revoked = admin.post(f"/api/admin/common-copies/{copy}/revoke",
                         {"confirm_revoke": True, "reason_code": "rights_withdrawn"})
    cleaned = compose("run", "--rm", "--no-deps", "worker", "python", "-m", "app.cli.cleanup_revoked").stdout
    step("Admin revoked the copy through the web; cleanup removed its object",
         revoked.status_code == 200 and "deleted=1" in cleaned and not present(storage, common_key))
    journal_text = (JOURNAL_DIR / "revocations.jsonl").read_text(encoding="ascii")
    step("the journal (host directory, outside both backups) holds the revocation: IDs and codes only",
         copy in journal_text and all(s not in journal_text for s in (title, term, f"Bsecret{run}", checksum,
                                                                         "Smoke Admin check")))

    # 3. Restore the old database and objects; keep the journal.
    compose("stop", "api")
    ENGINE.dispose()
    psql(f"DROP DATABASE {db} WITH (FORCE)")
    psql(f"ALTER DATABASE {db}_m6c1_backup RENAME TO {db}")
    for key, data in saved.items():
        storage.put(key, io.BytesIO(data), "application/octet-stream")
    compose("start", "api", "worker", "beat")
    step("restored: the old database says listed/indexed and the object is back",
         wait_http(f"{API}/healthz", 200)
         and tuple(ENGINE.connect().execute(text("SELECT publication_state, content_status FROM library_items"
                                                 " WHERE id=:i"), {"i": uuid.UUID(copy)}).one())
         == ("listed", "indexed") and present(storage, common_key))

    # 4. Before replay: nothing is served, directly or through the web; workers process nothing.
    requests = [("GET", "/api/library/items?limit=100"), ("GET", f"/api/library/items/{copy}"),
                ("GET", f"/api/files/{common_file}/original"), ("GET", f"/api/library/items/{copy}/chunks"),
                ("GET", f"/api/search/chunks/{chunk}"), ("GET", f"/api/admin/common-copies/{copy}"),
                ("POST", "/api/search", {"query": term, "mode": "keyword"}),
                ("POST", "/api/rag/ask", {"question": f"{term} result?", "mode": "keyword", "item_id": copy}),
                ("POST", "/api/rag/ask", {"question": f"{term} result?", "mode": "keyword", "scope": "all"})]
    for direct in (False, True):
        answers = [(p.get(path, direct) if method == "GET" else p.post(path, body[0], direct))
                   for p in people.values() for method, path, *body in requests]
        step(f"before replay, {'straight to the API' if direct else 'through the web proxy'}: all "
             f"{len(answers)} requests (A, B, Admin) answer 503 service_recovering",
             all(r.status_code == 503 and r.json() == RECOVERING for r in answers))
    pages = [b.get(path) for path in (f"/items/{copy}", "/search", "/")]
    step("pages are only the client shell: no title, text or file of the copy",
         all(r.status_code == 200 and title not in r.text and term not in r.text for r in pages))
    step("/readyz 503 and /healthz 200 (API process alive, not ready)",
         httpx.get(f"{API}/readyz").status_code == 503 and httpx.get(f"{API}/healthz").status_code == 200)
    time.sleep(15)  # three beat polls
    worker_log = compose("logs", "worker").stdout
    step("worker left the pending job untouched and logged the closed gate (codes only)",
         scalar("SELECT state FROM ingestion_jobs WHERE id=:j", j=left_job) == "received"
         and "recovery gate closed" in worker_log and "db_behind_journal" in worker_log)

    # 5. Recover in a one-off container with API/worker/beat stopped.
    compose("stop", "api", "worker", "beat")
    status = compose("run", "--rm", "--no-deps", "api", "python", "-m", "app.cli.recovery", "status", check=False)
    recovered = compose("run", "--rm", "--no-deps", "api", "python", "-m", "app.cli.recovery", "recover",
                        check=False)
    print("   ", status.stdout.strip())
    print("   ", recovered.stdout.strip() or recovered.stderr.strip())
    step("recover replayed the revocation, cleaned up and opened the gate",
         status.returncode == 3 and "gate=db_behind_journal" in status.stdout and recovered.returncode == 0
         and "gate=open" in recovered.stdout)
    compose("start", "api", "worker", "beat")
    step("API ready again", wait_http(f"{API}/readyz", 200))
    closed = [p.get(path) for p in people.values() for path in (
        f"/api/library/items/{copy}", f"/api/files/{common_file}/original", f"/api/library/items/{copy}/chunks",
        f"/api/search/chunks/{chunk}")]
    step("after recover, the copy is 404 for A, B and the Admin (item, original, chunks, source chunk)",
         all(r.status_code == 404 for r in closed))
    found = {who: hits(p.post("/api/search", {"query": term, "mode": "keyword"})) for who, p in people.items()}
    step("search: nobody finds the copy; B still finds its private copy of the same bytes",
         all(copy not in found[who] for who in people) and {b_same["item_id"], b_only["item_id"]} <= found["b"])
    rag = b.post("/api/rag/ask", {"question": f"{term} result?", "mode": "keyword", "item_id": copy})
    step("RAG on the copy is 404", rag.status_code == 404)
    step("derived rows and object of the copy are gone; tombstone purged",
         scalar("SELECT cardinality(ras_common_copy_derived_left(:i))", i=uuid.UUID(copy)) == 0
         and scalar("SELECT count(*) FROM chunks WHERE item_id=:i", i=uuid.UUID(copy)) == 0
         and scalar("SELECT purged_at IS NOT NULL FROM deletion_tombstones WHERE item_id=:i", i=uuid.UUID(copy))
         and not present(storage, common_key))
    step("B's private copy of the same bytes and A's file are intact (download SHA-256)",
         hashlib.sha256(b.get(f"/api/files/{b_same['file_id']}/original").content).hexdigest() == checksum
         and hashlib.sha256(a.get(f"/api/files/{a_main['file_id']}/original").content).hexdigest() == checksum
         and present(storage, b_key))
    wait_state(left_job, "awaiting_index")
    step("with the gate open, the worker processed the left-over upload", True)
    logs = compose("logs").stdout
    step("container logs: no traceback, no paper text, title or Admin evidence",
         "Traceback" not in logs and all(s not in logs for s in (title, term, "Smoke Admin check", checksum)))
    print({"steps": len(RESULTS), "passed": sum(ok for _, ok in RESULTS), "fake_embedding_requests": len(CALLS),
           "log_lines": len(logs.splitlines())})


if __name__ == "__main__":
    main()
