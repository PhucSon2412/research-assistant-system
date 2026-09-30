"""M6b-2b2b demo: index a published common copy on an *isolated* Compose project.

HTTP against the Compose API (Admin, User A, User B): A uploads a Markdown and
a PDF (the Compose Celery worker extracts them), A's private copies are indexed
on the host with a fake embedding; A submits review requests, the Admin
approves the same bytes with rights that permit full-text search and provider
processing, publishes, and starts indexing twice (one job). The Compose worker
extracts the common file and stops at ``awaiting_index`` (no key in the
containers); the index step then runs on the host through the same
``process_job`` entry point with only the embedding transport replaced.
Checks: indexed, not served (search, chunks, RAG), private rows untouched,
application-only accounting; then revoke and cleanup remove the common rows.

Reads only ``POSTGRES_*``/``MINIO_ROOT_*`` names from ``.env`` (never the
OpenRouter key) and refuses a database holding any account it did not create,
so it cannot run against the dev stack by mistake. Prints IDs-free step
results, counts and timings of these synthetic fixtures (not a service level).
"""

from __future__ import annotations

import hashlib
import io
import os
import secrets
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import SecretStr
from reportlab.pdfgen import canvas
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
ALLOWED = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD")
PREFIX = "m6b2b2b-demo-"


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
    ("POSTGRES_HOST_PORT", "5432"), ("API_HOST_PORT", "8000"), ("WEB_HOST_PORT", "3000"),
    ("MINIO_API_HOST_PORT", "9000"))}
for name, value in {
    "RAS_DB__HOST": "127.0.0.1", "RAS_DB__PORT": PORTS["POSTGRES_HOST_PORT"], "RAS_DB__USER": ENV["POSTGRES_USER"],
    "RAS_DB__PASSWORD": ENV["POSTGRES_PASSWORD"], "RAS_DB__NAME": ENV["POSTGRES_DB"],
    "RAS_STORAGE__ENDPOINT_URL": f"http://127.0.0.1:{PORTS['MINIO_API_HOST_PORT']}",
    "RAS_STORAGE__ACCESS_KEY": ENV["MINIO_ROOT_USER"], "RAS_STORAGE__SECRET_KEY": ENV["MINIO_ROOT_PASSWORD"],
    "RAS_STORAGE__BUCKET": "ras-originals", "RAS_EMBEDDING__API_KEY": "", "OPENROUTER_API_KEY": "",
}.items():
    os.environ[name] = str(value)

from app.config import get_settings  # noqa: E402
from app.identity.passwords import hash_password  # noqa: E402
from app.ingest import indexer  # noqa: E402
from app.ingest.embedding import EmbeddingResult  # noqa: E402
from app.ingest.worker import process_job  # noqa: E402
from app.publication import cleanup_revoked  # noqa: E402
from app.storage import OriginalStorage  # noqa: E402

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
API = f"http://127.0.0.1:{PORTS['API_HOST_PORT']}"
WEB = f"http://127.0.0.1:{PORTS['WEB_HOST_PORT']}"
INDEXABLE = {"audience": "instance_users", "original_download": False, "fulltext_search": True,
             "external_processing": True, "conditions": None}
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


def fake_embed(value: str, selected):
    assert "References" not in value and "[1]" not in value and not value.startswith("%PDF-")
    CALLS.append(value)
    return EmbeddingResult([0.1, 0.2, 0.3], selected.embedding.model_id, len(value.split()))


def synthetic_pdf() -> bytes:
    out = io.BytesIO()
    pdf = canvas.Canvas(out, pagesize=(612, 792), pageCompression=0)
    line = "A measured synthetic observation with enough text for page coverage checks."
    for lines in (["Synthetic demo paper", "Abstract", "A short synthetic abstract."],
                  ["Introduction"] + [line] * 25, ["Methods"] + [line] * 25,
                  ["References", "[1] Example Author. Example Work. 2020."]):
        y = 750
        for text_line in lines:
            pdf.drawString(35, y, text_line)
            y -= 15
        pdf.showPage()
    pdf.save()
    return out.getvalue()


class Person:
    def __init__(self, email: str, password: str):
        self.client = httpx.Client(base_url=API, timeout=30)
        assert self.client.get("/auth/csrf").status_code == 200
        response = self.client.post("/auth/login", json={"email": email, "password": password},
                                    headers=self.headers())
        assert response.status_code == 200, response.status_code

    def headers(self) -> dict:
        return {"origin": WEB, "x-csrf-token": self.client.cookies.get("ras_csrf", "")}

    def get(self, path: str):
        return self.client.get(path)

    def post(self, path: str, body: dict):
        return self.client.post(path, json=body, headers=self.headers())

    def upload(self, body: bytes, kind: str) -> dict:
        notice = self.get("/uploads/notice").json()["version"]
        intent = self.post("/uploads/intents", {"acknowledged": True, "notice_version": notice, "file_kind": kind,
                                                "filename": f"demo.{'pdf' if kind == 'pdf' else 'md'}",
                                                "byte_size": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        assert intent.status_code == 201, intent.status_code
        sent = self.client.put("/uploads/original", content=body, headers={
            **self.headers(), "x-upload-intent": intent.json()["intent_token"],
            "content-type": "application/octet-stream"})
        assert sent.status_code == 201, sent.status_code
        return sent.json()


def wait_state(job_id: uuid.UUID, wanted: str, seconds: int = 120) -> float:
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        if scalar("SELECT state FROM ingestion_jobs WHERE id=:j", j=job_id) == wanted:
            return time.monotonic() - started
        time.sleep(0.5)
    raise AssertionError(f"job did not reach {wanted}")


def main() -> None:
    others = scalar("SELECT count(*) FROM users WHERE email NOT LIKE :p", p=f"{PREFIX}%")
    step("isolated database (no account other than this demo's)", others == 0)
    run = uuid.uuid4().hex[:8]
    password = secrets.token_urlsafe(24)
    with ENGINE.begin() as conn:
        for who, role in (("admin", "admin"), ("a", "user"), ("b", "user")):
            conn.execute(text("INSERT INTO users(email, password_verifier, role) VALUES (:e, :p, :r)"),
                         {"e": f"{PREFIX}{run}-{who}@example.org", "p": hash_password(password), "r": role})
    admin, a, b = (Person(f"{PREFIX}{run}-{who}@example.org", password) for who in ("admin", "a", "b"))
    indexer.embed = fake_embed
    storage = OriginalStorage(FAKE)
    md = (f"# Demo {run}\n\n## Abstract\nA synthetic abstract for the demo.\n\n## Methods\n"
          f"Demo result {run} is eleven units.\n\n## References\n[1] A. Alpha. Demo Study. 2026.\n").encode()
    bodies = {"md": md, "pdf": synthetic_pdf()}
    private = {kind: a.upload(body, "pdf" if kind == "pdf" else "markdown") for kind, body in bodies.items()}
    for kind, ids in private.items():
        waited = wait_state(uuid.UUID(ids["job_id"]), "awaiting_index")
        step(f"Compose worker extracted A's private {kind} ({waited:.1f}s)", True)
        step(f"A's private {kind} indexed on the host with the fake transport",
             process_job(uuid.UUID(ids["job_id"]), FAKE, storage) == "ready")
    user_budgets = scalar("SELECT coalesce(sum(committed_usd + reserved_usd), 0) FROM embedding_user_budget_days")
    CALLS.clear()

    copies = {}
    for kind, ids in private.items():
        rid = a.post("/review-requests", {"acknowledged": True, "item_id": ids["item_id"],
                                          "version_id": ids["version_id"], "source_file_id": ids["file_id"],
                                          "notice_version": a.get("/review-requests/notice").json()["version"],
                                          "declared_source": "Synthetic demo file", "declared_rights": "Demo"})
        assert rid.status_code == 201, rid.status_code
        approved = admin.post(f"/admin/review-requests/{rid.json()['request']['id']}/approve", {
            "confirm_verified": True, "file_checksum_sha256": hashlib.sha256(bodies[kind]).hexdigest(),
            "title": f"Demo common {kind}", "verified_source": "Demo Admin check: synthetic file by the demo script",
            "evidence_note": "Demo Admin check: synthetic permission for this checksum",
            "checked_on": datetime.now(UTC).date().isoformat(), "scope": INDEXABLE})
        assert approved.status_code == 200, approved.status_code
        copy = approved.json()["request"]["pending_publication_id"]
        assert admin.post(f"/admin/common-copies/{copy}/publish", {
            "confirm_publish": True, "file_checksum_sha256": hashlib.sha256(bodies[kind]).hexdigest()}).status_code == 200
        first = admin.post(f"/admin/common-copies/{copy}/index", {"confirm_external_processing": True}).json()
        again = admin.post(f"/admin/common-copies/{copy}/index", {"confirm_external_processing": True}).json()
        step(f"{kind}: index started once (repeat returns the same job)",
             first["created"] is True and again["created"] is False and first["job"]["id"] == again["job"]["id"])
        copies[kind] = (copy, uuid.UUID(first["job"]["id"]))
    for kind, (copy, job) in copies.items():
        waited = wait_state(job, "awaiting_index")
        step(f"{kind}: Compose worker extracted the common file and waited, no provider ({waited:.1f}s)",
             scalar("SELECT content_status FROM library_items WHERE id=:i", i=uuid.UUID(copy)) == "metadata_only")
        started = time.monotonic()
        state = process_job(job, FAKE, storage)
        step(f"{kind}: host index step with the fake transport -> {state} ({time.monotonic() - started:.2f}s)",
             state == "ready" and scalar("SELECT content_status FROM library_items WHERE id=:i",
                                         i=uuid.UUID(copy)) == "indexed")
        disjoint = scalar("""SELECT count(*) FROM chunks c JOIN chunks p ON p.id = c.id
            WHERE c.item_id=:c AND p.item_id=:p""", c=uuid.UUID(copy), p=uuid.UUID(private[kind]["item_id"]))
        owners = scalar("""SELECT count(*) FROM embedding_calls e JOIN source_files f ON f.id=e.source_file_id
            WHERE f.item_id=:c AND e.owner_user_id IS NOT NULL""", c=uuid.UUID(copy))
        step(f"{kind}: own rows (no shared chunk) and application-charged calls", disjoint == 0 and owners == 0)
    step("no User quota moved by common indexing",
         scalar("SELECT coalesce(sum(committed_usd + reserved_usd), 0) FROM embedding_user_budget_days") == user_budgets)
    for kind, (copy, _) in copies.items():
        detail = b.get(f"/library/items/{copy}").json()["item"]
        closed = all(b.get(f"/library/items/{copy}/{p}").status_code == 404 for p in ("chunks", "references"))
        hits = b.post("/search", {"query": f"Demo result {run} eleven units", "mode": "keyword"}).json()["hits"]
        rag = b.post("/rag/ask", {"item_id": copy, "question": "What is the demo result?", "mode": "keyword"})
        note = admin.get(f"/admin/common-copies/{copy}").json()["item"]["index"]["note"]
        step(f"{kind}: indexed but not served to B (search 0 hits, chunks/RAG 404, note shown)",
             detail["content_status"] == "indexed" and detail["fulltext_search_available"] is False and closed
             and not hits and rag.status_code == 404 and "chưa mở tìm kiếm/hỏi đáp" in (note or ""))
    for kind, (copy, _) in copies.items():
        assert admin.post(f"/admin/common-copies/{copy}/revoke",
                          {"confirm_revoke": True, "reason_code": "rights_withdrawn"}).status_code == 200
    started = time.monotonic()
    outcome = cleanup_revoked(ENGINE, storage, datetime.now(UTC), 50)
    left = sum(scalar("SELECT count(*) FROM chunks WHERE item_id=:i", i=uuid.UUID(c)) for c, _ in copies.values())
    step(f"revoke + cleanup removed the common rows {outcome} ({time.monotonic() - started:.2f}s)", left == 0)
    private_left = sum(scalar("SELECT count(*) FROM chunks WHERE item_id=:i", i=uuid.UUID(i["item_id"]))
                       for i in private.values())
    step("A's private rows are still there", private_left > 0)
    print({"steps": len(RESULTS), "passed": sum(ok for _, ok in RESULTS), "fake_embedding_requests": len(CALLS)})


if __name__ == "__main__":
    main()
