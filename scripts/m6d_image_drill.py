"""M6d real-image/proxy/Chrome and pg_dump + object restore acceptance.

Requires an external C4 context, initialized accounts and controlled fake provider.
Never reads .env; all mutations target contexts validated by c4_stack/ras_backup.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from datetime import datetime,timezone

import httpx
from playwright.sync_api import sync_playwright,expect

REPO=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('--context',type=Path,required=True)
    p.add_argument('phase',choices=['prepare','delete','restore']);a=p.parse_args()
    root=a.context.resolve()
    if root==REPO or REPO in root.parents:raise SystemExit('external context required')
    c=json.loads((root/'stack.json').read_text());assert c['project'].startswith('rasc4')
    e=c['env'];origin='http://127.0.0.1:'+e['WEB_HOST_PORT']
    accounts=json.loads((root/'synthetic-accounts.json').read_text())
    checks=[];log=root/('m6d-'+a.phase+'-'+str(time.time_ns()));log.mkdir()
    def check(name,condition):
        checks.append({'check':name,'pass':bool(condition)})
        (log/'checks.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
        print(('PASS ' if condition else 'FAIL ')+name,flush=True)
        assert condition,name
    def compose(ctx,*args):
        r=subprocess.run([sys.executable,str(REPO/'scripts/c4_stack.py'),'--context',str(ctx),'compose',*args],
            capture_output=True,text=True,encoding='utf-8')
        (log/('compose-'+str(time.time_ns())+'.log')).write_text(r.stdout+r.stderr,encoding='utf-8')
        assert r.returncode==0,'isolated compose command failed; see log'
        return r
    def env_for(ctx):
        conf=json.loads((ctx/'stack.json').read_text())
        out={k:v for k,v in os.environ.items() if not k.startswith(('RAS_','POSTGRES_','MINIO_','REDIS_','OPENROUTER_'))}
        out.update(conf['env'])
        out.update(RAS_COMPOSE_ENV_FILE=str(ctx/'stack.env'),RAS_COMPOSE_OVERRIDE_FILE=str(ctx/'override.json'),
            RAS_COMPOSE_PORTS_FILE=str(ctx/'ports.yaml'),PYTHONUTF8='1',PYTHONPATH=str(REPO/'api'))
        return out
    def backup_cli(ctx,*args):
        r=subprocess.run([sys.executable,str(REPO/'scripts/ras_backup.py'),*map(str,args)],env=env_for(ctx),
            capture_output=True,text=True,encoding='utf-8')
        (log/('backup-'+str(time.time_ns())+'.log')).write_text(r.stdout+r.stderr,encoding='utf-8')
        assert r.returncode==0,'backup/restore failed; see log'
        return json.loads(r.stdout.strip().splitlines()[-1])
    def post(client,path,body):
        return client.post('/api'+path,json=body,headers={'x-csrf-token':client.cookies.get('ras_csrf','')})
    def login(key,url=origin):
        out=httpx.Client(base_url=url,headers={'origin':url},timeout=120)
        out.get('/api/auth/csrf')
        r=post(out,'/auth/login',accounts[key]);assert r.status_code==200,r.text
        return out
    def upload(client,data):
        notice=client.get('/api/uploads/notice').json()['version']
        r=post(client,'/uploads/intents',dict(acknowledged=True,notice_version=notice,file_kind='markdown',
            filename='m6d-synthetic.md',byte_size=len(data),sha256=hashlib.sha256(data).hexdigest()))
        assert r.status_code==201,r.text
        r=client.put('/api/uploads/original',content=data,headers={'x-csrf-token':client.cookies.get('ras_csrf',''),
            'x-upload-intent':r.json()['intent_token'],'content-type':'application/octet-stream'})
        assert r.status_code==201,r.text
        return r.json()
    def wait_index(client,item):
        end=time.monotonic()+180
        while time.monotonic()<end:
            r=client.get('/api/library/items/'+item)
            if r.status_code==200 and r.json()['item']['content_status']=='indexed':return r.json()['item']
            time.sleep(.5)
        raise AssertionError('real image worker did not index')
    def review(client,ids):
        notice=client.get('/api/review-requests/notice').json()['version']
        r=post(client,'/review-requests',dict(acknowledged=True,notice_version=notice,item_id=ids['item_id'],
            version_id=ids['version_id'],source_file_id=ids['file_id'],declared_source='M6d synthetic declaration',
            declared_rights='M6d synthetic permission'))
        assert r.status_code==201,r.text
        return r.json()['request']['id']
    target_file=root/'m6d-targets.json'
    if a.phase=='prepare':
        admin,user,other=(login(k) for k in ('admin','a','b'))
        data=('# M6d synthetic method '+str(time.time_ns())+'\n\n## Methods\nSynthetic method measures nine units.\n').encode()
        ids,same,unknown=(upload(out,data) for out in (user,other,user))
        for out,item in ((user,ids),(other,same),(user,unknown)):wait_index(out,item['item_id'])
        check('three real worker indexes and per-upload consent',True)
        rid=review(user,ids)
        sha=hashlib.sha256(data).hexdigest()
        r=post(admin,f'/admin/review-requests/{rid}/approve',dict(confirm_verified=True,file_checksum_sha256=sha,
            title='M6d independent common control',verified_source='synthetic source',evidence_note='synthetic permission',
            checked_on=datetime.now(timezone.utc).date().isoformat(),scope=dict(audience='instance_users',
                original_download=True,fulltext_search=True,external_processing=True,conditions=None)))
        assert r.status_code==200,r.text
        common=r.json()['request']['pending_publication_id']
        assert post(admin,f'/admin/common-copies/{common}/publish',dict(confirm_publish=True,file_checksum_sha256=sha)).status_code==200
        assert post(admin,f'/admin/common-copies/{common}/index',dict(confirm_external_processing=True)).status_code==200
        common_detail=wait_index(other,common)
        rid=review(user,ids)  # open grant in the backup, independent approved common already exists
        collection=post(user,'/collections',{'name':'M6d deletion collection'}).json()['collection']['id']
        assert post(user,f'/collections/{collection}/items',{'item_id':ids['item_id']}).status_code==201
        hits=post(user,'/search',dict(query='Synthetic method',mode='keyword',item_id=ids['item_id'])).json()['hits']
        assert hits
        target={'origin':origin,'item':ids,'same':same,'unknown':unknown,'common':common,
            'common_file':common_detail['files'][0]['id'],'rid':rid,'chunk':hits[0]['chunk_id'],'collection':collection}
        target_file.write_text(json.dumps(target,indent=2),encoding='utf-8')
        check('open review, indexed common/B controls and source before backup',True)
        compose(root,'stop','api','web','worker','beat')
        result=backup_cli(root,'backup','--project',c['project'],'--out',root.parent/'backups-before')
        target['backup_before']=result['artifact'];target_file.write_text(json.dumps(target,indent=2),encoding='utf-8')
        check('full exported snapshot pg_dump and verified object backup',result['file_rows_checked']>=3)
        compose(root,'up','-d','--no-deps','--wait','api','web')
        return
    t=json.loads(target_file.read_text())
    if a.phase=='delete':
        expect.set_options(timeout=60000)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True)
            ctx=browser.new_context();page=ctx.new_page();requests=[];outside=[]
            def network(route):
                if route.request.url.startswith(origin+'/'):route.continue_()
                else:outside.append(route.request.url);route.abort()
            ctx.route('**/*',network)
            page.on('request',lambda r:requests.append({'method':r.method,'path':r.url.removeprefix(origin)})
                    if r.url.startswith(origin+'/api/') else None)
            def ui_login(key):
                ctx.clear_cookies();page.goto(origin+'/login')
                page.get_by_label('Email',exact=True).fill(accounts[key]['email'])
                page.get_by_label('Mật khẩu',exact=True).fill(accounts[key]['password'])
                page.get_by_role('button',name='Đăng nhập',exact=True).click();page.wait_for_url(origin+'/')
            ui_login('a');page.goto(origin+'/items/'+t['item']['item_id'])
            button=page.get_by_role('button',name='Xóa vĩnh viễn item riêng',exact=True)
            expect(button).to_be_disabled()
            page.get_by_label('Tôi xác nhận xóa toàn item và mọi version/file.').check()
            with page.expect_response(lambda r:r.url.endswith('/delete') and r.request.method=='POST') as receipt:button.click()
            check('Chrome unchecked confirmation then one owner deletion POST',receipt.value.status==200)
            expect(page.get_by_role('status')).to_contain_text('đang chờ cleanup')
            page.screenshot(path=str(log/'logical-delete.png'),full_page=True)
            for path in ('/api/files/'+t['item']['file_id']+'/original','/api/search/chunks/'+t['chunk']):
                check('saved original/source URL denied '+path.split('/')[2],ctx.request.get(origin+path).status==404)
            page.goto(origin+'/');expect(page.locator('a[href="/items/'+t['item']['item_id']+'"]')).to_have_count(0)
            collection=ctx.request.get(origin+'/api/collections/'+t['collection']).json()
            check('library and collection removed immediately',t['item']['item_id'] not in json.dumps(collection))
            for who in ('b','admin'):
                ui_login(who);page.goto(origin+'/items/'+t['item']['item_id'])
                expect(page.get_by_role('button',name='Xóa vĩnh viễn item riêng',exact=True)).to_have_count(0)
                token=next(x['value'] for x in ctx.cookies() if x['name']=='ras_csrf')
                r=ctx.request.post(origin+'/api/library/items/'+t['item']['item_id']+'/delete',
                    data={'confirm_delete':True},headers={'origin':origin,'x-csrf-token':token})
                check('Chrome '+who+' cannot delete A',r.status==404)
            ui_login('a');unknown_path='/api/library/items/'+t['unknown']['item_id']+'/delete'
            sent=[]
            def lost(route):
                sent.append(True);response=route.fetch();assert response.status==200
                route.fulfill(status=503,content_type='application/json',body='{"error":"api_response_incomplete"}')
            page.route('**'+unknown_path,lost)
            page.goto(origin+'/items/'+t['unknown']['item_id'])
            page.get_by_label('Tôi xác nhận xóa toàn item và mọi version/file.').check()
            page.get_by_role('button',name='Xóa vĩnh viễn item riêng',exact=True).click()
            expect(page.get_by_role('alert')).to_contain_text('Chưa xác định')
            expect(page.get_by_role('button',name='Xóa vĩnh viễn item riêng',exact=True)).to_be_disabled()
            page.wait_for_timeout(5500)
            check('Chrome lost committed response remains unknown and never resends POST',len(sent)==1)
            page.screenshot(path=str(log/'unknown-outcome.png'),full_page=True)
            check('Chrome blocked every external origin',not outside)
            (log/'chrome.json').write_text(json.dumps({'chrome':browser.version,'requests':requests,'outside':outside},indent=2))
            browser.close()
        user=login('a');admin=login('admin');other=login('b')
        late=upload(user,b'# M6d post-backup\n\n## Methods\nSynthetic method after backup.\n')
        assert post(user,'/library/items/'+late['item_id']+'/delete',{'confirm_delete':True}).status_code==200
        t['late']=late
        for who,out in (('a',user),('b',other),('admin',admin)):
            check('image immediate private denial '+who,out.get('/api/library/items/'+t['item']['item_id']).status_code==404)
            check('image common control '+who,out.get('/api/files/'+t['common_file']+'/original').status_code==200)
        check('image B same bytes control',other.get('/api/files/'+t['same']['file_id']+'/original').status_code==200)
        check('review grant closed',admin.get('/api/admin/review-requests/'+t['rid']).json()['request']['review_available'] is False)
        result=compose(root,'exec','-T','api','python','-m','app.cli.cleanup_revoked')
        check('runtime CLI purges three private items', 'private:purged=3' in result.stdout)
        receipt=post(user,'/library/items/'+t['item']['item_id']+'/delete',{'confirm_delete':True})
        check('idempotent post-purge receipt completed',receipt.json()['cleanup_status']=='completed')
        compose(root,'stop','api','web','worker','beat')
        result=backup_cli(root,'backup','--project',c['project'],'--out',root.parent/'backups-after')
        t['backup_after']=result['artifact']
        journal_dir=root.parent/'current-journal'
        backup_cli(root,'copy-journal','--from-project',c['project'],'--to',journal_dir,'--artifact',t['backup_before'])
        t['current_journal']=str(journal_dir)
        target_file.write_text(json.dumps(t,indent=2),encoding='utf-8')
        check('post-purge backup and current independent journal copied',True)
        return
    # Two old-snapshot restores, then a purged snapshot with old objects restored too.
    dst=root.parent/'restore-stack'
    if not (dst/'stack.json').exists():
        subprocess.run([sys.executable,str(REPO/'scripts/c4_stack.py'),'--context',str(dst),'setup'],check=True)
    dc=json.loads((dst/'stack.json').read_text());dc['env']['RAS_API_IMAGE']=e['RAS_API_IMAGE']
    dc['env']['RAS_JOURNAL_SOURCE']=str(dst/'current-journal').replace('\\','/')
    (dst/'stack.json').write_text(json.dumps(dc));(dst/'stack.env').write_text(''.join(f'{k}={v}\n' for k,v in dc['env'].items()))
    override=json.loads((dst/'override.json').read_text());override['services']['web']['image']=c['project']+'-web:local'
    (dst/'override.json').write_text(json.dumps(override))
    shutil.copytree(Path(t['current_journal']),dst/'current-journal',dirs_exist_ok=True)
    source_journal=Path(t['current_journal'])/'revocations.jsonl';source_hash=hashlib.sha256(source_journal.read_bytes()).hexdigest()
    for number,artifact in enumerate((t['backup_before'],t['backup_before'],t['backup_after']),1):
        restored=backup_cli(dst,'restore','--artifact',artifact,'--project',dc['project'],'--journal-dir',dst/'current-journal')
        check('restore '+str(number)+' real DB and objects remains held',restored['gate']!='open')
        if number==3:
            # Restore purged objects from the earlier artifact while retaining the
            # newer DB's purged_at: recovery must sweep them again, without reset.
            sys.path.insert(0,str(REPO/'api'))
            from app.backup import verify_artifact,restore_objects
            from ras_backup import storage_at
            from pydantic import SecretStr
            old=Path(t['backup_before']);manifest=verify_artifact(old)
            storage=storage_at(int(dc['env']['MINIO_API_HOST_PORT']),dc['env'])
            restore_objects(storage,old,manifest)
            check('purged originals physically restored from older object snapshot',True)
        compose(dst,'run','--rm','--no-deps','recovery-init','python','-m','app.cli.recovery','recover')
        compose(dst,'run','--rm','--no-deps','recovery-init','python','-m','app.cli.recovery','recover')
        compose(dst,'up','-d','--no-deps','--wait','api','web')
        url='http://127.0.0.1:'+dc['env']['WEB_HOST_PORT']
        check('restore '+str(number)+' readiness after two recover passes',httpx.get(url+'/api/readyz',timeout=120).status_code==200)
        for who in ('a','b','admin'):
            out=login(who,url)
            check('restore '+str(number)+' '+who+' original/source/private denied',
                all(out.get('/api'+path).status_code==404 for path in ('/library/items/'+t['item']['item_id'],
                    '/files/'+t['item']['file_id']+'/original','/search/chunks/'+t['chunk'])))
            check('restore '+str(number)+' '+who+' common original control',out.get('/api/files/'+t['common_file']+'/original').status_code==200)
            if who=='b':check('restore B private same bytes control',out.get('/api/files/'+t['same']['file_id']+'/original').status_code==200)
            if who=='admin':check('restore review grant still closed',out.get('/api/admin/review-requests/'+t['rid']).json()['request']['review_available'] is False)
            if who=='a':check('post-backup deleted item stays absent',out.get('/api/library/items/'+t['late']['item_id']).status_code==404)
        compose(dst,'stop','api','web','worker','beat')
        check('source current journal unchanged by restore recovery',hashlib.sha256(source_journal.read_bytes()).hexdigest()==source_hash)
    print('M6d full drill complete: '+str(len(checks))+' checks',flush=True)


if __name__=='__main__':main()
