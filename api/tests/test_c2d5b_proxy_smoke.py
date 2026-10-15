"""Actual socket Uvicorn -> Next JSON proxy -> installed headless Chrome approve form."""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time

import httpx
from playwright.sync_api import sync_playwright
import uvicorn

from app.config import get_settings
from app.main import app
from tests.resource_fixtures import BODY
from tests.test_c2d5b_review_approval import approvals, Originals, body, opened, state  # noqa: F401
from tests.test_c2d5a_review_writes import writers  # noqa: F401
from tests.test_c2d1_final_caller import browser  # noqa: F401
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_c2c2_invitations import operational_world as world  # noqa: F401

API, WEB = 58476, 53476


def test_real_proxy_chrome_approve_thin_unknown_outcome_and_exact_timeout(approvals, tmp_path, monkeypatch):
    b = approvals
    root = Path(__file__).resolve().parents[2]
    assert not list((root / 'web').glob('.env*'))
    chrome = Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    assert chrome.is_file()
    api, web = f'http://127.0.0.1:{API}', f'http://127.0.0.1:{WEB}'
    for port in (API, WEB):
        with socket.socket() as s:
            s.bind(('127.0.0.1', port))
    monkeypatch.setenv('RAS_HTTP__ALLOWED_ORIGINS', json.dumps(['http://web.test', web]))
    get_settings.cache_clear()
    posts = []
    delay = {'seconds': 0}

    def slow(key):
        if key == b.own.key:
            posts.append(time.monotonic())
            time.sleep(delay['seconds'])

    b.storage.hooks['open'] = slow
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=API, log_level='critical', access_log=False))
    thread = threading.Thread(target=server.run)
    proc = None
    log = (tmp_path / 'next.log').open('w', encoding='utf-8')

    def wait(url):
        for _ in range(100):
            try:
                if httpx.get(url).status_code in (200, 401):
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        raise AssertionError('isolated proxy server unavailable')

    thread.start()
    try:
        wait(api + '/healthz')
        # Exact approve gets 20 s here (normal 15 s): long enough for a 16 s copy,
        # short enough to exercise the unknown outcome with a 25 s copy.
        proc = subprocess.Popen(['node', str(root / 'web/node_modules/next/dist/bin/next'), 'start', str(root / 'web'),
                                 '--hostname', '127.0.0.1', '--port', str(WEB)],
                                env=os.environ | {'API_INTERNAL_URL': api, 'WEB_PROXY_REVIEW_APPROVAL_TIMEOUT_SECONDS': '20'},
                                stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        wait(web + '/login')
        with sync_playwright() as pw:
            chrome_browser = pw.chromium.launch(executable_path=str(chrome), headless=True)
            try:
                ctx = chrome_browser.new_context()
                ctx.add_cookies([{'name': 'ras_session', 'value': b.w.actor.token, 'url': web, 'httpOnly': True},
                                 {'name': 'ras_csrf', 'value': b.w.actor.csrf_token, 'url': web}])
                page = ctx.new_page()

                def field(label):
                    return page.get_by_role('textbox', name=re.compile('^' + re.escape(label)))

                def fill_and_submit():
                    page.goto(web + f'/admin/reviews/{b.rid}')
                    confirm = page.get_by_role('checkbox', name=re.compile('Tôi đã tự kiểm quyền'))
                    confirm.wait_for()
                    submit = page.get_by_role('button', name='Phê duyệt (chờ công bố)', exact=True)
                    # Every attempt starts empty and unticked; nothing comes from the declaration.
                    assert not confirm.is_checked() and submit.is_disabled()
                    for label in ('Tiêu đề bản chung', 'Nguồn file đã kiểm', 'URL giấy phép đã kiểm',
                                  'Bằng chứng khác đã kiểm'):
                        assert field(label).input_value() == ''
                    field('Tiêu đề bản chung').fill('Synthetic browser common')
                    field('Nguồn file đã kiểm').fill('Synthetic source checked in browser')
                    field('Bằng chứng khác đã kiểm').fill('Synthetic written permission')
                    page.get_by_label('Ngày kiểm').fill(b.at.date().isoformat())
                    page.get_by_label('Cho User tải file gốc').check()
                    assert submit.is_disabled()  # still needs the explicit confirmation
                    confirm.check()
                    return confirm, submit

                confirm, submit = fill_and_submit()
                with page.expect_response(lambda r: r.url.endswith('/approve') and r.request.method == 'POST') as rsp:
                    submit.click()
                r = rsp.value
                assert r.status == 200, r.text()
                data = r.json()['request']
                assert set(data) == {'id', 'status', 'submitted_at', 'closed_at', 'close_reason_code',
                                     'review_available', 'publication', 'pending_note', 'pending_publication_id'}
                assert data['status'] == 'approved' and data['review_available'] is False
                page.get_by_text('Không còn quyền xem nội dung của yêu cầu này').wait_for()
                assert page.get_by_role('checkbox', name=re.compile('Tôi đã tự kiểm quyền')).count() == 0
                first = b.rid
                # The old review download is gone through the real streaming route.
                old = httpx.get(web + f'/api/admin/review-requests/{first}/files/{b.own.file}/original',
                                headers={'cookie': f'ras_session={b.w.actor.token}'})
                assert old.status_code == 404

                # A copy longer than the normal 15 s still answers through the exact approve budget.
                b.rid = None
                opened(b)
                delay['seconds'] = 16
                headers = {'cookie': f'ras_session={b.w.actor.token}; ras_csrf={b.w.actor.csrf_token}',
                           'Origin': web, 'X-CSRF-Token': b.w.actor.csrf_token}
                started = time.monotonic()
                slow_ok = httpx.post(web + f'/api/admin/review-requests/{b.rid}/approve', json=body(), headers=headers,
                                     timeout=60)
                assert slow_ok.status_code == 200 and time.monotonic() - started >= 16, slow_ok.text
                assert slow_ok.headers.get('cache-control') == 'no-store'

                # Proxy timeout: the browser is told the outcome is unknown and never re-sends.
                b.rid = None
                opened(b)
                delay['seconds'] = 25
                before = len(posts)
                confirm, submit = fill_and_submit()
                with page.expect_response(lambda r: r.url.endswith('/approve') and r.request.method == 'POST',
                                          timeout=40_000) as rsp:
                    submit.click()
                assert rsp.value.status == 504 and rsp.value.json() == {'error': 'api_timeout'}
                page.get_by_text('Không xác định được kết quả phê duyệt').wait_for()
                assert not confirm.is_checked()
                time.sleep(8)  # the backend attempt settles on its own; no second POST from the page
                assert len(posts) == before + 1
                page.reload()
                page.get_by_text('Không còn quyền xem nội dung của yêu cầu này').wait_for()
                assert [r[1] for r in state(b).requests].count('approved') == 3
                print(f'Chrome {chrome_browser.version}: empty/unticked form, approve 200 thin, '
                      '16 s approve via exact budget, 504 shown as unknown outcome without re-POST')
                ctx.close()
            finally:
                chrome_browser.close()
        # Controlled denials pass through unchanged.
        b.rid = None
        opened(b)
        delay['seconds'] = 0
        owner = {'cookie': f'ras_session={b.owner_session.token}; ras_csrf={b.owner_session.csrf_token}',
                 'Origin': web, 'X-CSRF-Token': b.owner_session.csrf_token}
        r = httpx.post(web + f'/api/admin/review-requests/{b.rid}/approve', json=body(), headers=owner)
        assert r.status_code == 403 and r.json() == {'error': 'forbidden'}
        r = httpx.post(web + f'/api/admin/review-requests/{b.rid}/approve', json=body(),
                       headers={'cookie': 'ras_session=expired-synthetic; ras_csrf=x', 'Origin': web, 'X-CSRF-Token': 'x'})
        assert r.status_code == 401 and 'Max-Age=0' in r.headers['set-cookie']
        assert r.headers.get('cache-control') == 'no-store'
        assert b.storage.objects[b.own.key] == BODY
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(15)
        server.should_exit = True
        thread.join(30)
        assert not thread.is_alive()
        log.close()
