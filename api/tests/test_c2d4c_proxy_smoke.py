"""Socket Uvicorn -> actual Next streaming routes -> headless installed Chrome.

Standalone test servers and runtime fixture DB only. Never reads repository
.env, starts worker/provider, or changes production cutover.
"""
import hashlib
import io
import os
from pathlib import Path
import subprocess
import threading
import time

import httpx
from playwright.sync_api import sync_playwright
from sqlalchemy.orm import Session
from sqlalchemy import text
import uvicorn

from app.db import get_engine
from app.models import SourceFile
from app.identity.login import login
from app.main import app
from tests.resource_fixtures import source, BODY, SHA
from tests.test_c2d4c_original_downloads import downloads
from tests.test_c2d1_final_caller import browser
from tests.test_c2c1_bootstrap import installation
from tests.test_c2c2_invitations import world, issue, accept, EMAIL, PASSWORD
from tests.test_auth_safety_foundation import NOW


def test_real_next_proxy_chrome_three_gates_errors_and_cancel(downloads, tmp_path):
    b=downloads
    root=Path(__file__).resolve().parents[2]
    assert not list((root/'web').glob('.env*')), 'Next test must not load a .env file'
    chrome=Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    assert chrome.is_file(), 'Chrome smoke requires installed Chrome'
    user_b=accept(b.w,issue(b.w,email='proxy-b@example.test'))
    own_b=source(b.w,user_b.id,'B proxy synthetic');b.storage.put(own_b.key,io.BytesIO(BODY),'text/markdown')
    tokens={}
    for label,email in (('a',EMAIL),('b','proxy-b@example.test')):
        with Session(b.w.runtime) as db:
            tokens[label]=login(db,email=email,password=PASSWORD,now=NOW,settings=b.w.settings,clock=b.clock)[1].token
    tokens['admin']=b.w.actor.token
    api_base='http://127.0.0.1:58458';web_base='http://127.0.0.1:53457'
    config=uvicorn.Config(app,host='127.0.0.1',port=58458,log_level='critical',access_log=False)
    server=uvicorn.Server(config);thread=threading.Thread(target=server.run)
    log=(tmp_path/'next.log').open('w',encoding='utf-8')
    proc=None;thread.start()
    def wait(url):
        for _ in range(100):
            try:
                if httpx.get(url).status_code in (200,401):return
            except httpx.HTTPError:pass
            time.sleep(.1)
        raise AssertionError('isolated socket server unavailable')
    try:
        wait(api_base+'/healthz')
        proc=subprocess.Popen(['node',str(root/'web/node_modules/next/dist/bin/next'),'start',str(root/'web'),
            '--hostname','127.0.0.1','--port','53457'],env=os.environ|{'API_INTERNAL_URL':api_base},stdout=log,stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW)
        wait(web_base+'/login')
        with sync_playwright() as pw:
            chrome_browser=pw.chromium.launch(executable_path=str(chrome),headless=True)
            try:
                successes=0;denials=0
                for label,token in tokens.items():
                    context=chrome_browser.new_context(accept_downloads=True)
                    context.add_cookies([{'name':'ras_session','value':token,'url':web_base}])
                    page=context.new_page();page.goto(web_base+'/login')
                    paths=[f'/files/{b.review.file if label=="a" else own_b.file if label=="b" else b.own.file}/original',b.path('common')]
                    if label=='admin':paths.append(b.path('review'))
                    for n,path in enumerate(paths):
                        page.evaluate("path => { const a=document.createElement('a');a.href='/api'+path;a.id='synthetic-download';a.textContent='Download';document.body.appendChild(a); }",path)
                        with page.expect_download() as outcome:page.locator('#synthetic-download').click()
                        download=outcome.value;target=tmp_path/f'{label}-{n}.bin';download.save_as(target)
                        assert download.failure() is None and hashlib.sha256(target.read_bytes()).hexdigest()==SHA
                        page.locator('#synthetic-download').evaluate('(a)=>a.remove()');successes+=1
                    path=f'/files/{b.review.file if label!="a" else own_b.file}/original'
                    result=page.evaluate("async path => {const r=await fetch('/api'+path);return {status:r.status,body:await r.json(),attachment:r.headers.get('content-disposition')};}",path)
                    assert result=={'status':404,'body':{'error':'not_found'},'attachment':None};denials+=1
                    context.close()
                print(f'Chrome {chrome_browser.version}: {successes} successful link downloads, {denials} ACL denials')
            finally:chrome_browser.close()
        # Preserve exactly the cookie(s) the current API clear helper emits.
        r=httpx.get(web_base+'/api'+b.path('private'),headers={'cookie':'ras_session=expired-synthetic'})
        direct=httpx.get(api_base+b.path('private'),headers={'cookie':'ras_session=expired-synthetic'})
        assert r.status_code==direct.status_code==401
        assert len(r.headers.get_list('set-cookie'))==len(direct.headers.get_list('set-cookie'))==1
        assert 'ras_session=' in r.headers['set-cookie'] and 'Max-Age=0' in r.headers['set-cookie']
        assert 'content-disposition' not in r.headers
        # Integrity denial through real MinIO and Next, followed by busy denial.
        b.storage.put(b.own.key,io.BytesIO(b'x'*len(BODY)),'text/markdown')
        cookie={'cookie':'ras_session='+tokens['admin']}
        r=httpx.get(web_base+'/api'+b.path('private'),headers=cookie)
        assert r.status_code==409 and r.json()=={'error':'private_file_integrity_failed'}
        assert 'content-disposition' not in r.headers
        from app.common_original import _slots
        slots=_slots(4)
        assert all(slots.acquire(blocking=False) for _ in range(4))
        try:
            r=httpx.get(web_base+'/api'+b.path('common'),headers=cookie)
            assert r.status_code==503 and r.json()=={'error':'download_busy'}
        finally:
            for _ in range(4):slots.release()
        # One 30 MiB synthetic object: bounded spool, cancel web body, wait for
        # ASGI executor/response cleanup, then reuse the slot via the proxy.
        data=b'%PDF-synthetic\n'+b'x'*(30*1024*1024-15)
        with Session(b.w.engine) as db:
            file=SourceFile(item_id=b.own.item,version_id=b.own.version,storage_key=b.own.key+'-large',
                byte_size=len(data),checksum_sha256=hashlib.sha256(data).hexdigest(),verified_mime='application/pdf')
            db.add(file);db.flush();large=file.id;large_key=file.storage_key;db.commit()
        b.storage.put(large_key,io.BytesIO(data),'application/pdf')
        start=time.monotonic()
        with httpx.stream('GET',web_base+f'/api/files/{large}/original',headers=cookie,timeout=30) as r:
            assert r.status_code==200 and int(r.headers['content-length'])==len(data)
            assert next(r.iter_bytes())
        for _ in range(100):
            if all(s.closed for s in b.spools):break
            time.sleep(.05)
        assert all(s.closed for s in b.spools) and get_engine().pool.checkedout()==0
        assert httpx.get(web_base+'/api'+b.path('common'),headers=cookie).content==BODY
        print(f'30 MiB verify-to-first-byte/cancel: {time.monotonic()-start:.3f}s (one observation, not SLO)')
    finally:
        if proc is not None:
            proc.terminate();proc.wait(15)
        server.should_exit=True;thread.join(15);assert not thread.is_alive()
        log.close()
