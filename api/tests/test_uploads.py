from __future__ import annotations

import hashlib
import io
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from pypdf import PdfWriter
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config as app_config
from app.api.routes.uploads import _storage
from app.main import app
from app.storage import get_storage
from app.clock import utcnow
from app.cli import reconcile_originals
from app.models import SourceFile
from tests.conftest import _read_dotenv, _unquote, REPO_DIR
from tests.helpers import WEB_ORIGIN


@pytest.fixture
def upload_api(api, monkeypatch):
    env = {k: os.environ[k] for k in ("MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD", "MINIO_API_HOST_PORT") if k in os.environ}
    if not all(env.get(k) for k in ("MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD")):
        env = {k: _unquote(v) for k, v in _read_dotenv(REPO_DIR / ".env").items()}
    assert env.get("MINIO_ROOT_USER") and env.get("MINIO_ROOT_PASSWORD")
    monkeypatch.setenv("RAS_STORAGE__ENDPOINT_URL", f"http://127.0.0.1:{env.get('MINIO_API_HOST_PORT', '9000')}")
    monkeypatch.setenv("RAS_STORAGE__ACCESS_KEY", env["MINIO_ROOT_USER"])
    monkeypatch.setenv("RAS_STORAGE__SECRET_KEY", env["MINIO_ROOT_PASSWORD"])
    bucket = f"ras-test-{uuid.uuid4().hex[:16]}"
    monkeypatch.setenv("RAS_STORAGE__BUCKET", bucket)
    app_config.get_settings.cache_clear()
    storage = get_storage(app_config.get_settings())
    storage.ensure_bucket()
    yield api, storage
    app.dependency_overrides.pop(_storage, None)
    try:
        for key, _ in storage.orphan_keys(""):
            storage.delete(key)
        storage.client.delete_bucket(Bucket=bucket)
    except Exception:
        pass
    app_config.get_settings.cache_clear()


def pdf(pages=1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def intent(browser, data: bytes, filename: str = "paper.md", *, notice=None):
    kind = "pdf" if filename.endswith(".pdf") else "markdown"
    version = notice or browser.get("/uploads/notice").json()["version"]
    return browser.client.post("/uploads/intents", json={
        "acknowledged": True, "notice_version": version, "file_kind": kind,
        "filename": filename, "byte_size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
    }, headers={"origin": WEB_ORIGIN, "x-csrf-token": browser.csrf()})


def send(browser, token, data: bytes, **headers):
    return browser.client.put("/uploads/original", content=data, headers={
        "origin": WEB_ORIGIN, "x-csrf-token": browser.csrf(),
        "x-upload-intent": token, "content-type": "application/octet-stream", **headers,
    })


def test_private_markdown_upload_download_and_acl(upload_api):
    api, storage = upload_api
    api.bootstrap_admin()
    a_id = api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b, admin = api.user_browser("a@example.org"), api.user_browser("b@example.org"), api.admin_browser()
    original = b"# Private paper\n\nA short body.\n"
    created = intent(a, original)
    assert created.status_code == 201, created.text
    token = created.json()["intent_token"]
    result = send(a, token, original)
    assert result.status_code == 201, result.text
    ids = result.json()
    assert ids["state"] == "received"
    detail = a.get(f"/library/items/{ids['item_id']}").json()["item"]
    assert detail["content_status"] == "metadata_only"
    assert detail["owned_by_me"] is True
    assert detail["jobs"][0]["state"] == "received"
    assert detail["files"][0]["verified_mime"] == "text/markdown"
    assert a.get(f"/files/{ids['file_id']}/original").content == original
    for other in (b, admin):
        assert other.get(f"/library/items/{ids['item_id']}").status_code == 404
        assert other.get(f"/files/{ids['file_id']}/original").status_code == 404
        assert ids["item_id"] not in str(other.get("/library/items").json())
        assert ids["item_id"] not in str(other.get("/library/items?scope=mine").json())
    assert api.scalar("SELECT owner_user_id FROM library_items WHERE id=:i", i=uuid.UUID(ids["item_id"])) == a_id
    assert api.scalar("SELECT count(*) FROM source_files") == 1
    assert api.scalar("SELECT count(*) FROM processing_acknowledgements") == 1
    assert api.scalar("SELECT count(*) FROM upload_intents WHERE used_at IS NOT NULL") == 1
    assert token not in api.dump_all_text()
    assert send(a, token, original).status_code == 404
    assert api.scalar("SELECT count(*) FROM library_items") == 1


def test_intent_expiry_session_binding_csrf_origin_and_mismatch(upload_api):
    api, storage = upload_api
    api.create_user("a@example.org")
    api.create_user("b@example.org")
    a, b, other_session = api.user_browser("a@example.org"), api.user_browser("b@example.org"), api.user_browser("a@example.org")
    data = b"# One\n"
    assert intent(a, data, notice="old").status_code == 409
    assert a.client.post("/uploads/intents", json={}, headers={"origin": WEB_ORIGIN}).status_code == 403
    bad_origin = a.client.post("/uploads/intents", json={}, headers={"origin": "https://evil.test", "x-csrf-token": a.csrf()})
    assert bad_origin.status_code == 403
    no_origin = a.client.post("/uploads/intents", json={
        "acknowledged": True, "notice_version": a.get("/uploads/notice").json()["version"],
        "file_kind": "markdown", "filename": "paper.md", "byte_size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }, headers={"x-csrf-token": a.csrf()})
    assert no_origin.status_code == 403
    created = intent(a, data)
    token = created.json()["intent_token"]
    assert send(b, token, data).status_code == 404
    assert send(other_session, token, data).status_code == 404
    assert send(a, token, b"# Two\n").status_code == 422
    assert send(a, token, data + b"extra").status_code == 413
    assert send(a, "missing", data).status_code == 404
    assert send(a, token, data, **{"origin": "https://evil.test"}).status_code == 403
    assert api.scalar("SELECT count(*) FROM source_files") == 0
    assert send(a, token, data).status_code == 201
    expired = intent(a, data).json()["intent_token"]
    api.clock.advance(minutes=10)
    assert send(a, expired, data).status_code == 404
    assert api.scalar("SELECT count(*) FROM source_files") == 1


def test_pdf_markdown_validation_and_limits(upload_api):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    one_page = pdf()
    result = send(a, intent(a, one_page, "one.pdf").json()["intent_token"], one_page)
    assert result.status_code == 201, result.text
    assert api.scalar("SELECT page_count FROM source_files") == 1
    for name, data, code in [
        ("fake.pdf", b"# markdown\n", "invalid_pdf"),
        ("binary.md", b"\x00binary", "invalid_markdown"),
        ("huge.pdf", pdf(101), "invalid_pdf"),
    ]:
        token = intent(a, data, name).json()["intent_token"]
        response = send(a, token, data)
        assert response.status_code == 422 and response.json()["error"] == code
    too_big = b"a" * (2 * 1024 * 1024 + 1)
    assert intent(a, too_big).status_code == 413
    assert api.scalar("SELECT count(*) FROM source_files") == 1
    assert len(list(storage.orphan_keys())) == 1


def test_pdf_near_30mb_limit(upload_api):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Padding": "x" * (29 * 1024 * 1024)})
    out = io.BytesIO()
    writer.write(out)
    data = out.getvalue()
    assert 28 * 1024 * 1024 < len(data) < 30 * 1024 * 1024
    response = send(a, intent(a, data, "large.pdf").json()["intent_token"], data)
    assert response.status_code == 201, response.text
    assert api.scalar("SELECT byte_size FROM source_files") == len(data)


def test_same_intent_race_at_most_one_success(upload_api):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    data = b"# Race\n"
    token = intent(a, data).json()["intent_token"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: send(a, token, data).status_code, range(2)))
    assert sorted(results) == [201, 404]
    assert api.scalar("SELECT count(*) FROM library_items") == 1
    assert api.scalar("SELECT count(*) FROM source_files") == 1


def test_storage_failure_leaves_no_item(upload_api):
    api, real_storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    data = b"# storage error\n"
    token = intent(a, data).json()["intent_token"]

    class FailingStorage:
        def put(self, *args):
            raise OSError("injected")

        def delete(self, *args):
            pass

    app.dependency_overrides[_storage] = lambda: FailingStorage()
    assert send(a, token, data).status_code == 503
    app.dependency_overrides.pop(_storage)
    assert api.scalar("SELECT count(*) FROM library_items") == 0
    assert api.scalar("SELECT count(*) FROM source_files") == 0
    assert len(list(real_storage.orphan_keys())) == 0


def test_db_commit_failure_removes_unreferenced_object(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    data = b"# database error\n"
    token = intent(a, data).json()["intent_token"]
    original_commit = Session.commit
    failed = False

    def fail_once(session):
        nonlocal failed
        if not failed and any(isinstance(obj, SourceFile) for obj in session.identity_map.values()):
            failed = True
            raise OSError("injected commit error")
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", fail_once)
    assert send(a, token, data).status_code == 503
    assert failed
    assert api.scalar("SELECT count(*) FROM source_files") == 0
    assert api.scalar("SELECT count(*) FROM library_items") == 0
    assert len(list(storage.orphan_keys())) == 0
    monkeypatch.setattr(Session, "commit", original_commit)
    assert send(a, token, data).status_code == 201


def test_reconcile_removes_crash_orphan_but_keeps_referenced_file(upload_api, monkeypatch):
    api, storage = upload_api
    api.create_user("a@example.org")
    a = api.user_browser("a@example.org")
    data = b"# keep\n"
    assert send(a, intent(a, data).json()["intent_token"], data).status_code == 201
    storage.put("originals/crash-orphan", io.BytesIO(b"orphan"), "text/markdown")
    assert len(list(storage.orphan_keys())) == 2
    monkeypatch.setattr(reconcile_originals, "get_settings", app_config.get_settings)
    monkeypatch.setattr(reconcile_originals, "get_engine", lambda: api.engine)
    monkeypatch.setattr(reconcile_originals, "utcnow", lambda: utcnow() + timedelta(days=2))
    reconcile_originals.main()
    assert len(list(storage.orphan_keys())) == 1


def test_source_file_job_version_must_share_item(upload_api):
    api, _ = upload_api
    user_a = api.create_user("a@example.org")
    user_b = api.create_user("b@example.org")
    item_a = api.add_item(owner=user_a, title="A")
    item_b = api.add_item(owner=user_b, title="B")
    common = api.add_item(owner=None, title="common")
    with api.engine.begin() as conn:
        job = conn.scalar(text("""INSERT INTO ingestion_jobs
            (origin, scope_visibility, scope_owner_user_id, item_id, idempotency_key)
            VALUES ('upload', 'private', :u, :i, :k) RETURNING id"""),
            {"u": user_a, "i": item_a, "k": str(uuid.uuid4())})
        version_a = conn.scalar(text("SELECT id FROM paper_versions WHERE item_id=:i"), {"i": item_a})
    for item in (item_b, common):
        with pytest.raises(IntegrityError), api.engine.begin() as conn:
            version = conn.scalar(text("SELECT id FROM paper_versions WHERE item_id=:i"), {"i": item})
            conn.execute(text("""INSERT INTO source_files
                (item_id, version_id, job_id, storage_key, verified_mime, byte_size, checksum_sha256)
                VALUES (:i, :v, :j, :k, 'text/markdown', 1, :h)"""),
                {"i": item_a, "v": version, "j": job, "k": str(uuid.uuid4()), "h": "a" * 64})
    with api.engine.begin() as conn:
        conn.execute(text("""INSERT INTO source_files
            (item_id, version_id, job_id, storage_key, verified_mime, byte_size, checksum_sha256)
            VALUES (:i, :v, :j, :k, 'text/markdown', 1, :h)"""),
            {"i": item_a, "v": version_a, "j": job, "k": str(uuid.uuid4()), "h": "a" * 64})
    for sql, params in [
        ("UPDATE ingestion_jobs SET item_id=:other WHERE id=:id", {"other": item_b, "id": job}),
        ("UPDATE paper_versions SET item_id=:other WHERE id=:id", {"other": item_b, "id": version_a}),
    ]:
        with pytest.raises(IntegrityError), api.engine.begin() as conn:
            conn.execute(text(sql), params)
