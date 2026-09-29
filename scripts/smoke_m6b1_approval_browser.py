"""M6b-1 approval smoke in headless Chrome: A submits → Admin fills the empty
rights form and approves → a pending common copy exists only on the Admin
pending page; B, A and the Admin's normal routes never see it; the old review
URL is dead; A's private item, file and collection are unchanged.

Runs against an *isolated* Compose project (own volumes/ports, see
docs/dev-setup.md) and reuses the M6a smoke fixtures: it refuses a database
holding any non-fixture account. One synthetic Markdown per User is uploaded
through the web origin; the Compose worker extracts it and this host process
indexes A's with a fake embedding (no provider call). Only method/path/status
and pass/fail are printed: no cookies, tokens, passwords, bodies, screenshots
or traces.
"""

from __future__ import annotations

import hashlib
import re
import sys
import traceback
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from dotenv import dotenv_values
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_m6a_review_browser import CHROME, ROOT, fixtures, submit_in_ui  # noqa: E402
from smoke_proxy_browser import fetch, login, prepare, search, watch  # noqa: E402

STAGE = "setup"
SOURCE = "Smoke Admin check: synthetic Markdown written by the smoke script itself"
EVIDENCE = "Smoke Admin check: synthetic permission recorded for this exact checksum"


def get_json(page, path: str) -> dict:
    return page.evaluate("(p) => fetch(p).then((r) => r.json())", path)


def main() -> None:
    global STAGE
    sys.stdout.reconfigure(encoding="utf-8")
    env = dotenv_values(ROOT / ".env")
    engine, web = prepare(env)
    results: list[str] = []

    def ok(name: str) -> None:
        results.append(name)
        print(f"PASS {len(results):2d}. {name}")

    try:
        STAGE = "fixtures"
        accounts, items = fixtures(engine, web)
        a_item = items["a"]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True)
            try:
                STAGE = "A submits and files the item in a collection"
                ctx_a, page_a = login(browser, web, accounts["a"])
                log_a: list = []
                watch(page_a, log_a)
                submit_in_ui(page_a, web, a_item)
                rid = get_json(page_a, "/api/review-requests")["requests"][0]["id"]
                box = page_a.evaluate("""async () => {
                    const token = decodeURIComponent(document.cookie.split("; ").find((p) => p.startsWith("ras_csrf=")).slice(9));
                    const h = { "content-type": "application/json", "x-csrf-token": token };
                    const c = await fetch("/api/collections", { method: "POST", headers: h, body: JSON.stringify({ name: "Smoke box" }) }).then((r) => r.json());
                    return c.collection.id; }""")
                assert fetch(page_a, "POST", f"/api/collections/{box}/items", body={"item_id": a_item["id"]})[
                    "status"] == 201
                a_before = get_json(page_a, f"/api/library/items/{a_item['id']}")
                ok("A submits one exact version/file and keeps the item in a collection")

                STAGE = "Admin sees an empty, unticked rights form"
                ctx_admin, page_admin = login(browser, web, accounts["admin"])
                log_admin: list = []
                watch(page_admin, log_admin)
                page_admin.goto(f"{web}/admin/reviews/{rid}")
                form = page_admin.locator("form", has=page_admin.get_by_role("heading", name="Phê duyệt và ghi nhận quyền"))
                expect(form.get_by_text("không").first).to_be_visible()
                expect(form.get_by_text(re.compile("arXiv/Hugging Face"))).to_be_visible()
                expect(form.get_by_text(re.compile("chờ công bố")).first).to_be_visible()
                for label in ("Tiêu đề bản chung", "Nguồn file đã kiểm", "URL giấy phép đã kiểm", "Bằng chứng khác đã kiểm"):
                    expect(form.get_by_label(re.compile(f"^{label}"))).to_have_value("")
                confirm = form.get_by_role("checkbox", name=re.compile("Tôi đã tự kiểm quyền"))
                expect(confirm).not_to_be_checked()
                button = form.get_by_role("button", name="Phê duyệt (chờ công bố)")
                expect(button).to_be_disabled()
                expect(page_admin.get_by_text("Synthetic claim by the requester; unverified")).to_be_visible()
                ok("Approval form starts empty and unticked; the notice says free reading/upload grant no rights")

                STAGE = "Admin approves"
                form.get_by_label(re.compile("^Tiêu đề bản chung")).fill("Smoke common copy")
                form.get_by_label(re.compile("^Nguồn file đã kiểm")).fill(SOURCE)
                form.get_by_label(re.compile("^Bằng chứng khác đã kiểm")).fill(EVIDENCE)
                form.get_by_label(re.compile("^Ngày kiểm")).fill(datetime.now(UTC).date().isoformat())
                form.get_by_label(re.compile("Cho index/tìm kiếm toàn văn")).check()
                expect(button).to_be_disabled()
                confirm.check()
                button.click()
                expect(page_admin.get_by_role("status").filter(has_text="chưa công bố")).to_be_visible()
                assert ("POST", f"/api/admin/review-requests/{rid}/approve", 200) in log_admin
                detail = get_json(page_admin, f"/api/admin/review-requests/{rid}")["request"]
                assert detail["status"] == "approved" and detail["review_available"] is False
                pending_id = detail["pending_publication_id"]
                old_url = f"/api/admin/review-requests/{rid}/files/{a_item['file_id']}/original"
                assert fetch(page_admin, "GET", old_url) == {"status": 404, "error": "not_found"}
                assert fetch(page_admin, "POST", f"/api/admin/review-requests/{rid}/approve", body={})[
                    "status"] == 422
                ok("Approve → 200; request 'approved', review grant over, old download URL → 404")

                STAGE = "Admin pending page"
                page_admin.get_by_role("link", name="Xem bản chung chờ công bố").click()
                page_admin.wait_for_url(re.compile(r"/admin/publications"))
                entry = page_admin.locator("section", has=page_admin.get_by_role("heading", name="Smoke common copy"))
                expect(entry.get_by_text(SOURCE)).to_be_visible()
                expect(entry.get_by_text(EVIDENCE)).to_be_visible()
                expect(entry.get_by_text(a_item["sha256"])).to_be_visible()
                pending = get_json(page_admin, f"/api/admin/pending-publications/{pending_id}")["item"]
                common_file = pending["file"]["id"]
                assert pending["publication_state"] == "pending" and pending["published"] is False
                ok("Admin pending page lists the copy with the Admin's own source/evidence and checksum")

                STAGE = "B and the Admin's normal routes never see it"
                ctx_b, page_b = login(browser, web, accounts["b"])
                for page in (page_b, page_admin, page_a):
                    listed = get_json(page, "/api/library/items?limit=100")
                    assert pending_id not in str(listed)
                    assert get_json(page, "/api/library/items/count")["total"] == listed["total"]
                    for path in (f"/api/library/items/{pending_id}", f"/api/library/items/{pending_id}/chunks",
                                 f"/api/files/{common_file}/original"):
                        assert fetch(page, "GET", path)["status"] == 404, path
                    assert fetch(page, "POST", f"/api/collections/{box}/items", body={"item_id": pending_id})[
                        "status"] == 404
                    assert fetch(page, "POST", "/api/rag/ask", body={"item_id": pending_id, "question": "Q",
                                                                     "mode": "keyword"})["status"] == 404
                assert fetch(page_b, "GET", "/api/admin/pending-publications")["status"] == 403
                assert fetch(page_b, "GET", f"/api/admin/pending-publications/{pending_id}")["status"] == 403
                page_b.goto(f"{web}/items/{pending_id}")
                expect(page_b.get_by_text("Không tìm thấy").first).to_be_visible()
                search(page_b, web, "basalt review marker")
                expect(page_b.get_by_text("0 đoạn")).to_be_visible()
                search(page_admin, web, "basalt review marker")
                expect(page_admin.get_by_text("0 đoạn")).to_be_visible()
                assert fetch(page_admin, "GET", f"/api/library/items/{a_item['id']}")["status"] == 404
                ok("B/Admin/A: pending item absent from list/count/ID/chunks/file/collection/RAG; B search 0; "
                   "B → 403 on pending routes")

                STAGE = "A is unchanged"
                page_a.goto(f"{web}/items/{a_item['id']}")
                expect(page_a.get_by_role("listitem").filter(has_text="Admin đã ghi nhận quyền")).to_have_count(1)
                assert get_json(page_a, f"/api/library/items/{a_item['id']}") == a_before
                members = get_json(page_a, f"/api/collections/{box}/items")
                assert [m["id"] for m in members["items"]] == [a_item["id"]]
                own = page_a.evaluate("""(p) => fetch(p).then((r) => r.arrayBuffer()).then((b) =>
                    crypto.subtle.digest("SHA-256", b)).then((d) => Array.from(new Uint8Array(d))
                    .map((x) => x.toString(16).padStart(2, "0")).join(""))""", f"/api/files/{a_item['file_id']}/original")
                assert own == a_item["sha256"]
                search(page_a, web, "basalt review marker")
                expect(page_a.get_by_text(re.compile(r"· [1-9]\d* đoạn"))).to_be_visible()
                assert pending_id not in str(get_json(page_a, f"/api/review-requests/{rid}"))
                ok("A: status 'approved'; item detail, collection, file SHA-256 and own search unchanged")

                print(f"Chrome {browser.version}; Playwright {version('playwright')}; "
                      f"{len(results)}/{len(results)} UI steps passed; external provider calls: 0")
                for context in (ctx_a, ctx_b, ctx_admin):
                    context.close()
            finally:
                browser.close()

        STAGE = "storage copy"
        from app.config import get_settings
        from app.storage import OriginalStorage
        get_settings.cache_clear()
        storage = OriginalStorage(get_settings())
        with engine.connect() as conn:
            keys = dict(conn.execute(text("SELECT id::text, storage_key FROM source_files WHERE id IN (:a, :c)"),
                                     {"a": uuid.UUID(a_item["file_id"]), "c": uuid.UUID(common_file)}).all())
            rights = conn.execute(text("SELECT file_checksum_sha256, origin_review_request_id::text FROM access_rights"
                                       " WHERE item_id=:i"), {"i": uuid.UUID(pending_id)}).one()
        assert keys[a_item["file_id"]] != keys[common_file]
        for key in keys.values():
            assert hashlib.sha256(storage.open(key).read()).hexdigest() == a_item["sha256"]
        assert tuple(rights) == (a_item["sha256"], rid)
        ok("DB/MinIO: common copy has its own object key; both objects hash to the submitted SHA-256")
        print(f"{len(results)}/{len(results)} passed")
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        # Suppress detail: Playwright errors can quote DOM text or request data.
        line = next((f.lineno for f in reversed(traceback.extract_tb(exc.__traceback__))
                     if f.filename == __file__), None)
        print(f"approval smoke failed at {STAGE} (line {line}): {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
