"""One full API inventory, isolated shard contexts, exact JUnit identities and hashes."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
import hashlib
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

REPO=Path(__file__).resolve().parents[1]
HOST=REPO/'scripts/c3_test_host.py'


def sources():
    names=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','api','scripts','web','docker-compose.yml'],cwd=REPO,text=True).splitlines()
    return {n:hashlib.sha256((REPO/n).read_bytes()).hexdigest() for n in sorted(set(names))
        if (REPO/n).is_file() and not any(part.startswith('.env') for part in Path(n).parts)}


def main():
    p=argparse.ArgumentParser();p.add_argument('--context',type=Path,required=True)
    p.add_argument('--jobs',type=int,default=6);p.add_argument('--name',required=True)
    p.add_argument('--image',required=True);p.add_argument('--files',nargs='*')
    a=p.parse_args();ctx=a.context.resolve()
    if ctx==REPO or REPO in ctx.parents or not 1<=a.jobs<=6:raise SystemExit('external context and 1..6 jobs required')
    root=ctx/a.name;root.mkdir(parents=True,exist_ok=False)
    before=sources();(root/'source-before.json').write_text(json.dumps(before,indent=2))
    env=os.environ.copy();env['RAS_TEST_PROCESS_IMAGE']=a.image
    available=Queue()
    for number in range(a.jobs):
        shard=ctx/('shard-'+str(number))
        if not (shard/'credentials.json').exists():
            setup="import sys;sys.path.insert(0,sys.argv.pop(1));import c3_test_host as h;from c4_isolation import free_port;h.free_port=free_port;raise SystemExit(h.main())"
            r=subprocess.run([sys.executable,'-c',setup,str(REPO/'scripts'),'--context',str(shard),'setup'],capture_output=True,text=True)
            (root/('setup-'+str(number)+'.log')).write_text(r.stdout+r.stderr,encoding='utf-8')
            if r.returncode:raise SystemExit('shard setup failed')
            config=json.loads((shard/'compose.json').read_text())
            config['services']['db']['tmpfs']=['/var/lib/postgresql/data']
            (shard/'compose.json').write_text(json.dumps(config))
            project=json.loads((shard/'credentials.json').read_text())['project']
            subprocess.run(['docker','compose','--env-file',str(shard/'empty.env'),'-p',project,'-f',str(shard/'compose.json'),
                'up','-d','--wait','--force-recreate','db'],check=True,capture_output=True)
        available.put(shard)
    def call(shard,name,args):
        argfile=root/(name+'.args.txt');argfile.write_text('\n'.join(args)+'\n',encoding='utf-8')
        with (root/(name+'.log')).open('w',encoding='utf-8') as log:
            return subprocess.run([sys.executable,str(HOST),'--context',str(shard),'test','@'+str(argfile)],
                cwd=REPO,env=env,stdout=log,stderr=subprocess.STDOUT).returncode
    shard=available.get()
    try:code=call(shard,'collection',['api/tests','--collect-only','-q'])
    finally:available.put(shard)
    if code:raise SystemExit('collection failed')
    from c2d7_seal import collected
    nodes=collected(root/'collection.log');(root/'collection.json').write_text(json.dumps(nodes,indent=2))
    parent=json.loads((REPO/'docs/c4-test-inventory.json').read_text(encoding='utf-8'))['api']['nodes']
    missing_parent=sorted(set(parent)-set(nodes))
    if missing_parent:raise SystemExit('C4 identities missing: '+str(len(missing_parent)))
    counts=Counter(n.split('::')[0] for n in nodes)
    files=sorted(counts,key=lambda name:-counts[name])
    if a.files:files=[f for f in files if f in a.files or Path(f).stem in a.files]
    summary={'collection':nodes,'c4_ids_retained':len(parent),'missing_c4':missing_parent,'groups':{},'image':a.image,
             'mode':'full API inventory' if not a.files else 'selected whole-file rerun'}
    def execute(file):
        shard=available.get();name=Path(file).stem;started=time.monotonic()
        try:exit_code=call(shard,name,[file,'-q','--tb=short','-o','faulthandler_timeout=0','--junitxml='+str(root/(name+'.xml'))])
        finally:available.put(shard)
        xml=root/(name+'.xml');states={};raw=[]
        if xml.exists():
            for case in ET.parse(xml).findall('.//testcase'):
                ident='api/'+case.attrib['classname'].replace('.','/')+'.py::'+case.attrib['name'];raw.append(ident)
                state=next((s for s in ('failure','error','skipped') if case.find(s) is not None),'pass')
                states[ident]=state
        return name,dict(exit=exit_code,seconds=round(time.monotonic()-started,2),selected=[n for n in nodes if n.split('::')[0]==file],
            states=states,duplicates=[n for n,c in Counter(raw).items() if c>1],xml=str(xml),context=str(shard))
    heavy=lambda f:any(k in f for k in ('_browser','_proxy_smoke','_process_smoke','test_c3_'))
    for workload,jobs in (([f for f in files if not heavy(f)],a.jobs),([f for f in files if heavy(f)],1)):
        with ThreadPoolExecutor(jobs) as pool:
            for future in as_completed([pool.submit(execute,file) for file in workload]):
                name,result=future.result();summary['groups'][name]=result
                (root/'summary.json').write_text(json.dumps(summary,indent=2))
                print(name,'exit='+str(result['exit']),dict(Counter(result['states'].values())),flush=True)
    after=sources();summary['source_equal']=before==after
    (root/'source-after.json').write_text(json.dumps(after,indent=2))
    observed={n:s for g in summary['groups'].values() for n,s in g['states'].items()}
    selected={n for g in summary['groups'].values() for n in g['selected']}
    summary['counts']=dict(Counter(observed.values()));summary['not_run']=sorted(selected-set(observed))
    summary['unexpected']=sorted(set(observed)-selected)
    (root/'summary.json').write_text(json.dumps(summary,indent=2))
    okay=summary['source_equal'] and not summary['not_run'] and not summary['unexpected'] and all(
        g['exit']==0 and not g['duplicates'] for g in summary['groups'].values())
    print('source_equal='+str(summary['source_equal']),summary['counts'],flush=True)
    return 0 if okay else 1


if __name__=='__main__':raise SystemExit(main())
