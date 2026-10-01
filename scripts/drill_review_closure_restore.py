"""M6c-2b1 drill: a restore never gives an Admin back a review grant that was closed after the backup.

Runs on the same two *isolated* Compose projects as ``scripts/drill_backup_restore.py`` (and
reuses its helpers), never the dev stack:

1. Source ``ras-drill-src`` (fresh volumes, journal in ``<work>/source-journal``): User A uploads
   three synthetic Markdown files and User B one, through the web; A sends a review request for
   each of A's files, B for B's. The Admin opens every request and downloads each original
   (the URLs are kept, as a browser history would).
2. Every writer stopped: ``ras_backup.py backup`` + ``verify``. All four requests are open in it.
3. Writers back: A withdraws R1, the Admin rejects R2 (with a note) and approves R3 into a common
   copy, then publishes it. The saved URLs of R1–R3 answer 404; R4 (B) stays open.
4. Writers stopped; ``copy-journal`` to ``<work>/journal-copy``; ``ras_backup.py restore`` into
   ``ras-drill-restore`` with that copy. The restored database has R1–R3 open again; before
   ``recover`` every request (saved review URLs included, through the web and straight to the API)
   answers 503.
5. ``recovery recover``, start. The Admin: R1–R3 not in the open list, detail without any review
   metadata, saved original URLs 404; R4 still reviewable. A: R1–R3 ``closed_by_recovery`` with the
   original codes, A's files intact; the common copy does not exist (it was created after the
   backup). A new request + approval works through the normal flow.
6. ``restore`` + ``recover`` again (and ``recover`` once more): the same end state.

Prints step results, counts and timings only, then a JSON summary. Needs ``DRILL_WORK_DIR``
(absolute, outside the repository, absent or empty). ``--down`` removes both projects' volumes
at the end. No provider is called: nothing is indexed.
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
import drill_backup_restore  # noqa: E402,F401  (sets the RAS_* environment for the source project)
from drill_backup_restore import (BACKUPS, COPY, DST, MEASURED, RECOVERING, RESULTS,  # noqa: E402
                                  SOURCE_JOURNAL, SRC, WORK, WRITERS, Person, dc, engine_for, recovery_cli, scalar,
                                  sha, start_app, step, tool, url, volumes, wait_http, wait_state)

from app.identity.passwords import hash_password  # noqa: E402

PREFIX = "m6c2b1-drill-"
NOTE = "Drill rejection note RNOTE-m6c2b1"
DECLARED_SOURCE, DECLARED_RIGHTS = "Drill declared source text", "Drill declared rights text"
SCOPE = {"audience": "instance_users", "original_download": True, "fulltext_search": False,
         "external_processing": False, "conditions": None}
CODES = {"R1": "withdrawn_by_requester", "R2": "rights_not_established", "R3": "approved_by_admin"}


def submit(person: Person, ids: dict) -> str:
    notice = person.get("/api/review-requests/notice").json()["version"]
    response = person.post("/api/review-requests", {
        "acknowledged": True, "notice_version": notice, "item_id": ids["item_id"], "version_id": ids["version_id"],
        "source_file_id": ids["file_id"], "declared_source": DECLARED_SOURCE, "declared_rights": DECLARED_RIGHTS})
    assert response.status_code == 201, response.status_code
    return response.json()["request"]["id"]


def approve_body(checksum: str, title: str) -> dict:
    return {"confirm_verified": True, "file_checksum_sha256": checksum, "title": title,
            "verified_source": "Drill Admin check: synthetic file by the drill script",
            "evidence_note": "Drill Admin check: synthetic permission for this checksum",
            "checked_on": datetime.now(UTC).date().isoformat(), "scope": SCOPE}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--down", action="store_true", help="remove both drill projects and their volumes at the end")
    args = parser.parse_args()
    if WORK.exists() and any(WORK.iterdir()):
        raise SystemExit("DRILL_WORK_DIR must be absent or empty")
    if volumes(SRC) or volumes(DST):
        raise SystemExit(f"{SRC} / {DST} already have volumes; remove them first (docker compose -p <name> down -v)")
    SOURCE_JOURNAL.mkdir(parents=True)

    started = time.monotonic()
    dc(SRC, "up", "-d", "--build", "--wait")
    MEASURED["source_up_seconds"] = round(time.monotonic() - started, 1)
    src_db = engine_for(SRC)
    step("source project is fresh (no account) and ready",
         scalar(src_db, "SELECT count(*) FROM users") == 0 and httpx.get(f"{url(SRC, 'api')}/readyz").status_code == 200)

    # 1. Accounts, uploads, four open requests; the Admin opens each one.
    run = uuid.uuid4().hex[:8]
    password = secrets.token_urlsafe(24)
    with src_db.begin() as conn:
        for who, role in (("admin", "admin"), ("a", "user"), ("b", "user")):
            conn.execute(text("INSERT INTO users(email, password_verifier, role) VALUES (:e, :p, :r)"),
                         {"e": f"{PREFIX}{run}-{who}@example.org", "p": hash_password(password), "r": role})
    emails = {who: f"{PREFIX}{run}-{who}@example.org" for who in ("admin", "a", "b")}
    admin, a, b = (Person(SRC, email=emails[who], password=password) for who in ("admin", "a", "b"))
    term = f"Reviewdrill{run}"
    bodies = {name: f"# {name} {run}\n\n## Methods\n{term} {name} private body.\n".encode()
              for name in ("R1", "R2", "R3", "R4")}
    files = {name: (a if name != "R4" else b).upload(body) for name, body in bodies.items()}
    for ids in files.values():
        wait_state(src_db, uuid.UUID(ids["job_id"]), "awaiting_index")
    rid = {name: submit(a if name != "R4" else b, ids) for name, ids in files.items()}
    saved = {name: {"detail": f"/api/admin/review-requests/{r}",
                    "original": f"/api/admin/review-requests/{r}/files/{files[name]['file_id']}/original"}
             for name, r in rid.items()}
    opened = {name: (admin.get(u["detail"]).json()["request"]["review_available"],
                     sha(admin.get(u["original"]).content) == sha(bodies[name])) for name, u in saved.items()}
    step("A (3 files) and B (1 file) sent review requests; the Admin opened each and downloaded its original",
         all(ok == (True, True) for ok in opened.values()))

    # 2. Backup with every writer stopped.
    dc(SRC, "stop", *WRITERS)
    code, made, err = tool(SRC, "backup", "--project", SRC, "--out", str(BACKUPS))
    step("backup with writers stopped (all four requests open in it)", code == 0 and made)
    artifact = Path(made["artifact"])
    code, verified, _ = tool(SRC, "verify", str(artifact))
    step("verify: manifest and every file's SHA-256 match", code == 0 and verified["verified"] == artifact.name)
    MEASURED["backup"] = made

    # 3. After the backup: withdraw, reject, approve (+ publish).
    start_app(SRC)
    assert wait_http(f"{url(SRC, 'api')}/readyz", 200) and wait_http(url(SRC, "web"), 200)
    admin, a, b = admin.at(SRC), a.at(SRC), b.at(SRC)
    withdrawn = a.post(f"/api/review-requests/{rid['R1']}/withdraw", {})
    rejected = admin.post(f"/api/admin/review-requests/{rid['R2']}/reject", {"reason_code": CODES["R2"], "note": NOTE})
    title = f"Drill review copy {run}"
    approved = admin.post(f"/api/admin/review-requests/{rid['R3']}/approve", approve_body(sha(bodies["R3"]), title))
    copy = approved.json()["request"]["pending_publication_id"]
    published = admin.post(f"/api/admin/common-copies/{copy}/publish",
                           {"confirm_publish": True, "file_checksum_sha256": sha(bodies["R3"])})
    step("after the backup: A withdrew R1, the Admin rejected R2 and approved R3 into a common copy, published",
         [r.status_code for r in (withdrawn, rejected, approved, published)] == [200] * 4)
    live = {name: admin.get(saved[name]["original"]).status_code for name in rid}
    step("live: the saved original URLs of R1-R3 answer 404, R4 (B, still open) 200",
         live == {"R1": 404, "R2": 404, "R3": 404, "R4": 200})
    source_journal = SOURCE_JOURNAL / "revocations.jsonl"
    lines = [json.loads(line) for line in source_journal.read_text(encoding="ascii").splitlines()]
    closes = {r["body"]["request_id"]: r["body"] for r in lines if r["kind"] == "review_closure"}
    journal_text = source_journal.read_text(encoding="ascii")
    step("source journal: one review_closure per close (decision + code), IDs only: no title, note, declared text,"
         " email, checksum or Admin evidence",
         {k: (v["status"], v["reason_code"]) for k, v in closes.items()} == {
             rid["R1"]: ("withdrawn", CODES["R1"]), rid["R2"]: ("rejected", CODES["R2"]),
             rid["R3"]: ("approved", CODES["R3"])}
         and all(s not in journal_text for s in (title, NOTE, DECLARED_SOURCE, DECLARED_RIGHTS, emails["a"],
                                                 emails["admin"], sha(bodies["R3"]), "Drill Admin check", term)))

    # 4. Copy the journal, restore into the isolated project; nothing served before recover.
    dc(SRC, "stop", *WRITERS)
    source_before = sha(source_journal.read_bytes())
    code, copied, _ = tool(SRC, "copy-journal", "--from-project", SRC, "--to", str(COPY), "--artifact", str(artifact))
    step("copy-journal: the copy equals the source journal", code == 0 and copied["source_sha256"] == source_before)
    dc(DST, "build")
    t_restore = time.monotonic()
    code, restored, err = tool(DST, "restore", "--artifact", str(artifact), "--project", DST,
                               "--journal-dir", str(COPY))
    step("restore into the isolated project: loaded, gate held shut",
         code == 0 and restored and restored["gate"] == "db_behind_journal")
    MEASURED["restore"] = restored
    dst_db = engine_for(DST)
    with dst_db.connect() as conn:
        states = dict(conn.execute(text("SELECT id::text, status FROM review_requests")).all())
        commons = conn.scalar(text("SELECT count(*) FROM library_items WHERE visibility='common'"))
    step("the restored database has R1-R3 open again and no common copy (only the gate stands between)",
         all(states[rid[n]] == "open" for n in rid) and commons == 0)
    start_app(DST)
    assert wait_http(f"{url(DST, 'api')}/healthz", 200) and wait_http(url(DST, "web"), 200)
    people = {"admin": admin.at(DST), "a": a.at(DST), "b": b.at(DST)}
    for direct in (False, True):
        answers = [people["admin"].get(u, direct) for name in rid for u in saved[name].values()]
        answers += [people["admin"].get("/api/admin/review-requests", direct)]
        answers += [people[w].get("/api/review-requests", direct) for w in ("a", "b")]
        step(f"before recover, {'straight to the API' if direct else 'through the web'}: every saved review URL, the"
             f" Admin list and the owners' lists answer 503 ({len(answers)} requests)",
             all(r.status_code == 503 and r.json() == RECOVERING for r in answers))

    # 5. Recover with services stopped, then start.
    dc(DST, "stop", *WRITERS)
    status = recovery_cli(DST, "status")
    t_recover = time.monotonic()
    recovered = recovery_cli(DST, "recover")
    MEASURED["recover_seconds"] = round(time.monotonic() - t_recover, 2)
    print("   ", status.stdout.strip())
    print("   ", recovered.stdout.strip() or recovered.stderr.strip())
    step("recover replayed the three closes and opened the gate",
         status.returncode == 3 and "gate=db_behind_journal" in status.stdout and recovered.returncode == 0
         and "gate=open" in recovered.stdout and "'closed': 3" in recovered.stdout)
    start_app(DST)
    step("API ready again", wait_http(f"{url(DST, 'api')}/readyz", 200) and wait_http(url(DST, "web"), 200))
    MEASURED["restore_to_ready_seconds_including_drill_checks"] = round(time.monotonic() - t_restore, 1)

    def after_recover(label: str) -> None:
        people = {"admin": admin.at(DST), "a": a.at(DST), "b": b.at(DST)}
        ad = people["admin"]
        open_ids = {r["id"] for r in ad.get("/api/admin/review-requests?status=open").json()["requests"]}
        step(f"{label}: the Admin's open list holds only B's R4", open_ids == {rid["R4"]})
        for direct in (False, True):
            originals = {n: ad.get(saved[n]["original"], direct).status_code for n in ("R1", "R2", "R3")}
            step(f"{label}, {'API' if direct else 'web'}: the saved original URLs of R1-R3 answer 404",
                 originals == {"R1": 404, "R2": 404, "R3": 404})
        details = [ad.get(saved[n]["detail"]).json()["request"] for n in ("R1", "R2", "R3")]
        step(f"{label}: R1-R3 detail has no review metadata (no item, file, email, declaration, URL)",
             all(d["review_available"] is False and not {"item", "file", "requester_email", "declaration",
                                                        "original_url"} & set(d) for d in details))
        step(f"{label}: R4 (B) is still reviewable: the Admin downloads exactly B's file",
             sha(ad.get(saved["R4"]["original"]).content) == sha(bodies["R4"]))
        mine = {n: people["a"].get(f"/api/review-requests/{rid[n]}").json()["request"] for n in ("R1", "R2", "R3")}
        step(f"{label}: A sees R1-R3 closed_by_recovery with the original decision codes, no approval",
             all((mine[n]["status"], mine[n]["close_reason_code"]) == ("closed_by_recovery", CODES[n])
                 and mine[n]["publication"] is False for n in mine))
        step(f"{label}: A's three files are intact (download SHA-256) and B sees none of A's requests",
             all(sha(people["a"].get(f"/api/files/{files[n]['file_id']}/original").content) == sha(bodies[n])
                 for n in ("R1", "R2", "R3"))
             and all(people["b"].get(f"/api/review-requests/{rid[n]}").status_code == 404 for n in ("R1", "R2", "R3")))
        step(f"{label}: the common copy approved after the backup does not exist for anyone",
             ad.get(f"/api/admin/common-copies/{copy}").status_code == 404
             and people["b"].get(f"/api/library/items/{copy}").status_code == 404
             and scalar(dst_db, "SELECT count(*) FROM library_items WHERE visibility='common'") == 0)

    after_recover("after recover")
    again = submit(people["a"].at(DST), files["R3"])
    approved_again = admin.at(DST).post(f"/api/admin/review-requests/{again}/approve",
                                        approve_body(sha(bodies["R3"]), title))
    step("the normal flow still works: A sends R3's file again, the Admin approves it into a new pending copy",
         approved_again.status_code == 200 and approved_again.json()["request"]["pending_publication_id"] != copy)
    step("the source journal is byte-for-byte unchanged; the copy only grew",
         sha(source_journal.read_bytes()) == source_before
         and (COPY / "revocations.jsonl").read_bytes().startswith(source_journal.read_bytes()))

    # 6. Restore and recover again.
    dc(DST, "stop", *WRITERS)
    dst_db.dispose()
    code, again_restored, _ = tool(DST, "restore", "--artifact", str(artifact), "--project", DST,
                                   "--journal-dir", str(COPY))
    first_run, second_run = recovery_cli(DST, "recover"), recovery_cli(DST, "recover")
    step("restore again, recover twice: gate held, then open both times",
         code == 0 and again_restored["gate"] == "db_behind_journal" and first_run.returncode == 0
         and "gate=open" in first_run.stdout and second_run.returncode == 0 and "gate=open" in second_run.stdout)
    start_app(DST)
    assert wait_http(f"{url(DST, 'api')}/readyz", 200) and wait_http(url(DST, "web"), 200)
    after_recover("after the second restore")
    logs = dc(SRC, "logs").stdout + dc(DST, "logs").stdout
    step("container logs (both projects): no traceback, no title, note, declared text, email or Admin evidence",
         "Traceback" not in logs and all(s not in logs for s in (title, NOTE, DECLARED_SOURCE, DECLARED_RIGHTS,
                                                                  "Drill Admin check", term)))
    print(json.dumps({"steps": len(RESULTS), "passed": sum(ok for _, ok in RESULTS), "measured": MEASURED},
                     sort_keys=True, default=str))
    if args.down:
        dc(DST, "down", "-v")
        dc(SRC, "down", "-v")


if __name__ == "__main__":
    main()
