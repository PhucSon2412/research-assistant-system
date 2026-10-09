"""Sequential, reversible counterchecks. Raw weak/setup runs are never overwritten."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
WORKER='api/app/ingest/worker.py'
CHECKPOINT='api/app/ingest/checkpoints.py'
RUNTIME='api/app/ingest/runtime.py'
TEST='api/tests/test_c2d5d2a_worker_runtime.py::'

MUTANTS=[
 ('runtime',RUNTIME,"if forbidden is not False:","if False:",TEST+'test_wrong_runtime_role_no_fallback[owner]'),
 ('final-gate',CHECKPOINT,'auth_safety.require_final(db, writer=True)','None',TEST+'test_gate_closes_after_dml_before_final_boundary'),
 ('lease-clock',CHECKPOINT,'now = w.utcnow()  # after the LAST SQL predicate and any journal wait',
  'now = rows[0].updated_at  # mutant stale clock',TEST+'test_fresh_lease_clock_after_final_sql[equal-begin]'),
 ('post-io-cancel',WORKER,'if job.cancel_requested or item.rights_epoch != job.rights_epoch_at_start:',
  'if False:',TEST+'test_change_during_unlocked_io_discards_result[cancel-parser]'),
 ('post-io-binding',CHECKPOINT,"if proof is None or proof.binding != expected:",
  'if proof is None:',TEST+'test_change_during_unlocked_io_discards_result[binding-parser]'),
 ('sha256',WORKER,'or hashlib.sha256(raw).hexdigest() != source.checksum_sha256',
  'or False',TEST+'test_same_size_overwrite_hash_fences'),
 ('uncertain',RUNTIME,"raise Halt('worker_commit_uncertain') from None",'return',
  TEST+'test_uncertain_commit_stops_without_failure_or_following_io[True-claim]'),
 ('item-job',CHECKPOINT,'    if candidate.origin == \'upload\' and candidate.scope_owner_user_id is not None:',
  "    db.scalar(select(IngestionJob).where(IngestionJob.id == job_id).with_for_update())\n    if candidate.origin == 'upload' and candidate.scope_owner_user_id is not None:",
  'api/tests/test_c2d5d2a_common_extraction.py::test_claim_revoke_order_business_result_no_deadlock[revoke]'),
]


def main():
    p=argparse.ArgumentParser();p.add_argument('--context',type=Path,required=True)
    p.add_argument('--only',choices=[m[0] for m in MUTANTS])
    p.add_argument('--prefix',default='mutant',choices=['mutant','strong'])
    a=p.parse_args()
    report=[]
    for name,path,old,new,node in MUTANTS:
        if a.only and name!=a.only:continue
        file=ROOT/path;original=file.read_bytes();source=original.decode('utf-8').replace('\r\n','\n')
        if source.count(old)!=(2 if name=='sha256' else 1):raise SystemExit('nonunique mutant anchor: '+name)
        changed=source.replace(old,new,1)
        if name=='item-job':
            changed=changed.replace('.with_for_update(skip_locked=skip).execution_options(populate_existing=True))',
                '.with_for_update().execution_options(populate_existing=True))',1)
        xml=a.context/(a.prefix+'-'+name+'.xml');log=a.context/(a.prefix+'-'+name+'.log')
        if xml.exists() or log.exists():raise SystemExit('refusing to overwrite raw mutant evidence')
        try:
            file.write_bytes(changed.encode('utf-8'))
            with log.open('w',encoding='utf-8') as out:
                proc=subprocess.run([sys.executable,str(ROOT/'scripts/c2d5d2a_test_host.py'),'--context',str(a.context),
                    'test',node,'-q','--junitxml='+str(xml)],cwd=ROOT,stdout=out,stderr=subprocess.STDOUT)
            tree=ET.parse(xml);failures=tree.findall('.//failure');errors=tree.findall('.//error')
            report.append(dict(name=name,node=node,exit=proc.returncode,failures=len(failures),errors=len(errors),
                caught=proc.returncode==1 and bool(failures) and not errors,
                original_sha256=hashlib.sha256(original).hexdigest(),mutant_sha256=hashlib.sha256(changed.encode()).hexdigest(),
                log=str(log),junit=str(xml)))
        finally:
            file.write_bytes(original)
            assert file.read_bytes()==original
        print(name+': '+json.dumps(report[-1]),flush=True)
    (a.context/('counterchecks'+('-'+a.prefix if a.prefix!='mutant' else '')+'.json')).write_text(json.dumps(report,indent=2),encoding='utf-8')
    return 0 if all(r['caught'] for r in report) else 1


if __name__=='__main__':raise SystemExit(main())
