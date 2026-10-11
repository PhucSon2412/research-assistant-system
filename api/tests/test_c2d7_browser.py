"""Installed Chrome, actual Next proxy and uvicorn; disposable fixture only.

The app's pending result is patched only by upload_api. The final check restores
the real gate. Copies tracked web source (never .env) to an external directory.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from app import auth_safety
from app.config import get_settings
from app.main import app
from tests.test_c2d7_rag_runtime import generated, ready  # noqa: F401
from tests.test_c2d5d2b_document_runtime import indexed  # noqa: F401
from tests.test_c2d5d2a_worker_runtime import work  # noqa: F401
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_uploads import upload_api  # noqa: F401
from tests.helpers import USER_PASSWORD, ADMIN_EMAIL, ADMIN_PASSWORD

REPO=Path(__file__).resolve().parents[2]


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));return s.getsockname()[1]


def test_chrome_proxy_business_and_pending(generated,tmp_path,monkeypatch):
    b=generated;api_port,web_port=port(),port();origin=f'http://127.0.0.1:{web_port}'
    b.api.create_user('browser-b@example.test')
    from tests.test_common_search import copy_of
    from tests.test_c2d5d2a_worker_runtime import BODY, sql
    from tests.test_publication import revoke_in_own_session
    world=SimpleNamespace(a=b.browser,admin=b.api.admin_browser(),api=b.api,storage=b.storage,settings=b.settings)
    ids={'item_id':str(b.item),'file_id':str(b.file),
        'version_id':str(sql(b,'SELECT active_version_id FROM library_items WHERE id=:i',i=b.item))}
    common=copy_of(world,ids,BODY,index=True)
    b.calls.clear()
    for cfg in (get_settings(),b.settings):
        cfg.http.allowed_origins=list(set(cfg.http.allowed_origins+[origin]));cfg.http.public_base_url=origin
    web=tmp_path/'web';web.mkdir()
    names=subprocess.check_output(['git','ls-files','web'],cwd=REPO,text=True).splitlines()
    for name in names:
        rel=Path(name).relative_to('web')
        assert not any(part.startswith('.env') for part in rel.parts)
        dst=web/rel;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(REPO/name,dst)
    # Node follows this task's external source; installed dependencies are read-only shared.
    subprocess.run(['cmd','/c','mklink','/J',str(web/'node_modules'),str(REPO/'web/node_modules')],check=True,capture_output=True)
    env=os.environ.copy();env['API_INTERNAL_URL']=f'http://127.0.0.1:{api_port}';env['NEXT_TELEMETRY_DISABLED']='1'
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=api_port,log_level='error',access_log=False))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    log=(tmp_path/'next.log').open('w',encoding='utf-8')
    process=subprocess.Popen(['node',str(REPO/'web/node_modules/next/dist/bin/next'),'dev',str(web),
        '--hostname','127.0.0.1','--port',str(web_port),'--webpack'],cwd=web,env=env,stdout=log,stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW)
    facts=[]
    try:
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            with socket.socket() as s:
                if s.connect_ex(('127.0.0.1',web_port))==0 and server.started:break
            assert process.poll() is None,'Next failed; see external next.log'
            time.sleep(.2)
        assert server.started
        with sync_playwright() as pw:
            chrome=pw.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True)
            facts.append({'chrome':chrome.version})
            # Network guard applies to the browser too, before any navigation.
            context=chrome.new_context()
            context.route('**/*',lambda route: route.continue_() if route.request.url.startswith(origin+'/') else route.abort())
            page=context.new_page();requests=[]
            page.on('request',lambda r:requests.append(r.url) if r.url.endswith('/api/rag/ask') else None)
            def login(email,password):
                page.goto(origin+'/login');page.get_by_label('Email',exact=True).fill(email)
                page.get_by_label('Mật khẩu',exact=True).fill(password)
                page.get_by_role('button',name='Đăng nhập',exact=True).click()
                page.wait_for_url(origin+'/',timeout=60000)
            login('worker@example.test',USER_PASSWORD)
            collection=b.browser.post('/collections',{'name':'Chrome RAG'}).json()['collection']['id']
            assert b.browser.post('/collections/'+collection+'/items',{'item_id':str(b.item)}).status_code==201
            def form(scope='item',mode='keyword',item=None):
                page.goto(origin+'/items/'+str(item or b.item))
                page.get_by_label('Phạm vi').select_option(scope)
                page.get_by_label('Cách tìm bằng chứng').select_option(mode)
                page.get_by_label('Câu hỏi',exact=True).fill('measured')
                page.get_by_role('button',name='Hỏi',exact=True).click()
            for scope,mode in [('item','keyword'),(collection,'hybrid'),('all','keyword')]:
                form(scope,mode)
                expect(page.get_by_text('Trạng thái: Có câu trả lời',exact=False)).to_be_visible(timeout=60000)
                page.locator('a[href^="/sources/"]').first.click()
                page.wait_for_url('**/sources/*',timeout=60000)
                expect(page.get_by_text('A measured result.',exact=False)).to_be_visible(timeout=60000)
                if scope=='item': chunk_path=page.url.removeprefix(origin)
            assert len(b.calls)==4
            from app import search_boundary
            original=search_boundary.commit
            def uncertain(db,outcome):
                if db.info.get('search_phase')=='settlement':
                    original(db,outcome)
                    from app.api.errors import ApiError
                    raise ApiError(503,'search_commit_uncertain')
                return original(db,outcome)
            monkeypatch.setattr(search_boundary,'commit',uncertain)
            before=len(requests);form()
            expect(page.get_by_text('Chưa xác định được kết quả ghi kế toán hỏi đáp.',exact=False)).to_be_visible(timeout=60000)
            assert len(requests)==before+1 and len(b.calls)==5
            monkeypatch.setattr(search_boundary,'commit',original)
            b.settings.generation.pricing_verified=False
            before=len(requests);form()
            expect(page.get_by_text('Hỏi đáp hiện chưa khả dụng',exact=False)).to_be_visible(timeout=60000)
            assert len(requests)==before+1 and len(b.calls)==5
            b.settings.generation.pricing_verified=True
            for email,password in [('browser-b@example.test',USER_PASSWORD),(ADMIN_EMAIL,ADMIN_PASSWORD)]:
                context.clear_cookies();login(email,password)
                page.goto(origin+chunk_path)
                expect(page.get_by_text('Không tìm thấy',exact=False)).to_be_visible(timeout=60000)
                assert 'A measured result.' not in page.locator('body').inner_text()
                form(item=common)
                expect(page.get_by_text('Trạng thái: Có câu trả lời',exact=False)).to_be_visible(timeout=60000)
                page.locator('a[href^="/sources/"]').first.click()
                page.wait_for_url('**/sources/*',timeout=60000)
                expect(page.get_by_text('A measured result.',exact=False)).to_be_visible(timeout=60000)
            # Actual HTTP revoke commits during the fake provider call.
            b.gen_action=lambda *_: (revoke_in_own_session(b.api,common) and None)
            form(item=common)
            expect(page.get_by_text('Quyền hoặc phạm vi của nguồn đã thay đổi',exact=False)).to_be_visible(timeout=60000)
            assert len(b.calls)==8
            page.goto(origin+'/items/'+common)
            expect(page.get_by_text('Không tìm thấy',exact=False)).to_be_visible(timeout=60000)
            monkeypatch.setattr(auth_safety,'cutover',lambda db:'auth_safety_pending')
            # Real HTTP pending is separately asserted after the scoped business checks.
            result=context.request.get(f'http://127.0.0.1:{api_port}/search/chunks/'+chunk_path.rsplit('/',1)[1])
            assert result.status==503
            assert context.request.get(f'http://127.0.0.1:{api_port}/healthz').status==200
            assert context.request.get(f'http://127.0.0.1:{api_port}/readyz').status==503
            facts.append({'checks':['item-keyword-citation','collection-hybrid-citation','all-keyword-citation',
                'uncertain-no-retry','pricing-no-retry','common-B-Admin-citation','revoke-during-generation',
                'new-request-revoked','B-denial','Admin-denial','pending-http'],'rag_posts':len(requests),'provider_calls':len(b.calls)})
            chrome.close()
        root=Path(os.environ['RAS_C2D7_CONTEXT'])
        (root/'chrome-smoke.json').write_text(json.dumps(facts,indent=2),encoding='utf-8')
    finally:
        server.should_exit=True;thread.join(15)
        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True)
        process.wait(timeout=15);log.close()
