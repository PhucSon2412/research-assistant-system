"""M6e finite mutants against business assertions; always restore original bytes."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

REPO=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('--context',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    root=a.out.resolve();root.mkdir(parents=True,exist_ok=False)
    if REPO==root or REPO in root.parents:raise SystemExit('external evidence directory required')
    tests='api/tests/test_m6e_private_duplicate.py::'
    rules='api/app/ingest/duplicates.py'
    cases=[
        ('ready-target',rules,[(b'                   ready))',b'                   false()))'),
                                (b'from sqlalchemy import Select, and_, exists, or_, select',
                                 b'from sqlalchemy import Select, and_, exists, false, or_, select')],
         [tests+'test_new_intent_same_bytes_after_ready_is_duplicate_without_reprocessing[markdown]']),
        ('owner-scope',rules,[(b'               IngestionJob.scope_owner_user_id == owner_user_id,\n',b''),
                              (b'               LibraryItem.owner_user_id == owner_user_id,\n',b'')],
         [tests+'test_controls_other_owner_common_and_different_bytes_stay_independent']),
        ('deletion-decision',rules,[(b'               ~exists().where(PrivateItemDeletion.item_id == LibraryItem.id),\n',b'')],
         [tests+'test_decision_or_tombstone_alone_excludes_otherwise_live_target[private_item_deletion]']),
        ('duplicate-as-target',rules,[(b'or_(and_(IngestionJob.state.in_(IN_FLIGHT), unindexed),',
                                       b"or_(IngestionJob.state == 'duplicate', and_(IngestionJob.state.in_(IN_FLIGHT), unindexed),")],
         [tests+'test_multiple_candidates_stable_earliest_and_duplicate_of_duplicate',
          tests+'test_eligibility_table_by_target_state_binding_and_epoch[duplicate]']),
        ('read-target-lock','api/app/api/routes/library.py',[
            (b'item_ids=lambda data: [data["item"]["id"]] + [j["existing_copy"]["item_id"]\n            for j in data["item"]["jobs"] if j["existing_copy"]])',
             b'item_ids=lambda data: [data["item"]["id"]])')],
         [tests+'test_read_link_holds_target_share_lock_delete_waits']),
    ]
    results=[]
    for name,file,replacements,selected in cases:
        path=REPO/file;original=path.read_bytes();mutant=original
        # Git working trees may use CRLF; match only the intended source fragment.
        for old,new in replacements:
            if old not in mutant and b'\n' in old:old,new=old.replace(b'\n',b'\r\n'),new.replace(b'\n',b'\r\n')
            if mutant.count(old)!=1:raise SystemExit('countercheck fragment not unique: '+name)
            mutant=mutant.replace(old,new,1)
        xml=root/(name+'.xml')
        try:
            path.write_bytes(mutant)
            with (root/(name+'.log')).open('w',encoding='utf-8') as log:
                r=subprocess.run([sys.executable,str(REPO/'scripts/c3_test_host.py'),'--context',str(a.context),
                    'test',*selected,'-q','--tb=short','-p','no:cacheprovider','--junitxml='+str(xml)],
                    cwd=REPO,stdout=log,stderr=subprocess.STDOUT)
        finally:
            path.write_bytes(original)
            assert hashlib.sha256(path.read_bytes()).digest()==hashlib.sha256(original).digest()
        cases_xml=ET.parse(xml).findall('.//testcase') if xml.exists() else []
        failed=sum(c.find('failure') is not None for c in cases_xml);errors=sum(c.find('error') is not None for c in cases_xml)
        result=dict(name=name,file=file,tests=selected,exit=r.returncode,failures=failed,errors=errors,restored=True,
                    source_sha256=hashlib.sha256(original).hexdigest(),xml=str(xml),caught=r.returncode==1 and failed>0 and errors==0)
        results.append(result);(root/'summary.json').write_text(json.dumps(results,indent=2))
        print(name,'caught='+str(result['caught']),flush=True)
    return 0 if all(r['caught'] for r in results) else 1


if __name__=='__main__':raise SystemExit(main())
