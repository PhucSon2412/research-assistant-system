"""Isolated M6c-2b2b2 drill. Fresh projects; explicit synthetic env, never reads .env.

Host: Conda ras. Artifacts and current journal copies remain outside the repo.
Delivery compensation is injected on a link already forwarded; the same helper
as the API adapter-error route executes in its own transaction after backup.
The API tests separately inject the real delivery route and both accept orders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
sys.path.insert(0, str(ROOT / "scripts"))

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

import ras_backup
from drill_accounts import seed_accounts,runtime_url,current_revision
from app.identity import invitations
from app.identity.passwords import hash_password

SRC, DST = "ras-inv-drill-src", "ras-inv-drill-restore"
PORTS = {
    SRC: dict(POSTGRES_HOST_PORT="45432", API_HOST_PORT="48000", WEB_HOST_PORT="43000",
              MINIO_API_HOST_PORT="49000", MINIO_CONSOLE_HOST_PORT="49001"),
    DST: dict(POSTGRES_HOST_PORT="46432", API_HOST_PORT="46800", WEB_HOST_PORT="46300",
              MINIO_API_HOST_PORT="46900", MINIO_CONSOLE_HOST_PORT="46901"),
}
WRITERS = ("api", "web", "worker", "beat")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--down", action="store_true")
    args = parser.parse_args()
    work = ras_backup.outside_repo(args.work, "work_inside_repo")
    if work.exists() and any(work.iterdir()):
        raise SystemExit("work must be absent or empty")
    runtime_url(URL.create("postgresql+psycopg",username="invdrill"))  # fail before any mutation
    # Ownership inventory before ANY mutation. Never reuse an unknown project.
    for project in (SRC, DST):
        for kind in ("container", "volume"):
            result = subprocess.run(["docker", kind, "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
                                    capture_output=True, text=True, check=True)
            if result.stdout.strip():
                raise SystemExit("drill project is not fresh; untouched")
        for port in PORTS[project].values():
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", int(port)))
    work.mkdir(parents=True, exist_ok=True)
    env_file = work / "empty.env"
    env_file.write_text("", encoding="ascii")
    source_journal, copied = work / "source-journal", work / "journal-copy"
    source_journal.mkdir()
    credentials = dict(POSTGRES_USER="invdrill", POSTGRES_PASSWORD=secrets.token_urlsafe(24), POSTGRES_DB="invdrill",
                       MINIO_ROOT_USER="invdrill", MINIO_ROOT_PASSWORD=secrets.token_urlsafe(24),
                       REDIS_PASSWORD=secrets.token_urlsafe(24))
    base_env = dict(os.environ) | credentials | dict(OPENROUTER_API_KEY="", RAS_DISCOVERY__ENABLED="false",
                                                    RAS_COMPOSE_ENV_FILE=str(env_file))
    # No changes to pricing flags; all remain at defaults from the base Compose file.
    for name in list(base_env):
        if name.endswith("__API_KEY") or name.endswith("__PRICING_VERIFIED"):
            base_env.pop(name)
    checks = []
    measured = {}
    def env(project):
        return base_env | PORTS[project] | dict(RAS_JOURNAL_SOURCE=str(source_journal if project == SRC else copied))
    def dc(project, *argv,runenv=None):
        r = subprocess.run(["docker", "compose", "--env-file", str(env_file), "-p", project, *argv], cwd=ROOT,
                           env=runenv or env(project), capture_output=True, encoding="utf-8", errors="replace")
        if r.returncode:
            raise RuntimeError(f"isolated compose {argv[0]} failed; code={r.returncode}")
        return r
    def step(name, ok):
        checks.append((name, bool(ok)))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}", flush=True)
        if not ok:
            raise SystemExit(1)
    def tool(project, *argv):
        r = subprocess.run([sys.executable, str(ROOT / "scripts/ras_backup.py"), *argv], cwd=ROOT, env=env(project),
                           capture_output=True, encoding="utf-8", errors="replace")
        lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
        if r.returncode or not lines:
            raise RuntimeError(f"backup tool {argv[0]} failed; code={r.returncode}; {r.stderr.strip()[-200:]}")
        return json.loads(lines[-1])
    def cli(project, *argv):
        return dc(project, "run", "--rm", "--no-deps", "-e","RAS_DB__USER","-e","RAS_DB__PASSWORD",
                  "api", "python", "-m", "app.cli.recovery", *argv,
                  runenv=env(project)|{"RAS_DB__USER":credentials["POSTGRES_USER"],"RAS_DB__PASSWORD":credentials["POSTGRES_PASSWORD"]}).stdout
    def engine(project):
        return create_engine(URL.create("postgresql+psycopg", username=credentials["POSTGRES_USER"],
            password=credentials["POSTGRES_PASSWORD"], host="127.0.0.1", port=int(PORTS[project]["POSTGRES_HOST_PORT"]),
            database=credentials["POSTGRES_DB"]), hide_parameters=True)
    def scalar(eng, sql, **values):
        with eng.connect() as conn:
            return conn.scalar(text(sql), values)
    def wait(project):
        deadline = time.monotonic()+50
        while time.monotonic() < deadline:
            try:
                if (httpx.get(f"http://127.0.0.1:{PORTS[project]['WEB_HOST_PORT']}", timeout=2).status_code == 200
                        and httpx.get(f"http://127.0.0.1:{PORTS[project]['API_HOST_PORT']}/healthz", timeout=2).status_code == 200):
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.2)
        raise RuntimeError("web readiness timeout")
    class Person:
        def __init__(self, project, cookies=None):
            self.client = httpx.Client(base_url=f"http://127.0.0.1:{PORTS[project]['WEB_HOST_PORT']}", timeout=25,
                                       cookies=cookies or {})
            self.project = project
        def get(self, route):
            return self.client.get("/api"+route)
        def post(self, route, body=None):
            if not self.client.cookies.get("ras_csrf"):
                assert self.get("/auth/csrf").status_code == 200
            return self.client.post("/api"+route, json=body or {}, headers={"x-csrf-token": self.client.cookies.get("ras_csrf"),
                "origin": f"http://127.0.0.1:{PORTS[self.project]['WEB_HOST_PORT']}"})
    dc(SRC, "build", "api", "web", "worker", "beat", "migrate", "recovery-init")
    for service in ("api","worker","beat","migrate","recovery-init"):
        step(f"source {service} image head {current_revision()}", current_revision()+" (head)" in dc(SRC, "run", "--rm", "--no-deps", service, "alembic", "heads").stdout)
    dc(SRC, "up", "-d", "--wait")
    source = engine(SRC)
    password = secrets.token_urlsafe(24)
    tag = uuid.uuid4().hex[:8]
    emails = {name: f"inv-{tag}-{name}@example.org" for name in ("admin", "existing", "accept", "revoke", "replace", "delivery", "valid")}
    runtime=create_engine(runtime_url(source.url),hide_parameters=True)
    try:
        seeded=seed_accounts(runtime,{name:emails[name] for name in ("admin","existing")},password)
        admin_id=seeded["admin"]
    finally:runtime.dispose()
    admin, public, existing = Person(SRC), Person(SRC), Person(SRC)
    assert admin.post("/auth/login", dict(email=emails["admin"], password=password)).status_code == 200
    assert existing.post("/auth/login", dict(email=emails["existing"], password=password)).status_code == 200
    assert public.get("/auth/csrf").status_code == 200
    saved = dict(public.client.cookies)
    existing_saved = dict(existing.client.cookies)
    old, ids = {}, {}
    for name in ("accept", "revoke", "replace", "delivery", "valid"):
        r = admin.post("/admin/invitations", dict(email=emails[name]))
        assert r.status_code == 201
        ids[name] = r.json()["invitation"]["id"]
        message = next(m for m in admin.get("/admin/dev-outbox").json()["messages"] if m["recipient"] == emails[name])
        old[name] = message["link"].split("#token=", 1)[1]
    assert public.post("/auth/password-reset/request", dict(email=emails["existing"])).status_code == 202
    reset_msg = next(m for m in admin.get("/admin/dev-outbox").json()["messages"] if m["kind"] == "password_reset")
    reset_token = reset_msg["link"].split("#token=",1)[1]
    # Actual MinIO bytes are part of backup and verified after both restores.
    store = ras_backup.storage_at(int(PORTS[SRC]["MINIO_API_HOST_PORT"]), credentials)
    store.ensure_bucket()
    import io
    payload = b"synthetic invitation restore drill object"
    store.put("invitation-drill-object", io.BytesIO(payload), "application/octet-stream")
    dc(SRC, "stop", *WRITERS)
    made = tool(SRC, "backup", "--project", SRC, "--out", str(work/"backups"))
    artifact = Path(made["artifact"])
    step("backup and verify five open invitations with real MinIO", tool(SRC, "verify", str(artifact))["verified"])
    measured["backup"] = made
    dc(SRC, "up", "-d", "--no-deps", *WRITERS)
    wait(SRC)
    public = Person(SRC, saved)
    admin = Person(SRC, dict(admin.client.cookies))
    step("accept after backup only creates User", public.post("/auth/invitations/accept", dict(token=old["accept"], password=password)).status_code == 201)
    step("admin revoke after backup", admin.post(f"/admin/invitations/{ids['revoke']}/revoke").status_code == 204)
    step("reinvite after backup", admin.post("/admin/invitations", dict(email=emails["replace"])).status_code == 201)
    # Adapter has already forwarded this saved link; an error cannot undo that delivery.
    from app.config import get_settings
    os.environ.update(RAS_DB__HOST="127.0.0.1", RAS_DB__PORT=PORTS[SRC]["POSTGRES_HOST_PORT"],
        RAS_DB__USER=credentials["POSTGRES_USER"], RAS_DB__PASSWORD=credentials["POSTGRES_PASSWORD"],
        RAS_DB__NAME=credentials["POSTGRES_DB"], RAS_RECOVERY__JOURNAL_PATH=str(source_journal/"revocations.jsonl"))
    get_settings.cache_clear()
    forwarded = []
    def adapter():
        forwarded.append(old["delivery"])
        raise RuntimeError("synthetic post-delivery failure")
    try:
        adapter()
    except RuntimeError:
        with Session(source) as db:
            invitations.revoke_invitation(db, invitation_id=uuid.UUID(ids["delivery"]), actor_id=admin_id,
                now=datetime.now(UTC), reason="delivery_failed", compensation=True)
    step("delivery can forward then fail; compensation journaled", forwarded == [old["delivery"]])
    step("previous reset/session journal protections retained", public.post("/auth/password-reset/confirm",
        dict(token=reset_token, new_password=secrets.token_urlsafe(24))).status_code == 200)
    step("no embedding/search/RAG provider ledger calls", scalar(source,"SELECT count(*) FROM embedding_calls") == 0
         and scalar(source,"SELECT count(*) FROM search_usage") == 0
         and scalar(source,"SELECT count(*) FROM rag_request_days") == 0)
    dc(SRC, "stop", *WRITERS)
    journal_path = source_journal/"revocations.jsonl"
    source_hash = hashlib.sha256(journal_path.read_bytes()).hexdigest()
    tool(SRC, "copy-journal", "--from-project", SRC, "--to", str(copied), "--artifact", str(artifact))
    raw = journal_path.read_text(encoding="ascii")
    closures = [r["body"] for r in map(json.loads, raw.splitlines()) if r["kind"] == "invitation_closure"]
    step("minimal decisions without identity payload", {r["invitation_id"] for r in closures} == {ids[n] for n in ("accept","revoke","replace","delivery")}
         and all(set(r) == {"invitation_id","end_kind","reason_code","ended_at"} for r in closures)
         and all(v not in raw for v in [password, *old.values(), *emails.values(), reset_token]))
    dc(DST, "build", "api", "web", "worker", "beat", "migrate", "recovery-init")
    for service in ("api","worker","beat","migrate","recovery-init"):
        # compose run materializes declared volumes even with --no-deps, which would
        # correctly make target_is_fresh refuse a not-yet-owned restore target.
        heads = subprocess.check_output(["docker","run","--rm","--entrypoint","alembic",f"{DST}-{service}","heads"], text=True)
        step(f"restore {service} image head 0032", heads.strip() == current_revision()+" (head)")
    for iteration in (1,2):
        restored = tool(DST, "restore", "--artifact", str(artifact), "--project", DST, "--journal-dir", str(copied))
        measured[f"restore_{iteration}"] = restored
        step(f"restore {iteration}: hold gate closed", restored["gate"] != "open")
        dc(DST, "run", "--rm", "migrate")
        target = engine(DST)
        step(f"restore {iteration}: real DB revision 0032", scalar(target,"SELECT version_num FROM alembic_version") == "0032")
        # Start web independently while the API gate is held; no writer resumed yet.
        dc(DST,"up","-d","db","minio","redis")
        dc(DST,"up","-d","--no-deps","api")
        dc(DST,"up","-d","--no-deps","web")
        wait(DST)
        p = Person(DST,saved)
        held_inspect = p.post("/auth/invitations/inspect",dict(token=old["accept"]))
        held_accept = p.post("/auth/invitations/accept",dict(token=old["accept"],password=password))
        step(f"restore {iteration}: web proxy held inspect/accept ({held_inspect.status_code}/{held_accept.status_code})",
             held_inspect.status_code == held_accept.status_code == 503)
        dc(DST,"stop",*WRITERS)
        began=time.monotonic()
        step(f"restore {iteration}: recover opens gate", "gate=open" in cli(DST,"recover"))
        measured[f"recover_{iteration}_seconds"]=round(time.monotonic()-began,2)
        dc(DST,"up","-d","--no-deps","--force-recreate",*WRITERS); wait(DST)
        for service in WRITERS:
            cid=dc(DST,"ps","-q",service).stdout.strip()
            actual_image=subprocess.check_output(["docker","inspect","--format","{{.Image}}",cid],text=True).strip()
            built_image=subprocess.check_output(["docker","image","inspect","--format","{{.Id}}",f"{DST}-{service}"],text=True).strip()
            step(f"restore {iteration}: {service} actually uses new image",actual_image == built_image)
        p=Person(DST,saved)
        for name in ("accept","revoke","replace","delivery"):
            step(f"restore {iteration}: {name} inspect and accept blocked inside TTL",
                 p.post("/auth/invitations/inspect",dict(token=old[name])).json() == {"error":"invalid_token"}
                 and p.post("/auth/invitations/accept",dict(token=old[name],password=password)).json() == {"error":"invalid_token"}
                 and scalar(target,"SELECT expires_at > now() FROM invitations WHERE id=:i",i=ids[name]))
        step(f"restore {iteration}: account/session/verifier not recreated",
             scalar(target,"SELECT count(*) FROM users WHERE email=:e",e=emails["accept"]) == 0
             and scalar(target,"SELECT count(*) FROM invitations WHERE closed_by_recovery_at IS NOT NULL AND accepted_user_id IS NULL") == 4)
        step(f"restore {iteration}: previous session and reset remain blocked", Person(DST,existing_saved).get("/auth/session").status_code == 401
             and p.post("/auth/password-reset/confirm",dict(token=reset_token,new_password=password)).json() == {"error":"invalid_token"})
        a=Person(DST)
        assert a.post("/auth/login",dict(email=emails["admin"],password=password)).status_code == 200
        listing=a.get("/admin/invitations").json()["invitations"]
        step(f"restore {iteration}: Admin reports recovery closure", all(next(i for i in listing if i["id"]==ids[n])["status"] == "closed_by_recovery"
             for n in ("accept","revoke","replace","delivery")))
        step(f"restore {iteration}: reinvite recovery-closed email", a.post("/admin/invitations",dict(email=emails["accept"])).status_code == 201)
        step(f"restore {iteration}: unrelated invitation valid", p.post("/auth/invitations/inspect",dict(token=old["valid"])).status_code == 200)
        target_store=ras_backup.storage_at(int(PORTS[DST]["MINIO_API_HOST_PORT"]),credentials)
        with target_store.open("invitation-drill-object") as stream:
            step(f"restore {iteration}: real object intact", stream.read() == payload)
        if iteration == 2:
            step("unrelated invitation accepts once", p.post("/auth/invitations/accept",dict(token=old["valid"],password=password)).status_code == 201
                 and p.post("/auth/invitations/accept",dict(token=old["valid"],password=password)).status_code == 400)
        dc(DST,"stop",*WRITERS)
        step(f"restore {iteration}: repeated recover idempotent", "gate=open" in cli(DST,"recover"))
        target.dispose()
    step("source journal unchanged", hashlib.sha256(journal_path.read_bytes()).hexdigest() == source_hash)
    logs=dc(SRC,"logs","--no-color").stdout+dc(DST,"logs","--no-color").stdout
    step("no traceback/raw token/verifier/email in container logs", "Traceback" not in logs
         and all(v not in logs for v in [password,*old.values(),*emails.values(),reset_token]))
    summary=dict(checks=len(checks),passed=sum(ok for _,ok in checks),provider_calls=0,measured=measured,
                 delivery_fault="host adapter forwards then raises; API compensation helper",dev_touched=False)
    (work/"result.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
    print(json.dumps(summary),flush=True)
    source.dispose()
    if args.down:
        # These exact projects were empty at entry and all resources were created here.
        for project in (SRC,DST):
            labels=dc(project,"ps","-a","--format","json").stdout
            if SRC not in labels and DST not in labels:
                raise RuntimeError("cleanup ownership cannot be verified")
            dc(project,"down","-v")


if __name__ == "__main__":
    main()
