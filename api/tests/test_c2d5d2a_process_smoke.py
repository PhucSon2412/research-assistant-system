"""Linux Celery worker+beat on the actual runtime Compose override, isolated Redis.

The mounted sitecustomize is TEST ONLY: it controls schedule and egress; the C4 gate is real.
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
from tests.test_c2c2_invitations import operational_world as world  # noqa: F401
from tests.test_c2d1_final_caller import browser  # noqa: F401
from tests.test_c2d5a_review_writes import writers  # noqa: F401
from tests.test_c2d5d1_common_index_request import indexes, index, jobs
from tests.test_pdf_ingest import full_pdf
from tests.resource_fixtures import source, BODY


def attach(w, ids, owner):
    with Session(w.runtime) as db:
        job=IngestionJob(origin='upload',scope_visibility='private',scope_owner_user_id=owner,
            item_id=ids.item,version_id=ids.version,rights_epoch_at_start=0,
            idempotency_key='smoke:'+uuid.uuid4().hex,state='received')
        db.add(job);db.flush()
        file=db.get(SourceFile,ids.file);file.job_id=job.id
        db.commit()
        return job.id


def test_real_worker_beat_runtime_redis_outage_and_three_jobs(indexes):
    b=indexes
    context=Path(os.environ['RAS_C2D5D2A_CONTEXT'])
    c=json.loads((context/'credentials.json').read_text())
    evidence=context/('process-'+uuid.uuid4().hex)
    evidence.mkdir()
    assert index(b).status_code==200
    common=jobs(b)[0]['id']
    markdown=attach(b.w,b.own,b.user.id)
    # A second private file uses the real PDF parser child in Linux.
    data=full_pdf()
    ids=source(b.w,b.user.id,'PDF-SYNTHETIC-SMOKE',data=data)
    with b.w.engine.begin() as conn:  # source fixture format correction, before worker runs
        conn.execute(text('ALTER TABLE source_files DISABLE TRIGGER USER'))
        conn.execute(text("UPDATE source_files SET verified_mime='application/pdf',page_count=4 WHERE id=:f"),{'f':ids.file})
        conn.execute(text('ALTER TABLE source_files ENABLE TRIGGER USER'))
    pdf=attach(b.w,ids,b.user.id)
    import io
    b.raw_storage.put(b.own.key,io.BytesIO(BODY),'text/markdown')
    b.raw_storage.put(ids.key,io.BytesIO(data),'application/pdf')
    targets=[markdown,pdf,common]
    # Explicit external env file; values are never printed or placed in argv.
    values={'POSTGRES_USER':'task_admin','POSTGRES_PASSWORD':c['password'],
        'POSTGRES_DB':b.w.runtime.url.database,'MINIO_ROOT_USER':'task_minio','MINIO_ROOT_PASSWORD':c['minio'],
        'REDIS_PASSWORD':c['redis'],'RAS_WORKER_RUNTIME_USER':b.w.runtime.url.username,
        'RAS_WORKER_RUNTIME_PASSWORD':c['runtime'],'RAS_JOURNAL_SOURCE':str(b.w.path.parent).replace('\\','/')}
    values.update(RAS_RUNTIME_DB_USER='c1_runtime',RAS_RUNTIME_DB_PASSWORD=c['runtime'],
        RAS_RECOVERY_DB_USER='c1_recovery',RAS_RECOVERY_DB_PASSWORD=c['recovery'],
        RAS_DEFINER_DB_ROLE='c1_definer',RAS_API_IMAGE=os.environ.get('RAS_TEST_PROCESS_IMAGE','rasc4-process:local'))
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

 from app.ingest.celery_app import celery_app
 celery_app.conf.beat_schedule={'extraction-only':{'task':'ras.ingest.discover','schedule':1.0}}
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
        extra['services'][name]={'image':os.environ.get('RAS_TEST_PROCESS_IMAGE','rasc2d5d2a-worker:20261009'), 'build':str(REPO_DIR/'api'),
            'labels':{'ras.task':c['project']},'environment':{'PYTHONPATH':'/harness:/srv/api',
                'RAS_REDIS_PORT':'6379'},
            'volumes':[str(harness).replace('\\','/')+':/harness:ro',str(evidence).replace('\\','/')+':/evidence']}
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
            fact=json.loads(command(['docker','inspect','--format','{{json .}}',container],name='inspect-'+container[:12]))
            environment=dict(value.split('=',1) for value in fact['Config']['Env'])
            assert environment['RAS_DB__USER']=='c1_runtime' and environment['RAS_DB__PASSWORD']==c['runtime']
            assert c['password'] not in environment.values() and c['recovery'] not in environment.values()
            # Replace the full transient inspect output immediately with public facts.
            public={'Id':fact['Id'],'Image':fact['Image'],'StartedAt':fact['State']['StartedAt'],
                'runtime_login':environment['RAS_DB__USER'],'operator_credentials':False}
            (evidence/('inspect-'+container[:12]+'.log')).write_text(json.dumps(public),encoding='utf-8')
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            assert all(j['attempt_count']==0 for j in jobs(b))
            time.sleep(.1)
        command(control+['start','redis'],name='redis-start')
        deadline=time.monotonic()+55
        while time.monotonic()<deadline:
            with b.w.runtime.connect() as conn:
                rows=conn.execute(text('SELECT id,state,attempt_count,finished_at,extraction_finished_at FROM ingestion_jobs WHERE id=ANY(:ids)'),{'ids':targets}).all()
            if len(rows)==3 and all(r.state=='awaiting_index' for r in rows):break
            time.sleep(.2)
        assert len(rows)==3 and all(r.state=='awaiting_index' and r.attempt_count==1
            and r.finished_at is None and r.extraction_finished_at is not None for r in rows)
        with b.w.runtime.connect() as conn:
            assert conn.scalar(text('SELECT count(*) FROM index_generations'))==0
            assert conn.scalar(text('SELECT count(*) FROM index_entries'))==0
            assert conn.scalar(text('SELECT count(*) FROM embedding_calls'))==0
            assert conn.scalar(text("SELECT count(*) FROM library_items WHERE content_status<>'metadata_only'"))==0
        facts=[json.loads(p.read_text()) for p in evidence.glob('identity-*.json')]
        assert len(facts)>=2 and all(f=={'login':'c1_runtime','raw_user_write':False,'temp':False} for f in facts)
        payloads=[json.loads(line) for p in evidence.glob('payload-*.jsonl') for line in p.read_text().splitlines()]
        assert {uuid.UUID(p['args'][0]) for p in payloads}==set(targets)
        (evidence/'result.json').write_text(json.dumps({'jobs':[{k:str(getattr(r,k)) for k in r._fields} for r in rows],
            'identities':facts,'override':'scripts/compose.worker-runtime.yml','provider_calls':0}),encoding='utf-8')
    finally:
        if started:
            command(compose+['logs','--no-color','worker','beat'],name='processes')
            command(compose+['stop','worker','beat'],name='stop')
            command(compose+['rm','-f','worker','beat'],name='remove')
        # Keep the isolated broker available for following tests even on smoke failure.
        command(control+['start','redis'],name='redis-final-start')
        env_file.unlink(missing_ok=True)
