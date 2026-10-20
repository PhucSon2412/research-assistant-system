"""M8b Chrome check over HTTPS after the host-loss restore (runs in rasm8a-browser:local).

The throwaway test CA lives in a per-run NSS database (Chrome verifies chain and name
itself); a control run without it must fail. Requests outside the HTTPS origin are aborted.
Checks: login, library, byte-exact original download of a kept item, deleted items 404,
and a session revoked before the host was lost cannot be reused.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright


def nss_home(path: Path, ca: Path | None) -> Path:
    db = path / ".pki" / "nssdb"
    db.mkdir(parents=True, exist_ok=True)
    subprocess.run(["certutil", "-d", f"sql:{db}", "-N", "--empty-password"], check=True)
    if ca is not None:
        subprocess.run(["certutil", "-d", f"sql:{db}", "-A", "-t", "C,,", "-n", "ras-m8b-drill-ca", "-i", str(ca)],
                       check=True)
    return path


SHA256_FETCH = """async (url) => {
  const r = await fetch(url, {credentials: 'same-origin'});
  if (r.status !== 200) return {status: r.status};
  const digest = await crypto.subtle.digest('SHA-256', await r.arrayBuffer());
  return {status: 200, sha256: Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, '0')).join('')};
}"""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    cfg = json.loads(a.input.read_text(encoding="utf-8"))
    origin = cfg["origin"]
    checks, denied = [], []

    def check(label, ok, detail=None):
        checks.append({"check": label, "pass": bool(ok), **({"detail": detail} if detail is not None else {})})
        (a.out / "result.json").write_text(json.dumps({"checks": checks, "outbound_denied": denied}, indent=1,
                                                      ensure_ascii=False))
        print(("PASS " if ok else "FAIL ") + label, flush=True)
        if not ok:
            raise SystemExit("Chrome check failed: " + label)

    expect.set_options(timeout=60000)
    trusted = nss_home(Path("/tmp/m8b-home-trusted"), Path("/pki/ca.crt"))
    empty = nss_home(Path("/tmp/m8b-home-empty"), None)
    with sync_playwright() as pw:
        control = pw.chromium.launch(channel="chrome", headless=True, env={**os.environ, "HOME": str(empty)})
        try:
            control.new_page().goto(origin + "/login")
            outcome = "loaded"
        except PlaywrightError as exc:
            outcome = "ERR_CERT_AUTHORITY_INVALID" if "ERR_CERT_AUTHORITY_INVALID" in str(exc) else str(exc)[:120]
        control.close()
        check("control: Chrome without the test CA refuses the restored site", outcome == "ERR_CERT_AUTHORITY_INVALID",
              outcome)
        browser = pw.chromium.launch(channel="chrome", headless=True, env={**os.environ, "HOME": str(trusted)})

        def guarded():
            context = browser.new_context()

            def route(r):
                if r.request.url.startswith(origin + "/"):
                    r.continue_()
                else:
                    denied.append(r.request.url)
                    r.abort()
            context.route("**/*", route)
            return context

        context = guarded()
        page = context.new_page()
        page.goto(origin + "/login")
        page.get_by_label("Email", exact=True).fill(cfg["a"]["email"])
        page.get_by_label("Mật khẩu", exact=True).fill(cfg["a"]["password"])
        page.get_by_role("button", name="Đăng nhập", exact=True).click()
        page.wait_for_url(origin + "/", timeout=60000)
        check("restored deployment: User logs in over HTTPS in Chrome", page.url == origin + "/")
        keep = cfg["keep"]
        expect(page.locator(f'a[href="/items/{keep["item"]}"]').first).to_be_visible()
        listed = {d: page.locator(f'a[href="/items/{d}"]').count() for d in cfg["deleted_items"]}
        check("library lists the kept item and none of the deleted ones", all(v == 0 for v in listed.values()), listed)
        page.screenshot(path=str(a.out / "library.png"), full_page=True)
        got = page.evaluate(SHA256_FETCH, f"/api/files/{keep['file']}/original")
        check("original of the kept item downloads byte-exact (SHA-256)", got == {"status": 200, "sha256": keep["sha256"]},
              got)
        gone = {d: page.evaluate("async (u) => (await fetch(u)).status", f"/api/library/items/{d}")
                for d in cfg["deleted_items"]}
        check("deleted items answer 404 after the restore", all(v == 404 for v in gone.values()), gone)
        page.goto(origin + "/items/" + cfg["deleted_items"][0])
        page.screenshot(path=str(a.out / "deleted-item.png"), full_page=True)
        page.get_by_role("button", name="Đăng xuất", exact=True).click()
        page.wait_for_url("**/login", timeout=60000)
        check("User logs out in the UI", True)
        context.close()
        revoked = guarded()
        revoked.add_cookies([{"name": name, "value": value, "url": origin}
                             for name, value in cfg["revoked_session"].items()])
        page = revoked.new_page()
        page.goto(origin + "/")
        status = page.evaluate("async () => (await fetch('/api/auth/session')).status")
        try:
            page.wait_for_url("**/login**", timeout=20000)
            at_login = True
        except PlaywrightError:
            at_login = "/login" in page.url
        page.screenshot(path=str(a.out / "revoked-session.png"), full_page=True)
        check("a session revoked before the host was lost is not usable (401, sent to login)",
              status == 401 and at_login, {"status": status, "url": page.url.removeprefix(origin)})
        check("no request left the HTTPS origin", denied == [], denied)
        version = browser.version
        browser.close()
    (a.out / "result.json").write_text(json.dumps({"chrome": version, "checks": checks, "outbound_denied": denied},
                                                  indent=1, ensure_ascii=False))
    print(f"Chrome {version}: {len(checks)} checks pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
