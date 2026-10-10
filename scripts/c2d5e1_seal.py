"""Run a finite risk selection with exact collection/JUnit and source manifests."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO=Path(__file__).resolve().parents[1]


def sources():
    names=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','api','scripts'],cwd=REPO,text=True).splitlines()
    return {n:hashlib.sha256((REPO/n).read_bytes()).hexdigest() for n in sorted(set(names))
        if (REPO/n).is_file() and not any(part.startswith('.env') for part in Path(n).parts)}


def collected(path):
    nodes=set()
    for line in path.read_text(encoding='utf-8').splitlines():
        node=line.strip().replace('\\','/')
        if node.startswith('tests/'): node='api/'+node
        if node.startswith('api/tests/') and '::' in node: nodes.add(node)
    return sorted(nodes)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--context',type=Path,required=True)
    p.add_argument('--name',required=True)
    a=p.parse_args()
    root=a.context.resolve()/a.name
    root.mkdir()  # never overwrite an earlier attempt
    host=[sys.executable,str(REPO/'scripts/c2d5e1_test_host.py'),'--context',str(a.context),'test']
    def run(name,args):
        path=root/(name+'.log')
        with path.open('w',encoding='utf-8') as log:
            code=subprocess.run(host+args,cwd=REPO,stdout=log,stderr=subprocess.STDOUT).returncode
        return code,path
    before=sources()
    (root/'source-before.json').write_text(json.dumps(before,indent=2),encoding='utf-8')
    code,log=run('all-collection',['api','--collect-only','-q'])
    if code: raise SystemExit('collection failed')
    all_nodes=collected(log)
    # Finite risk selection by call graph; every node runs anew, no historic pass reuse.
    T='api/tests/test_'
    groups={
        'cleanup-runtime':[T+'c2d5e1_cleanup_runtime.py'],
        'cleanup-legacy':[T+'common_cleanup.py',T+'publication.py'],
        'recovery':[T+'recovery.py',T+'backup.py'],
        'revoke':[T+'c2d5c2_common_revoke.py',T+'c2d5c2_migration.py'],
        'index':[T+s+'.py' for s in ('c2d5d2b_document_runtime','c2d5d2b_common_runtime','c2d5d2b_schema',
            'c2d5d2a_common_extraction','c2d5d2a_worker_runtime','common_index')],
        'upload-approval':[T+s+'.py' for s in ('uploads','c2d4b_upload_original','c2d5b_review_approval',
            'c2d5b_migration','review_approval')],
        'request-publish':[T+'c2d5d1_common_index_request.py',T+'c2d5c1_common_publish.py'],
        'schema':[T+s+'.py' for s in ('migrations','c2d5e1_migration','c2d5a_migration','c2c2_migration','auth_safety_foundation')],
        'smoke':[T+'c2d5e1_process_smoke.py'],
    }
    summary={'code_sha':subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip(),
        'all_collected':all_nodes,'groups':{},'unique':[]}
    selections={}
    for name,args in groups.items():
        code,log=run(name+'-collection',args+['--collect-only','-q'])
        if code: raise SystemExit('selection failed: '+name)
        expected=collected(log)
        selections[name]=expected
        (root/(name+'-selection.json')).write_text(json.dumps(expected,indent=2),encoding='utf-8')
    def execute(name):
        args,expected=groups[name],selections[name]
        xml=root/(name+'.xml')
        command=args+['-q','--tb=short','-o','faulthandler_timeout=60','--junitxml='+str(xml)]
        code,log=run(name,command)
        tree=ET.parse(xml)
        cases=tree.findall('.//testcase')
        actual=[v.attrib['classname'].replace('.','/')+'.py::'+v.attrib['name'] for v in cases]
        actual=['api/'+v if v.startswith('tests/') else v for v in actual]
        result=dict(args=args,selection=expected,executed=actual,exit=code,pass_count=sum(
            not any(v.find(k) is not None for k in ('failure','error','skipped')) for v in cases),
            failures=len(tree.findall('.//failure')),errors=len(tree.findall('.//error')),skips=len(tree.findall('.//skipped')))
        result['selection_matches']=sorted(actual)==expected and len(actual)==len(set(actual))
        return name,result
    # Each pytest process owns disposable DBs, buckets, journal/tmp paths and
    # settings. No role-attribute mutations occur in this selection. Bound host
    # concurrency to three; Linux worker/beat smoke groups NEVER overlap.
    waves=[['smoke'],['cleanup-runtime','cleanup-legacy','revoke'],['index','upload-approval','request-publish'],['schema','recovery']]
    summary['execution_waves']=waves
    for wave in waves:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures=[pool.submit(execute,name) for name in wave]
            for future in as_completed(futures):
                name,result=future.result()
                summary['groups'][name]=result
                summary['unique']=sorted(set(summary['unique'])|set(result['executed']))
                (root/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
                print(name+': '+str(result['pass_count'])+' passed; '+str(result['failures'])+' failures; '+str(result['errors'])+' errors',flush=True)
        if any(g['exit'] or not g['selection_matches'] for g in summary['groups'].values()): break
    after=sources()
    (root/'source-after.json').write_text(json.dumps(after,indent=2),encoding='utf-8')
    summary['source_equal']=before==after
    summary['not_run']=sorted(set(all_nodes)-set(summary['unique']))
    (root/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    if before!=after: raise SystemExit('source changed during seal')
    return int(any(g['exit'] or not g['selection_matches'] for g in summary['groups'].values()))


if __name__=='__main__': raise SystemExit(main())
