"""Installed Chrome -> Next start proxy -> socket API -> runtime PG/real MinIO.

Business cutover/clock are scoped fixtures, never production gate overrides.
No Compose API image is claimed: socket Uvicorn serves the working tree.
"""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time

import httpx
import uvicorn
from playwright.sync_api import sync_playwright

from app.config import get_settings
from app.main import app
from app.api import common_publish
from tests.test_c2d5c1_common_publish import publishes, pending, scalar, SHA  # noqa: F401
from tests.test_c2d5a_review_writes import writers  # noqa: F401
from tests.test_c2d1_final_caller import browser  # noqa: F401
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_c2c2_invitations import world  # noqa: F401

API, WEB = 58486, 53486


def test_chrome_publish_metadata_checksum_acl_slow_hash_and_unknown_no_repost(publishes, tmp_path, monkeypatch):
    b = publishes
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
    delay = {'seconds': 0}
    reply_delay = {'seconds': 0}
    b.storage.hook = lambda key: time.sleep(delay['seconds'])
    real_respond = common_publish.respond
    def delayed_response(*a, **kw):
        time.sleep(reply_delay['seconds'])
        return real_respond(*a, **kw)
    monkeypatch.setattr(common_publish, 'respond', delayed_response)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=API, log_level='critical', access_log=False))
    thread = threading.Thread(target=server.run)
    proc = None
    log = (tmp_path / 'next.log').open('w', encoding='utf-8')

    def wait(url):
        for _ in range(150):
            try:
                if httpx.get(url).status_code in (200, 401):
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        raise AssertionError('isolated proxy server unavailable')

    def headers(issued):
        return {'cookie': f'ras_session={issued.token}; ras_csrf={issued.csrf_token}',
                'Origin': web, 'X-CSRF-Token': issued.csrf_token}

    thread.start()
    try:
        wait(api + '/healthz')
        # Exact POST publish: 20 s test budget, normal JSON stays 15 s.
        proc = subprocess.Popen(['node', str(root / 'web/node_modules/next/dist/bin/next'), 'start', str(root / 'web'),
            '--hostname', '127.0.0.1', '--port', str(WEB)], env=os.environ | {'API_INTERNAL_URL': api,
            'WEB_PROXY_PUBLICATION_TIMEOUT_SECONDS': '20'}, stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW)
        wait(web + '/login')
        with sync_playwright() as pw:
            browser_ = pw.chromium.launch(executable_path=str(chrome), headless=True)
            try:
                ctx = browser_.new_context()
                ctx.add_cookies([{'name': 'ras_session', 'value': b.w.actor.token, 'url': web, 'httpOnly': True},
                                 {'name': 'ras_csrf', 'value': b.w.actor.csrf_token, 'url': web}])
                page = ctx.new_page()
                posts = []
                page.on('request', lambda r: posts.append(r.url) if r.method == 'POST' and r.url.endswith('/publish') else None)

                def form(copy):
                    page.goto(web + '/admin/publications')
                    section = page.locator(f'section[id="{copy.item}"]')
                    confirm = section.get_by_role('checkbox', name=re.compile('Tôi xác nhận công bố'))
                    confirm.wait_for()
                    submit = section.get_by_role('button', name='Công bố metadata', exact=True)
                    assert not confirm.is_checked() and submit.is_disabled()
                    assert section.locator('code').inner_text() == copy.checksum
                    confirm.check()
                    return confirm, submit, section

                confirm, submit, section = form(b.copy)
                with page.expect_response(lambda r: r.url.endswith('/publish') and r.request.method == 'POST') as rsp:
                    submit.click()
                r = rsp.value
                assert r.status == 200, r.text()
                dto = r.json()['item']
                assert (dto['publication_state'], dto['content_status'], dto['file']['checksum_sha256']) == (
                    'listed', 'metadata_only', SHA)
                page.get_by_text('Không có bản nào.', exact=True).wait_for()
                ordinary = httpx.get(web + f'/api/library/items/{b.copy.item}', headers=headers(b.owner_session))
                assert ordinary.status_code == 200 and ordinary.json()['item']['content_status'] == 'metadata_only'
                assert ordinary.json()['item']['fulltext_search_available'] is False

                # Unchanged bytes are a separate candidate, so revoke the first
                # via the labelled legacy journaled owner adversary before publishing another.
                from tests.test_c2d5c1_common_publish import revoke
                revoke(b)
                b.copy = pending(b)
                delay['seconds'] = 16
                confirm, submit, section = form(b.copy)
                started = time.monotonic()
                with page.expect_response(lambda r: r.url.endswith('/publish'), timeout=35_000) as rsp:
                    submit.click()
                assert rsp.value.status == 200 and time.monotonic() - started >= 16
                assert rsp.value.headers.get('cache-control') == 'no-store'
                page.get_by_text('Không có bản nào.', exact=True).wait_for()

                revoke(b)
                b.copy = pending(b)
                delay['seconds'] = 0
                reply_delay['seconds'] = 25  # known commit, proxy loses the response
                before = len(posts)
                confirm, submit, section = form(b.copy)
                with page.expect_response(lambda r: r.url.endswith('/publish'), timeout=40_000) as rsp:
                    submit.click()
                assert rsp.value.status == 504 and rsp.value.json() == {'error': 'api_timeout'}
                section.get_by_text('Không xác định được kết quả công bố', exact=False).wait_for()
                assert not confirm.is_checked() and submit.is_disabled()
                time.sleep(8)  # detached backend settles; page never retries the POST
                assert len(posts) == before + 1
                assert scalar(b, 'SELECT publication_state FROM library_items WHERE id=:i', i=b.copy.item) == 'listed'
                delay['seconds'] = 0
                reply_delay['seconds'] = 0

                # Network response loss is also unknown: route.abort only loses
                # this browser response, no artificial API success or auth override.
                revoke(b)
                b.copy = pending(b)
                page.route('**/api/admin/common-copies/*/publish', lambda route: route.abort('failed'))
                before = len(posts)
                confirm, submit, section = form(b.copy)
                submit.click()
                section.get_by_text('Không xác định được kết quả công bố', exact=False).wait_for()
                assert not confirm.is_checked() and submit.is_disabled()
                time.sleep(.2)
                assert len(posts) == before + 1
                page.unroute('**/api/admin/common-copies/*/publish')
                ctx.close()
                owner_context = browser_.new_context()
                owner_context.add_cookies([
                    {'name': 'ras_session', 'value': b.owner_session.token, 'url': web, 'httpOnly': True},
                    {'name': 'ras_csrf', 'value': b.owner_session.csrf_token, 'url': web}])
                owner_page = owner_context.new_page()
                owner_page.goto(web + '/admin/publications')
                owner_page.get_by_text('Bạn không có quyền thực hiện thao tác này.', exact=True).wait_for()
                assert owner_page.get_by_role('button', name='Công bố metadata', exact=True).count() == 0
                owner_context.close()
                print(f'Chrome {browser_.version}: runtime metadata/checksum, unticked form, 16 s hash, '
                      '504/lost response unknown without re-POST; Next/socket PG/MinIO, scoped cutover')
            finally:
                browser_.close()
        denied = httpx.post(web + f'/api/admin/common-copies/{b.copy.item}/publish',
            json={'confirm_publish': True, 'file_checksum_sha256': SHA}, headers=headers(b.owner_session))
        assert denied.status_code == 403 and denied.json() == {'error': 'forbidden'}
        expired = httpx.post(web + f'/api/admin/common-copies/{b.copy.item}/publish',
            json={'confirm_publish': True, 'file_checksum_sha256': SHA},
            headers={'cookie': 'ras_session=invalid; ras_csrf=x', 'Origin': web, 'X-CSRF-Token': 'x'})
        assert expired.status_code == 401 and 'Max-Age=0' in expired.headers['set-cookie']
        assert expired.headers.get('cache-control') == 'no-store'
    finally:
        if proc is not None:
            proc.terminate(); proc.wait(15)
        server.should_exit = True
        thread.join(30)
        assert not thread.is_alive()
        log.close()
