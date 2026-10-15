"""Installed Chrome -> Next production proxy -> working-tree socket API, index request.

Real isolated runtime PG/MinIO/C4 contract; only the test clock is scoped. The
lost-response case holds the API response past the normal 15 s proxy budget
with an in-process barrier (the API keeps running); it is not a network fault.
This does not claim a Compose API image or production safety cutover.
"""
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import threading
import time

import httpx
import uvicorn
from playwright.sync_api import sync_playwright

from app.config import get_settings
from app.main import app
from app.api import common_index_request
from tests.test_c2d5d1_common_index_request import indexes, jobs  # noqa: F401
from tests.test_c2d5c1_common_publish import scalar, change
from tests.test_c2d5a_review_writes import writers  # noqa: F401
from tests.test_c2d1_final_caller import browser  # noqa: F401
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_c2c2_invitations import operational_world as world  # noqa: F401

API_PORT, WEB_PORT = 58589, 53589


def test_chrome_index_unticked_busy_reuse_unknown_no_repost_and_denials(indexes, tmp_path, monkeypatch):
    b = indexes
    root = Path(__file__).resolve().parents[2]
    assert not list((root / 'web').glob('.env*'))
    chrome = Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    assert chrome.is_file()
    api, web = f'http://127.0.0.1:{API_PORT}', f'http://127.0.0.1:{WEB_PORT}'
    for port in (API_PORT, WEB_PORT):
        with socket.socket() as s:
            s.bind(('127.0.0.1', port))
    monkeypatch.setenv('RAS_HTTP__ALLOWED_ORIGINS', json.dumps(['http://web.test', web]))
    get_settings.cache_clear()
    entered, release = threading.Event(), threading.Event()
    mode = {'value': 'busy'}
    real_accept, real_respond = common_index_request.accept, common_index_request.respond
    def accept(*a):
        if mode['value'] == 'busy':
            entered.set(); assert release.wait(30)
        return real_accept(*a)
    def respond(*a):
        if mode['value'] == 'lost':
            entered.set(); assert release.wait(40)
        return real_respond(*a)
    monkeypatch.setattr(common_index_request, 'accept', accept)
    monkeypatch.setattr(common_index_request, 'respond', respond)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=API_PORT, log_level='critical', access_log=False))
    thread = threading.Thread(target=server.run)
    proc = None
    log = (tmp_path / 'next.log').open('w', encoding='utf-8')
    def wait(url):
        for _ in range(150):
            try:
                if httpx.get(url).status_code in (200, 401): return
            except httpx.HTTPError: pass
            time.sleep(.1)
        raise AssertionError('isolated proxy unavailable')
    def headers(issued):
        return {'cookie': f'ras_session={issued.token}; ras_csrf={issued.csrf_token}',
                'Origin': web, 'X-CSRF-Token': issued.csrf_token}
    def drain():
        for _ in range(200):
            if not common_index_request._attempts: break
            time.sleep(.05)
        assert not common_index_request._attempts
    thread.start()
    try:
        wait(api + '/healthz')
        proc = subprocess.Popen(['node', str(root / 'web/node_modules/next/dist/bin/next'), 'start', str(root / 'web'),
            '--hostname', '127.0.0.1', '--port', str(WEB_PORT)],
            env=os.environ | {'API_INTERNAL_URL': api, 'WEB_PROXY_TIMEOUT_SECONDS': '15'},
            stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        wait(web + '/login')
        with sync_playwright() as pw:
            browser_ = pw.chromium.launch(executable_path=str(chrome), headless=True)
            try:
                ctx = browser_.new_context()
                ctx.add_cookies([{'name': 'ras_session', 'value': b.w.actor.token, 'url': web, 'httpOnly': True},
                                 {'name': 'ras_csrf', 'value': b.w.actor.csrf_token, 'url': web}])
                page = ctx.new_page(); posts = []
                page.on('request', lambda r: posts.append(r.url)
                        if r.method == 'POST' and r.url.endswith('/index') else None)
                def form():
                    page.goto(web + '/admin/publications')
                    page.get_by_role('button', name='Đã công bố metadata (toàn văn chưa khả dụng)', exact=True).click()
                    section = page.locator(f'section[id="{b.copy.item}"]')
                    confirm = section.get_by_role('checkbox', name='Tôi xác nhận lập chỉ mục bản này', exact=False)
                    confirm.wait_for()
                    submit = section.get_by_role('button', name=re.compile('^(Lập chỉ mục|Chạy lại . xem job)$'))
                    return section, confirm, submit
                section, confirm, submit = form()
                # Unticked by default: the button is disabled and nothing is posted.
                assert not confirm.is_checked() and submit.is_disabled()
                submit.evaluate('(button)=>button.form.requestSubmit()')
                page.wait_for_timeout(300)
                assert posts == [] and jobs(b) == []
                confirm.check()
                with page.expect_response(lambda r: r.url.endswith('/index') and r.request.method == 'POST') as rsp:
                    # Both synchronous events reach React before state re-renders.
                    submit.evaluate('(button)=>{button.form.requestSubmit();button.form.requestSubmit()}')
                    assert entered.wait(10)
                    assert submit.is_disabled() and confirm.is_disabled() and len(posts) == 1
                    release.set()
                assert rsp.value.status == 200, rsp.value.text()
                assert rsp.value.json()['created'] is True and len(posts) == 1
                [job] = jobs(b)
                assert rsp.value.json()['job']['id'] == str(job['id'])
                # Refreshed from the current GET: the job is shown, still not indexed.
                section.get_by_text('job received', exact=False).wait_for()
                assert not confirm.is_checked()
                # An active new POST is deduplicated by the server: the same live job.
                mode['value'] = 'none'
                confirm.check()
                with page.expect_response(lambda r: r.url.endswith('/index')) as again:
                    submit.click()
                assert again.value.status == 200 and again.value.json()['created'] is False
                assert again.value.json()['job']['id'] == str(job['id']) and len(jobs(b)) == 1
                # Known commit, response held past the normal 15 s proxy budget: unknown, no repost.
                replica_sql = "UPDATE ingestion_jobs SET state='terminal_failed',finished_at=now() WHERE id=:j"
                from tests.test_c2d5c1_common_publish import replica
                replica(b, replica_sql, j=job['id'])  # labelled setup: earlier job finished
                section, confirm, submit = form()
                mode['value'] = 'lost'; entered.clear(); release.clear()
                before = len(posts)
                confirm.check()
                with page.expect_response(lambda r: r.url.endswith('/index'), timeout=30_000) as lost:
                    submit.click(); assert entered.wait(10)
                    assert len(jobs(b)) == 2  # committed before the response proof
                assert lost.value.status == 504 and lost.value.json() == {'error': 'api_timeout'}
                section.get_by_text('Không xác định được kết quả yêu cầu lập chỉ mục', exact=False).wait_for()
                assert not confirm.is_checked() and submit.is_disabled() and len(posts) == before + 1
                release.set(); drain()
                assert len(posts) == before + 1 and len(jobs(b)) == 2
                ctx.close()
                print(f'Chrome {browser_.version}: unticked no POST, busy/double submit one POST, create/reuse, '
                      'refresh, known commit -> normal 15s proxy 504 -> unknown/no repost; socket API, scoped cutover')
            finally:
                browser_.close()
        mode['value'] = 'none'
        data = {'confirm_external_processing': True}
        url = web + f'/api/admin/common-copies/{b.copy.item}/index'
        assert httpx.post(url, json={'confirm_external_processing': 1}, headers=headers(b.w.actor)).status_code == 422
        assert httpx.post(url, json=data, headers=headers(b.owner_session)).status_code == 403
        expired = httpx.post(url, json=data, headers={'cookie': 'ras_session=invalid; ras_csrf=x', 'Origin': web,
                                                       'X-CSRF-Token': 'x'})
        assert expired.status_code == 401 and 'Max-Age=0' in expired.headers['set-cookie']
        assert expired.headers.get('cache-control') == 'no-store'
        change(b, 'role')  # Admin role removed after the session was issued
        denied = httpx.post(url, json=data, headers=headers(b.w.actor))
        assert denied.status_code == 403 and denied.json() == {'error': 'forbidden'}
        assert len(jobs(b)) == 2 and scalar(b, 'SELECT count(*) FROM ingestion_jobs') == 2
    finally:
        release.set()
        if proc is not None: proc.terminate(); proc.wait(15)
        server.should_exit = True; thread.join(30); assert not thread.is_alive()
        log.close()
