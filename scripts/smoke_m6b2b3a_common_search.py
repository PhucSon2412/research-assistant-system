"""M6b-2b3a smoke: common full-text search through the web proxy, on an *isolated* Compose project.

Every checked request goes to the web origin (``/api/...`` same-origin proxy,
and the ``/search`` / ``/sources/{id}`` pages), never straight to FastAPI.
User A and User B upload synthetic Markdown (B one file with the *same bytes*
as A's); the Compose worker extracts, and this host process indexes with a
fake embedding (no key in the containers, no provider call). A's file is
approved into a common copy whose rights permit full-text search and provider
processing but **not** download, published and indexed. Checks: A, B and the
Admin find and open the common copy; nobody finds or opens another User's
private text; the common original stays ``404``; RAG does not take the copy;
after the Admin revokes, new requests no longer return it although its index
rows still await cleanup.

Reads only ``POSTGRES_*``/``MINIO_ROOT_*`` names from ``.env`` (never the
OpenRouter key) and refuses a database holding any account it did not create.
Prints step results and counts only: no cookies, tokens, passwords or bodies.
"""

from __future__ import annotations

import hashlib
import os
import secrets
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
PREFIX = "m6b2b3a-smoke-"


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
    ("POSTGRES_HOST_PORT", "5432"), ("WEB_HOST_PORT", "3000"), ("MINIO_API_HOST_PORT", "9000"))}
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
WEB = f"http://127.0.0.1:{PORTS['WEB_HOST_PORT']}"
SCOPE = {"audience": "instance_users", "original_download": False, "fulltext_search": True,
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
    assert "References" not in value and "[1]" not in value
    CALLS.append(value)
    return EmbeddingResult([0.1, 0.2, 0.3], selected.embedding.model_id, len(value.split()))


class Person:
    """A signed-in account talking only to the web origin (``/api`` proxy and pages)."""

    def __init__(self, email: str, password: str):
        self.client = httpx.Client(base_url=WEB, timeout=60)
        assert self.client.get("/api/auth/csrf").status_code == 200
        response = self.client.post("/api/auth/login", json={"email": email, "password": password},
                                    headers=self.headers())
        assert response.status_code == 200, response.status_code

    def headers(self) -> dict:
        return {"origin": WEB, "x-csrf-token": self.client.cookies.get("ras_csrf", "")}

    def get(self, path: str):
        return self.client.get(path)

    def post(self, path: str, body: dict):
        return self.client.post(path, json=body, headers=self.headers())

    def upload(self, body: bytes) -> dict:
        notice = self.get("/api/uploads/notice").json()["version"]
        intent = self.post("/api/uploads/intents", {"acknowledged": True, "notice_version": notice,
                                                    "file_kind": "markdown", "filename": "smoke.md",
                                                    "byte_size": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        assert intent.status_code == 201, intent.status_code
        sent = self.client.put("/api/uploads/original", content=body, headers={
            **self.headers(), "x-upload-intent": intent.json()["intent_token"],
            "content-type": "application/octet-stream"})
        assert sent.status_code == 201, sent.status_code
        return sent.json()

    def search(self, query: str, **body) -> httpx.Response:
        return self.post("/api/search", {"query": query, "mode": "keyword"} | body)


def wait_state(job_id: uuid.UUID, wanted: str, seconds: int = 120) -> None:
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        if scalar("SELECT state FROM ingestion_jobs WHERE id=:j", j=job_id) == wanted:
            return
        time.sleep(0.5)
    raise AssertionError(f"job did not reach {wanted}")


def indexed(person: Person, storage: OriginalStorage, body: bytes) -> dict:
    ids = person.upload(body)
    wait_state(uuid.UUID(ids["job_id"]), "awaiting_index")
    assert process_job(uuid.UUID(ids["job_id"]), FAKE, storage) == "ready"
    return ids


def items_of(response: httpx.Response) -> set[str]:
    assert response.status_code == 200, response.status_code
    return {hit["item_id"] for hit in response.json()["hits"]}


def main() -> None:
    others = scalar("SELECT count(*) FROM users WHERE email NOT LIKE :p", p=f"{PREFIX}%")
    step("isolated database (no account other than this smoke's)", others == 0)
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
    term = f"Smoke{run}"
    main_md = (f"# Smoke {run}\n\n## Abstract\n{term} abstract of the shared method.\n\n## Methods\n"
               f"{term} result is nine units.\n\n## References\n[1] A. Alpha. Smoke Study. 2026.\n").encode()
    a_main = indexed(a, storage, main_md)
    a_only = indexed(a, storage, f"## Methods\n{term} result Asecret{run} private body.\n".encode())
    b_same = indexed(b, storage, main_md)  # same bytes and checksum as A's file and the common copy
    b_only = indexed(b, storage, f"## Methods\n{term} result Bsecret{run} private body.\n".encode())
    step("A and B indexed private Markdown through the web upload route (Compose worker + host fake embed)", True)

    rid = a.post("/api/review-requests", {"acknowledged": True, "item_id": a_main["item_id"],
                                          "version_id": a_main["version_id"], "source_file_id": a_main["file_id"],
                                          "notice_version": a.get("/api/review-requests/notice").json()["version"],
                                          "declared_source": "Synthetic smoke file", "declared_rights": "Smoke"})
    assert rid.status_code == 201, rid.status_code
    checksum = hashlib.sha256(main_md).hexdigest()
    approved = admin.post(f"/api/admin/review-requests/{rid.json()['request']['id']}/approve", {
        "confirm_verified": True, "file_checksum_sha256": checksum, "title": f"Smoke common {run}",
        "verified_source": "Smoke Admin check: synthetic file by the smoke script",
        "evidence_note": "Smoke Admin check: synthetic permission for this checksum",
        "checked_on": datetime.now(UTC).date().isoformat(), "scope": SCOPE})
    assert approved.status_code == 200, approved.status_code
    copy = approved.json()["request"]["pending_publication_id"]
    assert admin.post(f"/api/admin/common-copies/{copy}/publish",
                      {"confirm_publish": True, "file_checksum_sha256": checksum}).status_code == 200
    started = admin.post(f"/api/admin/common-copies/{copy}/index", {"confirm_external_processing": True})
    job = uuid.UUID(started.json()["job"]["id"])
    wait_state(job, "awaiting_index")
    step("common copy approved (search + provider, no download), published and indexed",
         process_job(job, FAKE, storage) == "ready")
    status = admin.get(f"/api/admin/common-copies/{copy}").json()["item"]
    step("Admin page reports search open, RAG closed",
         (status["fulltext_available"], status["index"]["search_open"], status["index"]["rag_open"]) == (True, True, False))

    allowed = {"a": {a_main["item_id"], a_only["item_id"], copy}, "b": {b_same["item_id"], b_only["item_id"], copy},
               "admin": {copy}}
    found = {who: p.search(f"{term} result") for who, p in people.items()}
    step("keyword search via proxy: each account gets its own private items plus the common copy, nothing else",
         all(items_of(found[who]) == allowed[who] for who in people))
    step("no other User's canary in any response",
         f"Bsecret{run}" not in found["a"].text + found["admin"].text and f"Asecret{run}" not in found["b"].text)
    common_hits = [h for h in found["b"].json()["hits"] if h["item_id"] == copy]
    step("common hits are labelled common and carry no download link (rights do not permit download)",
         common_hits and all(h["visibility"] == "common" and h["original_url"] is None for h in common_hits))
    chunk, common_file = common_hits[0]["chunk_id"], common_hits[0]["source_file_id"]
    opened = {who: p.get(f"/api/search/chunks/{chunk}") for who, p in people.items()}
    step("A, B and the Admin open the common source chunk (API through proxy)",
         all(r.status_code == 200 and r.json()["item_id"] == copy for r in opened.values()))
    step("source viewer and search pages are served by the web", all(
        b.get(path).status_code == 200 for path in (f"/sources/{chunk}", "/search", f"/items/{copy}")))
    step("the common original stays 404 for everyone (search does not grant download)",
         all(p.get(f"/api/files/{common_file}/original").status_code == 404 for p in people.values()))
    step("common chunks/references/coverage open for B",
         all(b.get(f"/api/library/items/{copy}/{path}").status_code == 200
             for path in ("chunks", "references", "coverage", "metadata-candidates")))

    b_chunk = next(h["chunk_id"] for h in found["b"].json()["hits"] if h["item_id"] == b_only["item_id"])
    a_chunk = next(h["chunk_id"] for h in found["a"].json()["hits"] if h["item_id"] == a_only["item_id"])
    denied = [a.get(f"/api/search/chunks/{b_chunk}"), admin.get(f"/api/search/chunks/{b_chunk}"),
              b.get(f"/api/search/chunks/{a_chunk}"), admin.get(f"/api/search/chunks/{a_chunk}"),
              a.get(f"/api/library/items/{b_only['item_id']}/chunks"),
              b.search(term, item_id=a_only["item_id"]), admin.search(term, item_id=b_same["item_id"])]
    step("private text of the other User is 404 by chunk, item chunks and item scope (A, B, Admin)",
         all(r.status_code == 404 for r in denied))
    rag = b.post("/api/rag/ask", {"item_id": copy, "question": f"{term} result?", "mode": "keyword"})
    step("RAG does not take the common copy as a scope (404)", rag.status_code == 404)

    assert admin.post(f"/api/admin/common-copies/{copy}/revoke",
                      {"confirm_revoke": True, "reason_code": "rights_withdrawn"}).status_code == 200
    rows = scalar("""SELECT count(*) FROM index_entries e JOIN index_generations g ON g.id=e.generation_id
        WHERE g.item_id=:i""", i=uuid.UUID(copy))
    after = {who: p.search(f"{term} result") for who, p in people.items()}
    step(f"after revoke (index rows still present: {rows}), no new response returns the copy",
         rows > 0 and all(items_of(after[who]) == allowed[who] - {copy} for who in people)
         and all(p.get(f"/api/search/chunks/{chunk}").status_code == 404 for p in people.values())
         and all(p.get(f"/api/library/items/{copy}/chunks").status_code == 404 for p in people.values())
         and b.search(term, item_id=copy).status_code == 404)
    step("no search/rerank/generation provider usage recorded (keyword only, RAG refused before provider)",
         scalar("SELECT count(*) FROM search_usage") == 0)
    print({"steps": len(RESULTS), "passed": sum(ok for _, ok in RESULTS), "fake_embedding_requests": len(CALLS)})


if __name__ == "__main__":
    main()
