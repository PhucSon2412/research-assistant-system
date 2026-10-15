"""M6c-2b2a: isolated Compose backup -> logout/revoke -> restore -> recover.

Real PostgreSQL/MinIO and the web proxy, saved cookie AND CSRF, inside both
expiry windows. Reuses the M6c-2a drill's guarded project/port helpers. No
provider calls, no changes to dev or .env. DRILL_WORK_DIR is outside the repo.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent))
from drill_backup_restore import (BACKUPS, BASE, COPY, DST, MEASURED, RESULTS, SOURCE_JOURNAL, SRC, WORK, WRITERS,
                                  Person, dc, engine_for, recovery_cli, scalar, sha, start_app, step, tool,
                                  url, volumes, wait_http)
from app.identity.passwords import hash_password


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--down", action="store_true")
    args = parser.parse_args()
    if WORK.exists() and any(WORK.iterdir()):
        raise SystemExit("DRILL_WORK_DIR must be absent or empty")
    if volumes(SRC) or volumes(DST) or dc(SRC, "ps", "-aq").stdout.strip() or dc(DST, "ps", "-aq").stdout.strip():
        raise SystemExit("drill projects must be fresh; existing containers/volumes are left untouched")
    SOURCE_JOURNAL.mkdir(parents=True)
    dc(SRC, "up", "-d", "--build", "--wait")
    source = engine_for(SRC)
    step("source fresh and ready", scalar(source, "SELECT count(*) FROM users") == 0
         and httpx.get(url(SRC, "api") + "/readyz").status_code == 200)
    password, tag = secrets.token_urlsafe(24), uuid.uuid4().hex[:8]
    emails = {who: f"session-drill-{tag}-{who}@example.org" for who in ("admin", "a", "b")}
    from drill_backup_restore import runtime_engine_for
    from drill_accounts import seed_accounts
    runtime=runtime_engine_for(SRC)
    try:seed_accounts(runtime,emails,password)
    finally:runtime.dispose()
    admin = Person(SRC, email=emails["admin"], password=password)
    a = [Person(SRC, email=emails["a"], password=password) for _ in range(3)]
    b = Person(SRC, email=emails["b"], password=password)
    saved = [dict(p.web.cookies) for p in a]
    b_saved = dict(b.web.cookies)
    ids = [p.get("/api/auth/session").json()["session"]["id"] for p in a]
    original = b"# Session drill synthetic paper\n\nNo provider or index requested.\n"
    paper = b.upload(original)
    step("saved three A sessions, B session and CSRF; B original stored in real MinIO",
         all(c.get("ras_session") and c.get("ras_csrf") for c in saved + [b_saved])
         and sha(b.get(f"/api/files/{paper['file_id']}/original").content) == sha(original))

    dc(SRC, "stop", *WRITERS)
    code, made, _ = tool(SRC, "backup", "--project", SRC, "--out", str(BACKUPS))
    step("backup while all sessions are live, writers stopped", code == 0 and made)
    artifact = Path(made["artifact"])
    code, verified, _ = tool(SRC, "verify", str(artifact))
    step("artifact verified", code == 0 and verified)
    MEASURED["backup"] = made
    start_app(SRC)
    assert wait_http(url(SRC, "web"), 200)
    a = [Person(SRC, cookies=c) for c in saved]
    step("logout after backup", a[1].post("/api/auth/logout", {}).status_code == 204)
    step("revoke-others keeps exactly A0", a[0].post("/api/auth/sessions/revoke-others", {}).json() == {"revoked": 1})
    newer = Person(SRC, email=emails["a"], password=password)
    newer_saved = dict(newer.web.cookies)
    newer_id = newer.get("/api/auth/session").json()["session"]["id"]
    step("session created then logged out after backup", newer.post("/api/auth/logout", {}).status_code == 204)
    path = SOURCE_JOURNAL / "revocations.jsonl"
    raw = path.read_text(encoding="ascii")
    records = [json.loads(line) for line in raw.splitlines()]
    revokes = [r["body"] for r in records if r["kind"] == "session_revocation"]
    step("journal contains only session/user IDs, reason and time, no cookie/hash/CSRF/credential/email/paper",
         {r["session_id"] for r in revokes} == {ids[1], ids[2], newer_id}
         and all(set(r) == {"session_id", "user_id", "reason_code", "revoked_at"} for r in revokes)
         and all(secret not in raw for secret in [password, verifier, *emails.values()]
                 + [v for c in saved + [newer_saved] for v in c.values()]))
    dc(SRC, "stop", *WRITERS)
    source_hash = sha(path.read_bytes())
    code, copied, _ = tool(SRC, "copy-journal", "--from-project", SRC, "--to", str(COPY), "--artifact", str(artifact))
    step("current independent journal copied outside artifact", code == 0 and copied["source_sha256"] == source_hash)
    dc(DST, "build")
    code, restored, _ = tool(DST, "restore", "--artifact", str(artifact), "--project", DST, "--journal-dir", str(COPY))
    step("restore gate held closed", code == 0 and restored["gate"] == "db_behind_journal")
    MEASURED["restore"] = restored
    target = engine_for(DST)
    step("snapshot has all three A sessions live, lacks later session",
         scalar(target, "SELECT count(*) FROM sessions WHERE id=ANY(:ids) AND revoked_at IS NULL", ids=ids) == 3
         and scalar(target, "SELECT count(*) FROM sessions WHERE id=:s", s=newer_id) == 0)
    start_app(DST)
    assert wait_http(url(DST, "web"), 200)
    for direct in (False, True):
        responses = []
        for cookies in saved + [b_saved, newer_saved]:
            p = Person(DST, cookies=cookies)
            responses += [p.get("/api/auth/session", direct), p.post("/api/auth/sessions/revoke-others", {}, direct)]
        step(f"before recover {'API' if direct else 'web'}: all saved cookie/CSRF requests blocked by gate",
             all(r.status_code == 503 and r.json() == {"error": "service_recovering"} for r in responses))
    dc(DST, "stop", *WRITERS)
    began = time.monotonic()
    report = recovery_cli(DST, "recover")
    MEASURED["recover_seconds"] = round(time.monotonic() - began, 2)
    print(report.stdout.strip(), flush=True)
    step("recover replayed two snapshot sessions and recorded later session absent",
         report.returncode == 0 and "gate=open" in report.stdout and "'session_revoked': 2" in report.stdout
         and "'recorded_session_absent': 1" in report.stdout)
    start_app(DST)
    assert wait_http(url(DST, "api") + "/readyz", 200) and wait_http(url(DST, "web"), 200)

    def verify(label):
        now = datetime.now(UTC)
        with target.connect() as conn:
            rows = conn.execute(text("SELECT last_activity_at,absolute_expires_at FROM sessions WHERE id=ANY(:ids)"),
                                {"ids": ids}).all()
        MEASURED["max_snapshot_session_age_seconds"] = round(max((now-r.last_activity_at).total_seconds() for r in rows), 2)
        step(f"{label}: old cookies still within configured idle and absolute windows",
             all(now < r.last_activity_at + BASE.auth.session_idle and now < r.absolute_expires_at for r in rows))
        for direct in (False, True):
            for index in (1, 2):
                r = Person(DST, cookies=saved[index]).get("/api/auth/session", direct)
                post = Person(DST, cookies=saved[index]).post("/api/auth/sessions/revoke-others", {}, direct)
                step(f"{label} {'API' if direct else 'web'}: A{index} old cookie AND CSRF rejected due to revocation",
                     r.status_code == post.status_code == 401
                     and r.json() == post.json() == {"error": "session_expired"})
            step(f"{label} {'API' if direct else 'web'}: kept A0 and unrelated B stay live",
                 all(Person(DST, cookies=c).get("/api/auth/session", direct).status_code == 200 for c in (saved[0], b_saved)))
        step(f"{label}: later session not recreated and B original unchanged",
             Person(DST, cookies=newer_saved).get("/api/auth/session").status_code == 401
             and sha(Person(DST, cookies=b_saved).get(f"/api/files/{paper['file_id']}/original").content) == sha(original)
             and scalar(target, "SELECT count(*) FROM sessions WHERE id=:s", s=newer_id) == 0)
    verify("first recovery")
    dc(DST, "stop", *WRITERS)
    step("repeated recover idempotent", recovery_cli(DST, "recover").returncode == 0)
    target.dispose()  # pg_restore terminates connections; never reuse the old pool afterwards.
    code, restored_again, _ = tool(DST, "restore", "--artifact", str(artifact), "--project", DST,
                                  "--journal-dir", str(COPY))
    step("second restore held closed", code == 0 and restored_again["gate"] == "db_behind_journal")
    step("second recovery succeeds", recovery_cli(DST, "recover").returncode == 0)
    start_app(DST)
    assert wait_http(url(DST, "web"), 200)
    verify("second recovery")
    step("source journal unchanged by restore/recover", sha(path.read_bytes()) == source_hash)
    logs = dc(SRC, "logs", "--no-color").stdout + dc(DST, "logs", "--no-color").stdout
    step("container logs have no traceback or saved tokens", "Traceback" not in logs
         and all(v not in logs for c in saved + [newer_saved] for v in c.values()))
    print(json.dumps({"checks": len(RESULTS), "passed": sum(ok for _, ok in RESULTS), "provider_calls": 0,
                      "measured": MEASURED}, ensure_ascii=True), flush=True)
    source.dispose()
    target.dispose()
    if args.down:
        for project in (SRC, DST):
            dc(project, "down", "-v")


if __name__ == "__main__":
    main()
