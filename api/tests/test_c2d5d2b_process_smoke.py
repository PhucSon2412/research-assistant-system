"""Linux Celery worker+beat on the actual runtime Compose override, isolated Redis.

The mounted sitecustomize is TEST ONLY: it scopes cutover, schedule and egress.
No production switch, operator credential, provider or dev service is involved.
"""
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import IngestionJob, SourceFile
from tests.conftest import REPO_DIR
from tests.test_c2c1_bootstrap import installation  # noqa: F401
from tests.test_c2c2_invitations import world  # noqa: F401
from tests.test_c2d1_final_caller import browser  # noqa: F401
from tests.test_c2d5a_review_writes import writers  # noqa: F401
from tests.test_c2d5d1_common_index_request import indexes, index, jobs
from tests.test_pdf_ingest import full_pdf
from tests.resource_fixtures import source, BODY


def attach(w, ids, owner, actor):
    with Session(w.runtime) as db:
        job=IngestionJob(origin='upload',scope_visibility='private',scope_owner_user_id=owner,
            item_id=ids.item,version_id=ids.version,rights_epoch_at_start=0,
            idempotency_key='smoke:'+uuid.uuid4().hex,state='received')
        db.add(job);db.flush()
        file=db.get(SourceFile,ids.file);file.job_id=job.id
        # Labelled resource/consent setup with a REAL owner's session. Host
        # document tests cover HTTP intent+upload; this smoke covers Linux worker.
        from app.models import ProcessingAcknowledgement, UploadIntent
        from tests.test_auth_safety_foundation import NOW
        from datetime import timedelta
        ack=ProcessingAcknowledgement(actor_user_id=owner,session_id=actor.session_id,
            notice_version=get_settings().upload_notice_version,acknowledged_at=NOW)
        db.add(ack);db.flush()
        db.add(UploadIntent(token_hash=uuid.uuid4().hex*2,acknowledgement_id=ack.id,user_id=owner,
            session_id=actor.session_id,notice_version=ack.notice_version,
            file_kind='pdf' if file.verified_mime=='application/pdf' else 'markdown',filename='synthetic',
            declared_size=file.byte_size,declared_sha256=file.checksum_sha256,created_at=NOW,
            expires_at=NOW+timedelta(hours=1),used_at=NOW,item_id=ids.item))
        db.commit()
        return job.id


def test_real_worker_beat_fake_provider_restart_and_three_indexes(indexes):
    b=indexes
    context=Path(os.environ['RAS_C2D5D2A_CONTEXT'])
    c=json.loads((context/'credentials.json').read_text())
    evidence=context/('process-'+uuid.uuid4().hex)
    evidence.mkdir()
    assert index(b).status_code==200
    common=jobs(b)[0]['id']
    markdown=attach(b.w,b.own,b.user.id,b.owner_session)
    # A second private file uses the real PDF parser child in Linux.
    data=full_pdf()
    ids=source(b.w,b.user.id,'PDF-SYNTHETIC-SMOKE',data=data)
    with b.w.engine.begin() as conn:  # source fixture format correction, before worker runs
        conn.execute(text('ALTER TABLE source_files DISABLE TRIGGER USER'))
        conn.execute(text("UPDATE source_files SET verified_mime='application/pdf',page_count=4 WHERE id=:f"),{'f':ids.file})
        conn.execute(text('ALTER TABLE source_files ENABLE TRIGGER USER'))
    pdf=attach(b.w,ids,b.user.id,b.owner_session)
    import io
    b.raw_storage.put(b.own.key,io.BytesIO(BODY),'text/markdown')
    b.raw_storage.put(ids.key,io.BytesIO(data),'application/pdf')
    targets=[markdown,pdf,common]
    # Explicit external env file; values are never printed or placed in argv.
    values={'POSTGRES_USER':'task_admin','POSTGRES_PASSWORD':c['password'],
        'POSTGRES_DB':b.w.runtime.url.database,'MINIO_ROOT_USER':'task_minio','MINIO_ROOT_PASSWORD':c['minio'],
        'REDIS_PASSWORD':c['redis'],'RAS_WORKER_RUNTIME_USER':b.w.runtime.url.username,
        'RAS_WORKER_RUNTIME_PASSWORD':c['runtime'],'RAS_JOURNAL_SOURCE':str(b.w.path.parent).replace('\\','/')}
    env_file=evidence/'runtime.env'
    env_file.write_text('\n'.join(k+'='+v for k,v in values.items())+'\n',encoding='utf-8')
    harness=evidence/'harness';harness.mkdir()
    (harness/'sitecustomize.py').write_text('''
import json, os, socket, sys
from pathlib import Path
allowed={socket.gethostbyname(name) for name in ('db','minio')}
original=socket.socket.connect
def connect(self,address):
 if isinstance(address,tuple) and address[0] not in allowed and address[0]!='127.0.0.1':
  if address[1]!=6379 or address[0]!=socket.gethostbyname('redis'):
   raise OSError('test egress blocked')
 return original(self,address)
socket.socket.connect=connect
if sys.argv[0].endswith('celery'):
 from app import auth_safety
 auth_safety.cutover=lambda db:'ready'
 from app.ingest.celery_app import celery_app
 celery_app.conf.beat_schedule={'document-test-only':{'task':'ras.ingest.discover','schedule':1.0}}
 import httpx
 from pydantic import SecretStr
 from app.config import get_settings
 from app.ingest import celery_app as tasks
 selected=get_settings().model_copy(deep=True)
 selected.embedding.api_key=SecretStr('fake-process-transport-only')
 selected.embedding.pricing_verified=True
 tasks.settings=selected
 actual=httpx.Client
 def handle(request):
  from app.db import get_engine
  assert get_engine().pool.checkedout()==0
  payload=json.loads(request.content)
  assert payload['input_type']=='search_document'
  assert 'References' not in payload['input']
  with open('/evidence/provider-'+socket.gethostname()+'-'+str(os.getpid())+'.jsonl','a') as out:
   out.write(json.dumps({'model':payload['model'],'bytes':len(payload['input'].encode())})+'\\n')
  # One definite rejection after a completed chunk; the host restarts both
  # processes while the durable job is waiting, then proves completed reuse.
  total=sum(len(p.read_text().splitlines()) for p in Path('/evidence').glob('provider-*.jsonl'))
  if total==2 and not Path('/evidence/rejected-once').exists():
   Path('/evidence/rejected-once').write_text('429')
   return httpx.Response(429)
  return httpx.Response(200,json={'model':'text-embedding-3-small','provider':'Azure',
   'data':[{'embedding':[.1,.2,.3]}],'usage':{'prompt_tokens':3}})
 def fake(*a,**kw):
  kw['transport']=httpx.MockTransport(handle)
  return actual(*a,**kw)
 httpx.Client=fake
 from app.db import get_sessionmaker
 from app.ingest import runtime
 from sqlalchemy import text
 db=get_sessionmaker()()
 def proof(_):
  runtime.require_runtime(db)
  return dict(db.execute(text("SELECT current_user AS login,has_table_privilege('users','UPDATE') AS raw_user_write,has_database_privilege(current_database(),'TEMP') AS temp")).mappings().one())
 fact=runtime.short(db,proof)
 Path('/evidence/identity-'+socket.gethostname()+'-'+str(os.getpid())+'.json').write_text(json.dumps(fact))
 from celery.signals import before_task_publish
 @before_task_publish.connect
 def payload(sender=None,body=None,**kw):
  if sender=='ras.ingest.process':
   import uuid
   assert len(body[0])==1 and body[1]=={}
   uuid.UUID(body[0][0])
   with open('/evidence/payload-'+socket.gethostname()+'-'+str(os.getpid())+'.jsonl','a') as out:
    out.write(json.dumps({'task':sender,'args':body[0]})+'\\n')
''',encoding='utf-8')
    extra={'services':{},'networks':{'default':{'external':True,'name':c['project']+'_default'}}}
    for name in ('worker','beat'):
        extra['services'][name]={'image':os.environ.get('RAS_TEST_PROCESS_IMAGE','rasc2d5d2b-worker:20261010'), 'build':str(REPO_DIR/'api'),
            'labels':{'ras.task':c['project']},'environment':{'PYTHONPATH':'/harness:/srv/api',
                'RAS_REDIS_PORT':'6379'},
            'volumes':[str(REPO_DIR/'api'/'app').replace('\\','/')+':/srv/api/app:ro',
                str(harness).replace('\\','/')+':/harness:ro',str(evidence).replace('\\','/')+':/evidence']}
    extra['services']['worker']['environment']['RAS_STORAGE__BUCKET']=b.raw_storage.bucket
    extra['services']['beat']['command']=['celery','-A','app.ingest.celery_app:celery_app','beat','--loglevel=WARNING','--schedule=/tmp/beat']
    override=evidence/'smoke.json';override.write_text(json.dumps(extra),encoding='utf-8')
    compose=['docker','compose','--env-file',str(env_file),'-p',c['project'],'-f',str(REPO_DIR/'docker-compose.yml'),
        '-f',str(REPO_DIR/'scripts'/'compose.worker-runtime.yml'),'-f',str(override)]
    def command(args, *, name):
        result=subprocess.run(args,capture_output=True,text=True,encoding='utf-8',timeout=60)
        (evidence/(name+'.log')).write_text(result.stdout+result.stderr,encoding='utf-8')
        assert result.returncode==0, name+' failed; see external raw log'
        return result.stdout
    control=['docker','compose','--env-file',str(context/'empty.env'),'-p',c['project'],'-f',str(context/'compose.json')]
    started=False
    try:
        # Broker really disappears; no claim is lost and PostgreSQL stays durable.
        command(control+['stop','redis'],name='redis-stop')
        started=True
        command(compose+['up','-d','--no-deps','--no-build','worker','beat'],name='start')
        process_ids=command(compose+['ps','-q','worker','beat'],name='ids').split()
        facts=[]
        for container in process_ids:
            # Inspect public fields only; credentials are never written to logs.
            public=json.loads(command(['docker','inspect','--format',
                '{"Id":{{json .Id}},"Image":{{json .Image}},"StartedAt":{{json .State.StartedAt}}}',
                container],name='inspect-'+container[:12]))
            assert public['Id'] and public['Image']
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            assert all(j['attempt_count']==0 for j in jobs(b))
            time.sleep(.1)
        command(control+['start','redis'],name='redis-start')
        deadline=time.monotonic()+40
        waiting=[]
        while time.monotonic()<deadline:
            with b.w.runtime.connect() as conn:
                waiting=conn.execute(text("SELECT id FROM ingestion_jobs WHERE id=ANY(:ids) AND state='retryable_failed' AND resume_state='indexing'"),{'ids':targets}).all()
            if waiting: break
            time.sleep(.1)
        assert waiting, 'fake rejection did not reach durable retry'
        command(compose+['stop','worker','beat'],name='stop-for-resume')
        with b.w.runtime.connect() as conn:
            completed_before=conn.execute(text("SELECT id,reservation_token FROM embedding_calls WHERE status='completed'")).all()
        assert completed_before
        command(compose+['up','-d','--no-deps','--no-build','worker','beat'],name='resume')
        deadline=time.monotonic()+55
        while time.monotonic()<deadline:
            with b.w.runtime.connect() as conn:
                rows=conn.execute(text('SELECT id,state,attempt_count,finished_at,extraction_finished_at FROM ingestion_jobs WHERE id=ANY(:ids)'),{'ids':targets}).all()
            if len(rows)==3 and all(r.state=='ready' for r in rows):break
            time.sleep(.2)
        assert len(rows)==3 and any(r.attempt_count==3 for r in rows) and all(r.state=='ready' and r.attempt_count in (2,3)
            and r.finished_at is not None and r.extraction_finished_at is not None for r in rows)
        with b.w.runtime.connect() as conn:
            assert conn.scalar(text('SELECT count(*) FROM index_generations'))>=3
            assert conn.scalar(text('SELECT count(*) FROM index_entries'))>0
            assert conn.scalar(text("SELECT count(*) FROM embedding_calls WHERE status='completed' AND vector_dims(vector)=3"))>0
            assert conn.scalar(text("SELECT count(*) FROM library_items WHERE content_status='indexed'"))==3
            for call_id,nonce in completed_before:
                assert conn.execute(text("SELECT status,reservation_token FROM embedding_calls WHERE id=:i"),{'i':call_id}).one()==('completed',nonce)
        with b.w.runtime.connect() as conn:
            calls_before=conn.scalar(text('SELECT count(*) FROM embedding_calls'))
            committed_before=conn.scalar(text('SELECT sum(committed_usd) FROM embedding_budget_days'))
        command(compose+['restart','worker','beat'],name='restart')
        time.sleep(3)
        with b.w.runtime.connect() as conn:
            assert conn.scalar(text('SELECT count(*) FROM embedding_calls'))==calls_before
            assert conn.scalar(text('SELECT sum(committed_usd) FROM embedding_budget_days'))==committed_before
            assert conn.scalar(text("SELECT count(*) FROM embedding_calls WHERE owner_user_id IS NULL"))>0
        facts=[json.loads(p.read_text()) for p in evidence.glob('identity-*.json')]
        assert len(facts)>=2 and all(f=={'login':'c1_runtime','raw_user_write':False,'temp':False} for f in facts)
        payloads=[json.loads(line) for p in evidence.glob('payload-*.jsonl') for line in p.read_text().splitlines()]
        assert {uuid.UUID(p['args'][0]) for p in payloads}==set(targets)
        (evidence/'result.json').write_text(json.dumps({'jobs':[{k:str(getattr(r,k)) for k in r._fields} for r in rows],
            'identities':facts,'override':'scripts/compose.worker-runtime.yml','provider_calls':sum(len(p.read_text().splitlines()) for p in evidence.glob('provider-*.jsonl'))}),encoding='utf-8')
    finally:
        if started:
            command(compose+['logs','--no-color','worker','beat'],name='processes')
            command(compose+['stop','worker','beat'],name='stop')
            command(compose+['rm','-f','worker','beat'],name='remove')
        # Keep the isolated broker available for following tests even on smoke failure.
        command(control+['start','redis'],name='redis-final-start')
        env_file.unlink(missing_ok=True)
