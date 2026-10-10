"""Sequential negative controls for C2d-5e1; external logs, exact restoration even on failure.

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

REPO = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--context', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    root, out = a.context.resolve(), a.out.resolve()
    for path in (root, out):
        if REPO == path or REPO in path.parents:
            raise SystemExit('external context required')
    out.mkdir(parents=True)  # never overwrite an earlier run
    t = 'api/tests/test_c2d5e1_cleanup_runtime.py::'
    cleanup, sql = 'api/app/ingest/cleanup.py', 'api/migrations/versions/0044_object_delete_fences.sql'
    specs = [
        ('role', cleanup, [('        runtime.require_runtime(db)\n        try:\n            granted',
                            '        try:\n            granted')],
         t + 'test_entry_requires_actual_runtime_login[owner]'),
        ('final-gate', cleanup, [("        _final_gate(db, writer=True)\n        with db.no_autoflush:",
                                  "        with db.no_autoflush:")],
         t + 'test_gate_closing_inside_finalize_refuses_commit[pending]'),
        ('insert-fence', sql, [("  RAISE EXCEPTION 'object key is fenced for deletion' USING ERRCODE='check_violation'; END IF;",
                                "  NULL; END IF;")],
         t + 'test_prepare_first_refuses_waiting_insert_without_cycle[False]'),
        ('ref-protection', sql, [("  RETURN 'shared_kept';\n END IF;", "  NULL;\n END IF;")],
         t + 'test_protected_reference_keeps_object_without_fence[private]'),
        ('nonce-accounting', 'api/app/publication.py',
         [('.values(vector=None, chunk_hash=None, content_removed_at=now))',
           ".values(vector=None, chunk_hash=None, content_removed_at=now, status='rejected', lease_until=None))")],
         t + 'test_late_reap_and_settle_after_cleanup_keep_ceiling_once[settle]'),
        ('final-postcondition', cleanup, [("            if proof.kind == 'delete' and (_fence(db, proof.key) is None or _live(db, proof.key)):\n"
                                           "                raise runtime.Halt('cleanup_postcondition_failed')\n", ''),
                                          ("            if _live(db, proof.key):\n                raise runtime.Halt('cleanup_reference_conflict')\n"
                                           "        elif proof.kind == 'shared_kept':",
                                           "            pass\n        elif proof.kind == 'shared_kept':")],
         t + 'test_final_reference_conflict_after_delete_is_not_purged'),
        ('uncertain', 'api/app/ingest/runtime.py', [("        raise Halt('worker_commit_uncertain') from None",
                                                     "        return  # negative control")],
         t + 'test_final_commit_uncertain_stops_batch_without_inference[False]'),
        ('release', 'api/app/ingest/runtime.py', [("        raise Halt('worker_release_unavailable') from None",
                                                   "        pass  # negative control")],
         t + 'test_release_fault_after_known_commit_keeps_mutation_and_stops[prepare]'),
        ('operator-catchall', 'api/app/publication.py',
         [('        proven = None\n    released = True', '        proven = False  # negative control\n    released = True'),
          ('    if proven is not True:\n        raise CleanupError("operator_cleanup_requires_operator")\n', '')],
         t + 'test_operator_refuses_misprovisioned_runtime_with_real_pending_gate[temp]'),
    ]
    results = []
    code_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
    for name, file, replacements, node in specs:
        path = REPO / file
        original = path.read_bytes()
        before = hashlib.sha256(original).hexdigest()
        xml = out / ('mutant-' + name + '.xml')
        passed = False
        try:
            s = original.decode('utf-8')
            for old, new in replacements:
                old, new = old.replace('\n', '\r\n') if '\r\n' in s else old, new.replace('\n', '\r\n') if '\r\n' in s else new
                if s.count(old) != 1:
                    raise RuntimeError('mutation target must be unique: ' + name)
                s = s.replace(old, new, 1)
            path.write_bytes(s.encode('utf-8'))
            command = [sys.executable, str(REPO / 'scripts/c2d5e1_test_host.py'), '--context', str(root),
                       'test', node, '-q', '--tb=short', '-p', 'no:cacheprovider', '--junitxml=' + str(xml)]
            with (out / ('mutant-' + name + '.log')).open('w', encoding='utf-8') as log:
                completed = subprocess.run(command, cwd=REPO, stdout=log, stderr=subprocess.STDOUT)
            tree = ET.parse(xml)
            cases, failures, errors = tree.findall('.//testcase'), tree.findall('.//failure'), tree.findall('.//error')
            passed = (completed.returncode == 1 and len(cases) == 1 and len(failures) == 1 and not errors
                      and 'AssertionError' in ''.join(failures[0].itertext()))
            results.append(dict(name=name, node=node, valid_countercheck=passed, exit=completed.returncode,
                                code_sha=code_sha, original_sha256=before,
                                mutant_sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        finally:
            path.write_bytes(original)
            assert hashlib.sha256(path.read_bytes()).hexdigest() == before
        (out / 'counterchecks.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
        print(name + ': ' + ('assertion caught' if passed else 'NOT VALID'), flush=True)
    return 0 if all(r['valid_countercheck'] for r in results) and len(results) == len(specs) else 1


if __name__ == '__main__':
    raise SystemExit(main())
