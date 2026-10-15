"""M6c-2a backup artifacts, journal copies and the gate after a restore (PostgreSQL + MinIO, journal in tmp).

Artifacts here hold the test bucket's real objects and a synthetic stand-in for the
pg_dump file (pg_dump/pg_restore are not on the test host; the Compose drill
``scripts/drill_backup_restore.py`` runs the real ones). The database side of a restore
is ``CREATE DATABASE ... TEMPLATE`` of the live test database, as in ``test_recovery``.
Embedding, rerank and generation are fakes; nothing leaves the host.
"""
from __future__ import annotations
from tests.test_recovery import replayed_decisions

import hashlib
import io
import json
import shutil
import uuid
from types import SimpleNamespace
from contextlib import contextmanager

import pytest

from app.ingest import cleanup as runtime_cleanup  # normal (runtime) cleanup, not recovery
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import backup as bk
from app import db as app_db
from app import journal, publication, recovery
from app.backup import BackupError
from app.cli import recovery as recovery_cli
from app.clock import utcnow
from app.config import get_settings
from app.ingest.worker import process_job
from app.main import app
from app.recovery import RecoveryError
from tests.conftest import drop_database
from tests.test_publication import MISSING, common_file, common_key, key_present, revoke
from tests.test_recovery import (RECOVERING, assert_recovering, backup, celery_tasks, do_recover, gate, journal_file,
                                 listed, objects, put_back, sample_journal)
from tests.test_review_requests import A_BODY
from tests.test_uploads import upload_api  # noqa: F401  (fixture)

DUMP = b"PGDMP synthetic stand-in for a custom-format dump\n" * 64


def db_state(engine) -> tuple[list[tuple], dict | None]:
    with engine.connect() as conn:
        rows = [(k, int(s), c) for k, s, c in conn.exec_driver_sql(bk.FILES_SQL).all()]
        return rows, bk.journal_at_backup(conn.exec_driver_sql(bk.JOURNAL_SQL).fetchone())


def make_artifact(root, storage, engine):
    """What ``scripts/ras_backup.py backup`` writes, with ``DUMP`` in place of pg_dump output."""
    writer = bk.ArtifactWriter(root, bk.new_backup_id(utcnow()))
    database = writer.write_stream(bk.DUMP, [DUMP])
    entries = bk.copy_objects(storage, writer)
    rows, at = db_state(engine)
    checked = bk.check_db_objects(rows, entries)
    now = utcnow().isoformat()
    return writer.finish({
        "started_at": now, "finished_at": now, "source": {"compose_project": "test", "server_version": "16",
                                                          "system_identifier": "0", "alembic_revision": "0027"},
        "snapshot": {"id": "00000003-00000002-1", "taken_at": now, "wal_lsn": "0/0", "txid_snapshot": "1:1:"},
        "writers_stopped": ["api", "web", "worker", "beat"], "journal_at_backup": at, "database": database,
        "objects": {"bucket": storage.bucket, "count": len(entries), "bytes": sum(e["bytes"] for e in entries),
                    "entries": entries},
        "db_object_check": {"file_rows_checked": checked}})


def refused(code, fn, *args, **kwargs):
    with pytest.raises(BackupError) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == code, caught.value
    return caught.value


def damaged(artifact, tmp_path, name):
    copy = tmp_path / "cases" / name / artifact.name
    shutil.copytree(artifact, copy)
    return copy


def rewrite_manifest(artifact, change, reseal: bool) -> None:
    manifest = json.loads((artifact / bk.MANIFEST).read_text(encoding="ascii"))
    change(manifest)
    (artifact / bk.MANIFEST).write_text(bk.canonical(bk.seal(manifest) if reseal else manifest), encoding="ascii")


# --- artifacts ----------------------------------------------------------------

def test_complete_artifact_verifies_and_every_damage_is_refused(upload_api, monkeypatch, tmp_path):
    w = listed(upload_api, monkeypatch)
    artifact = make_artifact(tmp_path / "backups", w.storage, w.api.engine)
    manifest = bk.verify_artifact(artifact)
    assert manifest["objects"]["count"] == len(objects(w.storage)) == 3  # A, B, the common copy
    assert manifest["db_object_check"] == {"file_rows_checked": 3}
    assert [p.name for p in (tmp_path / "backups").iterdir()] == [artifact.name]  # no partial directory left
    first = manifest["objects"]["entries"][0]

    case = damaged(artifact, tmp_path, "missing")
    (case / first["path"]).unlink()
    assert refused("file_missing", bk.verify_artifact, case).detail == first["path"]
    case = damaged(artifact, tmp_path, "flipped")
    data = bytearray((case / bk.DUMP).read_bytes())
    data[100] ^= 1
    (case / bk.DUMP).write_bytes(bytes(data))  # same size, one bit changed
    refused("file_checksum_mismatch", bk.verify_artifact, case)
    case = damaged(artifact, tmp_path, "truncated")
    (case / first["path"]).write_bytes((case / first["path"]).read_bytes()[:-1])
    refused("file_size_mismatch", bk.verify_artifact, case)
    case = damaged(artifact, tmp_path, "extra")
    (case / "objects" / "000099").write_bytes(b"not in the manifest")
    assert refused("unexpected_file", bk.verify_artifact, case).detail == "objects/000099"
    case = damaged(artifact, tmp_path, "edited")
    rewrite_manifest(case, lambda m: m["snapshot"].update(taken_at="2000-01-01T00:00:00+00:00"), reseal=False)
    refused("manifest_checksum_mismatch", bk.verify_artifact, case)
    case = damaged(artifact, tmp_path, "resealed")  # a consistent manifest still has to match the files
    rewrite_manifest(case, lambda m: m["objects"]["entries"][0].update(sha256="0" * 64), reseal=True)
    refused("file_checksum_mismatch", bk.verify_artifact, case)
    case = damaged(artifact, tmp_path, "no-manifest")
    (case / bk.MANIFEST).unlink()
    refused("manifest_missing", bk.verify_artifact, case)
    case = damaged(artifact, tmp_path, "garbage")
    (case / bk.MANIFEST).write_bytes(b"{not json")
    refused("manifest_corrupt", bk.verify_artifact, case)
    case = damaged(artifact, tmp_path, "renamed")
    renamed = case.with_name(bk.new_backup_id(utcnow()))
    case.rename(renamed)
    refused("manifest_mismatch", bk.verify_artifact, renamed)
    refused("artifact_missing", bk.verify_artifact, tmp_path / "nowhere" / artifact.name)

    # A file changed after verification is caught again while it is read for upload.
    case = damaged(artifact, tmp_path, "late")
    bk.verify_artifact(case)
    (case / first["path"]).write_bytes(b"x" * first["bytes"])
    for key, _ in w.storage.orphan_keys(""):
        w.storage.delete(key)
    refused("file_checksum_mismatch", bk.restore_objects, w.storage, case, manifest)


def test_a_failed_or_interrupted_backup_never_leaves_an_artifact_that_verifies(upload_api, monkeypatch, tmp_path):
    w = listed(upload_api, monkeypatch)
    root = tmp_path / "backups"

    def failing_dump():
        yield DUMP
        raise OSError("pg_dump stream broke")
    writer = bk.ArtifactWriter(root, bk.new_backup_id(utcnow()))
    with pytest.raises(OSError):
        writer.write_stream(bk.DUMP, failing_dump())
    writer.abort()  # what the tool does on any failure
    assert list(root.iterdir()) == []

    # Killed before the manifest: the partial directory is never taken for a backup, under any name.
    writer = bk.ArtifactWriter(root, bk.new_backup_id(utcnow()))
    writer.write_stream(bk.DUMP, [DUMP])
    bk.copy_objects(w.storage, writer)
    refused("artifact_partial", bk.verify_artifact, writer.partial)
    renamed = writer.partial.rename(writer.final)
    refused("manifest_missing", bk.verify_artifact, renamed)
    shutil.rmtree(renamed)

    # A file changed between being written and the manifest: finish refuses and names nothing final.
    writer = bk.ArtifactWriter(root, bk.new_backup_id(utcnow()))
    database = writer.write_stream(bk.DUMP, [DUMP])
    (writer.path(bk.DUMP)).write_bytes(DUMP + b"late write")
    with pytest.raises(BackupError) as caught:
        writer.finish({"started_at": "", "finished_at": "", "source": {}, "snapshot": {}, "writers_stopped": [],
                       "journal_at_backup": None, "database": database,
                       "objects": {"bucket": "b", "count": 0, "bytes": 0, "entries": []},
                       "db_object_check": {"file_rows_checked": 0}})
    assert caught.value.code == "file_size_mismatch" and not writer.final.exists()
    writer.abort()

    # The bucket changed while it was copied (a writer still running).
    writer = bk.ArtifactWriter(root, bk.new_backup_id(utcnow()))
    real_get = w.storage.client.get_object

    def get_then_write(**kwargs):
        w.storage.put(f"originals/{uuid.uuid4().hex}", io.BytesIO(b"late upload"), "text/markdown")
        w.storage.client.get_object = real_get
        return real_get(**kwargs)
    w.storage.client.get_object = get_then_write
    refused("objects_changed_during_backup", bk.copy_objects, w.storage, writer)
    writer.abort()

    # A database row whose object is missing or different is not a consistent backup.
    writer = bk.ArtifactWriter(root, bk.new_backup_id(utcnow()))
    entries = bk.copy_objects(w.storage, writer)
    rows, _ = db_state(w.api.engine)
    key_a = w.api.scalar("SELECT storage_key FROM source_files WHERE id=:f", f=uuid.UUID(w.ids_a["file_id"]))
    refused("object_missing_for_database_row", bk.check_db_objects, rows, [e for e in entries if e["key"] != key_a])
    refused("object_differs_from_database_row", bk.check_db_objects, rows,
            [e | {"sha256": "0" * 64} if e["key"] == key_a else e for e in entries])
    writer.abort()
    assert list(root.iterdir()) == []


# --- the journal copy ---------------------------------------------------------

def test_journal_copy_only_reads_the_source_and_stays_apart_from_the_backup(tmp_path):
    source, chain = sample_journal(tmp_path)
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    backups = tmp_path / "backups"
    artifact = backups / bk.new_backup_id(utcnow())
    artifact.mkdir(parents=True)
    for inside in (artifact / "journal", backups / "journal", tmp_path):  # inside the backup, or around it
        refused("journal_copy_not_apart", bk.copy_journal, source, inside, utcnow(), apart_from=[artifact, backups])
    refused("journal_copy_is_source", bk.copy_journal, source, source.parent, utcnow())
    dest = tmp_path / "drill-journal"
    info = bk.copy_journal(source, dest, utcnow(), apart_from=[artifact, backups])
    assert (dest / bk.JOURNAL_FILE).read_bytes() == before[0]
    assert info["head_seq"] == chain[-1].seq and info["source_sha256"] == hashlib.sha256(before[0]).hexdigest()
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before  # the source journal is untouched
    refused("journal_copy_exists", bk.copy_journal, source, dest, utcnow())

    torn = tmp_path / "torn" / bk.JOURNAL_FILE
    torn.parent.mkdir()
    torn.write_bytes(before[0] + b'{"body":')
    refused("journal_torn", bk.copy_journal, torn, tmp_path / "c1", utcnow())
    torn.write_bytes(before[0].replace(b'"seq":2', b'"seq":3'))
    refused("journal_corrupt", bk.copy_journal, torn, tmp_path / "c2", utcnow())
    refused("journal_missing", bk.copy_journal, tmp_path / "absent.jsonl", tmp_path / "c3", utcnow())

    state = journal.read(dest / bk.JOURNAL_FILE)
    at = {"journal_id": str(state.journal_id), "genesis_hash": state.genesis_hash, "db_records": 3,
          "db_head_seq": 3, "db_head_hash": chain[2].hash, "unconfirmed": 0}
    bk.check_journal_for_backup(state, {"journal_at_backup": at})  # newer than the backup: fine
    refused("journal_mismatch", bk.check_journal_for_backup, state,
            {"journal_at_backup": at | {"journal_id": str(uuid.uuid4())}})
    refused("journal_behind_backup", bk.check_journal_for_backup, state,
            {"journal_at_backup": at | {"db_head_seq": chain[-1].seq + 1}})
    refused("journal_diverged", bk.check_journal_for_backup, state,
            {"journal_at_backup": at | {"db_head_hash": "f" * 64}})


# --- the gate after a restore ---------------------------------------------------

def routes(w) -> list[tuple]:
    return [("GET", "/library/items?limit=100"), ("GET", f"/library/items/{w.copy}"),
            ("GET", f"/files/{common_file(w.api, w.copy)}/original"), ("GET", f"/files/{w.ids_a['file_id']}/original"),
            ("POST", "/search", {"query": "anything", "mode": "keyword"})]


def test_restore_with_a_missing_or_foreign_journal_serves_nothing_and_recover_refuses(upload_api, monkeypatch,
                                                                                       admin_engine, app_env, tmp_path):
    w = listed(upload_api, monkeypatch)
    artifact = make_artifact(tmp_path / "backups", w.storage, w.api.engine)
    manifest = bk.verify_artifact(artifact)
    path = journal_file()
    with backup(w.api, admin_engine) as snap:
        assert revoke(w.admin, w.copy).status_code == 200
        assert runtime_cleanup.cleanup_revoked(w.api.engine, w.storage) == {"deleted": 1}
        current = path.read_bytes()
        app_env(snap.url)
        bk.restore_objects(w.storage, artifact, manifest)
        assert key_present(w.storage, common_key(w.api, w.copy))

        path.unlink()  # the journal copy never made it
        assert gate(snap.engine) == "journal_missing"
        assert_recovering(w.browsers, routes(w))
        assert TestClient(app).get("/readyz").status_code == 503
        settings = get_settings()
        assert process_job(uuid.UUID(w.ids_a["job_id"]), settings, w.storage) == "recovery_gate_closed"
        with pytest.raises(RecoveryError, match="journal_missing"):
            recovery.hold(snap.engine, settings, w.api.clock.now)
        with pytest.raises(RecoveryError, match="journal_missing"):
            do_recover(snap.engine, w.storage, w.api.clock.now)
        assert not path.exists()  # neither wrote a journal of its own

        foreign, _ = sample_journal(tmp_path / "other-install")
        path.write_bytes(foreign.read_bytes())  # another install's journal
        assert gate(snap.engine) == "journal_mismatch"
        assert_recovering(w.browsers, routes(w))
        refused("journal_mismatch", bk.check_journal_for_backup, journal.read(path), manifest)
        with pytest.raises(RecoveryError, match="journal_mismatch"):
            recovery.hold(snap.engine, settings, w.api.clock.now)
        with pytest.raises(RecoveryError, match="journal_mismatch"):
            do_recover(snap.engine, w.storage, w.api.clock.now)
        assert path.read_bytes() == foreign.read_bytes()  # refused without writing

        path.write_bytes(current)  # the right, current journal
        bk.check_journal_for_backup(journal.read(path), manifest)
        assert gate(snap.engine) == "db_behind_journal"
        assert do_recover(snap.engine, w.storage, w.api.clock.now).gate == "ready"
        assert not key_present(w.storage, common_key(w.api, w.copy))
        for browser in w.browsers.values():
            assert browser.get(f"/library/items/{w.copy}").json() == MISSING
        assert w.a.get(f"/files/{w.ids_a['file_id']}/original").content == A_BODY


def test_hold_shuts_the_gate_after_a_restore_even_with_nothing_revoked_since_the_backup(upload_api, monkeypatch,
                                                                                         admin_engine, app_env,
                                                                                         capsys):
    w = listed(upload_api, monkeypatch)
    settings = get_settings()
    path = journal_file()
    with backup(w.api, admin_engine) as snap:
        app_env(snap.url)
        assert gate(snap.engine) == "open"  # database and journal agree: without hold, traffic would start
        assert recovery_cli.main(["hold"]) == 0
        held = capsys.readouterr().out
        assert held.startswith("Recovery held open: ") and "gate=db_behind_journal" in held
        assert gate(snap.engine) == "db_behind_journal"
        assert_recovering(w.browsers, routes(w))
        assert process_job(uuid.UUID(w.ids_a["job_id"]), settings, w.storage) == "recovery_gate_closed"
        tasks = celery_tasks(monkeypatch)
        assert tasks.cleanup_revoked_copies() == {"skipped": "recovery_gate_closed"}
        lines = path.read_bytes()
        recovery_id = journal.read(path).open_recovery
        assert recovery.hold(snap.engine, settings, w.api.clock.now) == recovery_id  # again: same recovery, no write
        assert path.read_bytes() == lines

        report = do_recover(snap.engine, w.storage, w.api.clock.now)
        assert replayed_decisions(report) == {"recovery_begin": 1, "recovery_end": 1} and report.gate == "ready"
        assert journal.read(path).open_recovery is None
        assert w.b.get(f"/library/items/{w.copy}").status_code == 200  # nothing was revoked: still listed


def test_hold_refuses_a_journal_behind_the_database_rather_than_diverging_it(upload_api, monkeypatch):
    w = listed(upload_api, monkeypatch)
    settings = get_settings()
    assert revoke(w.admin, w.copy).status_code == 200
    path = journal_file()
    path.write_bytes(path.read_bytes().split(b"\n")[0] + b"\n")  # an older copy: genesis only
    with pytest.raises(RecoveryError, match="journal_behind_db"):
        recovery.hold(w.api.engine, settings, w.api.clock.now)
    assert gate(w.api.engine) == "journal_behind_db"  # still repairable with --confirm-db-is-current
    assert do_recover(w.api.setup_engine, w.storage, w.api.clock.now, confirm_db_current=True).gate == "ready"


@contextmanager
def restored_from(admin_engine, snap, app_env):
    """A fresh database cloned from the snapshot: one more 'pg_restore' of the same dump."""
    snap.engine.dispose()
    url = snap.url.set(database=f"ras_test_{uuid.uuid4().hex[:12]}")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{url.database}" TEMPLATE "{snap.url.database}"'))
    engine = app_db.make_engine(admin_engine.url.set(database=url.database))
    from tests.test_auth_safety_foundation import runtime_engine
    runtime = runtime_engine(SimpleNamespace(engine=engine))
    runtime.dispose()
    app_env(url)
    try:
        yield engine
    finally:
        engine.dispose()
        app_db.get_engine().dispose()
        app_db.get_engine.cache_clear()
        drop_database(admin_engine, url)


def test_restore_and_recover_run_again_safely(upload_api, monkeypatch, admin_engine, app_env, tmp_path):
    w = listed(upload_api, monkeypatch)
    settings = get_settings()
    artifact = make_artifact(tmp_path / "backups", w.storage, w.api.engine)
    manifest = bk.verify_artifact(artifact)
    key = common_key(w.api, w.copy)
    saved = objects(w.storage)
    total = len(saved)
    with backup(w.api, admin_engine) as snap:
        assert revoke(w.admin, w.copy).status_code == 200
        assert runtime_cleanup.cleanup_revoked(w.api.engine, w.storage) == {"deleted": 1}
        put_back(w.storage, {f"originals/{uuid.uuid4().hex}": b"written after the backup"})  # live system moves on

        for attempt in range(2):  # restore the same artifact twice, each followed by recover
            with restored_from(admin_engine, snap, app_env) as engine:
                counts = bk.restore_objects(w.storage, artifact, manifest)
                assert counts == {"put": 1, "unchanged": total - 1, "deleted": 1 if attempt == 0 else 0}, counts
                assert bk.restore_objects(w.storage, artifact, manifest) == {"put": 0, "unchanged": total,
                                                                             "deleted": 0}
                recovery.hold(engine, settings, w.api.clock.now)
                assert gate(engine) == "db_behind_journal"
                assert_recovering(w.browsers, routes(w))
                assert do_recover(engine, w.storage, w.api.clock.now).gate == "ready"
                again = do_recover(engine, w.storage, w.api.clock.now)  # recover again right away: no change
                assert again.gate == "ready" and again.cleanup == {} and again.objects_removed == 0
                assert replayed_decisions(again) == {"recovery_begin": 1, "recovery_end": 1}
                assert not key_present(w.storage, key)
                for browser in w.browsers.values():
                    assert browser.get(f"/library/items/{w.copy}").json() == MISSING
                assert w.a.get(f"/files/{w.ids_a['file_id']}/original").content == A_BODY

                # Objects put back over a recovered database (DB not reloaded): hold shuts the gate again.
                assert bk.restore_objects(w.storage, artifact, manifest)["put"] == 1
                assert key_present(w.storage, key)
                recovery.hold(engine, settings, w.api.clock.now)
                assert gate(engine) == "db_behind_journal"
                assert TestClient(app).get(f"/library/items/{w.copy}").json() == RECOVERING[1]
                assert do_recover(engine, w.storage, w.api.clock.now).objects_removed == 1
                assert gate(engine) == "open" and not key_present(w.storage, key)


# --- review PR #27 ------------------------------------------------------------

def ras_backup_tool():
    import sys

    from tests.conftest import REPO_DIR
    sys.path.insert(0, str(REPO_DIR / "scripts"))
    import ras_backup
    return ras_backup


class Touched(Exception):
    """The fake docker CLI reached a command that changes a project."""


def fake_docker(monkeypatch, *, db_running: bool, marker: bool):
    """Record every docker command ``restore`` issues; refuse (raise) at the first one that changes a project."""
    import subprocess
    calls: list[tuple[list[str], str | None]] = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs.get("input")))
        out = ""
        if argv[:3] == ["docker", "volume", "ls"]:
            out = "foreign_db-data\nforeign_minio-data\n"
        elif argv[:2] == ["docker", "compose"] and argv[4:6] == ["ps", "-a"]:
            out = "0123456789ab\n"
        elif argv[:2] == ["docker", "compose"] and argv[4:6] == ["ps", "--status"]:
            out = "db\nminio\nredis\n" if db_running else "minio\n"
        elif argv[:2] == ["docker", "compose"] and argv[4] == "exec":
            out = "1\n" if marker else ""
        else:
            raise Touched(" ".join(argv[4:6]))
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    return calls


@pytest.mark.parametrize("db_running", [True, False], ids=["db_running", "db_stopped"])
def test_restore_leaves_a_project_without_the_marker_completely_untouched(upload_api, monkeypatch, tmp_path,
                                                                          db_running):
    """Review PR #27: ownership is checked read-only before any stop/up/run on the target."""
    from types import SimpleNamespace

    tool = ras_backup_tool()
    w = listed(upload_api, monkeypatch)
    artifact = make_artifact(tmp_path / "backups", w.storage, w.api.engine)
    bk.copy_journal(journal_file(), tmp_path / "journal-copy", utcnow())
    for name, port in zip(tool.PORTS, ("46432", "46800", "46300", "46900", "46901")):
        monkeypatch.setenv(name, port)
    args = SimpleNamespace(project="foreign", artifact=str(artifact), journal_dir=str(tmp_path / "journal-copy"))

    calls = fake_docker(monkeypatch, db_running=db_running, marker=False)
    with pytest.raises(BackupError) as caught:
        tool.do_restore(args, {})
    assert caught.value.code == ("target_not_restore_owned" if db_running else "target_db_not_running")
    commands = [argv for argv, _ in calls]
    assert all(argv[:3] == ["docker", "volume", "ls"] or argv[4] in ("ps", "exec") for argv in commands), commands
    for argv, sql in calls:
        if argv[:2] == ["docker", "compose"] and argv[4] == "exec":  # only the marker SELECT, in a read-only session
            assert "default_transaction_read_only=on" in argv[-3] and sql.strip().upper().startswith("SELECT")
            assert all(word not in sql.upper() for word in ("DROP", "CREATE", "COMMENT", "INSERT", "UPDATE"))
    assert any(argv[4] == "exec" for argv in commands) is db_running  # a stopped db is never started to ask

    # Control: the same project *with* the marker goes on, and its first change comes after the read-only check.
    calls = fake_docker(monkeypatch, db_running=True, marker=True)
    with pytest.raises(Touched, match="stop"):
        tool.do_restore(args, {})
    assert [argv[4] for argv, _ in calls if argv[:2] == ["docker", "compose"]][-2:] == ["exec", "stop"]


def test_restore_refuses_an_object_read_back_with_other_bytes_of_the_same_size_and_the_gate_stays_shut(
        upload_api, monkeypatch, admin_engine, app_env, tmp_path):
    """Review PR #27: after putting objects, every one is read back and compared by SHA-256, not by size."""
    w = listed(upload_api, monkeypatch)
    settings = get_settings()
    artifact = make_artifact(tmp_path / "backups", w.storage, w.api.engine)
    manifest = bk.verify_artifact(artifact)
    key = common_key(w.api, w.copy)
    with backup(w.api, admin_engine) as snap:
        assert revoke(w.admin, w.copy).status_code == 200
        assert runtime_cleanup.cleanup_revoked(w.api.engine, w.storage) == {"deleted": 1}
        app_env(snap.url)
        recovery.hold(snap.engine, settings, w.api.clock.now)  # the tool holds before restoring objects
        real_put = w.storage.put

        def faulty_put(k, file, mime):  # the store keeps other bytes of the same length
            data = file.read()
            return real_put(k, io.BytesIO(bytes(b ^ 0xFF for b in data) if k == key else data), mime)
        monkeypatch.setattr(w.storage, "put", faulty_put)
        refused("objects_restore_mismatch", bk.restore_objects, w.storage, artifact, manifest)
        assert w.storage.size(key) == next(e["bytes"] for e in manifest["objects"]["entries"] if e["key"] == key)
        assert gate(snap.engine) == "db_behind_journal"
        assert_recovering(w.browsers, routes(w))
        assert TestClient(app).get("/readyz").status_code == 503

        monkeypatch.setattr(w.storage, "put", real_put)  # rerun once the store behaves
        assert bk.restore_objects(w.storage, artifact, manifest)["put"] == 1
        assert do_recover(snap.engine, w.storage, w.api.clock.now).gate == "ready"
        assert w.a.get(f"/files/{w.ids_a['file_id']}/original").content == A_BODY
        for browser in w.browsers.values():
            assert browser.get(f"/library/items/{w.copy}").json() == MISSING
