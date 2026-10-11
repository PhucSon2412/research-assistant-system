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
    names=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','api','scripts','web'],cwd=REPO,text=True).splitlines()
    return {n:hashlib.sha256((REPO/n).read_bytes()).hexdigest() for n in sorted(set(names))
        if (REPO/n).is_file() and not any(part.startswith('.env') for part in Path(n).parts)}


def collected(path):
    nodes=set()
    for line in path.read_text(encoding='utf-8').splitlines():
        node=line.strip()
        if '::' in node:  # only the path is OS-specific; parametrize IDs keep their own backslash escapes
            head,tail=node.split('::',1)
            node=head.replace('\\','/')+'::'+tail
        if node.startswith('tests/'): node='api/'+node
        if node.startswith('api/tests/') and '::' in node: nodes.add(node)
    return sorted(nodes)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--context',type=Path,required=True)
    p.add_argument('--name',required=True)
    p.add_argument('--skip',action='append',default=[],help='group=reason; kept as historical, listed in summary')
    a=p.parse_args()
    root=a.context.resolve()/a.name
    root.mkdir()  # never overwrite an earlier attempt
    host=[sys.executable,str(REPO/'scripts/c2d6_test_host.py'),'--context',str(a.context),'test']
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
    # Finite selection covering the changed runtime/transport and its lock graph.
    T='api/tests/test_'
    groups={
        'browser':[T+'c2d6_browser.py'],
        'runtime':[T+'c2d6_search_runtime.py'],
        'search':[T+'search.py',T+'keyword_query.py'],
        'common':[T+'common_search.py'],
        'accounting':[T+'c2d5d2b_document_runtime.py'],
        'schema':[T+'migrations.py',T+'config.py'],
        'rag':[T+'rag.py::'+n for n in (
            'test_private_paper_citation_and_acl','test_hybrid_rag_reuses_private_vector_gate',
            'test_collection_and_all_scope_multi_paper_citations_and_acl',
            'test_provider_errors_and_reservation','test_generation_reservation_concurrent',
            'test_generation_user_request_and_budget_limits')]+[T+'common_rag.py::'+n for n in (
            'test_every_account_asks_the_permitted_copy_and_never_anothers_private',
            'test_generation_on_common_evidence_is_charged_to_the_asking_user',
            'test_searchable_common_text_without_provider_rights_is_withheld')],
    }
    skipped=dict(v.split('=',1) for v in a.skip)
    for name in skipped: groups.pop(name)
    summary={'code_sha':subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip(),
        'all_collected':all_nodes,'groups':{},'unique':[],'skipped_groups':skipped}
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
        # Exact identity: executed == selected == a subset of the full collection (no rename/duplicate/missing).
        result['selection_matches']=sorted(actual)==expected and len(actual)==len(set(actual))
        result['selection_in_collection']=set(expected)<=set(all_nodes)
        return name,result
    # Each pytest process owns disposable DBs, buckets, journal/tmp paths and
    # settings. Grant revocations in this selection are per test database. Bound host
    # concurrency to three; the Linux CLI/worker smoke never overlaps another group.
    waves=[[n for n in w if n in groups] for w in [['browser'],['runtime','search','schema'],['common','accounting','rag']]]
    waves=[w for w in waves if w]
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
        if any(g['exit'] or not g['selection_matches'] or not g['selection_in_collection'] for g in summary['groups'].values()): break
    after=sources()
    (root/'source-after.json').write_text(json.dumps(after,indent=2),encoding='utf-8')
    summary['source_equal']=before==after
    summary['not_run']=sorted(set(all_nodes)-set(summary['unique']))
    (root/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    if before!=after: raise SystemExit('source changed during seal')
    return int(any(g['exit'] or not g['selection_matches'] or not g['selection_in_collection'] for g in summary['groups'].values())
               or set(summary['groups'])!=set(groups))


if __name__=='__main__': raise SystemExit(main())
