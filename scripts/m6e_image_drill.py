"""M6e real-image/proxy/Chrome smoke: exact-byte private duplicate and navigation.

Requires an external C4 context (c4_stack setup/build/up, c4_http_flow default for
synthetic accounts) with the controlled test-only provider (c4_stack fake-on).
Never reads .env, touches dev/Dify or calls a real provider/arXiv.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import httpx
from playwright.sync_api import sync_playwright, expect

REPO = Path(__file__).resolve().parents[1]
KEPT = 'file gốc mới được giữ'


def main():
    p = argparse.ArgumentParser(); p.add_argument('--context', type=Path, required=True); a = p.parse_args()
    root = a.context.resolve()
    if root == REPO or REPO in root.parents: raise SystemExit('external context required')
    c = json.loads((root/'stack.json').read_text()); assert c['project'].startswith('rasc4')
    e = c['env']; origin = 'http://127.0.0.1:'+e['WEB_HOST_PORT']; api = 'http://127.0.0.1:'+e['API_HOST_PORT']
    accounts = json.loads((root/'synthetic-accounts.json').read_text())
    calls_log = root/'provider-fixture/calls.jsonl'
    checks = []; log = root/('m6e-smoke-'+str(time.time_ns())); log.mkdir()

    def check(name, condition):
        checks.append({'check': name, 'pass': bool(condition)})
        (log/'checks.json').write_text(json.dumps(checks, indent=2), encoding='utf-8')
        print(('PASS ' if condition else 'FAIL ')+name, flush=True)
        assert condition, name

    def provider_calls():
        return len(calls_log.read_text().splitlines()) if calls_log.exists() else 0

    def post(client, path, body):
        return client.post('/api'+path, json=body, headers={'x-csrf-token': client.cookies.get('ras_csrf', '')})

    def login(key):
        out = httpx.Client(base_url=origin, headers={'origin': origin}, timeout=120)
        out.get('/api/auth/csrf')
        r = post(out, '/auth/login', accounts[key]); assert r.status_code == 200, r.text
        return out

    def upload(client, data):
        notice = client.get('/api/uploads/notice').json()['version']
        r = post(client, '/uploads/intents', dict(acknowledged=True, notice_version=notice, file_kind='markdown',
            filename='m6e-synthetic.md', byte_size=len(data), sha256=hashlib.sha256(data).hexdigest()))
        assert r.status_code == 201, r.text
        r = client.put('/api/uploads/original', content=data, headers={'x-csrf-token': client.cookies.get('ras_csrf', ''),
            'x-upload-intent': r.json()['intent_token'], 'content-type': 'application/octet-stream'})
        assert r.status_code == 201, r.text
        return r.json()

    def wait_job(client, item, states, seconds=240):
        end = time.monotonic()+seconds
        while time.monotonic() < end:
            r = client.get('/api/library/items/'+item)
            if r.status_code == 200 and r.json()['item']['jobs'] and r.json()['item']['jobs'][0]['state'] in states:
                return r.json()['item']
            time.sleep(.5)
        raise AssertionError('real image worker did not reach '+str(states))

    check('real API readiness and Next proxy live', httpx.get(api+'/readyz', timeout=30).status_code == 200
          and httpx.get(origin, timeout=30).status_code == 200)
    user, other = login('a'), login('b')
    data = ('# M6e exact duplicate '+str(time.time_ns())+'\n\n## Methods\nSynthetic duplicate method measures seven units.\n').encode()
    first = upload(user, data)
    ready = wait_job(user, first['item_id'], ('ready',))
    check('A first upload indexed by real worker with controlled provider', ready['content_status'] == 'indexed'
          and ready['jobs'][0]['existing_copy'] is None)
    independent = upload(other, data)
    theirs = wait_job(other, independent['item_id'], ('ready', 'duplicate'))
    check('B same bytes processed independently, no cross-owner target',
          theirs['jobs'][0]['state'] == 'ready' and theirs['jobs'][0]['existing_copy'] is None)
    baseline_calls = provider_calls()
    selected = log/'m6e-synthetic.md'; selected.write_bytes(data)
    expect.set_options(timeout=60000)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe', headless=True)
        ctx = browser.new_context(); page = ctx.new_page(); requests = []; outside = []

        def network(route):
            if route.request.url.startswith(origin+'/'): route.continue_()
            else: outside.append(route.request.url); route.abort()
        ctx.route('**/*', network)
        page.on('request', lambda r: requests.append({'method': r.method, 'path': r.url.removeprefix(origin)})
                if r.url.startswith(origin+'/api/') else None)

        def ui_login(key):
            ctx.clear_cookies(); page.goto(origin+'/login')
            page.get_by_label('Email', exact=True).fill(accounts[key]['email'])
            page.get_by_label('Mật khẩu', exact=True).fill(accounts[key]['password'])
            page.get_by_role('button', name='Đăng nhập', exact=True).click(); page.wait_for_url(origin+'/')

        def puts():
            return [r for r in requests if r['method'] == 'PUT' and r['path'] == '/api/uploads/original']

        def intents():
            return [r for r in requests if r['method'] == 'POST' and r['path'] == '/api/uploads/intents']

        ui_login('a')
        consent = page.get_by_label('Tôi đồng ý xử lý ngoài cho lần upload này.')
        submit = page.get_by_role('button', name='Tải lên', exact=True)
        page.locator('input[type=file]').set_input_files(str(selected))
        check('consent not preselected and submit disabled', not consent.is_checked() and submit.is_disabled())
        consent.check()
        with page.expect_response(lambda r: r.url.endswith('/api/uploads/original') and r.request.method == 'PUT') as receipt:
            submit.dblclick()
        received = page.get_by_role('link', name='Xem item', exact=True)
        expect(received).to_be_visible()
        page.wait_for_timeout(3000)
        check('double click sends one intent and one PUT', receipt.value.status == 201 and len(puts()) == 1 and len(intents()) == 1)
        check('consent resets after the upload', not consent.is_checked())
        duplicate_id = received.get_attribute('href').rsplit('/', 1)[1]
        detail = wait_job(user, duplicate_id, ('duplicate', 'ready', 'awaiting_index'))
        check('new intent same bytes after ready is a duplicate warning', detail['jobs'][0]['state'] == 'duplicate'
              and detail['content_status'] == 'metadata_only' and len(detail['files']) == 1)
        page.goto(origin+'/items/'+duplicate_id)
        note = page.get_by_role('note')
        expect(note).to_contain_text('trùng chính xác')
        expect(note).to_contain_text(KEPT)
        expect(page.get_by_text('Nội dung: Chỉ metadata', exact=False)).to_be_visible()
        page.screenshot(path=str(log/'duplicate-notice.png'), full_page=True)
        link = note.get_by_role('link')
        check('notice links only to the owner live copy', link.get_attribute('href') == '/items/'+first['item_id'])
        link.click(); page.wait_for_url(origin+'/items/'+first['item_id'])
        expect(page.get_by_text('Nội dung: Đã index', exact=False)).to_be_visible()
        check('navigation opens the existing indexed copy through normal ACL', True)
        lost = []
        mine = lambda: {i['id'] for i in user.get('/api/library/items', params={'scope': 'mine', 'limit': 100}).json()['items']}
        known = mine()

        def lose(route):
            lost.append(True); response = route.fetch(); assert response.status == 201
            route.fulfill(status=502, content_type='text/plain', body='upstream lost')
        page.route('**/api/uploads/original', lose)
        page.goto(origin+'/')
        page.locator('input[type=file]').set_input_files(str(selected))
        consent.check(); submit.click()
        expect(page.locator('section[aria-label="Tải paper riêng"]').get_by_role('alert')).to_be_visible()
        page.wait_for_timeout(5500)
        alert = page.locator('section[aria-label="Tải paper riêng"]').get_by_role('alert').inner_text()
        check('lost committed PUT response is unknown, never resent', len(lost) == 1 and len(puts()) == 2
              and page.get_by_role('link', name='Xem item', exact=True).count() == 0)
        (log/'unknown-alert.txt').write_text(alert, encoding='utf-8')
        page.unroute('**/api/uploads/original')
        landed = sorted(mine() - known)
        check('unknown outcome landed exactly once', len(landed) == 1)
        late = wait_job(user, landed[0], ('duplicate', 'ready', 'awaiting_index'))
        check('landed unknown upload also a duplicate', late['jobs'][0]['state'] == 'duplicate')
        check('duplicates made zero provider calls', provider_calls() == baseline_calls)
        check('owner B sees no A target and A cannot read B', other.get('/api/library/items/'+first['item_id']).status_code == 404
              and user.get('/api/library/items/'+independent['item_id']).status_code == 404)
        assert post(user, '/library/items/'+first['item_id']+'/delete', {'confirm_delete': True}).status_code == 200
        page.goto(origin+'/items/'+duplicate_id)
        expect(page.get_by_role('note')).to_contain_text('không còn bản hợp lệ')
        check('after target delete no link, no stale id', page.locator('a[href="/items/'+first['item_id']+'"]').count() == 0
              and first['item_id'] not in json.dumps(user.get('/api/library/items/'+duplicate_id).json()))
        page.screenshot(path=str(log/'duplicate-no-target.png'), full_page=True)
        r = ctx.request.get(origin+'/api/library/items/'+first['item_id'])
        check('saved target link denied after delete', r.status == 404)
        check('B independent copy still readable', other.get('/api/files/'+independent['file_id']+'/original').status_code == 200)
        check('Chrome blocked every external origin', not outside)
        (log/'chrome.json').write_text(json.dumps({'chrome': browser.version, 'requests': requests, 'outside': outside}, indent=2))
        browser.close()
    check('readiness stays open after mutations', httpx.get(api+'/readyz', timeout=30).status_code == 200)
    print('M6e image smoke: '+str(len(checks))+' checks; evidence '+str(log))


if __name__ == '__main__':
    main()
