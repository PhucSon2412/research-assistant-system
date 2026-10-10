"""Run a finite risk selection with exact collection/JUnit and source manifests."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

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
    host=[sys.executable,str(REPO/'scripts/c2d5d2b_test_host.py'),'--context',str(a.context),'test']
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
    old=json.loads((REPO/'docs/c2d5d2a-test-inventory.json').read_text(encoding='utf-8'))
    # Historical inventory supplies names only. Every selected node is run anew.
    groups={
        'runtime':[f'api/tests/test_c2d5d2b_{s}.py' for s in ('document_runtime','common_runtime','process_smoke','schema')],
        'extraction':[f'api/tests/test_{s}.py' for s in ('c2d5d2a_worker_runtime','c2d5d2a_common_extraction','c2d5d2a_process_smoke','ingest_worker','pdf_ingest')],
        'index-shared':[f'api/tests/test_{s}.py' for s in ('private_index','common_index','search','embedding_transport')],
        'request':['api/tests/test_c2d5d1_common_index_request.py'],
        'revoke':['api/tests/test_c2d5c2_common_revoke.py'],
        'cleanup':['api/tests/test_common_cleanup.py'],
        'schema':['api/tests/test_migrations.py::test_orm_models_match_migrated_schema',
            'api/tests/test_migrations.py::test_empty_database_upgrades_to_head',
            'api/tests/test_migrations.py::test_0024_removes_derived_rows_of_copies_purged_under_0023_and_fences_writes'],
    }
    # Recovery/upload selection is explicitly listed from the predecessor's
    # selection artifact (paths in its groups), resolved below without pass reuse.
    for name in ('recovery','upload'):
        prior=old['final']['groups'].get(name)
        if prior:
            args=prior.get('selected')
            if isinstance(args,list) and args and all('::' in v for v in args): groups[name]=args
    # Keep the gate/journal/restore regression finite even if old manifest embeds
    # only hashes instead of node lists.
    if 'recovery' not in groups:
        groups['recovery']=['api/tests/test_recovery.py']
    if 'upload' not in groups:
        groups['upload']=['api/tests/test_uploads.py']
    summary={'code_sha':subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip(),
        'all_collected':all_nodes,'groups':{},'unique':[]}
    for name,args in groups.items():
        code,log=run(name+'-collection',args+['--collect-only','-q'])
        if code: raise SystemExit('selection failed: '+name)
        expected=collected(log)
        (root/(name+'-selection.json')).write_text(json.dumps(expected,indent=2),encoding='utf-8')
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
        summary['groups'][name]=result
        summary['unique']=sorted(set(summary['unique'])|set(actual))
        (root/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
        print(name+': '+str(result['pass_count'])+' passed; '+str(result['failures'])+' failures; '+str(result['errors'])+' errors',flush=True)
        if code or not result['selection_matches']: break
    after=sources()
    (root/'source-after.json').write_text(json.dumps(after,indent=2),encoding='utf-8')
    summary['source_equal']=before==after
    summary['not_run']=sorted(set(all_nodes)-set(summary['unique']))
    (root/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    if before!=after: raise SystemExit('source changed during seal')
    return int(any(g['exit'] or not g['selection_matches'] for g in summary['groups'].values()))


if __name__=='__main__': raise SystemExit(main())
