"""Linux CLI and Celery/Redis: SIGKILL during second real loopback-fixture HTTP GET.

Mounted sitecustomize scopes cutover and OAI endpoint in TEST ONLY, and denies
egress except isolated db/redis/feed. The production transport/boundary runs
unchanged. No dev Compose, provider, live arXiv or operator credentials.
"""
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import pytest
from sqlalchemy import text

from tests.conftest import REPO_DIR
from tests.resource_fixtures import installation  # noqa: F401
from tests.test_c2d5f2_discovery_runtime import disc  # noqa: F401
from tests.test_discovery import PAGE_ONE, PAGE_TWO

IMAGE = 'rasc2d5f2-worker:20261011'
HARNESS = '''
import os, socket, json, uuid
from pathlib import Path
if os.environ.get('SMOKE_ROLE') in ('cli','worker','sender'):
    allowed = {'127.0.0.1'}
    for host in ('db','redis','feed'):
        allowed.add(socket.gethostbyname(host))
    original = socket.socket.connect
    def connect(self, address):
        if isinstance(address, tuple) and address[0] not in allowed:
            raise OSError('test egress blocked')
        return original(self,address)
    socket.socket.connect = connect
    from app import auth_safety, discovery
    auth_safety.cutover = lambda db: 'ready'
    discovery.OAI_URL = 'http://feed:8080/oai'
    from app.db import get_engine
    from app.ingest import runtime
    from sqlalchemy.orm import Session
    from sqlalchemy import text
    db = Session(get_engine())
    def proof(_):
        runtime.require_runtime(db)
        return dict(db.execute(text("SELECT current_user AS login, has_table_privilege('users','UPDATE') AS raw_user_write,"
            "has_table_privilege('revocation_journal_records','INSERT') AS journal_write")).mappings().one())
    fact = runtime.short(db, proof)
    Path('/evidence/identity-' + os.environ['SMOKE_ROLE'] + '-' + uuid.uuid4().hex + '.json').write_text(json.dumps(fact))
    # Record every fetch admission and prove that no Session is held by it.
    real = discovery.fetch_page
    def fetch(*args):
        with Path('/evidence/pools.txt').open('a') as f:
            f.write(str(get_engine().pool.checkedout()) + '\\n')
        return real(*args)
    discovery.fetch_page = fetch
'''
SERVER = '''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from pathlib import Path
import time, json, threading
lock = threading.Lock()
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_): pass
    def do_GET(self):
        second = 'resumptionToken' in parse_qs(urlsplit(self.path).query)
        with lock, Path('/evidence/requests.jsonl').open('a') as f:
            f.write(json.dumps({'second':second,'at':time.monotonic()})+'\\n')
        if second and not Path('/evidence/resume').exists():
            Path('/evidence/in-second-fetch').write_text('1')
            while not Path('/evidence/resume').exists(): time.sleep(.05)
        data = Path('/evidence/two.xml' if second else '/evidence/one.xml').read_bytes()
        try:
            self.send_response(200); self.send_header('Content-Length',str(len(data))); self.end_headers()
            self.wfile.write(data)
        except (OSError,ConnectionError): pass
ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()
'''


def wait(predicate, label, seconds=45):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.1)
    raise AssertionError(label)


@pytest.mark.parametrize('entry', ['cli', 'celery'])
def test_linux_kill_fetch_resume_idempotent_runtime(disc, entry):
    w = disc
    context = Path(os.environ['RAS_C2D5F2_CONTEXT'])
    c = json.loads((context / 'credentials.json').read_text(encoding='utf-8-sig'))
    evidence = context / ('smoke-' + entry + '-' + uuid.uuid4().hex)
    harness = evidence / 'harness'; harness.mkdir(parents=True)
    (harness / 'sitecustomize.py').write_text(HARNESS, encoding='utf-8')
    (harness / 'server.py').write_text(SERVER, encoding='utf-8')
    (evidence / 'one.xml').write_bytes(PAGE_ONE)
    (evidence / 'two.xml').write_bytes(PAGE_TWO)
    env = {'RAS_DB__HOST':'db', 'RAS_DB__PORT':'5432', 'RAS_DB__USER':'c1_runtime',
           'RAS_DB__PASSWORD':c['runtime'], 'RAS_DB__NAME':w.runtime.url.database,
           'RAS_REDIS_HOST':'redis', 'RAS_REDIS_PORT':'6379', 'RAS_REDIS_PASSWORD':c['redis'],
           'RAS_RECOVERY__JOURNAL_PATH':'/journal/'+w.path.name,
           'RAS_EMBEDDING__API_KEY':'', 'RAS_EMBEDDING__PRICING_VERIFIED':'false',
           'RAS_DISCOVERY__TOPICS':'["cs.AI"]', 'RAS_DISCOVERY__MAX_PAGES_PER_TOPIC':'2',
           'RAS_DISCOVERY__HTTP_DEADLINE_SECONDS':'4', 'RAS_DISCOVERY__TIMEOUT_SECONDS':'3',
           'RAS_DISCOVERY__LEASE_SECONDS':'8', 'RAS_DISCOVERY__LEASE_MARGIN_SECONDS':'1',
           'SMOKE_ROLE':'cli' if entry=='cli' else 'worker', 'PYTHONPATH':'/harness:/srv/api'}
    env_file = evidence / 'runtime.env'
    env_file.write_text('\n'.join(k+'='+v for k,v in env.items())+'\n', encoding='utf-8')
    prefix = c['project'] + '-' + uuid.uuid4().hex[:8]
    names = []
    def command(label, args, check=True, timeout=60):
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', timeout=timeout)
        (evidence / (label+'.log')).write_text(result.stdout+result.stderr, encoding='utf-8')
        assert not check or result.returncode == 0, label + ': see external evidence'
        return result
    base = ['docker','run','--label','ras.task='+c['project'],'--network',c['project']+'_default',
            '--env-file',str(env_file), '-v',str(REPO_DIR/'api'/'app')+':/srv/api/app:ro',
            '-v',str(harness)+':/harness:ro','-v',str(evidence)+':/evidence',
            '-v',str(w.path.parent)+':/journal']
    def state():
        with w.engine.connect() as conn:
            return tuple(conn.execute(text('SELECT pages_seen,records_seen,resumption_token FROM discovery_sources')).one())
    def send(label):
        return command(label, base+['--rm','-e','SMOKE_ROLE=sender',IMAGE,'python','-c',
            "from app.ingest.celery_app import celery_app; celery_app.send_task('ras.discovery.sync')"])
    try:
        feed = prefix+'-feed'; names.append(feed)
        command('start-feed',base+['-d','--name',feed,'--network-alias','feed','-e','SMOKE_ROLE=feed',IMAGE,'python','/harness/server.py'])
        if entry == 'cli':
            args = [IMAGE,'python','-m','app.cli.sync_arxiv']
        else:
            args = [IMAGE,'celery','-A','app.ingest.celery_app:celery_app','worker','--pool=solo','--loglevel=WARNING','--concurrency=1']
        worker = prefix+'-worker'; names.append(worker)
        command('start',base+['-d','--name',worker]+args)
        if entry == 'celery':
            send('send-initial')
        wait(lambda:(evidence/'in-second-fetch').exists(),'second fetch not reached')
        assert state() == (1,1,'page-two')
        command('kill',['docker','kill','--signal','KILL',worker])
        killed = command('exit',['docker','inspect','--format','{{.State.ExitCode}} {{.State.Running}}',worker])
        assert killed.stdout.split() == ['137','false']
        assert state() == (1,1,'page-two')
        (evidence/'resume').write_text('1')
        # Wait for actual lease expiry + conservative pacing; no artificial DB clock advance.
        time.sleep(9)
        if entry == 'cli':
            resumed = command('resume',base+['--rm']+args)
            assert 'pages=1' in resumed.stdout and 'records=1' in resumed.stdout
        else:
            restarted = prefix+'-restart'; names.append(restarted)
            command('restart-worker',base+['-d','--name',restarted]+args)
            send('send-resume')
            wait(lambda:state() == (2,2,None),'Celery resume failed')
        assert state() == (2,2,None)
        if entry == 'cli':
            again = command('again',base+['--rm']+args)
            assert 'requests=0' in again.stdout
        else:
            send('send-again'); time.sleep(2)
        assert state() == (2,2,None)
        with w.engine.connect() as conn:
            assert conn.scalar(text('SELECT count(*) FROM discovery_papers')) == 2
            assert conn.scalar(text('SELECT count(*) FROM source_files')) == 0
        starts = [json.loads(line) for line in (evidence/'requests.jsonl').read_text().splitlines()]
        assert len(starts) == 3 and [s['second'] for s in starts] == [False,True,True]
        assert all(b['at']-a['at'] >= 3 for a,b in zip(starts,starts[1:]))
        assert set((evidence/'pools.txt').read_text().splitlines()) == {'0'}
        facts = [json.loads(p.read_text()) for p in evidence.glob('identity-*.json')]
        assert facts and all(f == {'login':'c1_runtime','raw_user_write':False,'journal_write':False} for f in facts)
        (evidence/'result.json').write_text(json.dumps({'entry':entry,'kill_exit':137,'state':state(),
            'requests':starts,'identity':facts,'image':IMAGE}), encoding='utf-8')
    finally:
        for name in reversed(names):
            command('logs-'+name,['docker','logs',name], check=False)
            command('remove-'+name,['docker','rm','-f',name], check=False)
        env_file.unlink(missing_ok=True)
