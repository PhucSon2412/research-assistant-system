"""M6b-2b1 common original download smoke in headless Chrome, with Admin, User A and User B.

A and B each submit their private file in the UI; the Admin approves A's with
download permitted and no condition, B's with download permitted *and* a text
condition, and a second copy of A's that stays pending; both first copies are
published (API; the forms were smoked in M6b-1/M6b-2a). Then, through the web
origin's streaming route: A, B and the Admin download A's common copy with the
exact reviewed SHA-256 (B by clicking the item page link); B's copy shows its
metadata but no link, and its file, the pending copy, and each other's private
files answer 404; a same-size replacement of the common object answers 409 with
no bytes; a large copy revoked while B's response is streaming still completes
(the documented limit) while B's next request is refused; the Admin revokes A's
copy in the UI and the saved link answers 404.

Runs against an *isolated* Compose project (own volumes/ports, see
docs/dev-setup.md) and reuses the M6a smoke fixtures: it refuses a database
holding any non-fixture account. A's private copy is indexed on the host with a
fake embedding (no provider call); nothing common is indexed or sent anywhere.
Only method/path/status, sizes, timings and pass/fail are printed: no cookies,
tokens, passwords, bodies, screenshots or traces; downloaded files are hashed
and deleted with their browser context.
"""

from __future__ import annotations

import hashlib
import io
import re
import sys
import time
import traceback
from contextlib import closing
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import httpx
from dotenv import dotenv_values
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_m6a_review_browser import CHROME, ROOT, fixtures, submit_in_ui  # noqa: E402
from smoke_proxy_browser import fetch, login, prepare, watch  # noqa: E402

STAGE = "setup"
TITLE_A, TITLE_B, TITLE_BIG = "Smoke downloadable copy", "Smoke conditioned copy", "Smoke large copy"


def get_json(page, path: str) -> dict:
    return page.evaluate("(p) => fetch(p).then((r) => r.json())", path)


def approve_body(checksum: str, title: str, conditions: str | None = None) -> dict:
    return {"confirm_verified": True, "file_checksum_sha256": checksum, "title": title,
            "verified_source": "Smoke Admin check: synthetic Markdown written by the smoke script itself",
            "evidence_note": "Smoke Admin check: synthetic permission recorded for this exact checksum",
            "checked_on": datetime.now(UTC).date().isoformat(),
            "scope": {"audience": "instance_users", "original_download": True, "fulltext_search": False,
                      "external_processing": False, "conditions": conditions}}


def download_facts(page, path: str) -> dict:
    """Status, cache header and SHA-256 of the body, computed in the page; the body never leaves it."""
    return page.evaluate("""async (p) => {
        const r = await fetch(p);
        const b = await r.arrayBuffer();
        const d = await crypto.subtle.digest("SHA-256", b);
        return { status: r.status, cache: r.headers.get("cache-control"), size: b.byteLength,
                 disposition: r.headers.get("content-disposition") !== null,
                 sha: Array.from(new Uint8Array(d)).map((x) => x.toString(16).padStart(2, "0")).join("") };
    }""", path)


def approve_copy(page_admin, rid: str, body: dict) -> tuple[str, str]:
    approved = fetch(page_admin, "POST", f"/api/admin/review-requests/{rid}/approve", body=body)
    assert approved["status"] == 200, approved
    copy = get_json(page_admin, f"/api/admin/review-requests/{rid}")["request"]["pending_publication_id"]
    return copy, get_json(page_admin, f"/api/admin/common-copies/{copy}")["item"]["file"]["id"]


def publish(page_admin, copy: str, checksum: str) -> None:
    assert fetch(page_admin, "POST", f"/api/admin/common-copies/{copy}/publish",
                 body={"confirm_publish": True, "file_checksum_sha256": checksum})["status"] == 200


def web_client(web: str, account: tuple[str, str]) -> httpx.Client:
    client = httpx.Client(base_url=web, timeout=60)
    client.get("/api/auth/csrf").raise_for_status()
    headers = {"origin": web, "x-csrf-token": client.cookies["ras_csrf"]}
    assert client.post("/api/auth/login", json={"email": account[0], "password": account[1]},
                       headers=headers).status_code == 200
    client.headers.update({"origin": web, "x-csrf-token": client.cookies["ras_csrf"]})
    return client


def upload_and_submit(client: httpx.Client, body: bytes, filename: str) -> tuple[dict, str]:
    notice = client.get("/api/uploads/notice").json()["version"]
    intent = client.post("/api/uploads/intents", json={
        "acknowledged": True, "notice_version": notice, "file_kind": "markdown", "filename": filename,
        "byte_size": len(body), "sha256": hashlib.sha256(body).hexdigest()})
    assert intent.status_code == 201, intent.status_code
    sent = client.put("/api/uploads/original", content=body, headers={
        "x-upload-intent": intent.json()["intent_token"], "content-type": "application/octet-stream"})
    assert sent.status_code == 201, sent.status_code
    item = sent.json()
    review_notice = client.get("/api/review-requests/notice").json()["version"]
    submitted = client.post("/api/review-requests", json={
        "acknowledged": True, "notice_version": review_notice, "item_id": item["item_id"],
        "version_id": item["version_id"], "source_file_id": item["file_id"],
        "declared_source": "Synthetic Markdown written for the smoke test",
        "declared_rights": "Synthetic claim by the requester; unverified"})
    assert submitted.status_code == 201, submitted.status_code
    return item, submitted.json()["request"]["id"]


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
        from app.config import get_settings
        from app.storage import OriginalStorage
        get_settings.cache_clear()
        storage = OriginalStorage(get_settings())
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True)
            try:
                STAGE = "approve and publish"
                ctx_a, page_a = login(browser, web, accounts["a"])
                ctx_b, page_b = login(browser, web, accounts["b"])
                ctx_admin, page_admin = login(browser, web, accounts["admin"])
                log_b: list = []
                watch(page_b, log_b)
                submit_in_ui(page_a, web, a_item)
                submit_in_ui(page_b, web, b_item)
                rid_a = get_json(page_a, "/api/review-requests")["requests"][0]["id"]
                rid_b = get_json(page_b, "/api/review-requests")["requests"][0]["id"]
                page_admin.goto(f"{web}/admin")
                copy_a, file_a = approve_copy(page_admin, rid_a, approve_body(a_item["sha256"], TITLE_A))
                copy_b, file_b = approve_copy(page_admin, rid_b, approve_body(
                    b_item["sha256"], TITLE_B, "Chỉ dùng nội bộ, ghi nguồn khi trích dẫn"))
                submit_in_ui(page_a, web, a_item)
                rid_p = next(r["id"] for r in get_json(page_a, "/api/review-requests")["requests"]
                             if r["status"] == "open")
                copy_p, file_p = approve_copy(page_admin, rid_p, approve_body(a_item["sha256"], "Smoke pending"))
                publish(page_admin, copy_a, a_item["sha256"])
                publish(page_admin, copy_b, b_item["sha256"])
                page_admin.goto(f"{web}/admin/publications")
                page_admin.get_by_role("button", name=re.compile("^Đã công bố metadata")).click()
                entry_a = page_admin.locator("section", has=page_admin.get_by_role("heading", name=TITLE_A))
                entry_b = page_admin.locator("section", has=page_admin.get_by_role("heading", name=TITLE_B))
                expect(entry_a.get_by_text(re.compile("Tải file gốc qua thư viện: mở cho mọi tài khoản"))).to_be_visible()
                expect(entry_b.get_by_text(re.compile("Tải file gốc qua thư viện: không mở \\(có điều kiện"))).to_be_visible()
                ok("Two copies published (A's: download, no condition; B's: download + text condition), one "
                   "pending; Admin page shows 'mở' vs 'không mở (có điều kiện …)' from the API")

                STAGE = "download the permitted copy"
                page_b.goto(f"{web}/items/{copy_a}")
                expect(page_b.get_by_role("heading", name=TITLE_A)).to_be_visible()
                expect(page_b.get_by_text(re.compile("tải được theo quyền Admin đã ghi"))).to_be_visible()
                link = page_b.get_by_role("link", name=re.compile(r"\.md$"))
                expect(link).to_have_count(1)
                assert link.get_attribute("href") == f"/api/files/{file_a}/original"
                with page_b.expect_download() as info:
                    link.click()
                saved = Path(info.value.path())
                assert hashlib.sha256(saved.read_bytes()).hexdigest() == a_item["sha256"]
                info.value.delete()
                assert ("GET", f"/api/files/{file_a}/original", 200) in log_b
                for page in (page_a, page_admin, page_b):
                    facts = download_facts(page, f"/api/files/{file_a}/original")
                    assert (facts["status"], facts["cache"], facts["sha"]) == (200, "no-store", a_item["sha256"])
                ok("B clicks the item-page link (streaming route) and gets A's reviewed bytes; A and the Admin "
                   "too; Cache-Control no-store")

                STAGE = "refusals"
                page_b.goto(f"{web}/items/{copy_b}")
                expect(page_b.get_by_role("heading", name=TITLE_B)).to_be_visible()
                expect(page_b.get_by_text("Không có file gốc được phép tải.")).to_be_visible()
                expect(page_b.locator("a[href^='/api/files/']")).to_have_count(0)
                for page in (page_a, page_b, page_admin):
                    for path in (f"/api/files/{file_b}/original", f"/api/files/{file_p}/original",
                                 f"/api/library/items/{copy_a}/chunks"):
                        assert fetch(page, "GET", path) == {"status": 404, "error": "not_found"}, path
                    assert fetch(page, "GET", f"/api/library/items/{copy_p}")["status"] == 404
                    assert fetch(page, "POST", "/api/rag/ask", body={"item_id": copy_a, "question": "Q",
                                                                     "mode": "keyword"})["status"] == 404
                for page, other in ((page_b, a_item), (page_admin, a_item), (page_a, b_item), (page_admin, b_item)):
                    assert fetch(page, "GET", f"/api/files/{other['file_id']}/original")["status"] == 404
                assert download_facts(page_a, f"/api/files/{a_item['file_id']}/original")["sha"] == a_item["sha256"]
                ok("B's conditioned copy: metadata, 'Không có file gốc được phép tải', no link; its file, the "
                   "pending copy, chunks/RAG of A's copy and each other's private files → 404 for A/B/Admin")

                STAGE = "integrity"
                with engine.connect() as conn:
                    key_a = conn.scalar(text("SELECT storage_key FROM source_files WHERE id=CAST(:f AS uuid)"),
                                        {"f": file_a})
                original = storage.open(key_a).read()
                forged = bytes(b ^ 0x20 if chr(b).isalpha() else b for b in original)
                assert len(forged) == len(original) and forged != original
                storage.put(key_a, io.BytesIO(forged), "text/markdown")
                facts = download_facts(page_b, f"/api/files/{file_a}/original")
                assert (facts["status"], facts["disposition"]) == (409, False)
                assert fetch(page_b, "GET", f"/api/files/{file_a}/original")["error"] == "common_file_integrity_failed"
                storage.put(key_a, io.BytesIO(original), "text/markdown")
                assert download_facts(page_b, f"/api/files/{file_a}/original")["sha"] == a_item["sha256"]
                ok("Common object replaced by same-size bytes → 409 common_file_integrity_failed, no file bytes; "
                   "restored → 200 with the reviewed SHA-256")

                STAGE = "revoke while streaming"
                big = b"## Methods\n" + b"".join(b"Synthetic large smoke line %06d for streaming.\n" % i
                                                 for i in range(30_000))
                with closing(web_client(web, accounts["a"])) as client_a:
                    big_item, rid_big = upload_and_submit(client_a, big, "review-smoke-large.md")
                big_sha = hashlib.sha256(big).hexdigest()
                copy_big, file_big = approve_copy(page_admin, rid_big, approve_body(big_sha, TITLE_BIG))
                publish(page_admin, copy_big, big_sha)
                with closing(web_client(web, accounts["b"])) as client_b:
                    # Clients that go away right after the headers, more times than the API has spool
                    # slots (default 4): a slot kept by any of them would turn a later one into 503.
                    early = []
                    for _ in range(6):
                        with client_b.stream("GET", f"/api/files/{file_big}/original") as response:
                            early.append(response.status_code)  # closed without reading the body
                        time.sleep(0.5)
                    assert early == [200] * 6, early
                    assert download_facts(page_b, f"/api/files/{file_big}/original")["sha"] == big_sha
                    print(f"      6 streams closed right after the headers: {early}; next full download OK")
                    started = time.monotonic()
                    with client_b.stream("GET", f"/api/files/{file_big}/original") as response:
                        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
                        chunks = response.iter_bytes(chunk_size=64 * 1024)
                        first = next(chunks)
                        ttfb_ms = (time.monotonic() - started) * 1000
                        assert fetch(page_admin, "POST", f"/api/admin/common-copies/{copy_big}/revoke",
                                     body={"confirm_revoke": True, "reason_code": "rights_withdrawn"})["status"] == 200
                        assert fetch(page_b, "GET", f"/api/files/{file_big}/original")["status"] == 404
                        received = first + b"".join(chunks)
                assert len(received) == len(big) and hashlib.sha256(received).hexdigest() == big_sha
                print(f"      large copy: {len(big)} bytes, first byte after {ttfb_ms:.0f} ms (verified spool)")
                ok("6 early client disconnects gave their spool slots back; revoke committed while B's response was streaming: that response still delivered the whole "
                   "verified file (documented limit); B's new request right after the commit → 404")

                STAGE = "Admin revokes in the UI"
                saved_href = f"/api/files/{file_a}/original"
                page_admin.goto(f"{web}/admin/publications")
                page_admin.get_by_role("button", name=re.compile("^Đã công bố metadata")).click()
                entry = page_admin.locator("section", has=page_admin.get_by_role("heading", name=TITLE_A))
                entry.get_by_label("Lý do").select_option("rights_withdrawn")
                entry.get_by_role("checkbox", name=re.compile("thu hồi bản đã công bố")).check()
                entry.get_by_role("button", name="Thu hồi").click()
                expect(entry).to_have_count(0)
                for page in (page_a, page_b, page_admin):
                    assert fetch(page, "GET", saved_href) == {"status": 404, "error": "not_found"}
                page_b.goto(f"{web}/items/{copy_a}")
                expect(page_b.get_by_text("Không tìm thấy").first).to_be_visible()
                assert download_facts(page_a, f"/api/files/{a_item['file_id']}/original")["sha"] == a_item["sha256"]
                ok("Admin revokes A's copy in the UI → the saved link → 404 for A/B/Admin, B's page 'Không tìm "
                   "thấy'; A's private original still downloads")

                STAGE = "audit and side effects"
                with engine.connect() as conn:
                    audits = conn.execute(text("""SELECT outcome, details FROM audit_events
                        WHERE action='common_copy.original.download'""")).all()
                    counts = conn.execute(text("""SELECT (SELECT count(*) FROM search_usage),
                        (SELECT count(*) FROM rag_request_days), (SELECT count(*) FROM ingestion_jobs j
                         JOIN library_items i ON i.id=j.item_id WHERE i.visibility='common')""")).one()
                outcomes = sorted({o for o, _ in audits})
                assert outcomes == ["failure", "success"], outcomes
                assert all(set(d) <= {"item_id", "reason_code"} for _, d in audits)
                dumped = str([d for _, d in audits])
                for secret in (TITLE_A, TITLE_B, "review-smoke-a.md", a_item["sha256"], "Synthetic"):
                    assert secret not in dumped
                assert tuple(counts) == (0, 0, 0)
                ok(f"{len(audits)} download audit rows (success/failure) with IDs and codes only; "
                   "search_usage=0 rag_request_days=0 common_jobs=0")

                print(f"Chrome {browser.version}; Playwright {version('playwright')}; "
                      f"{len(results)}/{len(results)} steps passed; external provider calls: 0")
                for context in (ctx_a, ctx_b, ctx_admin):
                    context.close()
            finally:
                browser.close()
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
        print(f"download smoke failed at {STAGE} (line {line}): {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
