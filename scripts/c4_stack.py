"""Synthetic C4 Compose context outside the repo. Never reads .env or operates dev."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from c4_isolation import free_port, write_ports_override

REPO = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--context', required=True, type=Path)
    p.add_argument('action', choices=['setup', 'compose', 'evaluation', 'fake-off', 'fake-on', 'cleanup'])
    p.add_argument('args', nargs=argparse.REMAINDER)
    a = p.parse_args()
    root = a.context.resolve()
    if root == REPO or REPO in root.parents:
        raise SystemExit('external context required')
    if a.action == 'setup':
        root.mkdir(parents=True, exist_ok=True)
        if (root / 'stack.env').exists(): raise SystemExit('context already exists')
        project = 'rasc4' + secrets.token_hex(4)
        env = dict(POSTGRES_USER='c4_migrator', POSTGRES_DB='c4_installation',
                   POSTGRES_PASSWORD=secrets.token_hex(24), RAS_RUNTIME_DB_USER='c4_runtime',
                   RAS_RUNTIME_DB_PASSWORD=secrets.token_hex(24), RAS_RECOVERY_DB_USER='c4_recovery',
                   RAS_RECOVERY_DB_PASSWORD=secrets.token_hex(24), RAS_DEFINER_DB_ROLE='c4_definer',
                   MINIO_ROOT_USER='c4_minio', MINIO_ROOT_PASSWORD=secrets.token_hex(24),
                   REDIS_PASSWORD=secrets.token_hex(24), OPENROUTER_API_KEY='', RAS_DISCOVERY__ENABLED='false',
                   RAS_API_IMAGE=project+'-api:local', RAS_JOURNAL_SOURCE=(root/'journal').as_posix())
        for key in ('POSTGRES_HOST_PORT','API_HOST_PORT','WEB_HOST_PORT','MINIO_API_HOST_PORT','MINIO_CONSOLE_HOST_PORT'):
            env[key] = str(free_port())
        (root/'journal').mkdir()
        (root/'stack.env').write_text(''.join(f'{k}={v}\n' for k,v in env.items()), encoding='utf-8')
        (root/'stack.json').write_text(json.dumps({'project':project,'env':env}),encoding='utf-8')
        # Network has no outbound route, before any fake pricing/key is supplied.
        # Docker Desktop internal networks suppress their own published ports.
        # A fixed TCP relay exposes only our five services; app containers have
        # no connection to the externally routed network.
        relay = """const net=require('net');
for (const [port,host,target] of [[8000,'api',8000],[3000,'web',3000],[5432,'db',5432],[9000,'minio',9000],[9001,'minio',9001]]) {
 net.createServer(s=>{ s.setNoDelay(true); const t=net.connect(target,host);t.setNoDelay(true); s.pipe(t);t.pipe(s);
 s.on('error',()=>t.destroy());t.on('error',()=>s.destroy());s.on('close',()=>t.destroy());t.on('close',()=>s.destroy()); }).listen(port,'0.0.0.0');
}
"""
        (root/'relay.cjs').write_text(relay,encoding='utf-8')
        override = {'networks':{'default':{'internal':True},'exposure':{}}, 'services':{
            'web':{'image':project+'-web:local'},
            'exposure':{'image':'node:24-alpine','command':['node','/relay.cjs'],
                'volumes':[(root/'relay.cjs').as_posix()+':/relay.cjs:ro'],
                'networks':['default','exposure'],
                'ports':[f"127.0.0.1:{env[key]}:{port}" for key,port in (
                    ('POSTGRES_HOST_PORT',5432),('API_HOST_PORT',8000),('WEB_HOST_PORT',3000),
                    ('MINIO_API_HOST_PORT',9000),('MINIO_CONSOLE_HOST_PORT',9001))]}}}
        (root/'override.json').write_text(json.dumps(override),encoding='utf-8')
        write_ports_override(root)
        print('created isolated context: '+project)
        return 0
    c = json.loads((root/'stack.json').read_text(encoding='utf-8'))
    if not c['project'].startswith('rasc4') or c['project']=='ras': raise SystemExit('task project required')
    if a.action in ('fake-off','fake-on'):
        fixture = root/'provider-fixture'
        fixture.mkdir(exist_ok=True)
        shutil.copyfile(REPO/'scripts/c4_fake_provider.py',fixture/'sitecustomize.py')
        config = json.loads((root/'override.json').read_text(encoding='utf-8'))
        for service in ('api','worker','beat'):
            settings = config['services'].setdefault(service,{})
            settings['volumes']=[fixture.as_posix()+':/c4-fixture']
            settings['environment']={'PYTHONPATH':'/c4-fixture:/srv/api',
                'C4_FAKE_CALL_LOG':'/c4-fixture/calls.jsonl',
                'RAS_EMBEDDING__API_KEY':'c4-synthetic-controlled-provider',
                **{'RAS_'+task+'__PRICING_VERIFIED':str(a.action=='fake-on').lower()
                   for task in ('EMBEDDING','RERANK','GENERATION')}}
        (root/'override.json').write_text(json.dumps(config),encoding='utf-8')
        print('configured controlled provider transport; fake pricing='+str(a.action=='fake-on'))
        return 0
    env = {k:v for k,v in os.environ.items() if not k.startswith(('RAS_', 'OPENROUTER_', 'POSTGRES_', 'MINIO_', 'REDIS_'))}
    env.update(c['env'])
    env.update(PYTHONUTF8='1', PYTHONPATH=str(REPO/'api'), NEXT_TELEMETRY_DISABLED='1')
    port_override=['-f',str(root/'ports.yaml')] if (root/'ports.yaml').exists() else []
    if a.action=='cleanup':
        ids=subprocess.check_output(['docker','ps','-aq','--filter','label=com.docker.compose.project='+c['project']],text=True).split()
        for identifier in ids:
            observed=subprocess.check_output(['docker','inspect',identifier,'--format','{{index .Config.Labels "com.docker.compose.project"}}'],text=True).strip()
            if observed!=c['project']:raise SystemExit('container ownership changed')
        volumes=subprocess.check_output(['docker','volume','ls','-q','--filter','label=com.docker.compose.project='+c['project']],text=True).split()
        for volume in volumes:
            users=subprocess.check_output(['docker','ps','-aq','--filter','volume='+volume],text=True).split()
            if not set(users)<=set(ids):raise SystemExit('volume has a foreign consumer')
        return subprocess.call(['docker','compose','--env-file',str(root/'stack.env'),'-p',c['project'],
            '-f',str(REPO/'docker-compose.yml'),'-f',str(root/'override.json'),*port_override,'down','-v'],cwd=REPO,env=env)
    if a.action == 'compose':
        return subprocess.call(['docker','compose','--env-file',str(root/'stack.env'),'-p',c['project'],
            '-f',str(REPO/'docker-compose.yml'),'-f',str(root/'override.json'),*port_override,*a.args],cwd=REPO,env=env)
    e = c['env']
    evaluation = {'db_host':'127.0.0.1','db_port':int(e['POSTGRES_HOST_PORT']), 'db_user':e['POSTGRES_USER'],
        'db_password':e['POSTGRES_PASSWORD'],'db_name':e['POSTGRES_DB'],
        'storage_endpoint':'http://127.0.0.1:'+e['MINIO_API_HOST_PORT'], 'storage_access_key':e['MINIO_ROOT_USER'],
        'storage_secret_key':e['MINIO_ROOT_PASSWORD'],'journal_directory':str(root/'evaluation-journals')}
    (root/'evaluation.json').write_text(json.dumps(evaluation),encoding='utf-8')
    return subprocess.call([sys.executable,'-m','app.evaluation','run','--context',str(root/'evaluation.json'),
        '--output',str(root/'evaluation-output'),*a.args],cwd=REPO,env=env)


if __name__ == '__main__':
    raise SystemExit(main())
