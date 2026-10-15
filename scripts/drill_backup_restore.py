"""M6c-2a restore drill: real backup and restore on two *isolated* Compose projects, never the dev stack.

Replaces the M6c-1 smoke's ``CREATE DATABASE ... TEMPLATE`` with ``scripts/ras_backup.py``:

1. Source project ``ras-drill-src`` (fresh volumes, its journal in ``<work>/source-journal``):
   User A and User B upload synthetic Markdown through the web (B one file with the same
   bytes as A's, one of its own; A also ``--filler`` extra files); the Compose worker extracts,
   this host process indexes with a fake embedding (no key in any container). The Admin
   approves A's file into a common copy permitting download, full-text search and provider
   processing, publishes and indexes it.
2. With the worker stopped A uploads one more file (job left ``received``); with every writer
   stopped: ``ras_backup.py backup`` (pg_dump in an exported snapshot + every object +
   manifest), then ``verify``. A backup attempted while the API runs is refused first.
3. Writers back: A uploads a file after the backup (a write the restore will lose), the Admin
   revokes the copy through the web, cleanup removes its object and derived rows.
4. Writers stopped; ``copy-journal`` copies the source's current journal to
   ``<work>/journal-copy`` (apart from the artifact). Refusals checked first: a damaged
   artifact copy, a missing journal copy, the dev or source project as target.
5. ``ras_backup.py restore`` into ``ras-drill-restore`` (own volumes and ports, journal =
   the copy). Before ``recover``: every request through the restore's web proxy and straight to
   its API answers 503, ``/readyz`` 503, the worker processes nothing.
6. ``recovery recover`` (services stopped), start, then: the copy is 404 for everyone, its
   derived rows and object are gone, B's private copy of the same bytes and A's files are
   intact, the post-backup upload is absent, the left-over job is processed; the source
   journal is byte-for-byte unchanged.
7. ``restore`` and ``recover`` again (and ``recover`` once more): the same end state.

Prints step results, counts and timings only, then a JSON summary. Needs ``DRILL_WORK_DIR``
(absolute, outside the repository, absent or empty). ``--down`` removes both projects'
volumes at the end (only these two projects); the work directory is left for inspection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import shutil
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
sys.path.insert(0, str(ROOT / "scripts"))
BASE_ENV = dict(os.environ)  # for subprocesses: taken before the RAS_* variables below are set for host code
# Task-owned project names and fresh loopback bindings; never historical projects.
from c4_isolation import free_port
DRILL_ID = "rasc4drill" + uuid.uuid4().hex[:8]
SRC, DST, FOREIGN = (DRILL_ID + suffix for suffix in ("src", "restore", "foreign"))
PORTS = {project: {key:str(free_port()) for key in ("POSTGRES_HOST_PORT","API_HOST_PORT",
    "WEB_HOST_PORT","MINIO_API_HOST_PORT","MINIO_CONSOLE_HOST_PORT")} for project in (SRC,DST,FOREIGN)}
WORK = Path(os.environ.get("DRILL_WORK_DIR", ""))
if not WORK.is_absolute() or WORK.resolve() == ROOT or ROOT in WORK.resolve().parents:
    raise SystemExit("set DRILL_WORK_DIR to an absolute directory outside the repository")
WORK = WORK.resolve()
SOURCE_JOURNAL, COPY = WORK / "source-journal", WORK / "journal-copy"
BACKUPS = WORK / "backups"
JOURNAL_SOURCE = {SRC: SOURCE_JOURNAL, DST: COPY, FOREIGN: COPY}
PREFIX = "m6c2a-drill-"
WRITERS = ("api", "web", "worker", "beat")

import ras_backup  # noqa: E402
import c4_fake_provider  # test-only controlled HTTP transport, no auth/gate patches
from c4_isolation import write_override

if not all(os.environ.get(name) for name in ras_backup.ALLOWED):
    raise SystemExit("supply synthetic POSTGRES/MINIO variables explicitly; this drill never reads .env")
ENV = {name:os.environ[name] for name in ras_backup.ALLOWED}
EXPLICIT_ENV=Path(os.environ.get("RAS_COMPOSE_ENV_FILE",""))
if not EXPLICIT_ENV.is_absolute() or ROOT==EXPLICIT_ENV.resolve() or ROOT in EXPLICIT_ENV.resolve().parents:
    raise SystemExit("RAS_COMPOSE_ENV_FILE must be an explicit file outside the repository")
from drill_accounts import seed_accounts, runtime_url
# Validate the explicit identity before creating directories or Compose resources.
runtime_url(URL.create("postgresql+psycopg",username=ENV["POSTGRES_USER"]))
for name, value in {
    "RAS_DB__HOST": "127.0.0.1", "RAS_DB__PORT": PORTS[SRC]["POSTGRES_HOST_PORT"], "RAS_DB__USER": ENV["POSTGRES_USER"],
    "RAS_DB__PASSWORD": ENV["POSTGRES_PASSWORD"], "RAS_DB__NAME": ENV["POSTGRES_DB"],
    "RAS_STORAGE__ENDPOINT_URL": f"http://127.0.0.1:{PORTS[SRC]['MINIO_API_HOST_PORT']}",
    "RAS_STORAGE__ACCESS_KEY": ENV["MINIO_ROOT_USER"], "RAS_STORAGE__SECRET_KEY": ENV["MINIO_ROOT_PASSWORD"],
    "RAS_STORAGE__BUCKET": ras_backup.BUCKET, "RAS_EMBEDDING__API_KEY": "", "OPENROUTER_API_KEY": "",
    "RAS_RECOVERY__JOURNAL_PATH": str(SOURCE_JOURNAL / "revocations.jsonl"),
}.items():
    os.environ[name] = str(value)

from app import backup  # noqa: E402
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
FAKE.embedding.api_key = SecretStr("c4-synthetic-controlled-provider")
FAKE.embedding.pricing_verified = True  # fake transport below; nothing reaches a provider
SCOPE = {"audience": "instance_users", "original_download": True, "fulltext_search": True,
         "external_processing": True, "conditions": None}
RECOVERING = {"error": "service_recovering"}
RESULTS: list[tuple[str, bool]] = []
CALLS: list[str] = []
MEASURED: dict = {}


def step(name: str, ok) -> None:
    ok = bool(ok)
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}", flush=True)
    if not ok:
        raise SystemExit(1)


def project_env(project: str) -> dict:
    if project not in PORTS:
        raise RuntimeError('unknown task project')
    override=WORK / 'compose' / project / 'isolation.json'
    if not override.exists():
        write_override(override.parent,PORTS[project])
    return {**BASE_ENV, **PORTS[project], "OPENROUTER_API_KEY": "", "RAS_DISCOVERY__ENABLED": "false",
            "RAS_JOURNAL_SOURCE": str(JOURNAL_SOURCE[project]), "RAS_COMPOSE_OVERRIDE_FILE":str(override),
            "RAS_COMPOSE_PORTS_FILE":str(override.parent/'ports.yaml')}


def dc(project: str, *args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")  # container logs are UTF-8; the Windows default codec is not
    environment=kwargs.pop("env",project_env(project))
    if environment.get('RAS_DRILL_PREBUILT_IMAGES')=='true':
        if args[0]=='build':
            # Every service/restore CLI uses the already verified C4 image;
            # rebuilding per project would change an attestation/tag mid-drill.
            for key in ('RAS_API_IMAGE','RAS_WEB_IMAGE'):
                image=environment.get(key,'')
                if not image.startswith('rasc4'):raise RuntimeError('task-owned prebuilt image required')
                subprocess.run(['docker','image','inspect',image,'--format','{{.Id}}'],check=True,capture_output=True)
            return subprocess.CompletedProcess(args,0,stdout='prebuilt C4 images verified',stderr='')
        if args[0]=='up':
            args=tuple(x for x in args if x!='--build')+('--no-build',)
    result = subprocess.run(["docker", "compose", "--env-file",str(EXPLICIT_ENV),"-p", project,
                            '-f',str(ROOT/'docker-compose.yml'),'-f',environment['RAS_COMPOSE_OVERRIDE_FILE'],
                            '-f',environment['RAS_COMPOSE_PORTS_FILE'],*args], cwd=ROOT, env=environment,
                            **kwargs)
    if check and result.returncode != 0:
        raise RuntimeError(f"docker compose -p {project} {args[0]} failed ({result.returncode})")
    return result


def tool(project: str, *args: str) -> tuple[int, dict | None, str]:
    """Run scripts/ras_backup.py as an operator would; returns exit code, the JSON line, stderr."""
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "ras_backup.py"), *args], cwd=ROOT,
                            env=project_env(project), capture_output=True, encoding="utf-8", errors="replace")
    with (WORK/'tool-outcomes.jsonl').open('a',encoding='utf-8') as evidence:
        evidence.write(json.dumps({'project':project,'action':args[0],'exit':result.returncode,
            'stdout':result.stdout,'stderr':result.stderr})+'\n')
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    return result.returncode, json.loads(lines[-1]) if lines else None, result.stderr.strip()


def volumes(project: str) -> list[str]:
    return subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
                          capture_output=True, text=True, check=True).stdout.split()


def engine_for(project: str):
    return create_engine(URL.create("postgresql+psycopg", username=ENV["POSTGRES_USER"],
                                    password=ENV["POSTGRES_PASSWORD"], host="127.0.0.1",
                                    port=int(PORTS[project]["POSTGRES_HOST_PORT"]), database=ENV["POSTGRES_DB"]),
                         hide_parameters=True)


def runtime_engine_for(project):
    owner=engine_for(project)
    try:url=runtime_url(owner.url)
    finally:owner.dispose()
    # Host worker/index calls use the same explicit runtime role. The separate
    # engine_for connection remains the migrator for read/setup/recovery work.
    for key,value in {"HOST":url.host,"PORT":url.port,"NAME":url.database,"USER":url.username,"PASSWORD":url.password}.items():
        os.environ["RAS_DB__"+key]=str(value)
    BASE.db=BASE.db.model_copy(update={"host":url.host,"port":url.port,"name":url.database,
                                      "user":url.username,"password":SecretStr(url.password)})
    FAKE.db=BASE.db.model_copy(deep=True)
    from app import db as app_db
    get_settings.cache_clear();app_db.get_engine.cache_clear()
    return create_engine(url,hide_parameters=True,pool_pre_ping=True)


def scalar(engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.scalar(text(sql), params)


def storage_for(project: str) -> OriginalStorage:
    return ras_backup.storage_at(int(PORTS[project]["MINIO_API_HOST_PORT"]), ENV)


def url(project: str, kind: str) -> str:
    return f"http://127.0.0.1:{PORTS[project]['WEB_HOST_PORT' if kind == 'web' else 'API_HOST_PORT']}"


def fake_embed(value: str, selected):
    CALLS.append(value)
    from app.ingest.embedding import embed
    return embed(value, selected)


class Person:
    """A signed-in account; ``web`` goes through the Next.js origin, ``api`` straight to FastAPI (same cookies)."""

    def __init__(self, project: str, cookies=None, email: str | None = None, password: str | None = None):
        self.project = project
        self.web = httpx.Client(base_url=url(project, "web"), timeout=60, cookies=cookies)
        if email:
            assert self.web.get("/api/auth/csrf").status_code == 200
            response = self.web.post("/api/auth/login", json={"email": email, "password": password},
                                     headers=self.headers())
            assert response.status_code == 200, response.status_code
        self.api = httpx.Client(base_url=url(project, "api"), timeout=60, cookies=self.web.cookies)

    def at(self, project: str) -> "Person":
        """The same session (its cookies) against another project: a restored database carries the sessions."""
        return Person(project, cookies=self.web.cookies)

    def headers(self) -> dict:
        return {"origin": url(self.project, "web"), "x-csrf-token": self.web.cookies.get("ras_csrf", "")}

    def get(self, path: str, direct: bool = False):
        return self.api.get(path.removeprefix("/api")) if direct else self.web.get(path)

    def post(self, path: str, body: dict, direct: bool = False):
        client, target = (self.api, path.removeprefix("/api")) if direct else (self.web, path)
        return client.post(target, json=body, headers=self.headers())

    def upload(self, body: bytes) -> dict:
        notice = self.get("/api/uploads/notice").json()["version"]
        intent = self.post("/api/uploads/intents", {"acknowledged": True, "notice_version": notice,
                                                    "file_kind": "markdown", "filename": "drill.md",
                                                    "byte_size": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        assert intent.status_code == 201, intent.status_code
        sent = self.web.put("/api/uploads/original", content=body, headers={
            **self.headers(), "x-upload-intent": intent.json()["intent_token"],
            "content-type": "application/octet-stream"})
        assert sent.status_code == 201, sent.status_code
        return sent.json()


def wait_state(engine, job_id: uuid.UUID, wanted: str, seconds: int = 600) -> None:
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        if scalar(engine, "SELECT state FROM ingestion_jobs WHERE id=:j", j=job_id) == wanted:
            return
        time.sleep(0.5)
    raise AssertionError(f"job did not reach {wanted}")


def wait_http(target: str, status: int, seconds: int = 180) -> bool:
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        try:
            if httpx.get(target, timeout=3).status_code == status:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False


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


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def start_app(project: str) -> None:
    """Start api/worker/beat/web even while the API is unready (gate shut).

    ``up`` waits for ``web``'s health dependency on ``api`` even with ``--no-deps``, so ``web``
    is created without starting, then started with ``docker start``.
    """
    dc(project, "up", "-d", "--no-deps", "redis", "api", "worker", "beat")
    if not wait_http(url(project, "api") + "/healthz", 200, seconds=120):
        raise RuntimeError("API did not become live before workflow requests")
    dc(project, "up", "--no-start", "--no-deps", "web")
    subprocess.run(["docker", "start", dc(project, "ps", "-aq", "web").stdout.strip()], check=True,
                   capture_output=True)


def recovery_cli(project: str, command: str) -> subprocess.CompletedProcess:
    # Explicit migrator identity for this privileged command, even after C4
    # configures the API service with runtime. Values travel via env, not argv.
    return dc(project, "run", "--rm", "--no-deps", "-e","RAS_DB__USER","-e","RAS_DB__PASSWORD",
              "api", "python", "-m", "app.cli.recovery", command, check=False,
              env=project_env(project)|{"RAS_DB__USER":ENV["POSTGRES_USER"],"RAS_DB__PASSWORD":ENV["POSTGRES_PASSWORD"]})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--filler", type=int, default=20, help="extra private Markdown uploads by A")
    parser.add_argument("--down", action="store_true", help="remove both drill projects and their volumes at the end")
    args = parser.parse_args()
    if WORK.exists() and any(WORK.iterdir()):
        raise SystemExit("DRILL_WORK_DIR must be absent or empty")
    if volumes(SRC) or volumes(DST) or volumes(FOREIGN):
        raise SystemExit(f"{SRC} / {DST} already have volumes; remove them first (docker compose -p <name> down -v)")
    SOURCE_JOURNAL.mkdir(parents=True)

    # 0. The source stack, fresh.
    started = time.monotonic()
    dc(SRC, "up", "-d", "--build", "--wait")
    MEASURED["source_up_seconds"] = round(time.monotonic() - started, 1)
    src_db = engine_for(SRC)
    others = scalar(src_db, "SELECT count(*) FROM users WHERE email NOT LIKE :p", p=f"{PREFIX}%")
    step("source project is fresh and isolated (no account other than this drill's); /readyz 200",
         others == 0 and httpx.get(f"{url(SRC, 'api')}/readyz").status_code == 200)

    # 1. Synthetic workload.
    run = uuid.uuid4().hex[:8]
    password = secrets.token_urlsafe(24)
    runtime=runtime_engine_for(SRC)
    try:seed_accounts(runtime,{who:f"{PREFIX}{run}-{who}@example.org" for who in ("admin","a","b")},password)
    finally:runtime.dispose()
    admin, a, b = (Person(SRC, email=f"{PREFIX}{run}-{who}@example.org", password=password)
                   for who in ("admin", "a", "b"))
    indexer.embed = fake_embed
    src_storage = OriginalStorage(FAKE)
    term = f"Drill{run}"
    title = f"Drill common {run}"
    main_md = (f"# Drill {run}\n\n## Abstract\n{term} abstract of the shared method.\n\n## Methods\n"
               f"{term} result is nine units.\n").encode()

    def indexed(person: Person, body: bytes) -> dict:
        ids = person.upload(body)
        wait_state(src_db, uuid.UUID(ids["job_id"]), "awaiting_index")
        assert process_job(uuid.UUID(ids["job_id"]), FAKE, src_storage) == "ready"
        return ids

    a_main = indexed(a, main_md)
    b_same = indexed(b, main_md)  # same bytes and checksum as A's file and the common copy
    b_only = indexed(b, f"## Methods\n{term} result Bsecret{run} private body.\n".encode())
    filler = [a.upload((f"# Filler {n}\n\n## Methods\n" + f"Synthetic filler sentence {n}. " * 500).encode())
              for n in range(args.filler)]
    for ids in filler:
        wait_state(src_db, uuid.UUID(ids["job_id"]), "awaiting_index")
    step(f"A and B uploaded through the web: 3 indexed + {args.filler} extracted private Markdown files", True)

    rid = a.post("/api/review-requests", {"acknowledged": True, "item_id": a_main["item_id"],
                                          "version_id": a_main["version_id"], "source_file_id": a_main["file_id"],
                                          "notice_version": a.get("/api/review-requests/notice").json()["version"],
                                          "declared_source": "Synthetic drill file", "declared_rights": "Drill"})
    checksum = sha(main_md)
    approved = admin.post(f"/api/admin/review-requests/{rid.json()['request']['id']}/approve", {
        "confirm_verified": True, "file_checksum_sha256": checksum, "title": title,
        "verified_source": "Drill Admin check: synthetic file by the drill script",
        "evidence_note": "Drill Admin check: synthetic permission for this checksum",
        "checked_on": datetime.now(UTC).date().isoformat(), "scope": SCOPE})
    copy = approved.json()["request"]["pending_publication_id"]
    assert admin.post(f"/api/admin/common-copies/{copy}/publish",
                      {"confirm_publish": True, "file_checksum_sha256": checksum}).status_code == 200
    job = uuid.UUID(admin.post(f"/api/admin/common-copies/{copy}/index",
                               {"confirm_external_processing": True}).json()["job"]["id"])
    wait_state(src_db, job, "awaiting_index")
    step("common copy (download + search + provider) approved, published and indexed",
         process_job(job, FAKE, src_storage) == "ready")
    common_hits = [h for h in b.post("/api/search", {"query": term, "mode": "keyword"}).json()["hits"]
                   if h["item_id"] == copy]
    chunk, common_file = common_hits[0]["chunk_id"], common_hits[0]["source_file_id"]
    common_key = scalar(src_db, "SELECT storage_key FROM source_files WHERE id=:f", f=uuid.UUID(common_file))
    b_key = scalar(src_db, "SELECT storage_key FROM source_files WHERE id=:f", f=uuid.UUID(b_same["file_id"]))
    step("before the backup, B finds the copy and downloads its original through the web",
         sha(b.get(f"/api/files/{common_file}/original").content) == checksum)

    # 2. Backup with every writer stopped (a backup with the API running is refused first).
    code, _, err = tool(SRC, "backup", "--project", SRC, "--out", str(BACKUPS))
    step("backup refused while writers run (writers_running), no artifact or partial directory left",
         code == 1 and "writers_running" in err and (not BACKUPS.exists() or not any(BACKUPS.iterdir())))
    dc(SRC, "stop", "worker", "beat")
    left = a.upload(f"## Methods\n{term} left-over upload.\n".encode())
    left_job = uuid.UUID(left["job_id"])
    dc(SRC, "stop", "api", "web")
    code, made, err = tool(SRC, "backup", "--project", SRC, "--out", str(BACKUPS))
    step("backup with writers stopped: pg_dump in an exported snapshot + objects + manifest", code == 0 and made)
    artifact = Path(made["artifact"])
    MEASURED["backup"] = made
    code, verified, _ = tool(SRC, "verify", str(artifact))
    step("verify: manifest and every file's SHA-256 match", code == 0 and verified["verified"] == artifact.name)
    MEASURED["verify_seconds"] = verified["seconds"]
    manifest = backup.read_manifest(artifact)
    manifest_text = (artifact / backup.MANIFEST).read_text(encoding="ascii")
    step("manifest: snapshot ID/time, journal head of the DB, no title or text",
         manifest["snapshot"]["id"] and manifest["journal_at_backup"]["db_head_seq"] >= 1
         and title not in manifest_text and term not in manifest_text)

    # 3. After the backup: a new upload (lost by the restore), a revocation and its cleanup.
    start_app(SRC)
    assert wait_http(f"{url(SRC, 'api')}/readyz", 200) and wait_http(url(SRC, "web"), 200)
    a, admin = a.at(SRC), admin.at(SRC)  # fresh connections: the old ones went to the stopped web container
    post_backup = a.upload(f"## Methods\n{term} written after the backup.\n".encode())
    revoked = admin.post(f"/api/admin/common-copies/{copy}/revoke",
                         {"confirm_revoke": True, "reason_code": "rights_withdrawn"})
    cleaned = dc(SRC, "run", "--rm", "--no-deps", "worker", "python", "-m", "app.cli.cleanup_revoked").stdout
    failure_at = datetime.now(UTC)
    step("after the backup: A uploaded one more file; the Admin revoked the copy; cleanup removed its object",
         revoked.status_code == 200 and "deleted=1" in cleaned and not present(src_storage, common_key))

    # 4. Writers stopped; the current journal is copied apart from the artifact.
    dc(SRC, "stop", *WRITERS)
    source_journal = SOURCE_JOURNAL / "revocations.jsonl"
    source_before = sha(source_journal.read_bytes())
    code, copied, err = tool(SRC, "copy-journal", "--from-project", SRC, "--to", str(COPY), "--artifact",
                             str(artifact))
    copy_text = (COPY / backup.JOURNAL_FILE).read_text(encoding="ascii")
    step("copy-journal (docker cp from the stopped source API): the copy equals the source and names the revocation"
         " by ID only", code == 0 and copied["source_sha256"] == source_before and copy in copy_text
         and all(s not in copy_text for s in (title, term, checksum, "Drill Admin check")))
    code, _, err = tool(SRC, "copy-journal", "--from-project", SRC, "--to", str(artifact / "journal"), "--artifact",
                        str(artifact))
    step("copy-journal refuses a destination inside the artifact", code == 1 and "journal_copy_not_apart" in err)

    damaged = WORK / "damaged" / artifact.name
    shutil.copytree(artifact, damaged)
    first = manifest["objects"]["entries"][0]["path"]
    data = bytearray((damaged / first).read_bytes())
    data[0] ^= 1
    (damaged / first).write_bytes(bytes(data))
    code, _, err = tool(DST, "verify", str(damaged))
    step("verify refuses a damaged copy (file_checksum_mismatch)", code == 1 and "file_checksum_mismatch" in err)
    refusals = {
        "damaged artifact": (["--artifact", str(damaged), "--project", DST, "--journal-dir", str(COPY)],
                             "file_checksum_mismatch"),
        "no journal copy": (["--artifact", str(artifact), "--project", DST, "--journal-dir", str(WORK / "none")],
                            "journal_missing"),
        "dev project": (["--artifact", str(artifact), "--project", "ras", "--journal-dir", str(COPY)],
                        "target_not_isolated"),
        "source project": (["--artifact", str(artifact), "--project", SRC, "--journal-dir", str(COPY)],
                           "target_not_isolated"),
    }
    outcomes = {name: tool(DST, "restore", *argv) for name, (argv, _) in refusals.items()}
    step("restore refuses a damaged artifact, a missing journal copy, the dev or source project, touching nothing",
         all(outcomes[n][0] == 1 and code_ in outcomes[n][2] for n, (_, code_) in refusals.items())
         and not volumes(DST))
    damaged_target=(WORK / "damaged").resolve()
    if damaged_target.parent != WORK:raise RuntimeError("damaged artifact cleanup escaped task work")
    shutil.rmtree(damaged_target)

    # A project this tool did not create (only its db, no marker) is refused and left exactly as it was.
    dc(FOREIGN, "up", "-d", "--wait", "db")

    def foreign_state() -> list[tuple]:
        ids = dc(FOREIGN, "ps", "-a", "-q").stdout.split()
        out = subprocess.run(["docker", "inspect", "--format", "{{.Name}} {{.State.Status}} {{.State.StartedAt}}", *ids],
                             capture_output=True, text=True, check=True).stdout
        return sorted(tuple(line.split()) for line in out.splitlines())
    restore_foreign = ["--artifact", str(artifact), "--project", FOREIGN, "--journal-dir", str(COPY)]
    before = foreign_state()
    code, _, err = tool(DST, "restore", *restore_foreign)
    step("restore refuses a running project without the marker (target_not_restore_owned) and leaves its"
         " containers exactly as they were (same set, state, start time)",
         code == 1 and "target_not_restore_owned" in err and foreign_state() == before
         and {row[0] for row in before} == {f'/{FOREIGN}-db-1',f'/{FOREIGN}-exposure-1'})
    dc(FOREIGN, "stop", "db")
    before = foreign_state()
    code, _, err = tool(DST, "restore", *restore_foreign)
    step("with its db stopped: refused (target_db_not_running), nothing started or created",
         code == 1 and "target_db_not_running" in err and foreign_state() == before
         and next(row[1] for row in before if row[0]==f'/{FOREIGN}-db-1') == "exited")
    dc(FOREIGN, "down", "-v")

    # 5. Restore into the isolated project; before recover nothing is served or processed.
    started = time.monotonic()
    dc(DST, "build")  # images ready beforehand, as for a prepared restore host; creates no volume
    MEASURED["restore_images_build_seconds"] = round(time.monotonic() - started, 1)
    t_restore = time.monotonic()
    code, restored, err = tool(DST, "restore", "--artifact", str(artifact), "--project", DST,
                               "--journal-dir", str(COPY))
    step("restore into the isolated project: verified, loaded, gate held shut", code == 0 and restored
         and restored["gate"] == "db_behind_journal" and restored["fresh_project"])
    MEASURED["restore"] = restored
    dst_db = engine_for(DST)
    dst_storage = storage_for(DST)
    step("the restored database says listed/indexed and the copy's object is back (only the gate stands between)",
         tuple(dst_db.connect().execute(text("SELECT publication_state, content_status FROM library_items"
                                             " WHERE id=:i"), {"i": uuid.UUID(copy)}).one()) == ("listed", "indexed")
         and present(dst_storage, common_key))
    start_app(DST)
    assert wait_http(f"{url(DST, 'api')}/healthz", 200) and wait_http(url(DST, "web"), 200)
    people = {who: p.at(DST) for who, p in (("a", a), ("b", b), ("admin", admin))}
    requests = [("GET", "/api/library/items?limit=100"), ("GET", f"/api/library/items/{copy}"),
                ("GET", f"/api/files/{common_file}/original"), ("GET", f"/api/library/items/{copy}/chunks"),
                ("GET", f"/api/search/chunks/{chunk}"), ("GET", f"/api/admin/common-copies/{copy}"),
                ("GET", f"/api/files/{a_main['file_id']}/original"),
                ("POST", "/api/search", {"query": term, "mode": "keyword"}),
                ("POST", "/api/rag/ask", {"question": f"{term} result?", "mode": "keyword", "item_id": copy}),
                ("POST", "/api/rag/ask", {"question": f"{term} result?", "mode": "keyword", "scope": "all"})]
    for direct in (False, True):
        answers = [(p.get(path, direct) if method == "GET" else p.post(path, body[0], direct))
                   for p in people.values() for method, path, *body in requests]
        step(f"before recover, {'straight to the API' if direct else 'through the web proxy'}: all "
             f"{len(answers)} requests (A, B, Admin) answer 503 service_recovering",
             all(r.status_code == 503 and r.json() == RECOVERING for r in answers))
    pages = [people["b"].get(path) for path in (f"/items/{copy}", "/search", "/")]
    step("pages are only the client shell: no title or text of the copy",
         all(r.status_code == 200 and title not in r.text and term not in r.text for r in pages))
    step("/readyz 503 and /healthz 200", httpx.get(f"{url(DST, 'api')}/readyz").status_code == 503
         and httpx.get(f"{url(DST, 'api')}/healthz").status_code == 200)
    time.sleep(15)  # several beat polls
    worker_log = dc(DST, "logs", "worker").stdout
    step("worker left the pending job untouched and logged the closed gate (codes only)",
         scalar(dst_db, "SELECT state FROM ingestion_jobs WHERE id=:j", j=left_job) == "received"
         and "recovery gate closed" in worker_log)

    # 6. Recover with services stopped, then start.
    dc(DST, "stop", *WRITERS)
    status = recovery_cli(DST, "status")
    t_recover = time.monotonic()
    recovered = recovery_cli(DST, "recover")
    MEASURED["recover_seconds"] = round(time.monotonic() - t_recover, 2)
    print("   ", status.stdout.strip())
    print("   ", recovered.stdout.strip() or recovered.stderr.strip())
    step("recover replayed the revocation, cleaned up, removed the object and opened the gate",
         status.returncode == 3 and "gate=db_behind_journal" in status.stdout and recovered.returncode == 0
         and "gate=open" in recovered.stdout and "'revoked': 1" in recovered.stdout)
    t_start = time.monotonic()
    start_app(DST)
    step("API ready again", wait_http(f"{url(DST, 'api')}/readyz", 200) and wait_http(url(DST, "web"), 200))
    MEASURED["start_to_ready_seconds"] = round(time.monotonic() - t_start, 2)
    MEASURED["restore_to_ready_seconds_including_drill_checks"] = round(time.monotonic() - t_restore, 1)
    MEASURED["operator_restore_seconds"] = round(
        restored["seconds"]["total"] + MEASURED["recover_seconds"] + MEASURED["start_to_ready_seconds"], 1)
    MEASURED["observed_data_loss_window"] = {
        "backup_snapshot_at": manifest["snapshot"]["taken_at"], "source_lost_at": failure_at.isoformat(),
        "seconds": round((failure_at - datetime.fromisoformat(manifest["snapshot"]["taken_at"])).total_seconds(), 1)}

    def after_recover(label: str) -> None:
        people = {who: p.at(DST) for who, p in (("a", a), ("b", b), ("admin", admin))}
        closed = [p.get(path) for p in people.values() for path in (
            f"/api/library/items/{copy}", f"/api/files/{common_file}/original", f"/api/library/items/{copy}/chunks",
            f"/api/search/chunks/{chunk}")]
        step(f"{label}: the copy is 404 for A, B and the Admin (item, original, chunks, source chunk)",
             all(r.status_code == 404 for r in closed))
        found = {who: hits(p.post("/api/search", {"query": term, "mode": "keyword"})) for who, p in people.items()}
        step(f"{label}: nobody finds the copy; B still finds its private copy of the same bytes",
             all(copy not in found[who] for who in people) and {b_same["item_id"], b_only["item_id"]} <= found["b"])
        step(f"{label}: RAG on the copy is 404",
             people["b"].post("/api/rag/ask", {"question": f"{term} result?", "mode": "keyword",
                                               "item_id": copy}).status_code == 404)
        step(f"{label}: derived rows and object of the copy are gone; tombstone purged",
             scalar(dst_db, "SELECT cardinality(ras_common_copy_derived_left(:i))", i=uuid.UUID(copy)) == 0
             and scalar(dst_db, "SELECT count(*) FROM chunks WHERE item_id=:i", i=uuid.UUID(copy)) == 0
             and scalar(dst_db, "SELECT purged_at IS NOT NULL FROM deletion_tombstones WHERE item_id=:i",
                        i=uuid.UUID(copy))
             and not present(dst_storage, common_key))
        step(f"{label}: B's private copy of the same bytes and A's file are intact (download SHA-256)",
             sha(people["b"].get(f"/api/files/{b_same['file_id']}/original").content) == checksum
             and sha(people["a"].get(f"/api/files/{a_main['file_id']}/original").content) == checksum
             and present(dst_storage, b_key))
        step(f"{label}: the upload made after the backup is absent (the observed data loss)",
             people["a"].get(f"/api/library/items/{post_backup['item_id']}").status_code == 404)

    after_recover("after recover")
    wait_state(dst_db, left_job, "awaiting_index")
    step("with the gate open, the restore's worker processed the left-over upload", True)
    step("the source journal is byte-for-byte unchanged; the copy gained only the drill's recovery records",
         sha(source_journal.read_bytes()) == source_before
         and (COPY / backup.JOURNAL_FILE).read_bytes().startswith(source_journal.read_bytes()))

    # 7. Run restore and recover again.
    dc(DST, "stop", *WRITERS)
    dst_db.dispose()
    code, again, err = tool(DST, "restore", "--artifact", str(artifact), "--project", DST,
                            "--journal-dir", str(COPY))
    step("restore again over the recovered project: same artifact reloaded, gate held shut",
         code == 0 and again["gate"] == "db_behind_journal" and not again["fresh_project"]
         and again["objects"]["put"] == 1)
    MEASURED["restore_again"] = again
    first_run = recovery_cli(DST, "recover")
    second_run = recovery_cli(DST, "recover")
    step("recover again, then once more: both open the gate",
         first_run.returncode == 0 and "gate=open" in first_run.stdout and second_run.returncode == 0
         and "gate=open" in second_run.stdout)
    start_app(DST)
    assert wait_http(f"{url(DST, 'api')}/readyz", 200) and wait_http(url(DST, "web"), 200)
    after_recover("after the second restore")
    logs = dc(SRC, "logs").stdout + dc(DST, "logs").stdout
    step("container logs (both projects): no traceback, no paper text, title, checksum or Admin evidence",
         "Traceback" not in logs and all(s not in logs for s in (title, term, "Drill Admin check", checksum)))

    info = subprocess.run(["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}} {{.ServerVersion}}"],
                          capture_output=True, text=True).stdout.split()
    with src_db.connect() as conn:
        workload = dict(conn.execute(text("""SELECT
            (SELECT count(*) FROM library_items) AS items, (SELECT count(*) FROM source_files) AS files,
            (SELECT count(*) FROM chunks) AS chunks, (SELECT count(*) FROM users) AS users""")).mappings().one())
    MEASURED["workload"] = {**workload, "objects_in_backup": MEASURED["backup"]["objects"],
                            "object_bytes": MEASURED["backup"]["object_bytes"],
                            "dump_bytes": MEASURED["backup"]["database_bytes"]}
    MEASURED["machine"] = {"os": platform.platform(), "docker_cpus": info[0] if info else None,
                           "docker_mem_bytes": info[1] if len(info) > 1 else None,
                           "docker": info[2] if len(info) > 2 else None}
    print(json.dumps({"steps": len(RESULTS), "passed": sum(ok for _, ok in RESULTS),
                      "fake_embedding_requests": len(CALLS), "log_lines": len(logs.splitlines()),
                      "measured": MEASURED}, sort_keys=True, default=str))
    if args.down:
        dc(DST, "down", "-v")
        dc(SRC, "down", "-v")


if __name__ == "__main__":
    main()
