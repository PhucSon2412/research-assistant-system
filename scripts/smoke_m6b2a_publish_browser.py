"""M6b-2a publish/revoke smoke in headless Chrome, with Admin, User A and User B.

A submits → Admin approves (API, as M6b-1 smoked the form) → Admin publishes
on /admin/publications → B sees the copy's metadata only (note "Toàn văn chưa
khả dụng"; file, chunks, search, RAG refused) → a second approval of the same
bytes is refused as a duplicate and cancelled → Admin revokes the published
copy → from that commit A/B/Admin no longer see it anywhere → cleanup deletes
both common objects and keeps both private ones.

Runs against an *isolated* Compose project (own volumes/ports, see
docs/dev-setup.md) and reuses the M6a smoke fixtures: it refuses a database
holding any non-fixture account. A's private copy is indexed on the host with a
fake embedding (no provider call); nothing common is indexed or sent anywhere.
Only method/path/status and pass/fail are printed: no cookies, tokens,
passwords, bodies, screenshots or traces.
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
TITLE = "Smoke published copy"


def get_json(page, path: str) -> dict:
    return page.evaluate("(p) => fetch(p).then((r) => r.json())", path)


def approve_body(checksum: str) -> dict:
    return {"confirm_verified": True, "file_checksum_sha256": checksum, "title": TITLE,
            "verified_source": "Smoke Admin check: synthetic Markdown written by the smoke script itself",
            "evidence_note": "Smoke Admin check: synthetic permission recorded for this exact checksum",
            "checked_on": datetime.now(UTC).date().isoformat(),
            "scope": {"audience": "instance_users", "original_download": True, "fulltext_search": True,
                      "external_processing": False, "conditions": None}}


def sha_of_download(page, path: str) -> str:
    return page.evaluate("""(p) => fetch(p).then((r) => r.arrayBuffer()).then((b) =>
        crypto.subtle.digest("SHA-256", b)).then((d) => Array.from(new Uint8Array(d))
        .map((x) => x.toString(16).padStart(2, "0")).join(""))""", path)


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
        a_item, b_item = items["a"], items["b"]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True)
            try:
                STAGE = "A submits, Admin approves into a pending copy"
                ctx_a, page_a = login(browser, web, accounts["a"])
                ctx_b, page_b = login(browser, web, accounts["b"])
                ctx_admin, page_admin = login(browser, web, accounts["admin"])
                log_admin: list = []
                watch(page_admin, log_admin)
                submit_in_ui(page_a, web, a_item)
                rid = get_json(page_a, "/api/review-requests")["requests"][0]["id"]
                page_admin.goto(f"{web}/admin")
                approved = fetch(page_admin, "POST", f"/api/admin/review-requests/{rid}/approve",
                                 body=approve_body(a_item["sha256"]))
                assert approved["status"] == 200, approved
                copy = get_json(page_admin, f"/api/admin/review-requests/{rid}")["request"]["pending_publication_id"]
                common_file = get_json(page_admin, f"/api/admin/common-copies/{copy}")["item"]["file"]["id"]
                a_before = get_json(page_a, f"/api/library/items/{a_item['id']}")
                for page in (page_a, page_b, page_admin):
                    assert copy not in str(get_json(page, "/api/library/items?limit=100"))
                    assert fetch(page, "GET", f"/api/library/items/{copy}")["status"] == 404
                assert fetch(page_b, "GET", "/api/admin/common-copies")["status"] == 403
                ok("Pending copy exists; A, B and the Admin's library do not list it; B → 403 on Admin routes")

                STAGE = "Admin publishes in the UI"
                page_admin.goto(f"{web}/admin/publications")
                entry = page_admin.locator("section", has=page_admin.get_by_role("heading", name=TITLE))
                expect(entry.get_by_text(a_item["sha256"])).to_be_visible()
                confirm = entry.get_by_role("checkbox", name=re.compile("công bố metadata"))
                button = entry.get_by_role("button", name="Công bố metadata")
                expect(confirm).not_to_be_checked()
                expect(button).to_be_disabled()
                expect(entry.get_by_text(re.compile("vẫn chưa mở"))).to_be_visible()
                confirm.check()
                button.click()
                expect(entry).to_have_count(0)
                assert ("POST", f"/api/admin/common-copies/{copy}/publish", 200) in log_admin
                page_admin.get_by_role("button", name=re.compile("^Đã công bố metadata")).click()
                listed = page_admin.locator("section", has=page_admin.get_by_role("heading", name=TITLE))
                expect(listed.get_by_text(re.compile("Toàn văn chưa khả dụng"))).to_be_visible()
                ok("Publish needs the unticked box; → 200; listed tab says full text is not available")

                STAGE = "B sees metadata only"
                page_b.goto(f"{web}/")
                expect(page_b.get_by_role("link", name=TITLE)).to_be_visible()
                page_b.goto(f"{web}/items/{copy}")
                expect(page_b.get_by_role("heading", name=TITLE)).to_be_visible()
                expect(page_b.get_by_text(re.compile("^Toàn văn chưa khả dụng"))).to_be_visible()
                detail = get_json(page_b, f"/api/library/items/{copy}")["item"]
                assert (detail["content_status"], detail["files"], detail["jobs"]) == ("metadata_only", [], [])
                for page in (page_b, page_a, page_admin):
                    for path in (f"/api/files/{common_file}/original", f"/api/library/items/{copy}/chunks",
                                 f"/api/library/items/{copy}/references"):
                        assert fetch(page, "GET", path)["status"] == 404, path
                    assert fetch(page, "POST", "/api/rag/ask", body={"item_id": copy, "question": "Q",
                                                                     "mode": "keyword"})["status"] == 404
                search(page_b, web, "basalt review marker")
                expect(page_b.get_by_text("0 đoạn")).to_be_visible()
                box = page_b.evaluate("""async () => {
                    const token = decodeURIComponent(document.cookie.split("; ").find((p) => p.startsWith("ras_csrf=")).slice(9));
                    const h = { "content-type": "application/json", "x-csrf-token": token };
                    const c = await fetch("/api/collections", { method: "POST", headers: h, body: JSON.stringify({ name: "B smoke" }) }).then((r) => r.json());
                    return c.collection.id; }""")
                assert fetch(page_b, "POST", f"/api/collections/{box}/items", body={"item_id": copy})["status"] == 201
                assert get_json(page_b, f"/api/collections/{box}/items")["total"] == 1
                ok("B: listed with metadata + 'Toàn văn chưa khả dụng'; file/chunks/references/RAG → 404 for A/B/"
                   "Admin (rights allow download, still closed); B search 0; B can file it in a collection")

                STAGE = "Duplicate bytes are refused"
                submit_in_ui(page_a, web, a_item)
                rid2 = next(r["id"] for r in get_json(page_a, "/api/review-requests")["requests"] if r["status"] == "open")
                assert fetch(page_admin, "POST", f"/api/admin/review-requests/{rid2}/approve",
                             body=approve_body(a_item["sha256"]) | {"title": "Smoke duplicate"})["status"] == 200
                dup = get_json(page_admin, f"/api/admin/review-requests/{rid2}")["request"]["pending_publication_id"]
                assert get_json(page_admin, f"/api/admin/common-copies/{dup}")["item"]["duplicate_published_item_id"] == copy
                assert fetch(page_admin, "POST", f"/api/admin/common-copies/{dup}/publish",
                             body={"confirm_publish": True, "file_checksum_sha256": a_item["sha256"]}) == {
                    "status": 409, "error": "duplicate_published_checksum"}
                page_admin.goto(f"{web}/admin/publications")
                entry = page_admin.locator("section", has=page_admin.get_by_role("heading", name="Smoke duplicate"))
                expect(entry.get_by_text(re.compile("cùng SHA-256"))).to_be_visible()
                entry.get_by_label("Lý do").select_option("duplicate_copy")
                entry.get_by_role("checkbox", name=re.compile("hủy bản chờ")).check()
                entry.get_by_role("button", name="Hủy bản chờ").click()
                expect(entry).to_have_count(0)
                assert ("POST", f"/api/admin/common-copies/{dup}/revoke", 200) in log_admin
                assert dup not in str(get_json(page_b, "/api/library/items?limit=100"))
                assert "duplicate" not in str(get_json(page_a, f"/api/review-requests/{rid2}"))
                ok("Second approval of the same bytes: Admin warned, publish → 409; cancelled in UI; A/B told nothing")

                STAGE = "Admin revokes the published copy"
                page_admin.get_by_role("button", name=re.compile("^Đã công bố metadata")).click()
                entry = page_admin.locator("section", has=page_admin.get_by_role("heading", name=TITLE))
                revoke_button = entry.get_by_role("button", name="Thu hồi")
                expect(revoke_button).to_be_disabled()
                entry.get_by_label("Lý do").select_option("rights_withdrawn")
                expect(revoke_button).to_be_disabled()
                entry.get_by_role("checkbox", name=re.compile("thu hồi bản đã công bố")).check()
                revoke_button.click()
                expect(entry).to_have_count(0)
                assert ("POST", f"/api/admin/common-copies/{copy}/revoke", 200) in log_admin
                for page in (page_a, page_b, page_admin):
                    listed = get_json(page, "/api/library/items?limit=100")
                    assert copy not in str(listed)
                    assert get_json(page, "/api/library/items/count")["total"] == listed["total"]
                    assert fetch(page, "GET", f"/api/library/items/{copy}")["status"] == 404
                    assert fetch(page, "GET", f"/api/files/{common_file}/original")["status"] == 404
                assert get_json(page_b, f"/api/collections/{box}/items")["total"] == 0
                page_b.goto(f"{web}/items/{copy}")
                expect(page_b.get_by_text("Không tìm thấy").first).to_be_visible()
                assert fetch(page_admin, "POST", f"/api/admin/common-copies/{copy}/publish",
                             body={"confirm_publish": True, "file_checksum_sha256": a_item["sha256"]}) == {
                    "status": 409, "error": "common_copy_revoked"}
                ok("Revoke needs reason + unticked box; → 200; from the commit A/B/Admin: list/count/ID/file/"
                   "collection hide it, B page 'Không tìm thấy', publish again → 409")

                STAGE = "cleanup"
                from app.clock import utcnow
                from app.config import get_settings
                from app.publication import operator_cleanup_revoked as cleanup_revoked  # operator path (historical smoke)
                from app.storage import OriginalStorage
                get_settings.cache_clear()
                storage = OriginalStorage(get_settings())
                assert cleanup_revoked(engine, storage, utcnow(), 50) == {"deleted": 2}
                assert cleanup_revoked(engine, storage, utcnow(), 50) == {}
                with engine.connect() as conn:
                    keys = dict(conn.execute(text("SELECT item_id::text, storage_key FROM source_files")).all())
                    tombstones = conn.execute(text("SELECT count(*) FILTER (WHERE purged_at IS NOT NULL),"
                                                   " count(*) FROM deletion_tombstones")).one()
                assert tuple(tombstones) == (2, 2)
                present = {k for k, _ in storage.orphan_keys("")}
                assert keys[copy] not in present and keys[dup] not in present
                for private in (a_item, b_item):
                    assert hashlib.sha256(storage.open(keys[private["id"]]).read()).hexdigest() == private["sha256"]
                with engine.connect() as conn:
                    left = conn.execute(text("""SELECT count(*) FROM library_items i
                        JOIN source_files f ON f.item_id=i.id JOIN access_rights r ON r.item_id=i.id
                        WHERE i.visibility='common' AND (i.title IS NOT NULL OR f.original_filename IS NOT NULL
                          OR r.verified_source IS NOT NULL OR r.evidence_note IS NOT NULL)""")).scalar_one()
                    own_name = conn.scalar(text("SELECT original_filename FROM source_files WHERE item_id=:i"),
                                           {"i": uuid.UUID(a_item["id"])})
                assert left == 0 and own_name
                page_admin.get_by_role("button", name="Đã thu hồi").click()
                done = page_admin.locator("section", has=page_admin.get_by_role(
                    "heading", name=f"(metadata đã xóa) {copy}"))
                expect(done.get_by_text(re.compile("đã xóa object chung"))).to_be_visible()
                expect(done.get_by_text(re.compile("^Metadata đã xóa: tiêu đề"))).to_be_visible()
                expect(page_admin.get_by_text(TITLE)).to_have_count(0)
                ok("Cleanup: both common objects and their metadata removed (title, file name, source, evidence), "
                   "rerun no-op, A/B private objects and A's file name intact; revoked tab shows it")

                STAGE = "A unchanged"
                assert get_json(page_a, f"/api/library/items/{a_item['id']}") == a_before
                assert sha_of_download(page_a, f"/api/files/{a_item['file_id']}/original") == a_item["sha256"]
                search(page_a, web, "basalt review marker")
                expect(page_a.get_by_text(re.compile(r"· [1-9]\d* đoạn"))).to_be_visible()
                ok("A: private item detail, file SHA-256 and own search unchanged")

                print(f"Chrome {browser.version}; Playwright {version('playwright')}; "
                      f"{len(results)}/{len(results)} steps passed; external provider calls: 0")
                for context in (ctx_a, ctx_b, ctx_admin):
                    context.close()
            finally:
                browser.close()
        with engine.connect() as conn:
            counts = conn.execute(text("SELECT (SELECT count(*) FROM search_usage), (SELECT count(*) FROM"
                                       " rag_request_days), (SELECT count(*) FROM ingestion_jobs j JOIN library_items i"
                                       " ON i.id=j.item_id WHERE i.visibility='common')")).one()
        assert tuple(counts) == (0, 0, 0)
        print(f"search_usage=0 rag_request_days=0 common_jobs=0; {len(results)}/{len(results)} passed")
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
        print(f"publish smoke failed at {STAGE} (line {line}): {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
