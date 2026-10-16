"""Run existing full restore drills with synthetic credentials and shared C4 images."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import hashlib

REPO=Path(__file__).resolve().parents[1]
DRILLS=('backup_restore','session_revocation_restore','reset_token_restore','review_closure_restore','invitation_restore')


def source_hashes():
    files=[p for folder in ('api/app','api/migrations') for p in (REPO/folder).rglob('*') if p.suffix in ('.py','.sql')]
    files+=list((REPO/'scripts').glob('drill_*.py'))
    files+=[REPO/'scripts'/name for name in ('ras_backup.py','c4_fake_provider.py','c4_isolation.py','c4_full_drills.py')]
    return {p.relative_to(REPO).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def main():
    p=argparse.ArgumentParser();p.add_argument('--context',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--only',choices=DRILLS)
    p.add_argument('--image',required=True);p.add_argument('--web-image',required=True)
    a=p.parse_args();root=a.context.resolve();output=a.output.resolve()
    for path in (root,output):
        if path==REPO or REPO in path.parents:raise SystemExit('external context required')
    c=json.loads((root/'stack.json').read_text(encoding='utf-8'))
    if not c['project'].startswith('rasc4'):raise SystemExit('task context required')
    output.mkdir(parents=True,exist_ok=True)
    env={k:v for k,v in os.environ.items() if not k.startswith(('RAS_','POSTGRES_','MINIO_','OPENROUTER_','REDIS_'))}
    env.update(c['env'])
    env.update(RAS_COMPOSE_ENV_FILE=str(root/'stack.env'),RAS_API_IMAGE=a.image,RAS_WEB_IMAGE=a.web_image,
        RAS_DRILL_PREBUILT_IMAGES='true',
        RAS_DRILL_RUNTIME_USER=c['env']['RAS_RUNTIME_DB_USER'],RAS_DRILL_RUNTIME_PASSWORD=c['env']['RAS_RUNTIME_DB_PASSWORD'],
        PYTHONUTF8='1',PYTHONUNBUFFERED='1',NEXT_TELEMETRY_DISABLED='1')
    results=[]
    for name in ([a.only] if a.only else DRILLS):
        attempt=output/(name+'-'+str(time.time_ns()));attempt.mkdir()
        work=attempt/'work';env['DRILL_WORK_DIR']=str(work)
        started=time.time()
        before=source_hashes()
        image=subprocess.check_output(['docker','image','inspect',a.image,'--format','{{.Id}}'],text=True).strip()
        with (attempt/'output.log').open('w',encoding='utf-8') as log:
            process=subprocess.run([sys.executable,str(REPO/'scripts'/('drill_'+name+'.py')),'--down'],
                cwd=REPO,env=env,stdout=log,stderr=subprocess.STDOUT)
        result={'drill':name,'exit':process.returncode,'seconds':round(time.time()-started,2),'log':str(attempt/'output.log'),
                'image':image,'source_before':before,'source_equal':before==source_hashes(),
                'image_unchanged':image==subprocess.check_output(['docker','image','inspect',a.image,'--format','{{.Id}}'],text=True).strip()}
        results.append(result);(attempt/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        (output/'summary.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
        print(name+' exit='+str(process.returncode),flush=True)
        if process.returncode: return process.returncode
    return 0


if __name__=='__main__':raise SystemExit(main())
