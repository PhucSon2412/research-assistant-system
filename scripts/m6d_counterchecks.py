"""Sequential finite mutants before the full inventory; always restore original bytes."""
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
    p.add_argument('--name',default='counterchecks');a=p.parse_args()
    if not a.name or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in a.name):
        raise SystemExit('simple lowercase evidence name required')
    root=a.context.resolve()/a.name;root.mkdir(exist_ok=False)
    if REPO==root or REPO in root.parents:raise SystemExit('external context required')
    tests='api/tests/test_m6d_private_delete.py::'
    cases=[
        ('private-sql-acl','api/app/authz.py',[(b'            LibraryItem.deleted_at.is_(None),\n',b'')],
         tests+'test_owner_atomic_closure_denial_idempotent_and_real_ready'),
        ('late-derived-fence','api/migrations/versions/0052_private_item_deletion.sql',[
            (b"publication_state='revoked' OR deleted_at IS NOT NULL",b"publication_state='revoked'"),
            (b"  OR EXISTS(SELECT 1 FROM private_item_deletions WHERE item_id=target);",b';')],
         tests+'test_deleted_id_version_late_derived_and_key_cannot_reappear'),
        ('known-commit-release','api/app/api/private_delete.py',[
            (b'if failure is not None or outcome.teardown_failed:',b'if failure is not None:')],
         tests+'test_known_commit_release_failure_never_reports_rollback_or_success'),
        ('shared-key-fence','api/migrations/versions/0052_private_item_deletion.sql',[
            (b' IF EXISTS(SELECT 1 FROM source_files f LEFT JOIN library_items i ON i.id=f.item_id',
             b' IF false AND EXISTS(SELECT 1 FROM source_files f LEFT JOIN library_items i ON i.id=f.item_id')],
         tests+'test_multiversion_purge_scrubs_all_and_preserves_shared_key'),
        ('coverage-rehydrate','api/migrations/versions/0052_private_item_deletion.sql',[
            (b'    OR NEW.coverage IS NOT NULL THEN',b'    THEN')],
         tests+'test_deleted_id_version_late_derived_and_key_cannot_reappear'),
        ('final-fsync-authority','api/app/api/private_delete.py',[

            (b'            _current(db,principal,csrf,clock,settings)\n        outcome.commit_started=True',
             b'        outcome.commit_started=True')],
         tests+'test_final_session_expiry_after_every_append_result_rolls_back_then_replay_only_restricts'),
        ('restore-purged-object','api/app/private_cleanup.py',[
            (b'if tomb.purged_at is not None and not sweep:',b'if tomb.purged_at is not None:')],
         tests+'test_restore_old_target_with_current_journal_twice'),
    ]
    results=[]
    for name,file,replacements,test in cases:
        path=REPO/file;original=path.read_bytes();mutant=original
        # Git working trees may use CRLF; match only the intended source fragment.
        for old,new in replacements:
            if old not in mutant and b'\n' in old:old=old.replace(b'\n',b'\r\n')
            if mutant.count(old)!=1:raise SystemExit('countercheck fragment not unique: '+name)
            mutant=mutant.replace(old,new,1)
        xml=root/(name+'.xml')
        try:
            path.write_bytes(mutant)
            with (root/(name+'.log')).open('w',encoding='utf-8') as log:
                r=subprocess.run([sys.executable,str(REPO/'scripts/c3_test_host.py'),'--context',str(a.context),
                    'test',test,'-q','--tb=short','--junitxml='+str(xml)],cwd=REPO,stdout=log,stderr=subprocess.STDOUT)
        finally:
            path.write_bytes(original)
            assert hashlib.sha256(path.read_bytes()).digest()==hashlib.sha256(original).digest()
        cases_xml=ET.parse(xml).findall('.//testcase') if xml.exists() else []
        failed=sum(c.find('failure') is not None for c in cases_xml);errors=sum(c.find('error') is not None for c in cases_xml)
        result=dict(name=name,exit=r.returncode,failures=failed,errors=errors,restored=True,
                    source_sha256=hashlib.sha256(original).hexdigest(),xml=str(xml),caught=r.returncode==1 and failed>0 and errors==0)
        results.append(result);(root/'summary.json').write_text(json.dumps(results,indent=2))
        print(name,'caught='+str(result['caught']),flush=True)
    return 0 if all(r['caught'] for r in results) else 1


if __name__=='__main__':raise SystemExit(main())
