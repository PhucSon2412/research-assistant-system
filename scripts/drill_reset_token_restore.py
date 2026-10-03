"""Isolated Compose reset-token backup/restore/recover within configured TTL.

Artifacts/journal copies stay in DRILL_WORK_DIR outside repo. Reuse guarded
M6c-2a backup tools. No provider key is read, no provider is called.
"""
from __future__ import annotations

import argparse
import json
import secrets
import socket
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent))
from drill_backup_restore import (BACKUPS, BASE, COPY, DST, MEASURED, PORTS, RESULTS, SOURCE_JOURNAL, SRC, WORK,
                                  WRITERS, Person, dc, engine_for, recovery_cli, scalar, sha, start_app, step,
                                  tool, url, volumes, wait_http)
from app.identity.passwords import hash_password


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--down", action="store_true")
    args = parser.parse_args()
    if WORK.exists() and any(WORK.iterdir()):
        raise SystemExit("DRILL_WORK_DIR must be absent or empty")
    if any(volumes(p) or dc(p, "ps", "-aq").stdout.strip() for p in (SRC, DST)):
        raise SystemExit("drill projects must be fresh; existing containers/volumes untouched")
    for p in (SRC, DST):
        for port in PORTS[p].values():
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", int(port)))  # Before any build/up/stop.
    SOURCE_JOURNAL.mkdir(parents=True)
    dc(SRC, "up", "-d", "--build", "--wait")
    source = engine_for(SRC)
    step("source fresh isolated and ready", scalar(source, "SELECT count(*) FROM users") == 0
         and httpx.get(url(SRC, "api") + "/readyz").status_code == 200)
    password, new_password, tag = secrets.token_urlsafe(24), secrets.token_urlsafe(24), uuid.uuid4().hex[:8]
    emails = {who: f"reset-drill-{tag}-{who}@example.org" for who in ("admin", "a", "b", "c", "d")}
    verifier = hash_password(password)
    ids = {}
    with source.begin() as conn:
        for who in emails:
            ids[who] = conn.scalar(text("INSERT INTO users(email,password_verifier,role) VALUES(:e,:p,:r) RETURNING id"),
                                   {"e": emails[who], "p": verifier, "r": "admin" if who == "admin" else "user"})
    admin = Person(SRC, email=emails["admin"], password=password)
    a = [Person(SRC, email=emails["a"], password=password) for _ in range(3)]
    b = Person(SRC, email=emails["b"], password=password)
    public = Person(SRC)
    assert public.get("/api/auth/csrf").status_code == 200
    public.api.cookies.update(public.web.cookies)
    public_saved = dict(public.web.cookies)
    saved, b_saved = [dict(p.web.cookies) for p in a], dict(b.web.cookies)
    original = b"# Reset drill\n\nSynthetic original; no provider requested.\n"
    paper = b.upload(original)

    def issue(who):
        assert public.post("/api/auth/password-reset/request", {"email": emails[who]}).status_code == 202
        messages = admin.get("/api/admin/dev-outbox").json()["messages"]
        msg = next(m for m in messages if m["recipient"] == emails[who] and m["kind"] == "password_reset")
        return msg["link"].split("#token=", 1)[1]
    old = {who: issue(who) for who in ("a", "b", "c", "d")}
    token_ids = {who: str(scalar(source, "SELECT id FROM password_reset_tokens WHERE user_id=:u", u=ids[who]))
                 for who in old}
    step("four live tokens, C/D have no sessions, three A sessions, real MinIO original",
         scalar(source, "SELECT count(*) FROM sessions WHERE user_id=ANY(:u)", u=[ids["c"], ids["d"]]) == 0
         and sha(b.get(f"/api/files/{paper['file_id']}/original").content) == sha(original))
    dc(SRC, "stop", *WRITERS)
    code, made, _ = tool(SRC, "backup", "--project", SRC, "--out", str(BACKUPS))
    step("backup with all tokens open and writers stopped", code == 0 and made)
    artifact = Path(made["artifact"])
    step("verify artifact", tool(SRC, "verify", str(artifact))[0] == 0)
    MEASURED["backup"] = made
    start_app(SRC)
    assert wait_http(url(SRC, "web"), 200)
    admin, public = Person(SRC, cookies=admin.web.cookies), Person(SRC, cookies=public_saved)
    step("consume A token and revoke three sessions after backup",
         public.post("/api/auth/password-reset/confirm", {"token": old["a"], "new_password": new_password}).status_code == 200)
    public = Person(SRC, cookies=public_saved)
    step("replace C token without a session",
         public.post("/api/auth/password-reset/request", {"email": emails["c"]}).status_code == 202)
    step("disable D and revoke its token without a session",
         admin.post(f"/api/admin/users/{ids['d']}/disable", {}).status_code == 200)
    path = SOURCE_JOURNAL / "revocations.jsonl"
    raw = path.read_text(encoding="ascii")
    closures = [r["body"] for r in map(json.loads, raw.splitlines()) if r["kind"] == "reset_token_closure"]
    step("same journal has exact minimal token events and no secrets",
         {r["token_id"] for r in closures} == {token_ids[w] for w in ("a", "c", "d")}
         and {r["reason_code"] for r in closures} == {"password_reset", "reset_replaced", "user_disabled"}
         and all(set(r) == {"token_id", "user_id", "end_kind", "reason_code", "ended_at"} for r in closures)
         and all(s not in raw for s in [password, new_password, verifier, *emails.values(), *old.values()]))
    dc(SRC, "stop", *WRITERS)
    source_hash = sha(path.read_bytes())
    step("current journal copied separately from artifact",
         tool(SRC, "copy-journal", "--from-project", SRC, "--to", str(COPY), "--artifact", str(artifact))[0] == 0)
    dc(DST, "build")

    def restore_and_recover(label):
        code, restored, _ = tool(DST, "restore", "--artifact", str(artifact), "--project", DST, "--journal-dir", str(COPY))
        step(f"{label}: restore/hold gate closed", code == 0 and restored["gate"] == "db_behind_journal")
        MEASURED[f"{label}_restore"] = restored
        dc(DST, "run", "--rm", "migrate")  # Never migrate the dev project.
        start_app(DST)
        assert wait_http(url(DST, "web"), 200)
        for direct in (False, True):
            response = Person(DST, cookies=public_saved).post("/api/auth/password-reset/confirm",
                        {"token": old["a"], "new_password": new_password}, direct)
            step(f"{label} {'API' if direct else 'web'}: held reset blocked", response.status_code == 503)
        dc(DST, "stop", *WRITERS)
        began = time.monotonic()
        report = recovery_cli(DST, "recover")
        MEASURED[f"{label}_recover_seconds"] = round(time.monotonic() - began, 2)
        step(f"{label}: recovery succeeds", report.returncode == 0 and "gate=open" in report.stdout)
        start_app(DST)
        assert wait_http(url(DST, "web"), 200) and wait_http(url(DST, "api") + "/readyz", 200)
        target = engine_for(DST)
        now = datetime.now(UTC)
        with target.connect() as conn:
            rows = conn.execute(text("SELECT created_at,expires_at FROM password_reset_tokens WHERE id=ANY(:t)"),
                                {"t": list(token_ids.values())}).all()
        MEASURED[f"{label}_max_token_age_seconds"] = round(max((now-r.created_at).total_seconds() for r in rows), 2)
        step(f"{label}: old links inside unchanged configured TTL", all(now < r.expires_at for r in rows)
             and all(r.expires_at-r.created_at == BASE.auth.password_reset for r in rows))
        for direct in (False, True):
            for who in ("a", "c", "d"):
                r = Person(DST, cookies=public_saved).post("/api/auth/password-reset/confirm",
                    {"token": old[who], "new_password": new_password}, direct)
                step(f"{label} {'API' if direct else 'web'}: {who} old link rejected inside TTL",
                     r.status_code == 400 and r.json() == {"error": "invalid_token"})
            r = Person(DST, cookies=public_saved).post("/api/auth/password-reset/confirm",
                     {"token": old["b"], "new_password": "short"}, direct)
            step(f"{label} {'API' if direct else 'web'}: B token still valid", r.json().get("error") == "weak_password")
            for cookies in saved:
                r = Person(DST, cookies=cookies).get("/api/auth/session", direct)
                step(f"{label} {'API' if direct else 'web'}: old session rejected", r.status_code == 401)
        step(f"{label}: B session and MinIO original intact; replay did not disable D/change A verifier",
             Person(DST, cookies=b_saved).get("/api/auth/session").status_code == 200
             and sha(Person(DST, cookies=b_saved).get(f"/api/files/{paper['file_id']}/original").content) == sha(original)
             and scalar(target, "SELECT status FROM users WHERE id=:u", u=ids["d"]) == "active"
             and scalar(target, "SELECT password_verifier FROM users WHERE id=:u", u=ids["a"]) == verifier)
        target.dispose()
        dc(DST, "stop", *WRITERS)
        step(f"{label}: repeated recovery idempotent", recovery_cli(DST, "recover").returncode == 0)
    restore_and_recover("first")
    restore_and_recover("second")
    start_app(DST)
    assert wait_http(url(DST, "web"), 200)
    step("unrelated B token completes a real reset after second restore",
         Person(DST, cookies=public_saved).post("/api/auth/password-reset/confirm",
                  {"token": old["b"], "new_password": new_password}, True).status_code == 200)
    step("source journal unchanged byte for byte", sha(path.read_bytes()) == source_hash)
    logs = dc(SRC, "logs", "--no-color").stdout + dc(DST, "logs", "--no-color").stdout
    step("logs contain no traceback or reset/session secrets", "Traceback" not in logs
         and all(s not in logs for s in [*old.values(), password, new_password]
                 + [v for c in saved for v in c.values()]))
    print(json.dumps(dict(checks=len(RESULTS), passed=sum(ok for _, ok in RESULTS), provider_calls=0,
                          measured=MEASURED), ensure_ascii=True), flush=True)
    source.dispose()
    if args.down:
        for p in (SRC, DST):
            dc(p, "down", "-v")


if __name__ == "__main__":
    main()
