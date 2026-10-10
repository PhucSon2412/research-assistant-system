"""Sequential negative controls; external logs, exact restoration even on failure.

Run only when no suite/process is using the source. Every control must fail a
business assertion (not setup/error). The final seal runs after restoration.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

REPO=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--context',type=Path,required=True)
    a=p.parse_args()
    root=a.context.resolve()
    if REPO==root or REPO in root.parents: raise SystemExit('external context required')
    prefix='api/tests/test_c2d5d2b_document_runtime.py::'
    specs=[
        ('role','api/app/ingest/checkpoints.py',[('        runtime.require_runtime(db)','        pass  # negative control')],
            prefix+'test_direct_index_entry_requires_actual_runtime[owner]'),
        ('consent','api/app/ingest/document_index.py',[('        consent(db, job, item, source)','        pass  # negative control')],
            prefix+'test_change_at_io_fences_content_but_settles_cost[hash-ack]'),
        ('owner-status','api/app/ingest/document_index.py',[("        if not db.scalar(text('SELECT ras_auth_status_live(:uid)'), {'uid': item.owner_user_id}):",'        if False:')],
            prefix+'test_change_at_io_fences_content_but_settles_cost[provider-disable]'),
        ('nonce','api/app/ingest/document_accounting.py',[("for k in FIELDS))","for k in FIELDS if k != 'reservation_token'))")],
            prefix+'test_old_nonce_cannot_renew_or_settle_re_reserved_row'),
        ('final-hash','api/app/ingest/document_index.py',[('                if not hashed_after_entry:','                if False:')],
            prefix+'test_completed_reuse_still_hashes_after_final_entry'),
        ('clock','api/app/ingest/document_index.py',[("        if deadline is not None and (at < deadline if claimed else at >= deadline):","        if False:"),
            ('        if fresh[0].lease_until is not None and fresh[0].lease_until <= at:','        if False:')],
            prefix+'test_clock_after_final_sql[equal-publish]'),
        ('commit','api/app/ingest/runtime.py',[("        raise Halt('worker_commit_uncertain') from None","        return  # negative control")],
            prefix+'test_commit_uncertain_never_continues_or_fails[True-reservation]'),
        ('query-lease','api/app/ingest/document_index.py',[("SearchUsage.status == 'reserved', SearchUsage.lease_until > capacity_at", "SearchUsage.status == 'reserved'")],
            prefix+'test_document_capacity_counts_only_live_query_leases[expired]'),
    ]
    results=[]
    code_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip()
    for name,file,replacements,node in specs:
        path=REPO/file
        original=path.read_bytes()
        before=hashlib.sha256(original).hexdigest()
        xml=root/('mutant-'+name+'.xml')
        try:
            s=original.decode('utf-8')
            for old,new in replacements:
                if s.count(old)!=1: raise RuntimeError('mutation target must be unique: '+name)
                s=s.replace(old,new,1)
            path.write_text(s,encoding='utf-8',newline='\n')
            command=[sys.executable,str(REPO/'scripts/c2d5d2b_test_host.py'),'--context',str(root),
                'test',node,'-q','--tb=short','--junitxml='+str(xml)]
            with (root/('mutant-'+name+'.log')).open('w',encoding='utf-8') as log:
                completed=subprocess.run(command,cwd=REPO,stdout=log,stderr=subprocess.STDOUT)
            tree=ET.parse(xml)
            cases=tree.findall('.//testcase')
            failures=tree.findall('.//failure')
            errors=tree.findall('.//error')
            passed=(completed.returncode==1 and len(cases)==1 and len(failures)==1 and not errors
                and 'AssertionError' in ''.join(failures[0].itertext()))
            results.append(dict(name=name,node=node,valid_countercheck=passed,exit=completed.returncode,
                code_sha=code_sha,
                original_sha256=before,mutant_sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        finally:
            path.write_bytes(original)
            assert hashlib.sha256(path.read_bytes()).hexdigest()==before
        (root/'counterchecks.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
        print(name+': '+('assertion caught' if passed else 'NOT VALID'),flush=True)
        if not passed: return 1
    return 0


if __name__=='__main__': raise SystemExit(main())
