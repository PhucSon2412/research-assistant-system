"""M6b-2b3b smoke: cited RAG over a common copy, in headless Chrome, on an *isolated* Compose project.

Two web origins of the same isolated project (see docs/dev-setup.md):

- ``WEB`` (``web`` -> Compose ``api``): the stack as shipped, no key and
  ``pricing_verified=false``. The common item page shows the RAG form (API
  ``rag_available``) and asking stops at the unchanged pricing gate (503).
- ``HOST_WEB`` (``web-host-api`` from ``scripts/compose.smoke-host-api.yml``):
  its proxy talks to an API process this script runs on the host with a *fake*
  generation (in-memory settings copy; any non-loopback HTTP is refused), so an
  answer, its citations and a revoke during generation can be exercised.

User A and User B upload synthetic Markdown through the web (B one file with the
*same bytes* as A's); the Compose worker extracts, the host indexes with a fake
embedding. A's file becomes a common copy whose rights permit full-text search and
provider processing but **not** download. Reads only ``POSTGRES_*``/``MINIO_ROOT_*``
from ``.env`` (never the OpenRouter key), refuses a database holding any account
it did not create, prints step names and counts only.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import threading
import time
import traceback
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
ALLOWED = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD")
PREFIX = "m6b2b3b-smoke-"
PRICING_MESSAGE = "Hỏi đáp hiện chưa khả dụng vì chi phí và provider chưa được xác minh."
SCOPE_CHANGED = "Quyền hoặc phạm vi của nguồn đã thay đổi trong lúc xử lý; câu trả lời không được hiển thị."
STAGE = "setup"


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
    ("POSTGRES_HOST_PORT", "5432"), ("WEB_HOST_PORT", "3000"), ("MINIO_API_HOST_PORT", "9000"),
    ("SMOKE_HOST_API_PORT", "58100"), ("SMOKE_HOST_WEB_PORT", "53100"))}
WEB = f"http://127.0.0.1:{PORTS['WEB_HOST_PORT']}"
HOST_WEB = f"http://127.0.0.1:{PORTS['SMOKE_HOST_WEB_PORT']}"
for name, value in {
    "RAS_DB__HOST": "127.0.0.1", "RAS_DB__PORT": PORTS["POSTGRES_HOST_PORT"], "RAS_DB__USER": ENV["POSTGRES_USER"],
    "RAS_DB__PASSWORD": ENV["POSTGRES_PASSWORD"], "RAS_DB__NAME": ENV["POSTGRES_DB"],
    "RAS_STORAGE__ENDPOINT_URL": f"http://127.0.0.1:{PORTS['MINIO_API_HOST_PORT']}",
    "RAS_STORAGE__ACCESS_KEY": ENV["MINIO_ROOT_USER"], "RAS_STORAGE__SECRET_KEY": ENV["MINIO_ROOT_PASSWORD"],
    "RAS_STORAGE__BUCKET": "ras-originals", "RAS_EMBEDDING__API_KEY": "", "OPENROUTER_API_KEY": "",
    # The host API behind HOST_WEB: development cookies/outbox like the Compose api, for that origin only.
    "RAS_ENVIRONMENT": "development", "RAS_HTTP__COOKIE_SECURE": "false", "RAS_HTTP__PUBLIC_BASE_URL": HOST_WEB,
    "RAS_HTTP__ALLOWED_ORIGINS": json.dumps([HOST_WEB]), "RAS_DELIVERY__MODE": "dev_outbox",
    "RAS_DISCOVERY__ENABLED": "false",
}.items():
    os.environ[name] = str(value)

_real_transport = httpx.HTTPTransport.handle_request


def _loopback_only(self, request):
    # The fakes below replace every provider adapter; anything else leaving the machine is a smoke bug.
    if request.url.host not in ("127.0.0.1", "localhost"):
        raise AssertionError("non-loopback HTTP attempted during the smoke")
    return _real_transport(self, request)


httpx.HTTPTransport.handle_request = _loopback_only

import uvicorn  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from app import rag as rag_module  # noqa: E402
from app import search as search_module  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.identity.passwords import hash_password  # noqa: E402
from app.ingest import indexer  # noqa: E402
from app.ingest.embedding import EmbeddingResult  # noqa: E402
from app.ingest.worker import process_job  # noqa: E402
from app.main import app  # noqa: E402
from app.rag import GenerationResult  # noqa: E402
from app.storage import OriginalStorage  # noqa: E402

get_settings.cache_clear()
BASE = get_settings()
assert BASE.embedding.api_key is None or not BASE.embedding.api_key.get_secret_value()
assert not BASE.generation.pricing_verified
FAKE = BASE.model_copy(deep=True)
FAKE.embedding.api_key = SecretStr("synthetic-fake-key")  # fakes below; nothing reaches a provider
FAKE.embedding.pricing_verified = True
FAKE.generation.pricing_verified = True
ENGINE = create_engine(URL.create("postgresql+psycopg", username=ENV["POSTGRES_USER"],
                                  password=ENV["POSTGRES_PASSWORD"], host="127.0.0.1",
                                  port=int(PORTS["POSTGRES_HOST_PORT"]), database=ENV["POSTGRES_DB"]),
                       hide_parameters=True)
SCOPE = {"audience": "instance_users", "original_download": False, "fulltext_search": True,
         "external_processing": True, "conditions": None}
RESULTS: list[str] = []
EMBEDDED: list[str] = []
PAYLOADS: list[tuple[str, list[dict]]] = []
DURING_GENERATION: list = []


def ok(name: str) -> None:
    RESULTS.append(name)
    print(f"PASS {len(RESULTS):2d}. {name}")


def scalar(sql: str, **params):
    with ENGINE.connect() as conn:
        return conn.scalar(text(sql), params)


def fake_embed(value: str, selected):
    assert "References" not in value and "[1]" not in value
    EMBEDDED.append(value)
    return EmbeddingResult([0.1, 0.2, 0.3], selected.embedding.model_id, len(value.split()))


def fake_generation(messages, settings):
    """Cites every evidence chunk; ``DURING_GENERATION`` actions run while the "provider" holds the payload."""
    payload = json.loads(messages[1]["content"])
    PAYLOADS.append((payload["question"], payload["evidence"]))
    while DURING_GENERATION:
        DURING_GENERATION.pop(0)()
    claims = [{"text": "The result is reported.", "citations": [
        {"chunk_id": row["chunk_id"], "quote": row["text"][:24]} for row in payload["evidence"]]}]
    return GenerationResult(json.dumps({"claims": claims}), 120, 30, settings.generation.model_id, "SmokeFake")


class Person:
    """A signed-in account talking only to one web origin (``/api`` proxy)."""

    def __init__(self, origin: str, email: str, password: str):
        self.origin = origin
        self.client = httpx.Client(base_url=origin, timeout=60)
        assert self.client.get("/api/auth/csrf").status_code == 200
        response = self.client.post("/api/auth/login", json={"email": email, "password": password},
                                    headers=self.headers())
        assert response.status_code == 200, response.status_code

    def headers(self) -> dict:
        return {"origin": self.origin, "x-csrf-token": self.client.cookies.get("ras_csrf", "")}

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


def chunk_ids(item: str) -> set[str]:
    with ENGINE.connect() as conn:
        return {str(c) for c in conn.scalars(text("""SELECT e.chunk_id FROM index_entries e
            JOIN index_generations g ON g.id=e.generation_id WHERE g.item_id=:i"""), {"i": uuid.UUID(item)})}


def ledger() -> list[tuple]:
    with ENGINE.connect() as conn:
        return [tuple(r) for r in conn.execute(text("""SELECT u.email, s.status FROM search_usage s
            JOIN users u ON u.id=s.user_id WHERE s.task_type='generation' ORDER BY s.created_at"""))]


def embedding_ledger() -> tuple:
    with ENGINE.connect() as conn:
        return tuple(conn.execute(text("""SELECT count(*), coalesce(sum(coalesce(cost_usd, reserved_usd)), 0)
            FROM embedding_calls""")).one())


def start_host_api() -> uvicorn.Server:
    app.dependency_overrides[get_settings] = lambda: FAKE
    rag_module.generate = fake_generation
    search_module.embed = lambda value, cfg, input_type: EmbeddingResult([0.1, 0.2, 0.3], cfg.embedding.model_id, 2)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=int(PORTS["SMOKE_HOST_API_PORT"]),
                                           log_level="warning", access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "host API did not start"
        time.sleep(0.1)
    return server


def login(browser, web: str, account: tuple[str, str]):
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"{web}/login")
    page.get_by_label("Email").fill(account[0])
    page.get_by_label("Mật khẩu").fill(account[1])
    page.get_by_role("button", name="Đăng nhập").click()
    page.wait_for_url(f"{web}/")
    log: list = []

    def on_response(response) -> None:
        path = urlparse(response.url).path
        if path.startswith("/api/"):
            log.append((response.request.method, path, response.status))
    page.on("response", on_response)
    return context, page, log


def ask_in_page(page, web: str, item: str, question: str, scope: str | None = None) -> None:
    page.goto(f"{web}/items/{item}")
    expect(page.get_by_role("heading", name="Hỏi đáp có dẫn nguồn")).to_be_visible()
    if scope:
        page.get_by_label("Phạm vi").select_option(label=scope)
    page.get_by_label("Câu hỏi").fill(question)
    page.get_by_role("button", name="Hỏi", exact=True).click()


def main() -> None:
    global STAGE
    sys.stdout.reconfigure(encoding="utf-8")
    STAGE = "fixtures"
    others = scalar("SELECT count(*) FROM users WHERE email NOT LIKE :p", p=f"{PREFIX}%")
    if others:
        raise SystemExit("refusing to run: database has non-smoke users; start an isolated Compose project")
    run = uuid.uuid4().hex[:8]
    password = secrets.token_urlsafe(24)
    accounts = {who: (f"{PREFIX}{run}-{who}@example.org", password) for who in ("admin", "a", "b")}
    with ENGINE.begin() as conn:
        for who, (email, secret) in accounts.items():
            conn.execute(text("INSERT INTO users(email, password_verifier, role) VALUES (:e, :p, :r)"),
                         {"e": email, "p": hash_password(secret), "r": "admin" if who == "admin" else "user"})
    admin, a, b = (Person(WEB, *accounts[who]) for who in ("admin", "a", "b"))
    indexer.embed = fake_embed
    storage = OriginalStorage(FAKE)
    term = f"Smoke{run}"
    main_md = (f"# Smoke {run}\n\n## Abstract\n{term} abstract of the shared method.\n\n## Methods\n"
               f"{term} result is nine units.\n\n## References\n[1] A. Alpha. Refcanary{run} Study. 2026.\n").encode()
    a_main = indexed(a, storage, main_md)
    a_only = indexed(a, storage, f"## Methods\n{term} result Asecret{run} private body.\n".encode())
    b_same = indexed(b, storage, main_md)  # same bytes and checksum as A's file and the common copy
    b_only = indexed(b, storage, f"## Methods\n{term} result Bsecret{run} private body.\n".encode())
    rid = a.post("/api/review-requests", {"acknowledged": True, "item_id": a_main["item_id"],
                                          "version_id": a_main["version_id"], "source_file_id": a_main["file_id"],
                                          "notice_version": a.get("/api/review-requests/notice").json()["version"],
                                          "declared_source": "Synthetic smoke file", "declared_rights": "Smoke"})
    assert rid.status_code == 201, rid.status_code
    checksum = hashlib.sha256(main_md).hexdigest()
    title = f"Smoke common {run}"
    approved = admin.post(f"/api/admin/review-requests/{rid.json()['request']['id']}/approve", {
        "confirm_verified": True, "file_checksum_sha256": checksum, "title": title,
        "verified_source": "Smoke Admin check: synthetic file by the smoke script",
        "evidence_note": "Smoke Admin check: synthetic permission for this checksum",
        "checked_on": datetime.now(UTC).date().isoformat(), "scope": SCOPE})
    assert approved.status_code == 200, approved.status_code
    copy = approved.json()["request"]["pending_publication_id"]
    assert admin.post(f"/api/admin/common-copies/{copy}/publish",
                      {"confirm_publish": True, "file_checksum_sha256": checksum}).status_code == 200
    job = uuid.UUID(admin.post(f"/api/admin/common-copies/{copy}/index",
                               {"confirm_external_processing": True}).json()["job"]["id"])
    wait_state(job, "awaiting_index")
    assert process_job(job, FAKE, storage) == "ready"
    status = admin.get(f"/api/admin/common-copies/{copy}").json()["item"]["index"]
    assert (status["search_open"], status["rag_open"]) == (True, True), status
    ok("A/B private Markdown indexed via web upload; common copy (search + provider, no download) published and "
       "indexed; Admin page: search and RAG open")
    chunks = {"a": chunk_ids(a_main["item_id"]) | chunk_ids(a_only["item_id"]) | chunk_ids(copy),
              "b": chunk_ids(b_same["item_id"]) | chunk_ids(b_only["item_id"]) | chunk_ids(copy),
              "admin": chunk_ids(copy)}
    common_file = str(scalar("SELECT id FROM source_files WHERE item_id=:i", i=uuid.UUID(copy)))
    collection = a.post("/api/collections", {"name": f"RAG smoke {run}"}).json()["collection"]["id"]
    for item in (copy, a_only["item_id"]):
        assert a.post(f"/api/collections/{collection}/items", {"item_id": item}).status_code == 201
    embedding_before = embedding_ledger()
    ledger_before = len(ledger())  # a rerun on the same isolated project keeps earlier runs' rows

    server = start_host_api()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True)
            try:
                STAGE = "shipped stack: RAG form and pricing gate"
                ctx, page, log = login(browser, WEB, accounts["b"])
                page.goto(f"{WEB}/items/{copy}")
                expect(page.get_by_text("Hỏi đáp (RAG) dùng được đoạn của bản này làm bằng chứng")).to_be_visible()
                expect(page.get_by_text("Bản chung: tìm kiếm toàn văn và hỏi đáp")).to_be_visible()
                expect(page.get_by_text("bản chung mà quyền Admin đã ghi cho phép tìm kiếm toàn văn và xử lý qua "
                                        "provider", exact=False)).to_be_visible()
                expect(page.get_by_text("chưa dùng bản chung")).to_have_count(0)
                ask_in_page(page, WEB, copy, f"{term} result?")
                expect(page.get_by_role("alert").filter(has_text=PRICING_MESSAGE)).to_be_visible()
                assert ("POST", "/api/rag/ask", 503) in log and len(ledger()) == ledger_before
                ctx.close()
                ok("Compose web -> Compose api (pricing unverified): common item shows the RAG form and rights "
                   "note; asking stops at the unchanged pricing gate (503, no generation ledger row)")

                STAGE = "host API: item scope"
                contexts = {who: login(browser, HOST_WEB, accounts[who]) for who in ("a", "b", "admin")}
                for who, (_, page, log) in contexts.items():
                    count = len(PAYLOADS)
                    ask_in_page(page, HOST_WEB, copy, f"{term} result?")
                    expect(page.get_by_text("Trạng thái: Có câu trả lời.")).to_be_visible()
                    expect(page.get_by_role("link", name=f"[{title} · dòng", exact=False).first).to_be_visible()
                    expect(page.get_by_text("Bản chung · Version", exact=False).first).to_be_visible()
                    # Search/RAG rights do not open the original: no download link on the common citation.
                    expect(page.get_by_role("link", name="file gốc")).to_have_count(0)
                    assert ("POST", "/api/rag/ask", 200) in log, who
                    [(_, evidence)] = PAYLOADS[count:]
                    assert {row["item_id"] for row in evidence} == {copy}, who
                    page.get_by_role("link", name=f"[{title} · dòng", exact=False).first.click()
                    page.wait_for_url(f"{HOST_WEB}/sources/**")
                    expect(page.get_by_role("heading", name=title)).to_be_visible()
                    expect(page.get_by_text("Không có file gốc được phép tải.")).to_be_visible()
                    assert any(m == "GET" and p.startswith("/api/search/chunks/") and s == 200 for m, p, s in log), who
                    assert page.request.get(f"{HOST_WEB}/api/files/{common_file}/original").status == 404, who
                ok("host fake generation: A, B and the Admin ask the common copy (item scope); citation labelled "
                   "'Bản chung', opens /sources/{chunk} (200) with no original link; original route 404")

                STAGE = "host API: all and collection scopes"
                allowed = {"a": {a_main["item_id"], a_only["item_id"], copy},
                           "b": {b_same["item_id"], b_only["item_id"], copy}, "admin": {copy}}
                for who, (_, page, log) in contexts.items():
                    count = len(PAYLOADS)
                    ask_in_page(page, HOST_WEB, copy, f"{term} result?",
                                scope="Tất cả paper riêng đã index của tôi và bản chung được phép hỏi đáp")
                    expect(page.get_by_text("Trạng thái: Có câu trả lời.")).to_be_visible()
                    [(_, evidence)] = PAYLOADS[count:]
                    assert {row["item_id"] for row in evidence} == allowed[who], who
                    assert {row["chunk_id"] for row in evidence} <= chunks[who], who
                    other = {"a": f"Bsecret{run}", "b": f"Asecret{run}", "admin": f"secret{run}"}[who]
                    # The item page lists the copy's references (labelled, not evidence); the answer must not cite them.
                    answer = page.locator("[aria-live=polite]").inner_text()
                    assert other not in json.dumps(evidence) and other not in page.content(), who
                    assert f"Refcanary{run}" not in json.dumps(evidence) and f"Refcanary{run}" not in answer, who
                    if who != "admin":
                        expect(page.get_by_text("Paper riêng của bạn · Version", exact=False).first).to_be_visible()
                        expect(page.get_by_role("link", name="file gốc").first).to_be_visible()  # own private file
                _, page_a, _ = contexts["a"]
                count = len(PAYLOADS)
                ask_in_page(page_a, HOST_WEB, copy, f"{term} result?", scope=f"Collection: RAG smoke {run}")
                expect(page_a.get_by_text("Trạng thái: Có câu trả lời.")).to_be_visible()
                expect(page_a.get_by_text("(chỉ các paper được phép trong đó)", exact=False)).to_be_visible()
                [(_, evidence)] = PAYLOADS[count:]
                assert {row["item_id"] for row in evidence} == {copy, a_only["item_id"]}
                ok("all scope: each account's evidence is its own private papers + the common copy only (B's file "
                   "with the same bytes never reaches A or the Admin); A's collection scope: copy + A's paper")

                STAGE = "host API: ledger"
                rows = ledger()[ledger_before:]
                assert all(status == "completed" for _, status in rows) and len(rows) == 7
                assert {email for email, _ in rows} == {email for email, _ in accounts.values()}
                assert embedding_ledger() == embedding_before
                ok("7 generation ledger rows, each charged to the asking account and settled once; document "
                   "embedding ledger unchanged by RAG")

                STAGE = "revoke during generation"
                _, page_b, log_b = contexts["b"]
                DURING_GENERATION.append(lambda: admin.post(
                    f"/api/admin/common-copies/{copy}/revoke", {"confirm_revoke": True, "reason_code": "rights_withdrawn"}))
                count = len(PAYLOADS)
                ask_in_page(page_b, HOST_WEB, copy, f"{term} result?")
                expect(page_b.get_by_role("alert").filter(has_text=SCOPE_CHANGED)).to_be_visible()
                assert ("POST", "/api/rag/ask", 409) in log_b
                [(_, evidence)] = PAYLOADS[count:]
                assert copy in {row["item_id"] for row in evidence}  # sent before t_rev: a stated limit
                expect(page_b.get_by_text("Trạng thái:")).to_have_count(0)
                rows_left = scalar("""SELECT count(*) FROM index_entries e JOIN index_generations g
                    ON g.id=e.generation_id WHERE g.item_id=:i""", i=uuid.UUID(copy))
                ok(f"revoke committed while the fake provider held the payload: 409 rag_scope_changed, UI shows no "
                   f"answer (payload sent before t_rev cannot be recalled; index rows still present: {rows_left})")

                STAGE = "after t_rev"
                page_b.goto(f"{HOST_WEB}/items/{copy}")
                expect(page_b.get_by_role("alert").filter(has_text="Không tìm thấy.")).to_be_visible()
                for who, (_, page, _) in contexts.items():
                    status = page.request.post(f"{HOST_WEB}/api/rag/ask", headers={
                        "x-csrf-token": unquote(next(c["value"] for c in page.context.cookies()
                                                     if c["name"] == "ras_csrf")),
                        "origin": HOST_WEB}, data={"item_id": copy, "question": f"{term} result?", "mode": "keyword"})
                    assert status.status == 404, who
                count = len(PAYLOADS)
                ask_in_page(page_b, HOST_WEB, b_only["item_id"], f"{term} result?",
                            scope="Tất cả paper riêng đã index của tôi và bản chung được phép hỏi đáp")
                expect(page_b.get_by_text("Trạng thái: Có câu trả lời.")).to_be_visible()
                expect(page_b.get_by_text("Bản chung · Version", exact=False)).to_have_count(0)
                [(_, evidence)] = PAYLOADS[count:]
                assert copy not in {row["item_id"] for row in evidence}
                ok("after t_rev: the copy's item page and RAG item scope are 404 for all three; B's all-scope "
                   "answer has no common evidence")
                print(f"Chrome {browser.version}; Playwright {version('playwright')}; {len(RESULTS)}/{len(RESULTS)} "
                      f"passed; fake generation payloads: {len(PAYLOADS)}; fake embedding requests: {len(EMBEDDED)}; "
                      "external provider calls: 0")
                for context, _, _ in contexts.values():
                    context.close()
            finally:
                browser.close()
    finally:
        server.should_exit = True


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        # Suppress detail: Playwright errors can quote DOM text or request data.
        line = next((f.lineno for f in reversed(traceback.extract_tb(exc.__traceback__))
                     if f.filename == __file__), None)
        print(f"RAG smoke failed at {STAGE} (line {line}): {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
