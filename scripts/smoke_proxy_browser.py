"""Proxy smoke: collections, keyword search, source chunks and RAG in headless Chrome.

Runs against an *isolated* Compose project (own volumes/ports, see
docs/dev-setup.md); it refuses a database holding any other account, so the dev
stack's data is never touched (earlier smoke fixtures are allowed). Fixture accounts are inserted into that fresh
database and one synthetic Markdown per User is uploaded through the web
origin; the Compose worker extracts it and this host process indexes it with a
fake embedding (no provider call). Every checked action then runs in Chrome.
Only method/path/status and pass/fail are printed: no cookies, tokens,
passwords, headers, bodies, screenshots or traces.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sys
import time
import traceback
import uuid
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values
from playwright.sync_api import expect, sync_playwright
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
PRICING_MESSAGE = "Hỏi đáp hiện chưa khả dụng vì chi phí và provider chưa được xác minh."
STAGE = "setup"


def setting(env: dict, name: str, default: str) -> str:
    # Compose gives shell variables precedence over .env; mirror that here.
    return os.environ.get(name) or env.get(name) or default


def prepare(env: dict):
    db_port = setting(env, "POSTGRES_HOST_PORT", "5432")
    for key, value in {
        "RAS_DB__HOST": "127.0.0.1", "RAS_DB__PORT": db_port,
        "RAS_DB__USER": env["POSTGRES_USER"], "RAS_DB__PASSWORD": env["POSTGRES_PASSWORD"],
        "RAS_DB__NAME": env["POSTGRES_DB"],
        "RAS_STORAGE__ENDPOINT_URL": f"http://127.0.0.1:{setting(env, 'MINIO_API_HOST_PORT', '9000')}",
        "RAS_STORAGE__ACCESS_KEY": env["MINIO_ROOT_USER"],
        "RAS_STORAGE__SECRET_KEY": env["MINIO_ROOT_PASSWORD"],
        "RAS_STORAGE__BUCKET": "ras-originals",
    }.items():
        os.environ[key] = str(value)
    engine = create_engine(URL.create(
        "postgresql+psycopg", username=env["POSTGRES_USER"], password=env["POSTGRES_PASSWORD"],
        host="127.0.0.1", port=int(db_port), database=env["POSTGRES_DB"]), hide_parameters=True)
    return engine, f"http://127.0.0.1:{setting(env, 'WEB_HOST_PORT', '3000')}"


def fixtures(engine, web: str) -> tuple[dict, dict]:
    from app.config import get_settings
    from app.identity.passwords import hash_password
    from app.ingest import indexer
    from app.ingest.embedding import EmbeddingResult
    from app.ingest.worker import process_job
    from app.storage import OriginalStorage

    with engine.connect() as conn:
        if conn.scalar(text("SELECT count(*) FROM users WHERE email NOT LIKE 'proxy-smoke-%@example.org'")):
            raise SystemExit("refusing to run: database has non-smoke users; start an isolated Compose project")
    marker = uuid.uuid4().hex[:10]
    accounts = {label: (f"proxy-smoke-{label}-{marker}@example.org", secrets.token_urlsafe(24))
                for label in ("a", "b", "admin")}
    with engine.begin() as conn:
        for label, (email, password) in accounts.items():
            conn.execute(text("INSERT INTO users (email, password_verifier, role) VALUES (:e, :p, :r)"),
                         {"e": email, "p": hash_password(password), "r": "admin" if label == "admin" else "user"})

    get_settings.cache_clear()
    settings = get_settings().model_copy(deep=True)
    settings.embedding.api_key = SecretStr("synthetic-fake-key")  # host fake only, never sent
    settings.embedding.pricing_verified = True
    indexer.embed = lambda value, selected: EmbeddingResult([.1, .2, .3], selected.embedding.model_id,
                                                           len(value.split()))
    items = {}
    for label in ("a", "b"):
        with httpx.Client(base_url=web, timeout=30) as client:  # through the web origin and proxy
            client.get("/api/auth/csrf").raise_for_status()
            headers = {"origin": web, "x-csrf-token": client.cookies["ras_csrf"]}
            login = client.post("/api/auth/login", json={"email": accounts[label][0],
                                "password": accounts[label][1]}, headers=headers)
            assert login.status_code == 200, login.status_code
            headers["x-csrf-token"] = client.cookies["ras_csrf"]
            body = (f"## Methods\nSynthetic zircon lattice observation owned by User {label.upper()}.\n"
                    "## References\n[1] Bibliography only.\n").encode()
            notice = client.get("/api/uploads/notice").json()["version"]
            intent = client.post("/api/uploads/intents", json={
                "acknowledged": True, "notice_version": notice, "file_kind": "markdown",
                "filename": f"proxy-smoke-{label}.md", "byte_size": len(body),
                "sha256": hashlib.sha256(body).hexdigest()}, headers=headers)
            assert intent.status_code == 201, intent.status_code
            sent = client.put("/api/uploads/original", content=body, headers={
                **headers, "x-upload-intent": intent.json()["intent_token"],
                "content-type": "application/octet-stream"})
            assert sent.status_code == 201, sent.status_code
            item = sent.json()
            title = f"Proxy smoke paper {label.upper()}"
            assert client.patch(f"/api/library/items/{item['item_id']}", json={"title": title},
                                headers=headers).status_code == 200
            for _ in range(60):
                jobs = client.get(f"/api/library/items/{item['item_id']}").json()["item"]["jobs"]
                if jobs[0]["state"] == "awaiting_index":
                    break
                time.sleep(1)
            else:
                raise AssertionError("worker extraction did not finish")
        assert process_job(uuid.UUID(item["job_id"]), settings, OriginalStorage(settings)) == "ready"
        items[label] = {"id": item["item_id"], "title": title}
    return accounts, items


def login(browser, web: str, account: tuple[str, str]):
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"{web}/login")
    page.get_by_label("Email").fill(account[0])
    page.get_by_label("Mật khẩu").fill(account[1])
    page.get_by_role("button", name="Đăng nhập").click()
    page.wait_for_url(f"{web}/")
    return context, page


def watch(page, log: list) -> None:
    def on_response(response) -> None:
        path = urlparse(response.url).path
        if path.startswith("/api/"):
            log.append((response.request.method, path, response.status))
    page.on("response", on_response)


def fetch(page, method: str, path: str, csrf: bool = True, body: dict | None = None) -> dict:
    # Status and API error code only; the response body is not returned.
    return page.evaluate("""async ([method, path, csrf, body]) => {
        const token = document.cookie.split("; ").find((p) => p.startsWith("ras_csrf="))?.slice(9);
        const headers = { accept: "application/json" };
        if (method !== "GET") { headers["content-type"] = "application/json"; if (csrf) headers["x-csrf-token"] = decodeURIComponent(token); }
        const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
        const data = await res.json().catch(() => null);
        return { status: res.status, error: data && data.error };
    }""", [method, path, csrf, body])


def collection_select(page):
    # The wrapping <label> also names the selected option, so match its prefix.
    return page.locator("label", has_text=re.compile(r"^Collection")).locator("select")


def search(page, web: str, query: str, collection: str | None = None) -> None:
    page.goto(f"{web}/search")
    if collection:
        collection_select(page).select_option(label=collection)
    page.get_by_label("Câu tìm kiếm").fill(query)
    page.get_by_role("button", name="Tìm kiếm", exact=True).click()


def main() -> None:
    global STAGE
    sys.stdout.reconfigure(encoding="utf-8")
    env = dotenv_values(ROOT / ".env")
    engine, web = prepare(env)
    try:
        STAGE = "fixtures"
        accounts, items = fixtures(engine, web)
    finally:
        engine.dispose()
    a_item, b_item = items["a"], items["b"]
    results: list[str] = []

    def ok(name: str) -> None:
        results.append(name)
        print(f"PASS {len(results):2d}. {name}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        try:
            STAGE = "User A login"
            ctx_a, page_a = login(browser, web, accounts["a"])
            log_a: list = []
            watch(page_a, log_a)
            page_a.on("dialog", lambda dialog: dialog.accept())

            STAGE = "A manages collections"
            page_a.goto(f"{web}/collections")
            name_box = page_a.get_by_label("Tên collection mới")
            name_box.fill("Proxy smoke temp")
            page_a.get_by_role("button", name="Tạo", exact=True).click()
            expect(collection_select(page_a)).to_have_value(re.compile(r"^[0-9a-f-]{36}$"))
            temp_id = collection_select(page_a).input_value()
            page_a.get_by_role("button", name="Xóa collection").click()
            expect(collection_select(page_a).locator("option", has_text="Proxy smoke temp")).to_have_count(0)
            name_box.fill("Proxy smoke A draft")
            page_a.get_by_role("button", name="Tạo", exact=True).click()
            select = collection_select(page_a)
            expect(select.locator("option", has_text="Proxy smoke A draft")).to_have_count(1)
            collection_id = select.input_value()
            name_box.fill("Proxy smoke A")
            page_a.get_by_role("button", name="Đổi tên theo ô tên").click()
            expect(select.locator("option", has_text="Proxy smoke A draft")).to_have_count(0)
            expect(select.locator("option", has_text="Proxy smoke A")).to_have_count(1)
            add_box = page_a.get_by_label("Paper từ thư viện")
            add_box.fill(a_item["id"])
            page_a.get_by_role("button", name="Thêm paper").click()
            expect(page_a.get_by_role("link", name=a_item["title"])).to_be_visible()
            expect(page_a.get_by_text("1 paper")).to_be_visible()
            page_a.get_by_role("button", name="Bỏ khỏi collection").click()
            expect(page_a.get_by_text("0 paper")).to_be_visible()
            add_box.fill(a_item["id"])
            page_a.get_by_role("button", name="Thêm paper").click()
            expect(page_a.get_by_text("1 paper")).to_be_visible()
            add_box.fill(b_item["id"])  # another User's item: API answers 404 like a missing ID
            page_a.get_by_role("button", name="Thêm paper").click()
            expect(page_a.get_by_role("alert").filter(has_text="Không tìm thấy.")).to_be_visible()
            wanted = {("GET", "/api/collections", 200), ("POST", "/api/collections", 201),
                      ("DELETE", f"/api/collections/{temp_id}", 204),
                      ("PATCH", f"/api/collections/{collection_id}", 200),
                      ("GET", f"/api/collections/{collection_id}/items", 200),
                      ("POST", f"/api/collections/{collection_id}/items", 201),
                      ("DELETE", f"/api/collections/{collection_id}/items/{a_item['id']}", 204),
                      ("POST", f"/api/collections/{collection_id}/items", 404)}
            assert wanted <= set(log_a), "collection calls"
            ok("A creates, deletes, renames a collection and adds/removes/re-adds own paper; B's paper → 404")

            STAGE = "A keyword search"
            search(page_a, web, "zircon lattice observation")
            expect(page_a.get_by_text("Chế độ: chỉ từ khóa · Rerank: not_requested · 1 đoạn")).to_be_visible()
            expect(page_a.get_by_role("link", name=a_item["title"])).to_be_visible()
            expect(page_a.get_by_text(b_item["title"])).to_have_count(0)
            expect(page_a.get_by_text("owned by User B")).to_have_count(0)
            ok("A keyword search returns only A's chunk (POST /api/search 200)")

            STAGE = "A opens source chunk"
            page_a.get_by_role("link", name="Mở đúng chunk và vị trí nguồn").click()
            page_a.wait_for_url(f"{web}/sources/**")
            a_chunk = page_a.url.rsplit("/", 1)[1]
            expect(page_a.get_by_role("heading", name=a_item["title"])).to_be_visible()
            expect(page_a.get_by_text("owned by User A")).to_be_visible()
            assert ("GET", f"/api/search/chunks/{a_chunk}", 200) in log_a
            ok("A opens the source chunk (GET /api/search/chunks/{id} 200)")

            STAGE = "A collection-scoped search"
            search(page_a, web, "zircon lattice observation", collection="Proxy smoke A")
            expect(page_a.get_by_text("1 đoạn")).to_be_visible()
            ok("A search scoped to the collection returns A's chunk")

            STAGE = "A RAG reaches the API pricing gate"
            for scope in ("item", "collection", "all"):
                page_a.goto(f"{web}/items/{a_item['id']}")
                if scope == "collection":
                    page_a.get_by_label("Phạm vi").select_option(label="Collection: Proxy smoke A")
                elif scope == "all":
                    page_a.get_by_label("Phạm vi").select_option(value="all")
                page_a.get_by_label("Câu hỏi").fill("What zircon lattice observation is reported?")
                count = len(log_a)
                page_a.get_by_role("button", name="Hỏi", exact=True).click()
                expect(page_a.get_by_role("alert").filter(has_text=PRICING_MESSAGE)).to_be_visible()
                assert ("POST", "/api/rag/ask", 503) in log_a[count:], scope
            ok("A RAG item/collection/all: POST /api/rag/ask 503 from API, UI shows pricing message")

            STAGE = "A proxy guards"
            assert fetch(page_a, "POST", "/api/collections", csrf=False, body={"name": "x"}) == \
                {"status": 403, "error": "csrf_failed"}
            assert fetch(page_a, "POST", "/api/rag/ask", csrf=False,
                         body={"scope": "all", "question": "Q"}) == {"status": 403, "error": "csrf_failed"}
            assert fetch(page_a, "GET", "/api/readyz") == {"status": 404, "error": "not_found"}
            assert fetch(page_a, "GET", "/api/docs") == {"status": 404, "error": "not_found"}
            ok("Missing CSRF → API 403; API-only /readyz and /docs → proxy 404")

            STAGE = "User B"
            ctx_b, page_b = login(browser, web, accounts["b"])
            log_b: list = []
            watch(page_b, log_b)
            search(page_b, web, "zircon lattice observation")
            expect(page_b.get_by_text("1 đoạn")).to_be_visible()
            expect(page_b.get_by_role("link", name=b_item["title"])).to_be_visible()
            expect(page_b.get_by_text(a_item["title"])).to_have_count(0)
            expect(page_b.get_by_text("owned by User A")).to_have_count(0)
            page_b.get_by_role("link", name="Mở đúng chunk và vị trí nguồn").click()
            expect(page_b.get_by_text("owned by User B")).to_be_visible()
            page_b.goto(f"{web}/sources/{a_chunk}")
            expect(page_b.get_by_role("alert").filter(has_text="Không tìm thấy.")).to_be_visible()
            expect(page_b.get_by_text("owned by User A")).to_have_count(0)
            assert ("GET", f"/api/search/chunks/{a_chunk}", 404) in log_b
            page_b.goto(f"{web}/collections")
            expect(collection_select(page_b).locator("option", has_text="Proxy smoke A")).to_have_count(0)
            assert fetch(page_b, "GET", f"/api/collections/{collection_id}/items")["status"] == 404
            assert fetch(page_b, "PATCH", f"/api/collections/{collection_id}", body={"name": "x"})["status"] == 404
            assert fetch(page_b, "POST", "/api/rag/ask", body={"item_id": a_item["id"], "question": "Q"})["status"] == 404
            assert fetch(page_b, "POST", "/api/rag/ask", body={"collection_id": collection_id, "question": "Q"})["status"] == 404
            ok("B sees only B's chunk; A's chunk, collection and RAG scopes → 404")

            STAGE = "Admin"
            ctx_admin, page_admin = login(browser, web, accounts["admin"])
            search(page_admin, web, "zircon lattice observation")
            expect(page_admin.get_by_text("0 đoạn")).to_be_visible()
            expect(page_admin.get_by_text("owned by User")).to_have_count(0)
            page_admin.goto(f"{web}/sources/{a_chunk}")
            expect(page_admin.get_by_role("alert").filter(has_text="Không tìm thấy.")).to_be_visible()
            page_admin.goto(f"{web}/items/{a_item['id']}")
            expect(page_admin.get_by_role("alert").filter(has_text="Không tìm thấy.")).to_be_visible()
            assert fetch(page_admin, "GET", f"/api/collections/{collection_id}/items")["status"] == 404
            assert fetch(page_admin, "POST", "/api/rag/ask", body={"item_id": a_item["id"], "question": "Q"})["status"] == 404
            ok("Admin: search 0 hits; A's chunk, item, collection and RAG scope → 404")

            proxied = [entry for log in (log_a, log_b) for entry in log
                       if entry[1].split("/")[2] in ("search", "collections", "rag")]
            print(f"Chrome {browser.version}; Playwright {version('playwright')}; "
                  f"{len(results)}/{len(results)} passed; {len(proxied)} search/collections/rag responses via proxy; "
                  "external provider calls: 0")
            for context in (ctx_a, ctx_b, ctx_admin):
                context.close()
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        # Suppress detail: Playwright errors can quote DOM text or request data.
        line = next((f.lineno for f in reversed(traceback.extract_tb(exc.__traceback__))
                     if f.filename == __file__), None)
        print(f"proxy smoke failed at {STAGE} (line {line}): {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
