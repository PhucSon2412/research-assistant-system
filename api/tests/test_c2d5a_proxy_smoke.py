"""Actual socket Uvicorn -> Next JSON proxy -> installed headless Chrome form."""
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
from tests.test_c2d5a_review_writes import writers
from tests.test_c2d1_final_caller import browser
from tests.test_c2c1_bootstrap import installation
from tests.test_c2c2_invitations import operational_world as world, issue, accept


def test_real_proxy_chrome_consent_submit_withdraw_reject_thin_response(writers,tmp_path,monkeypatch):
    b=writers;root=Path(__file__).resolve().parents[2]
    assert not list((root/'web').glob('.env*'))
    chrome=Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    assert chrome.is_file()
    api='http://127.0.0.1:58468';web='http://127.0.0.1:53467'
    for port in (58468,53467):
        with socket.socket() as s:s.bind(('127.0.0.1',port))
    monkeypatch.setenv('RAS_HTTP__ALLOWED_ORIGINS',json.dumps(['http://web.test',web]))
    get_settings.cache_clear()
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=58468,log_level='critical',access_log=False))
    thread=threading.Thread(target=server.run);proc=None
    log=(tmp_path/'next.log').open('w',encoding='utf-8')
    def wait(url):
        for _ in range(100):
            try:
                if httpx.get(url).status_code in (200,401):return
            except httpx.HTTPError:pass
            time.sleep(.1)
        raise AssertionError('isolated proxy server unavailable')
    thread.start()
    try:
        wait(api+'/healthz')
        proc=subprocess.Popen(['node',str(root/'web/node_modules/next/dist/bin/next'),'start',str(root/'web'),
            '--hostname','127.0.0.1','--port','53467'],env=os.environ|{'API_INTERNAL_URL':api},
            stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        wait(web+'/login')
        with sync_playwright() as pw:
            chrome_browser=pw.chromium.launch(executable_path=str(chrome),headless=True)
            try:
                def context(issued):
                    ctx=chrome_browser.new_context()
                    ctx.add_cookies([{'name':'ras_session','value':issued.token,'url':web,'httpOnly':True},
                        {'name':'ras_csrf','value':issued.csrf_token,'url':web}])
                    return ctx
                owner=context(b.owner_session);page=owner.new_page();page.goto(web+f'/items/{b.own.item}')
                checkbox=page.get_by_role('checkbox',name=re.compile('Tôi hiểu rằng gửi yêu cầu'))
                checkbox.wait_for();assert not checkbox.is_checked()
                submit=page.get_by_role('button',name='Gửi yêu cầu duyệt',exact=True)
                assert submit.is_disabled()
                def send():
                    page.get_by_label('Version/file gửi duyệt').select_option(str(b.own.file))
                    page.get_by_label('Nguồn của file').fill('Synthetic browser source')
                    page.get_by_label('Quyền phân phối bạn biết').fill('Synthetic browser permission, unverified')
                    checkbox.check()
                    with page.expect_response(lambda r:r.url.endswith('/api/review-requests') and r.request.method=='POST') as response:submit.click()
                    r=response.value;assert r.status==201,r.text()
                    return r.json()['request']['id']
                rid=send()
                with page.expect_response(lambda r:r.url.endswith('/withdraw') and r.request.method=='POST') as response:
                    page.get_by_role('button',name='Rút yêu cầu',exact=True).click()
                assert response.value.status==200 and response.value.json()['request']['status']=='withdrawn'
                # Consent remains per-attempt and is reset after submit.
                assert not checkbox.is_checked()
                second=send()
                admin=context(b.w.actor);review=admin.new_page();review.goto(web+f'/admin/reviews/{second}')
                review.get_by_label('Lý do').select_option('other')
                review.get_by_label('Ghi chú ngắn cho User').fill('Synthetic browser note')
                with review.expect_response(lambda r:r.url.endswith('/reject') and r.request.method=='POST') as response:
                    review.get_by_role('button',name='Từ chối yêu cầu',exact=True).click()
                r=response.value;assert r.status==200,r.text();data=r.json()['request']
                assert data['status']=='rejected' and data['review_available'] is False
                assert not {'item','file','version','declaration','requester_email','original_url'}&data.keys()
                review.get_by_role('button',name='Từ chối yêu cầu',exact=True).wait_for(state='hidden')
                review.screenshot(path=str(tmp_path/'thin-history.png'))
                print(f'Chrome {chrome_browser.version}: empty consent, submit/withdraw/reject via real Next JSON proxy')
                admin.close();owner.close()
            finally:chrome_browser.close()
        # Real proxy preserves controlled denial and session clearing.
        headers={'cookie':'ras_session='+b.owner_session.token+'; ras_csrf='+b.owner_session.csrf_token,
                 'Origin':web,'X-CSRF-Token':b.owner_session.csrf_token}
        r=httpx.post(web+f'/api/admin/review-requests/{second}/reject',headers=headers,json={'reason_code':'other'})
        assert r.status_code==403 and r.json()=={'error':'forbidden'}
        r=httpx.get(web+'/api/review-requests',headers={'cookie':'ras_session=expired-synthetic'})
        assert r.status_code==401 and 'Max-Age=0' in r.headers['set-cookie']
        assert r.headers.get('cache-control')=='no-store'
    finally:
        if proc is not None:proc.terminate();proc.wait(15)
        server.should_exit=True;thread.join(15);assert not thread.is_alive()
        log.close()
