"""Sequential negative controls for C2d-5f1; external logs, exact restoration even on failure.

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
    t = 'api/tests/test_c2d5f1_citation_runtime.py::'
    k = 'api/tests/test_c2d5f1_worker_hooks.py::'
    boundary, rt = 'api/app/citations.py', 'api/app/ingest/runtime.py'
    specs = [
        # Drop the actual-runtime login check at entry: the owner reads and writes links.
        ('runtime', boundary, [('        runtime.require_runtime(db)\n        try:\n            granted',
                                '        try:\n            granted')],
         t + 'test_entry_requires_actual_runtime_login_before_private_read[direct-owner]'),
        # Drop the per-privilege grant probe: a login without UPDATE on references reaches private SQL.
        ('grants', boundary, [("        if granted is not True:\n            raise runtime.Halt('worker_runtime_required')\n",
                               '')],
         t + 'test_entry_missing_citation_grant_refused[UPDATE ON reference_entries]'),
        # Drop the entry gate: the batch reads private references before the final gate closes.
        ('entry-gate', boundary, [('        try:\n            auth_safety.require_writer(db)\n        except Exception:\n'
                                   "            raise runtime.Halt('recovery_gate_closed') from None\n", '')],
         t + 'test_closed_gate_refuses_before_private_read_cli_direct_celery[hold]'),
        # Drop the final journal/gate boundary of the batch transaction.
        ('final-gate', boundary, [('        try:\n            auth_safety.require_final(db, writer=True)\n'
                                   '        except Exception:\n'
                                   "            raise runtime.Halt('recovery_gate_closed') from None\n"
                                   '        state = _binding(', '        state = _binding(')],
         t + 'test_gate_closing_before_final_writes_nothing[hold]'),
        # Drop the exact scalar rebinding under the journal lock.
        ('binding', boundary, [("                raise runtime.Halt('citation_binding_changed')",
                                '                pass  # negative control')],
         t + 'test_binding_change_before_final_refuses_commit'),
        # Drop the common-copy recheck under the item lock (the hint is stale after a revoke).
        ('common-fence', boundary, [("if h.visibility == 'private' or (h.item_id in locked and h.item_id not in closed)}",
                                     "if h.visibility == 'private' or (h.item_id in locked)}")],
         t + 'test_revoke_committing_between_hint_and_lock_is_rechecked_under_lock'),
        # Discovery takes the exclusive citation lock before waiting for the common item (old order).
        ('lock-order', boundary, [('    _key_share(db, hint)\n    _lock(db, exclusive=True)',
                                   '    _lock(db, exclusive=True)')],
         k + 'test_real_common_extraction_holding_item_and_discovery_retarget_never_deadlock'),
        # Drop the shared citation lock of the extraction hook: arrival orders no longer serialize.
        ('arrival', boundary, [('    _lock(db, exclusive=False)\n    rows = db.execute(stmt.order_by(ReferenceEntry.id)).all()',
                                '    rows = db.execute(stmt.order_by(ReferenceEntry.id)).all()')],
         k + 'test_real_extraction_and_discovery_both_arrival_orders_converge[discovery]'),
        # Continue after an uncertain commit.
        ('uncertain', rt, [("        raise Halt('worker_commit_uncertain') from None",
                            '        return  # negative control')],
         t + 'test_commit_uncertain_stops_without_inference_then_independent_resume[True]'),
        # Continue after a release fault following a known commit.
        ('release', rt, [("        raise Halt('worker_release_unavailable') from None",
                          '        pass  # negative control')],
         t + 'test_release_fault_after_known_commit_keeps_rows_and_stops'),
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
            command = [sys.executable, str(REPO / 'scripts/c2d5f1_test_host.py'), '--context', str(root),
                       'test', node, '-q', '--tb=short', '-p', 'no:cacheprovider', '-o', 'faulthandler_timeout=120',
                       '--junitxml=' + str(xml)]
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
