"""M1c form smoke test in real headless Chrome on the Compose dev stack.

Use the existing Conda ras Python. The only direct DB writes create isolated
synthetic User A/B fixtures; every product action is performed in the UI.
No trace, screenshot, HAR, credentials, tokens or file bytes are logged.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import time
import uuid
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from app.identity.passwords import hash_password


ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
FILE_NAME = "m1c-ui-smoke.md"
FILE_BYTES = b"# M1c browser smoke\n\nSynthetic private fixture for the upload form.\n"
STAGE = "setup"


def fixture_accounts() -> tuple[str, dict[str, tuple[str, str]]]:
    env = dotenv_values(ROOT / ".env")
    web = f"http://127.0.0.1:{env.get('WEB_HOST_PORT') or '3000'}"
    url = URL.create(
        "postgresql+psycopg",
        username=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"],
        host="127.0.0.1",
        port=int(env.get("POSTGRES_HOST_PORT") or "5432"),
        database=env["POSTGRES_DB"],
    )
    marker = uuid.uuid4().hex[:12]
    users = {
        label: (f"m1c-browser-{label}-{marker}@example.org", secrets.token_urlsafe(24))
        for label in ("a", "b")
    }
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.begin() as conn:
            for email, password in users.values():
                conn.execute(
                    text("INSERT INTO users (email, password_verifier) VALUES (:email, :verifier)"),
                    {"email": email, "verifier": hash_password(password)},
                )
    finally:
        engine.dispose()
    return web, users


def login_via_ui(page, web: str, account: tuple[str, str]) -> None:
    page.goto(f"{web}/login")
    page.get_by_label("Email").fill(account[0])
    page.get_by_label("Mật khẩu").fill(account[1])
    page.get_by_role("button", name="Đăng nhập").click()
    expect(page.get_by_role("heading", name="Tải paper vào kho riêng")).to_be_visible()


def main() -> None:
    global STAGE
    web, users = fixture_accounts()
    STAGE = "launch Chrome"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        try:
            context_a = browser.new_context(accept_downloads=True)
            page_a = context_a.new_page()
            STAGE = "login User A through UI"
            login_via_ui(page_a, web, users["a"])

            # Keep only method/path/timing/status. In particular, never retain
            # request headers, bodies, raw intent tokens or cookies.
            events: list[tuple[str, int, int | None]] = []

            def relevant(request) -> str | None:
                path = urlparse(request.url).path
                if request.method == "POST" and path == "/api/uploads/intents":
                    return "intent"
                if request.method == "PUT" and path == "/api/uploads/original":
                    return "file"
                return None

            def on_request(request) -> None:
                kind = relevant(request)
                if kind:
                    events.append((f"{kind}_start", time.perf_counter_ns(), None))

            def on_finished(request) -> None:
                kind = relevant(request)
                if kind:
                    events.append((f"{kind}_finish", time.perf_counter_ns(), None))

            def on_response(response) -> None:
                kind = relevant(response.request)
                if kind:
                    events.append((f"{kind}_status", time.perf_counter_ns(), response.status))

            page_a.on("request", on_request)
            page_a.on("requestfinished", on_finished)
            page_a.on("response", on_response)

            checkbox = page_a.get_by_role("checkbox", name="Tôi đồng ý xử lý ngoài cho lần upload này.")
            button = page_a.get_by_role("button", name="Tải lên")
            STAGE = "1: checkbox starts unchecked"
            expect(checkbox).not_to_be_checked()
            page_a.locator('input[type="file"]').set_input_files(
                {"name": FILE_NAME, "mimeType": "text/markdown", "buffer": FILE_BYTES}
            )
            expect(checkbox).to_be_enabled()

            STAGE = "2: no confirmation means no intent or file request"
            expect(button).to_be_disabled()
            try:
                button.click(timeout=400)
                raise AssertionError("disabled upload button accepted a click")
            except PlaywrightTimeoutError:
                pass
            page_a.wait_for_timeout(300)
            assert not events, "upload request occurred without confirmation"
            expect(checkbox).not_to_be_checked()

            STAGE = "3 and 5: stale notice makes real API reject intent"
            notice_text = page_a.locator('section[aria-label="Tải paper riêng"] > p').first.inner_text()
            notice_pattern = "**/api/uploads/notice"
            page_a.route(notice_pattern, lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"version": "obsolete-smoke-version", "text": notice_text}, ensure_ascii=False),
            ))
            page_a.reload()
            page_a.locator('input[type="file"]').set_input_files(
                {"name": FILE_NAME, "mimeType": "text/markdown", "buffer": FILE_BYTES}
            )
            expect(checkbox).to_be_enabled()
            expect(checkbox).not_to_be_checked()
            checkbox.check()
            button.click()
            STAGE = "3: failed intent error message"
            upload_alert = page_a.locator('section[aria-label="Tải paper riêng"] [role="alert"]')
            expect(upload_alert).to_contain_text("Thông báo xử lý đã thay đổi")
            STAGE = "5: checkbox reset after failed intent"
            expect(checkbox).not_to_be_checked()
            assert [event[0] for event in events].count("file_start") == 0, "PUT followed a failed intent"
            assert any(event[0] == "intent_status" and event[2] == 409 for event in events)
            page_a.unroute(notice_pattern)

            STAGE = "4 and 5: successful intent completes before PUT and checkbox resets"
            page_a.reload()
            page_a.locator('input[type="file"]').set_input_files(
                {"name": FILE_NAME, "mimeType": "text/markdown", "buffer": FILE_BYTES}
            )
            expect(checkbox).to_be_enabled()
            expect(checkbox).not_to_be_checked()
            events.clear()
            checkbox.check()
            button.click()
            result_link = page_a.get_by_role("link", name="Xem item")
            expect(result_link).to_be_visible(timeout=30_000)
            expect(checkbox).not_to_be_checked()
            names = [event[0] for event in events]
            assert names.count("intent_start") == names.count("intent_finish") == 1
            assert names.count("file_start") == names.count("file_finish") == 1
            assert names.index("intent_finish") < names.index("file_start"), "PUT began before POST intent finished"
            assert any(event[0] == "intent_status" and event[2] == 201 for event in events)
            assert any(event[0] == "file_status" and event[2] == 201 for event in events)
            item_path = result_link.get_attribute("href")
            assert item_path and item_path.startswith("/items/")

            STAGE = "6: User A downloads original from item page"
            result_link.click()
            file_link = page_a.get_by_role("link", name=FILE_NAME)
            expect(file_link).to_be_visible()
            file_path = file_link.get_attribute("href")
            assert file_path and file_path.startswith("/api/files/")
            with page_a.expect_download() as download_info:
                file_link.click()
            download = download_info.value
            assert download.suggested_filename == FILE_NAME
            # Playwright's temporary download is removed when the browser
            # context closes. Only a digest is compared; no file is saved.
            assert hashlib.sha256(Path(download.path()).read_bytes()).digest() == hashlib.sha256(FILE_BYTES).digest()

            STAGE = "6: User B cannot open A's item or original URL"
            context_b = browser.new_context(accept_downloads=True)
            page_b = context_b.new_page()
            STAGE = "6: User B login through UI"
            login_via_ui(page_b, web, users["b"])
            STAGE = "6: User B denied A item page"
            page_b.goto(f"{web}{item_path}")
            expect(page_b.get_by_role("alert").filter(has_text="Không tìm thấy")).to_be_visible()
            STAGE = "6: User B denied A original URL"
            response = page_b.goto(f"{web}{file_path}")
            assert response is not None and response.status == 404
            assert "not_found" in page_b.locator("body").inner_text()
            context_b.close()
            context_a.close()

            print(f"Chrome {browser.version}; Playwright {version('playwright')}; M1c UI smoke: 6/6 passed")
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Suppress exception detail: Playwright errors can contain selectors,
        # DOM text or request context. Never print secrets or file content.
        print(f"M1c UI smoke failed at {STAGE}: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
