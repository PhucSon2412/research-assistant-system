"""Installed Chrome against the actual C4 API/web images, no backend or gate seams."""
import argparse
import hashlib
import json
from pathlib import Path
import time
from playwright.sync_api import sync_playwright, expect

REPO=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('--context',type=Path,required=True);a=p.parse_args()
    root=a.context.resolve()
    if root==REPO or REPO in root.parents:raise SystemExit('external context required')
    c=json.loads((root/'stack.json').read_text());assert c['project'].startswith('rasc4')
    accounts=json.loads((root/'synthetic-accounts.json').read_text())
    targets=json.loads((root/'browser-targets.json').read_text());origin=targets['origin']
    out=root/('chrome-'+str(time.time_ns()));out.mkdir()
    checks=[]
    expect.set_options(timeout=60000)
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True)
        context=browser.new_context(accept_downloads=True)
        denied=[];requests=[]
        def network(route):
            if route.request.url.startswith(origin+'/'):route.continue_()
            else:denied.append(route.request.url);route.abort()
        context.route('**/*',network)
        page=context.new_page()
        page.on('request',lambda request:requests.append({'method':request.method,'path':request.url.removeprefix(origin)})
                if request.url.startswith(origin+'/api/') else None)
        def login(key):
            page.goto(origin+'/login')
            page.get_by_label('Email',exact=True).fill(accounts[key]['email'])
            page.get_by_label('Mật khẩu',exact=True).fill(accounts[key]['password'])
            page.get_by_role('button',name='Đăng nhập',exact=True).click()
            page.wait_for_url(origin+'/',timeout=60000)
        login('a')
        expect(page.get_by_role('heading',name='Thư viện',exact=True)).to_be_visible()
        data=('# Chrome synthetic upload '+str(time.time_ns())+'\n\n## Methods\nChrome synthetic evidence measures nine units.\n').encode()
        selected=out/'chrome-synthetic.md';selected.write_bytes(data)
        page.locator('input[type=file]').set_input_files(str(selected))
        expect(page.get_by_role('button',name='Tải lên',exact=True)).to_be_disabled()
        page.get_by_label('Tôi đồng ý xử lý ngoài cho lần upload này.').check()
        with page.expect_response(lambda r:r.url.endswith('/api/uploads/original') and r.request.method=='PUT',timeout=60000) as receipt:
            page.get_by_role('button',name='Tải lên',exact=True).click()
        response=receipt.value;assert response.status==201
        received=page.get_by_role('link',name='Xem item',exact=True)
        expect(received).to_be_visible(timeout=60000)
        item=received.get_attribute('href').rsplit('/',1)[1]
        detail=context.request.get(origin+'/api/library/items/'+item).json()['item']
        ids={'item_id':item,'file_id':detail['files'][0]['id']}
        checks.append('UI per-file consent then intent/original receipt')
        until=time.monotonic()+180
        while time.monotonic()<until:
            r=context.request.get(origin+'/api/library/items/'+ids['item_id'])
            if r.status==200 and r.json()['item']['content_status']=='indexed':break
            time.sleep(.5)
        else:raise AssertionError('real worker did not index Chrome upload')
        page.goto(origin+'/items/'+ids['item_id'])
        expect(page.get_by_text('Đã index',exact=False).first).to_be_visible()
        with page.expect_download(timeout=60000) as download:
            page.locator('a[href^="/api/files/"]').first.click()
        path=out/'original.md';download.value.save_as(str(path))
        assert path.read_bytes()==data
        checks.append('Chrome original download SHA256')
        for scope,mode in [('item','keyword'),(targets['collection'],'hybrid'),('all','keyword')]:
            page.goto(origin+'/items/'+targets['item']['item_id'])
            page.get_by_label('Phạm vi').select_option(scope)
            page.get_by_label('Cách tìm bằng chứng').select_option(mode)
            page.get_by_label('Câu hỏi',exact=True).fill('What does synthetic method measure?')
            page.get_by_role('button',name='Hỏi',exact=True).click()
            expect(page.get_by_text('Trạng thái: Có câu trả lời',exact=False)).to_be_visible(timeout=120000)
            page.locator('a[href^="/sources/"]').first.click()
            page.wait_for_url('**/sources/*',timeout=60000)
            expect(page.get_by_text('Synthetic method',exact=False).first).to_be_visible(timeout=60000)
            page.screenshot(path=str(out/('citation-'+mode+'-'+str(len(checks))+'.png')),full_page=True)
            checks.append('RAG '+scope+' '+mode+' opens actual source')
        for who in ('b','admin'):
            context.clear_cookies();login(who)
            page.goto(origin+'/items/'+targets['item']['item_id'])
            expect(page.get_by_text('Không tìm thấy',exact=False)).to_be_visible(timeout=60000)
            checks.append(who+' private owner denial')
        facts={'chrome':browser.version,'checks':checks,'outbound_denied':denied,'requests':requests,
               'download_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        (out/'result.json').write_text(json.dumps(facts,indent=2),encoding='utf-8')
        browser.close()
    print('Chrome acceptance: '+str(len(checks))+' checks; external requests='+str(len(denied)))


if __name__=='__main__':main()
