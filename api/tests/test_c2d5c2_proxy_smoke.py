"""Installed Chrome -> Next production proxy -> working-tree socket API.

Real isolated runtime PG/MinIO; only business cutover/clock are scoped.
This does not claim a Compose API image or production safety cutover.
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
from app.api import common_revoke
from tests.test_c2d5c1_common_publish import publishes, pending, publish, scalar  # noqa: F401
from tests.test_c2d5a_review_writes import writers  # noqa: F401
from tests.test_c2d1_final_caller import browser  # noqa: F401
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_c2c2_invitations import world  # noqa: F401


def test_chrome_revoke_busy_old_link_unknown_no_repost_cookie(publishes, tmp_path, monkeypatch):
    b = publishes
    root = Path(__file__).resolve().parents[2]
    assert not list((root / 'web').glob('.env*'))
    chrome = Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    assert chrome.is_file()
    api, web = 'http://127.0.0.1:58489', 'http://127.0.0.1:53489'
    for port in (58489, 53489):
        with socket.socket() as s:
            s.bind(('127.0.0.1', port))
    monkeypatch.setenv('RAS_HTTP__ALLOWED_ORIGINS', json.dumps(['http://web.test', web]))
    get_settings.cache_clear()
    from app.api.routes import uploads
    app.dependency_overrides[uploads._storage] = lambda: b.raw_storage
    assert publish(b).status_code == 200
    entered, release = threading.Event(), threading.Event()
    mode = {'value': 'busy'}
    real_accept, real_respond = common_revoke.accept, common_revoke.respond
    def accept(*a):
        if mode['value'] == 'busy':
            entered.set(); assert release.wait(30)
        return real_accept(*a)
    def respond(*a):
        if mode['value'] == 'lost':
            entered.set(); assert release.wait(40)
        return real_respond(*a)
    monkeypatch.setattr(common_revoke, 'accept', accept)
    monkeypatch.setattr(common_revoke, 'respond', respond)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=58489, log_level='critical', access_log=False))
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
    thread.start()
    try:
        wait(api + '/healthz')
        proc = subprocess.Popen(['node', str(root/'web/node_modules/next/dist/bin/next'), 'start', str(root/'web'),
            '--hostname', '127.0.0.1', '--port', '53489'], env=os.environ | {'API_INTERNAL_URL': api,'WEB_PROXY_TIMEOUT_SECONDS':'15'},
            stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        wait(web+'/login')
        with sync_playwright() as pw:
            browser_ = pw.chromium.launch(executable_path=str(chrome), headless=True)
            try:
                ctx = browser_.new_context()
                ctx.add_cookies([{'name':'ras_session','value':b.w.actor.token,'url':web,'httpOnly':True},
                                 {'name':'ras_csrf','value':b.w.actor.csrf_token,'url':web}])
                page=ctx.new_page(); posts=[]
                page.on('request',lambda r:posts.append(r.url) if r.method=='POST' and r.url.endswith('/revoke') else None)
                def form(listed=False):
                    page.goto(web+'/admin/publications')
                    if listed: page.get_by_role('button',name='Đã công bố metadata (toàn văn chưa khả dụng)',exact=True).click()
                    section=page.locator(f'section[id="{b.copy.item}"]')
                    confirm=section.get_by_role('checkbox',name=re.compile('Tôi xác nhận (thu hồi|hủy bản chờ)'))
                    confirm.wait_for()
                    submit=section.get_by_role('button',name='Thu hồi' if listed else 'Hủy bản chờ',exact=True)
                    reason=section.get_by_role('combobox')
                    assert not confirm.is_checked() and reason.input_value()=='' and submit.is_disabled()
                    reason.select_option('rights_withdrawn');confirm.check()
                    return section,confirm,submit,reason
                old=web+f'/api/files/{b.copy.file}/original'
                assert httpx.get(old,headers=headers(b.owner_session)).status_code==200
                section,confirm,submit,reason=form(True)
                with page.expect_response(lambda r:r.url.endswith('/revoke') and r.request.method=='POST') as rsp:
                    # Both synchronous events reach React before state rerenders.
                    submit.evaluate('(button)=>{button.form.requestSubmit();button.form.requestSubmit()}')
                    assert entered.wait(10)
                    assert submit.is_disabled() and reason.is_disabled() and confirm.is_disabled()
                    assert len(posts)==1
                    release.set()
                assert rsp.value.status==200,rsp.value.text()
                assert rsp.value.json()['item']['publication_state']=='revoked'
                assert httpx.get(old,headers=headers(b.owner_session)).status_code==404
                page.get_by_text('Không có bản nào.',exact=True).wait_for()
                b.copy=pending(b);mode['value']='lost';entered.clear();release.clear()
                section,confirm,submit,reason=form()
                before=len(posts)
                with page.expect_response(lambda r:r.url.endswith('/revoke'),timeout=30_000) as rsp:
                    submit.click();assert entered.wait(10)
                    assert scalar(b,'SELECT publication_state FROM library_items WHERE id=:i',i=b.copy.item)=='revoked'
                assert rsp.value.status==504 and rsp.value.json()=={'error':'api_timeout'}
                section.get_by_text('Không xác định được kết quả thu hồi.',exact=False).wait_for()
                assert not confirm.is_checked() and submit.is_disabled() and len(posts)==before+1
                release.set()
                # Drain detached backend without assuming a sleep proves settle.
                for _ in range(200):
                    if not common_revoke._attempts:break
                    time.sleep(.05)
                assert not common_revoke._attempts and len(posts)==before+1
                ctx.close()
                print(f'Chrome {browser_.version}: busy/double submit, runtime revoke, old original 404, '
                      'known commit -> normal 15s proxy 504 -> unknown/no repost; socket API, scoped cutover')
            finally: browser_.close()
        denied=httpx.post(web+f'/api/admin/common-copies/{b.copy.item}/revoke',json={'confirm_revoke':True,'reason_code':'other'},headers=headers(b.owner_session))
        assert denied.status_code==403
        expired=httpx.post(web+f'/api/admin/common-copies/{b.copy.item}/revoke',json={'confirm_revoke':True,'reason_code':'other'},
            headers={'cookie':'ras_session=invalid; ras_csrf=x','Origin':web,'X-CSRF-Token':'x'})
        assert expired.status_code==401 and 'Max-Age=0' in expired.headers['set-cookie']
        assert expired.headers.get('cache-control')=='no-store'
    finally:
        release.set()
        if proc is not None: proc.terminate();proc.wait(15)
        server.should_exit=True;thread.join(30);assert not thread.is_alive()
        log.close()
