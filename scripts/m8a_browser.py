"""M8a Chrome smoke over HTTPS, run inside rasm8a-browser:local on the public network.

The throwaway test CA is imported into a per-run NSS database in a private
HOME, so Chrome verifies the chain and host name itself (no ignore-cert
flags). A control run with an empty HOME must fail with a certificate error.
Every request outside the HTTPS origin is aborted. Mail links are read from
the isolated SMTP capture store mounted read-only.
"""
from __future__ import annotations

import argparse
import email
import email.policy
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright

CAPTURE = Path("/capture")


def nss_home(path: Path, ca: Path | None) -> Path:
    db = path / ".pki" / "nssdb"
    db.mkdir(parents=True, exist_ok=True)
    subprocess.run(["certutil", "-d", f"sql:{db}", "-N", "--empty-password"], check=True)
    if ca is not None:
        subprocess.run(["certutil", "-d", f"sql:{db}", "-A", "-t", "C,,", "-n", "ras-m8a-smoke-ca", "-i", str(ca)],
                       check=True)
    return path


def mails():
    out = []
    for meta in sorted(CAPTURE.glob("msg-*.json")):
        out.append(email.message_from_bytes(meta.with_suffix(".eml").read_bytes(), policy=email.policy.default))
    return out


def wait_link(to: str, before: int, timeout: float = 30) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for parsed in mails()[before:]:
            if parsed["To"] == to:
                return re.search(r"(https://\S+#token=[A-Za-z0-9_\-]+)", parsed.get_content()).group(1)
        time.sleep(0.25)
    raise AssertionError("no mail for " + to)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    cfg = json.loads(a.input.read_text(encoding="utf-8"))
    origin, admin = cfg["origin"], cfg["admin"]
    checks, denied, requests = [], [], []

    def check(label, ok, detail=None):
        checks.append({"check": label, "pass": bool(ok), **({"detail": detail} if detail is not None else {})})
        (a.out / "result.json").write_text(json.dumps({"checks": checks, "outbound_denied": denied,
                                                        "requests": requests}, indent=2, ensure_ascii=False))
        print(("PASS " if ok else "FAIL ") + label, flush=True)
        if not ok:
            raise SystemExit("Chrome check failed: " + label)

    expect.set_options(timeout=60000)
    trusted_home = nss_home(Path("/tmp/m8a-home-trusted"), Path("/pki/ca.crt"))
    empty_home = nss_home(Path("/tmp/m8a-home-empty"), None)
    with sync_playwright() as pw:
        control = pw.chromium.launch(channel="chrome", headless=True, env={**os.environ, "HOME": str(empty_home)})
        try:
            control.new_page().goto(origin + "/login")
            outcome = "loaded"
        except PlaywrightError as exc:
            outcome = "ERR_CERT_AUTHORITY_INVALID" if "ERR_CERT_AUTHORITY_INVALID" in str(exc) else str(exc)[:120]
        control.close()
        check("control: Chrome without the test CA refuses the site (ERR_CERT_AUTHORITY_INVALID)",
              outcome == "ERR_CERT_AUTHORITY_INVALID", outcome)

        browser = pw.chromium.launch(channel="chrome", headless=True, env={**os.environ, "HOME": str(trusted_home)})
        context = browser.new_context()

        def route(r):
            if r.request.url.startswith(origin + "/"):
                r.continue_()
            else:
                denied.append(r.request.url)
                r.abort()

        context.route("**/*", route)
        page = context.new_page()
        page.on("request", lambda req: requests.append({"method": req.method, "path": req.url.removeprefix(origin)})
                if req.url.startswith(origin + "/api/") else None)

        def login(email_address, password, expect_home=True):
            page.goto(origin + "/login")
            page.get_by_label("Email", exact=True).fill(email_address)
            page.get_by_label("Mật khẩu", exact=True).fill(password)
            page.get_by_role("button", name="Đăng nhập", exact=True).click()
            if expect_home:
                page.wait_for_url(origin + "/", timeout=60000)

        def logout():
            page.get_by_role("button", name="Đăng xuất", exact=True).click()
            page.wait_for_url("**/login", timeout=60000)

        login(admin["email"], admin["password"])
        check("Chrome verifies the test CA chain and logs the Admin in over HTTPS", page.url == origin + "/")
        session = next(c for c in context.cookies() if c["name"] == "__Host-ras_session")
        check("browser holds __Host-ras_session Secure, HttpOnly, SameSite=Lax",
              session["secure"] and session["httpOnly"] and session["sameSite"] == "Lax" and session["path"] == "/",
              {k: session[k] for k in ("secure", "httpOnly", "sameSite", "path", "domain")})
        member = {"email": f"m8a-chrome-{secrets.token_hex(3)}@pilot.test", "password": secrets.token_urlsafe(18)}
        before = len(mails())
        page.goto(origin + "/admin")
        page.get_by_placeholder("email@example.org").fill(member["email"])
        page.get_by_role("button", name="Gửi lời mời", exact=True).click()
        expect(page.get_by_role("status")).to_contain_text("chưa xác nhận người nhận đã nhận được email")
        check("Admin invites from the UI; the notice does not promise receipt", True)
        link = wait_link(member["email"], before)
        logout()
        page.goto(link)
        expect(page.get_by_text(member["email"], exact=False).first).to_be_visible()
        page.get_by_label("Mật khẩu mới (tối thiểu 12 ký tự)").fill(member["password"])
        page.get_by_label("Nhập lại mật khẩu").fill(member["password"])
        page.get_by_role("button", name="Tạo tài khoản", exact=True).click()
        expect(page.get_by_role("heading", name="Đã tạo tài khoản")).to_be_visible()
        check("invite link from the SMTP capture activates the account in Chrome", page.url == origin + "/invite")
        login(member["email"], member["password"])
        expect(page.get_by_role("heading", name="Thư viện", exact=True)).to_be_visible()
        check("new User logs in", True)
        logout()

        before = len(mails())
        page.goto(origin + "/forgot-password")
        page.get_by_label("Email", exact=True).fill(member["email"])
        page.get_by_role("button", name="Gửi liên kết", exact=True).click()
        expect(page.get_by_text("Yêu cầu đã được tiếp nhận", exact=False)).to_be_visible()
        link = wait_link(member["email"], before)
        page.goto(link)
        new_password = secrets.token_urlsafe(18)
        page.get_by_label("Mật khẩu mới (tối thiểu 12 ký tự)").fill(new_password)
        page.get_by_label("Nhập lại mật khẩu").fill(new_password)
        page.get_by_role("button", name="Đổi mật khẩu", exact=True).click()
        expect(page.get_by_role("heading", name="Đã đổi mật khẩu")).to_be_visible()
        check("reset link from the capture changes the password in Chrome", True)
        login(member["email"], member["password"], expect_home=False)
        expect(page.get_by_text("Email hoặc mật khẩu không đúng.", exact=True)).to_be_visible()
        check("old password is refused in the UI", True)
        login(member["email"], new_password)

        data = f"# Chrome M8a synthetic {time.time_ns()}\n\n## Methods\nChrome pilot synthetic evidence.\n".encode()
        selected = a.out / "chrome-m8a.md"
        selected.write_bytes(data)
        page.locator("input[type=file]").set_input_files(str(selected))
        expect(page.get_by_role("button", name="Tải lên", exact=True)).to_be_disabled()
        page.get_by_label("Tôi đồng ý xử lý ngoài cho lần upload này.").check()
        with page.expect_response(lambda r: r.url.endswith("/api/uploads/original") and r.request.method == "PUT") as sent:
            page.get_by_role("button", name="Tải lên", exact=True).click()
        check("per-upload consent then upload over HTTPS", sent.value.status == 201)
        received = page.get_by_role("link", name="Xem item", exact=True)
        expect(received).to_be_visible()
        item = received.get_attribute("href").rsplit("/", 1)[1]
        deadline = time.monotonic() + 120
        while True:
            page.goto(origin + "/items/" + item)
            try:  # the page renders the job list after its own client-side fetch
                page.get_by_text("Đã trích xuất, chờ lập chỉ mục", exact=False).first.wait_for(timeout=8000)
                break
            except PlaywrightError:
                if time.monotonic() > deadline:
                    page.screenshot(path=str(a.out / "item-not-extracted.png"), full_page=True)
                    raise SystemExit("extraction state never shown")
            time.sleep(1)
        page.screenshot(path=str(a.out / "item-extracted.png"), full_page=True)
        check("item page shows extraction done, waiting for index (pricing false)", True)
        page.goto(origin + "/")
        expect(page.locator(f'a[href="/items/{item}"]').first).to_be_visible()
        check("library lists the upload", True)
        page.goto(origin + "/collections")
        page.get_by_label("Tên collection mới").fill("Pilot M8a")
        page.get_by_role("button", name="Tạo", exact=True).click()
        expect(page.get_by_role("button", name="Thêm paper", exact=True)).to_be_visible()
        page.get_by_label("Paper từ thư viện").fill(item)
        page.get_by_role("button", name="Thêm paper", exact=True).click()
        expect(page.get_by_role("button", name="Bỏ khỏi collection", exact=True)).to_be_visible()
        check("collection created and the upload added", True)
        page.goto(origin + "/search")
        page.get_by_label("Câu tìm kiếm").fill("pilot synthetic evidence")
        page.get_by_label("Chế độ").select_option("hybrid")
        page.get_by_role("button", name="Tìm kiếm", exact=True).click()
        expect(page.get_by_text("Chế độ: hybrid", exact=False)).to_contain_text("0 đoạn")
        check("hybrid search over unindexed uploads shows 0 passages (no provider call; not a quality result)", True)
        page.get_by_label("Chế độ").select_option("keyword")
        with page.expect_response(lambda r: r.url.endswith("/api/search") and r.request.method == "POST") as found:
            page.get_by_role("button", name="Tìm kiếm", exact=True).click()
        check("keyword search request completes (unindexed upload is not searchable)", found.value.status == 200)
        page.screenshot(path=str(a.out / "search.png"), full_page=True)
        logout()

        ghost = f"ghost-{secrets.token_hex(3)}@pilot.test"
        print("... waiting 32s for the smoke rate-limit window", flush=True)
        time.sleep(32)
        posts_before = sum(1 for r in requests if r["method"] == "POST" and r["path"] == "/api/auth/login")
        for _ in range(6):
            login(ghost, "wrong-password-xx", expect_home=False)
            page.wait_for_timeout(400)
        message = page.get_by_text("Quá nhiều yêu cầu trong thời gian ngắn", exact=False)
        expect(message).to_be_visible()
        text = message.inner_text()
        page.wait_for_timeout(2000)
        posts = sum(1 for r in requests if r["method"] == "POST" and r["path"] == "/api/auth/login") - posts_before
        check("429 is shown with the wait time and the page does not resubmit",
              re.search(r"khoảng \d+ giây", text) is not None and posts == 6, {"text": text, "posts": posts})
        page.screenshot(path=str(a.out / "rate-limited.png"), full_page=True)
        check("no request left the HTTPS origin", denied == [], denied)
        version = browser.version
        browser.close()
    (a.out / "result.json").write_text(json.dumps({"chrome": version, "checks": checks, "outbound_denied": denied,
                                                    "requests": requests}, indent=2, ensure_ascii=False))
    print(f"Chrome {version}: {len(checks)} checks pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
