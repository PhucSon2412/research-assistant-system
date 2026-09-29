"""M6a review-request smoke in headless Chrome: submit → Admin opens the exact
original → reject / withdraw → the old download link is refused.

Runs against an *isolated* Compose project (own volumes/ports, see
docs/dev-setup.md); it refuses a database holding any non-fixture account, so
the dev stack's data is never touched. Fixture accounts are inserted into that
fresh database and one synthetic Markdown per User is uploaded through the web
origin; the Compose worker extracts it and this host process indexes A's with
a fake embedding (no provider call). Every checked action then runs in Chrome.
Only method/path/status and pass/fail are printed: no cookies, tokens,
passwords, headers, bodies, screenshots or traces; the downloaded file is only
hashed and deleted with its browser context.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_proxy_browser import fetch, login, prepare, search, watch  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
PREFIX = "review-smoke-"
STAGE = "setup"


def fixtures(engine, web: str) -> tuple[dict, dict]:
    from app.config import get_settings
    from app.identity.passwords import hash_password
    from app.ingest import indexer
    from app.ingest.embedding import EmbeddingResult
    from app.ingest.worker import process_job
    from app.storage import OriginalStorage

    with engine.connect() as conn:
        if conn.scalar(text("SELECT count(*) FROM users WHERE email NOT LIKE :p"), {"p": f"{PREFIX}%@example.org"}):
            raise SystemExit("refusing to run: database has non-smoke users; start an isolated Compose project")
    marker = uuid.uuid4().hex[:10]
    accounts = {label: (f"{PREFIX}{label}-{marker}@example.org", secrets.token_urlsafe(24))
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
            assert client.post("/api/auth/login", json={"email": accounts[label][0],
                               "password": accounts[label][1]}, headers=headers).status_code == 200
            headers["x-csrf-token"] = client.cookies["ras_csrf"]
            body = f"## Methods\nSynthetic basalt review marker owned by User {label.upper()}.\n".encode()
            notice = client.get("/api/uploads/notice").json()["version"]
            intent = client.post("/api/uploads/intents", json={
                "acknowledged": True, "notice_version": notice, "file_kind": "markdown",
                "filename": f"{PREFIX}{label}.md", "byte_size": len(body),
                "sha256": hashlib.sha256(body).hexdigest()}, headers=headers)
            assert intent.status_code == 201, intent.status_code
            sent = client.put("/api/uploads/original", content=body, headers={
                **headers, "x-upload-intent": intent.json()["intent_token"],
                "content-type": "application/octet-stream"})
            assert sent.status_code == 201, sent.status_code
            item = sent.json()
            title = f"Review smoke paper {label.upper()}"
            assert client.patch(f"/api/library/items/{item['item_id']}", json={"title": title},
                                headers=headers).status_code == 200
            for _ in range(60):
                jobs = client.get(f"/api/library/items/{item['item_id']}").json()["item"]["jobs"]
                if jobs[0]["state"] == "awaiting_index":
                    break
                time.sleep(1)
            else:
                raise AssertionError("worker extraction did not finish")
        if label == "a":
            assert process_job(uuid.UUID(item["job_id"]), settings, OriginalStorage(settings)) == "ready"
        items[label] = {"id": item["item_id"], "file_id": item["file_id"], "version_id": item["version_id"],
                        "title": title, "sha256": hashlib.sha256(body).hexdigest()}
    with engine.connect() as conn:
        items["a"]["chunk_id"] = str(conn.scalar(text("SELECT id FROM chunks WHERE item_id=:i LIMIT 1"),
                                                 {"i": uuid.UUID(items["a"]["id"])}))
    return accounts, items


def submit_in_ui(page, web: str, item: dict) -> None:
    page.goto(f"{web}/items/{item['id']}")
    section = page.locator("section", has=page.get_by_role("heading", name=re.compile("Đề nghị đưa vào kho chung")))
    expect(section.get_by_text("chưa phải là công bố").first).to_be_visible()
    button = section.get_by_role("button", name="Gửi yêu cầu duyệt")
    section.get_by_label("Version/file gửi duyệt").select_option(value=item["file_id"])
    section.get_by_label("Nguồn của file").fill("Synthetic Markdown written for the smoke test")
    section.get_by_label("Quyền phân phối bạn biết").fill("Synthetic claim by the requester; unverified")
    checkbox = section.get_by_role("checkbox")
    expect(checkbox).not_to_be_checked()  # consent is never pre-ticked
    expect(button).to_be_disabled()
    checkbox.check()
    button.click()
    expect(section.get_by_role("status").filter(has_text="đây chưa phải là công bố")).to_be_visible()
    expect(checkbox).not_to_be_checked()


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
            STAGE = "A submits"
            ctx_a, page_a = login(browser, web, accounts["a"])
            log_a: list = []
            watch(page_a, log_a)
            submit_in_ui(page_a, web, a_item)
            assert ("POST", "/api/review-requests", 201) in log_a
            open_row = page_a.get_by_role("listitem").filter(has_text="Đang chờ duyệt (chưa công bố)")
            expect(open_row).to_have_count(1)
            listed = page_a.evaluate("() => fetch('/api/review-requests').then((r) => r.json())")
            rid = listed["requests"][0]["id"]
            ok("A submits exactly one version/file after ticking the unticked box; UI says it is not publication")

            STAGE = "B is isolated"
            ctx_b, page_b = login(browser, web, accounts["b"])
            assert page_b.evaluate("() => fetch('/api/review-requests').then((r) => r.json())")["total"] == 0
            assert fetch(page_b, "GET", f"/api/review-requests/{rid}") == {"status": 404, "error": "not_found"}
            assert fetch(page_b, "POST", f"/api/review-requests/{rid}/withdraw") == {
                "status": 404, "error": "not_found"}
            notice = page_b.evaluate("() => fetch('/api/review-requests/notice').then((r) => r.json())")["version"]
            assert fetch(page_b, "POST", "/api/review-requests", body={
                "acknowledged": True, "notice_version": notice, "item_id": a_item["id"],
                "version_id": a_item["version_id"], "source_file_id": a_item["file_id"],
                "declared_source": "x", "declared_rights": "x"}) == {"status": 404, "error": "not_found"}
            assert fetch(page_b, "GET", "/api/admin/review-requests")["status"] == 403
            assert fetch(page_b, "GET", f"/api/admin/review-requests/{rid}/files/{a_item['file_id']}/original")[
                "status"] == 403
            ok("B: no list entry, request ID/withdraw → 404, Admin routes → 403")

            STAGE = "Admin opens the exact original"
            ctx_admin, page_admin = login(browser, web, accounts["admin"])
            log_admin: list = []
            watch(page_admin, log_admin)
            page_admin.goto(f"{web}/admin")
            page_admin.get_by_role("link", name="Yêu cầu duyệt paper riêng").click()
            page_admin.get_by_role("link", name=a_item["title"]).click()
            page_admin.wait_for_url(f"{web}/admin/reviews/{rid}")
            expect(page_admin.get_by_text(a_item["sha256"])).to_be_visible()
            expect(page_admin.get_by_text("chưa được Admin xác minh").first).to_be_visible()
            expect(page_admin.get_by_text("chưa phải là công bố").first).to_be_visible()
            expect(page_admin.get_by_role("button", name=re.compile("phê duyệt|công bố", re.I))).to_have_count(0)
            with page_admin.expect_download() as info:
                page_admin.get_by_role("link", name="Tải file gốc để duyệt").click()
            downloaded = Path(info.value.path()).read_bytes()
            assert hashlib.sha256(downloaded).hexdigest() == a_item["sha256"]
            review_url = f"/api/admin/review-requests/{rid}/files/{a_item['file_id']}/original"
            assert ("GET", review_url, 200) in log_admin
            ok("Admin list → detail shows checksum and 'unverified' declaration; download matches the SHA-256")

            STAGE = "Admin normal routes stay closed"
            assert fetch(page_admin, "GET", f"/api/admin/review-requests/{rid}/files/{b_item['file_id']}/original")[
                "status"] == 404
            for path in (f"/api/files/{a_item['file_id']}/original", f"/api/library/items/{a_item['id']}",
                         f"/api/library/items/{a_item['id']}/chunks", f"/api/search/chunks/{a_item['chunk_id']}"):
                assert fetch(page_admin, "GET", path)["status"] == 404, path
            assert fetch(page_admin, "POST", "/api/rag/ask", body={"item_id": a_item["id"], "question": "Q"})[
                "status"] == 404
            search(page_admin, web, "basalt review marker")
            expect(page_admin.get_by_text("0 đoạn")).to_be_visible()
            ok("Admin: B's file via the review route, normal original/item/chunk/RAG → 404; search 0 hits")

            STAGE = "Admin rejects"
            page_admin.goto(f"{web}/admin/reviews/{rid}")
            page_admin.get_by_label("Lý do").select_option(value="rights_not_established")
            page_admin.get_by_label(re.compile("Ghi chú ngắn cho User")).fill("Synthetic smoke rejection note")
            page_admin.get_by_role("button", name="Từ chối yêu cầu").click()
            expect(page_admin.get_by_role("status").filter(has_text="Không còn quyền xem")).to_be_visible()
            assert ("POST", f"/api/admin/review-requests/{rid}/reject", 200) in log_admin
            assert fetch(page_admin, "GET", review_url) == {"status": 404, "error": "not_found"}
            expect(page_admin.get_by_text(a_item["sha256"])).to_have_count(0)
            ok("Reject: detail hides the file; the old download URL → 404")

            STAGE = "A resubmits then withdraws"
            page_a.reload()
            expect(page_a.get_by_role("listitem").filter(has_text="Admin đã từ chối")
                   .filter(has_text="Synthetic smoke rejection note")).to_have_count(1)
            submit_in_ui(page_a, web, a_item)
            listed = page_a.evaluate("() => fetch('/api/review-requests').then((r) => r.json())")
            rid2 = next(r["id"] for r in listed["requests"] if r["status"] == "open")
            url2 = f"/api/admin/review-requests/{rid2}/files/{a_item['file_id']}/original"
            assert fetch(page_admin, "GET", url2)["status"] == 200
            assert fetch(page_a, "POST", f"/api/review-requests/{rid2}/withdraw", csrf=False) == {
                "status": 403, "error": "csrf_failed"}
            page_a.get_by_role("button", name="Rút yêu cầu").click()
            expect(page_a.get_by_role("status").filter(has_text="Đã rút yêu cầu")).to_be_visible()
            assert ("POST", f"/api/review-requests/{rid2}/withdraw", 200) in log_a
            assert fetch(page_admin, "GET", url2) == {"status": 404, "error": "not_found"}
            assert fetch(page_admin, "GET", review_url) == {"status": 404, "error": "not_found"}
            ok("A resubmits (Admin 200), withdraw without CSRF → 403, withdraw → Admin's new and old URLs 404")

            review_calls = [e for log in (log_a, log_admin) for e in log if "review-requests" in e[1]]
            print(f"Chrome {browser.version}; Playwright {version('playwright')}; "
                  f"{len(results)}/{len(results)} passed; {len(review_calls)} review-request responses; "
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
        print(f"review smoke failed at {STAGE} (line {line}): {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
