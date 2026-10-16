"""Real image HTTP/proxy acceptance on a guarded synthetic C4 context."""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
import httpx

REPO = Path(__file__).resolve().parents[1]


def bounded_get(*args, **kwargs):
    kwargs.setdefault("timeout",120)
    return httpx.get(*args,**kwargs)


def main():
    p=argparse.ArgumentParser();p.add_argument('--context',type=Path,required=True)
    p.add_argument('phase',choices=['default','paid','outage']);a=p.parse_args();root=a.context.resolve()
    if root==REPO or REPO in root.parents: raise SystemExit('external context required')
    c=json.loads((root/'stack.json').read_text());assert c['project'].startswith('rasc4')
    e=c['env'];origin='http://127.0.0.1:'+e['WEB_HOST_PORT'];api='http://127.0.0.1:'+e['API_HOST_PORT']
    results=[]
    result_path=root/(a.phase+'-http-'+str(time.time_ns())+'.json')
    def check(label,condition):
        results.append({'check':label,'pass':bool(condition)})
        result_path.write_text(json.dumps(results,indent=2))
        print(('PASS ' if condition else 'FAIL ')+label,flush=True)
        assert condition,label
    def compose(*args):
        return subprocess.run([sys.executable,str(REPO/'scripts/c4_stack.py'),'--context',str(root),
            'compose',*args],text=True,capture_output=True,check=True)
    check('real API and proxy live',bounded_get(api+'/readyz').status_code==200 and bounded_get(origin).status_code==200)
    def client(email=None,password=None):
        out=httpx.Client(base_url=origin,timeout=120,headers={'origin':origin})
        out.get('/api/auth/csrf')
        if email:
            r=post(out,'/auth/login',{'email':email,'password':password});assert r.status_code==200,r.text
        return out
    def headers(out):return {'x-csrf-token':out.cookies.get('ras_csrf',''),'origin':origin}
    def post(out,path,body):return out.post('/api'+path,json=body,headers=headers(out))
    def upload(out,data,kind='markdown',filename='c4-synthetic.md'):
        notice=out.get('/api/uploads/notice').json()['version']
        r=post(out,'/uploads/intents',{'acknowledged':True,'notice_version':notice,'file_kind':kind,
            'filename':filename,'byte_size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        assert r.status_code==201,r.text
        sent=out.put('/api/uploads/original',content=data,headers={**headers(out),
            'x-upload-intent':r.json()['intent_token'],'content-type':'application/octet-stream'})
        assert sent.status_code==201,sent.text
        return sent.json()
    account_path=root/'synthetic-accounts.json'
    if a.phase=='default':
        if not account_path.exists():
            accounts={k:{'email':f'c4-{k}@example.org','password':secrets.token_urlsafe(24)} for k in ('admin','a','b')}
            bootstrap=subprocess.run([sys.executable,str(REPO/'scripts/c4_stack.py'),'--context',str(root),'compose',
                'exec','-T','api','python','-m','app.cli.bootstrap_admin','--email',accounts['admin']['email'],'--password-stdin'],
                input=accounts['admin']['password']+'\n',text=True,capture_output=True)
            check('one-time runtime bootstrap CLI',bootstrap.returncode==0)
            account_path.write_text(json.dumps(accounts),encoding='utf-8')
            admin=client(**accounts['admin']);public=client()
            for key in ('a','b'):
                invitation=post(admin,'/admin/invitations',{'email':accounts[key]['email']})
                assert invitation.status_code==201,invitation.text
                entries=admin.get('/api/admin/dev-outbox').json()['messages']
                token=next(m['link'].split('#token=')[1] for m in entries if m['recipient']==accounts[key]['email'])
                accepted=post(public,'/auth/invitations/accept',{'token':token,'password':accounts[key]['password']})
                check('runtime invitation acceptance '+key,accepted.status_code==201)
        accounts=json.loads(account_path.read_text())
        user=client(**accounts['a'])
        ids=upload(user,('# Default pricing synthetic paper '+str(time.time_ns())+'\n\n## Methods\nSynthetic method has nine units.\n').encode())
        end=time.monotonic()+60
        blocked=False
        while time.monotonic()<end:
            detail=user.get('/api/library/items/'+ids['item_id']).json()
            jobs=detail.get('item',{}).get('jobs',detail.get('jobs',[]))
            if any(j.get('state')=='awaiting_index' for j in jobs):
                blocked=True;break
            time.sleep(.5)
        assert blocked,'worker extraction did not complete'
        code = 'from app.ingest.worker import process_job; from app.config import get_settings; from uuid import UUID; print(process_job(UUID('+repr(ids['job_id'])+'),get_settings()))'
        probe=subprocess.run([sys.executable,str(REPO/'scripts/c4_stack.py'),'--context',str(root),'compose',
            'exec','-T','worker','python','-c',code],text=True,capture_output=True)
        check('default document paid call denied embedding_pricing_unverified',probe.returncode==0 and 'embedding_pricing_unverified' in probe.stdout)
        log=root/'provider-fixture/calls.jsonl'
        check('default zero provider transport calls',not log.exists() or not log.read_text().strip())
        check('normal auth mutations keep ready',bounded_get(api+'/readyz').status_code==200)
        return
    accounts=json.loads(account_path.read_text())
    admin,user,other=(client(**accounts[k]) for k in ('admin','a','b'))
    if a.phase=='outage':
        compose('stop','redis')
        try:
            body=('# Redis outage '+str(time.time_ns())+'\n\n## Methods\nSynthetic durable job.\n').encode()
            pending=upload(user,body)
            detail=user.get('/api/library/items/'+pending['item_id']).json()['item']
            check('Redis outage retains durable upload and readiness',
                detail['jobs'][0]['state']=='received' and bounded_get(api+'/readyz').status_code==200)
        finally:
            compose('up','-d','--no-deps','--wait','redis')
        end=time.monotonic()+180
        while time.monotonic()<end:
            detail=user.get('/api/library/items/'+pending['item_id']).json()['item']
            if detail['content_status']=='indexed':break
            time.sleep(.5)
        else:raise AssertionError('worker did not resume after Redis outage')
        check('real worker resumes and indexes retained job',True)
        return
    data=('# C4 Synthetic Method '+str(time.time_ns())+'\n\n## Abstract\nSynthetic method measures nine units.\n\n## Methods\nSynthetic method uses controlled evidence and records nine units.\n').encode()
    ids=upload(user,data);same=upload(other,data)
    pdf=upload(user,(REPO/'evaluation/rag/corpus/pdf/p07-thermal-scheduling.pdf').read_bytes(),'pdf','thermal.pdf')
    partial=upload(user,(REPO/'evaluation/rag/corpus/pdf/p08-event-camera-wildlife.pdf').read_bytes(),'pdf','partial.pdf')
    import io
    from pypdf import PdfWriter
    scan=PdfWriter();scan.add_blank_page(width=612,height=792);raw=io.BytesIO();scan.write(raw)
    scan_bytes=raw.getvalue();unextractable=upload(user,scan_bytes,'pdf','blank-no-ocr.pdf')
    check('separate consent Markdown and PDF uploads',bool(ids['job_id'] and same['job_id'] and pdf['job_id']))
    def wait_index(out,item):
        end=time.monotonic()+180
        while time.monotonic()<end:
            r=out.get('/api/library/items/'+item)
            if r.status_code==200 and r.json()['item']['content_status']=='indexed':return r.json()
            time.sleep(.5)
        raise AssertionError('worker did not index '+item)
    wait_index(user,ids['item_id']);wait_index(other,same['item_id']);wait_index(user,pdf['item_id'])
    partial_detail=wait_index(user,partial['item_id'])['item']
    check('partial PDF remains labeled partial after indexing',partial_detail['versions'][0]['coverage']=='partial')
    end=time.monotonic()+180
    while time.monotonic()<end:
        rejected=user.get('/api/library/items/'+unextractable['item_id']).json()['item']
        if rejected['jobs'][0]['state']=='needs_review':break
        time.sleep(.5)
    else:raise AssertionError('blank PDF did not become needs_review')
    check('no OCR: needs_review stays metadata-only and preserves original',rejected['content_status']=='metadata_only'
          and user.get('/api/files/'+unextractable['file_id']+'/original').content==scan_bytes)
    check('real worker extracted and indexed Markdown/PDF',True)
    for direct in (False,True):
        target=api if direct else origin+'/api'
        own=bounded_get(target+'/files/'+ids['file_id']+'/original',cookies=user.cookies)
        denied=bounded_get(target+'/files/'+ids['file_id']+'/original',cookies=other.cookies)
        denied_admin=bounded_get(target+'/files/'+ids['file_id']+'/original',cookies=admin.cookies)
        check('original SHA256 and private ACL '+str(direct),own.status_code==200 and own.content==data
              and denied.status_code==denied_admin.status_code==404)
    coll=post(user,'/collections',{'name':'C4 synthetic collection'}).json()['collection']['id']
    check('runtime collection membership',post(user,f'/collections/{coll}/items',{'item_id':ids['item_id']}).status_code==201)
    last=None
    for scope in ({'item_id':ids['item_id']},{'collection_id':coll},{}):
        for mode in ('keyword','hybrid'):
            r=post(user,'/search',{'query':'Synthetic method','mode':mode,'rerank':True,**scope})
            check('search '+mode+' '+str(scope),r.status_code==200 and bool(r.json()['hits']))
            last=r.json()['hits'][0]
            rag=post(user,'/rag/ask',{'question':'What does synthetic method measure?','mode':mode,**(scope or {'scope':'all'})})
            check('RAG answered '+mode+' '+str(scope),rag.status_code==200 and rag.json()['status']=='answered')
    chunk=user.get('/api/search/chunks/'+last['chunk_id'])
    check('citation opens exact chunk/version/file/locator',chunk.status_code==200 and chunk.json()['version_id']==last['version_id'] and chunk.json()['source_file_id']==last['source_file_id'])
    notice=user.get('/api/review-requests/notice').json()['version']
    rr=post(user,'/review-requests',{'acknowledged':True,'notice_version':notice,'item_id':ids['item_id'],
        'version_id':ids['version_id'],'source_file_id':ids['file_id'],'declared_source':'synthetic C4',
        'declared_rights':'synthetic author permission'}).json()['request']['id']
    sha=hashlib.sha256(data).hexdigest()
    approved=post(admin,f'/admin/review-requests/{rr}/approve',{'confirm_verified':True,
        'file_checksum_sha256':sha,'title':'C4 Common Synthetic Method','verified_source':'synthetic controlled permission',
        'evidence_note':'synthetic file only','checked_on':datetime.now(timezone.utc).date().isoformat(),
        'scope':{'audience':'instance_users','original_download':True,'fulltext_search':True,'external_processing':True,'conditions':None}})
    assert approved.status_code==200,approved.text
    common=approved.json()['request']['pending_publication_id']
    check('approve creates separate pending common copy',common!=ids['item_id'] and other.get('/api/library/items/'+common).status_code==404)
    check('publish exact checksum',post(admin,f'/admin/common-copies/{common}/publish',{'confirm_publish':True,'file_checksum_sha256':sha}).status_code==200)
    check('common index accepted',post(admin,f'/admin/common-copies/{common}/index',{'confirm_external_processing':True}).status_code==200)
    wait_index(other,common)
    hits=post(other,'/search',{'query':'Synthetic method','mode':'hybrid','rerank':True}).json()['hits']
    common_hit=next(h for h in hits if h['item_id']==common)
    check('common accessible with exact rights',other.get('/api/files/'+common_hit['source_file_id']+'/original').content==data)
    compose('stop','worker','beat')  # Prove denial while cleanup cannot have run.
    revoked=post(admin,f'/admin/common-copies/{common}/revoke',{'confirm_revoke':True,'reason_code':'rights_withdrawn','retain':[]})
    check('runtime common revoke',revoked.status_code==200)
    check('new common requests denied before cleanup',other.get('/api/search/chunks/'+common_hit['chunk_id']).status_code==404
          and other.get('/api/files/'+common_hit['source_file_id']+'/original').status_code==404)
    check('private same bytes remains accessible',other.get('/api/files/'+same['file_id']+'/original').content==data)
    def rights_copy(title,scope):
        notice=user.get('/api/review-requests/notice').json()['version']
        request=post(user,'/review-requests',{'acknowledged':True,'notice_version':notice,'item_id':ids['item_id'],
            'version_id':ids['version_id'],'source_file_id':ids['file_id'],'declared_source':'synthetic C4',
            'declared_rights':'synthetic permission'}).json()['request']['id']
        result=post(admin,f'/admin/review-requests/{request}/approve',{'confirm_verified':True,
            'file_checksum_sha256':sha,'title':title,'verified_source':'synthetic C4',
            'evidence_note':'synthetic permission only','checked_on':datetime.now(timezone.utc).date().isoformat(),
            'scope':{'audience':'instance_users','conditions':None,**scope}})
        assert result.status_code==200,result.text
        common=result.json()['request']['pending_publication_id']
        result=post(admin,f'/admin/common-copies/{common}/publish',{'confirm_publish':True,'file_checksum_sha256':sha})
        assert result.status_code==200,result.text
        return common
    def revoke_copy(common):
        r=post(admin,f'/admin/common-copies/{common}/revoke',{'confirm_revoke':True,'reason_code':'rights_withdrawn','retain':[]})
        assert r.status_code==200,r.text
    download_only=rights_copy('C4 download only',{'original_download':True,'fulltext_search':False,'external_processing':False})
    detail=other.get('/api/library/items/'+download_only).json()['item']
    original=other.get('/api'+detail['files'][0]['original_url'])
    denied=post(admin,f'/admin/common-copies/{download_only}/index',{'confirm_external_processing':True})
    check('download right does not grant fulltext indexing',original.content==data and denied.status_code==409
          and denied.json()['error']=='fulltext_not_permitted')
    revoke_copy(download_only)
    no_download=rights_copy('C4 searchable without download',{'original_download':False,'fulltext_search':True,'external_processing':True})
    assert post(admin,f'/admin/common-copies/{no_download}/index',{'confirm_external_processing':True}).status_code==200
    compose('up','-d','--no-deps','--wait','worker','beat');wait_index(other,no_download)
    hits=post(other,'/search',{'query':'Synthetic method','mode':'keyword','item_id':no_download}).json()['hits']
    hit=hits[0]
    check('fulltext/source right does not grant original download',other.get('/api/search/chunks/'+hit['chunk_id']).status_code==200
          and hit['original_url'] is None and other.get('/api/files/'+hit['source_file_id']+'/original').status_code==404)
    compose('stop','worker','beat');revoke_copy(no_download)
    no_processing=rights_copy('C4 no external processing',{'original_download':True,'fulltext_search':True,'external_processing':False})
    denied=post(admin,f'/admin/common-copies/{no_processing}/index',{'confirm_external_processing':True})
    log=root/'provider-fixture/calls.jsonl';before=len(log.read_text().splitlines())
    rag=post(other,'/rag/ask',{'question':'What does synthetic method measure?','item_id':no_processing,'mode':'hybrid'})
    check('missing external-processing right denies indexing and provider payload',denied.status_code==409
          and denied.json()['error']=='external_processing_not_permitted' and rag.status_code==404
          and rag.json()['error']=='not_found' and len(log.read_text().splitlines())==before)
    revoke_copy(no_processing)
    removed=user.delete('/api/collections/'+coll+'/items/'+ids['item_id'],headers=headers(user))
    empty=post(user,'/search',{'query':'Synthetic method','collection_id':coll,'mode':'keyword'})
    check('changed membership excludes retained indexed chunks',removed.status_code==204 and empty.status_code==200
          and empty.json()['hits']==[])
    assert post(user,f'/collections/{coll}/items',{'item_id':ids['item_id']}).status_code==201
    old=dict(user.cookies)
    assert post(user,'/auth/logout',{}).status_code==204
    check('revoked session denied with live B control',bounded_get(api+'/auth/session',cookies=old).status_code==401
          and other.get('/api/auth/session').status_code==200)
    fresh=client(**accounts['a']); prior=dict(fresh.cookies)
    public=client()
    assert post(public,'/auth/password-reset/request',{'email':accounts['a']['email']}).status_code==202
    messages=admin.get('/api/admin/dev-outbox').json()['messages']
    token=next(m['link'].split('#token=')[1] for m in reversed(messages)
               if m['recipient']==accounts['a']['email'] and '/reset-password' in m['link'])
    password=secrets.token_urlsafe(24)
    confirmed=post(public,'/auth/password-reset/confirm',{'token':token,'new_password':password})
    check('real runtime reset confirmed',confirmed.status_code==200)
    accounts['a']['password']=password
    account_path.write_text(json.dumps(accounts),encoding='utf-8')
    replacement=client(**accounts['a'])
    check('reset closes prior session with live replacement and unrelated B control',
          bounded_get(api+'/auth/session',cookies=prior).status_code==401
          and replacement.get('/api/auth/session').status_code==200
          and other.get('/api/auth/session').status_code==200)
    check('reset token cannot be reused',post(public,'/auth/password-reset/confirm',
          {'token':token,'new_password':password}).status_code==400)
    again=subprocess.run([sys.executable,str(REPO/'scripts/c4_stack.py'),'--context',str(root),'compose',
        'exec','-T','api','python','-m','app.cli.bootstrap_admin','--email',accounts['admin']['email'],
        '--password-stdin'],input=accounts['admin']['password']+'\n',text=True,capture_output=True)
    check('runtime bootstrap refuses second account creation',again.returncode!=0)
    check('ready after full business mutations',bounded_get(api+'/readyz').status_code==200)
    compose('up','-d','--no-deps','--wait','worker','beat')
    (root/'browser-targets.json').write_text(json.dumps({'origin':origin,'item':ids,'collection':coll,'pdf':pdf}),encoding='utf-8')


if __name__=='__main__':main()
