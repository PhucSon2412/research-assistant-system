"""Sequential negative controls for C2d-5e2; external logs, exact restoration even on failure.

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
    t = 'api/tests/test_c2d5e2_orphan_reconcile.py::'
    boundary, sql = 'api/app/ingest/orphans.py', 'api/migrations/versions/0045_orphan_object_fences.sql'
    specs = [
        # Drop the actual-runtime login check at entry: the owner reaches storage.
        ('runtime', boundary, [('        runtime.require_runtime(db)\n        try:\n            granted',
                                '        try:\n            granted')],
         t + 'test_entry_requires_actual_runtime_login_before_any_storage[direct-owner]'),
        # Drop the entry gate: later checkpoints still close, but only after list/HEAD.
        ('entry-gate', boundary, [('        try:\n            auth_safety.require_writer(db)\n        except Exception:\n'
                                   "            raise runtime.Halt('recovery_gate_closed') from None\n", '')],
         t + 'test_closed_gate_no_storage_no_mutation_cli_and_direct[hold]'),
        # Drop the zero-reference refusal inside the routine (the only authority once hints are bypassed).
        ('zero-reference', sql, [("  RETURN 'referenced';\n END IF;", '  NULL;\n END IF;')],
         t + 'test_reference_hints_are_not_the_authority'),
        # Drop the orphan fence from the SourceFile INSERT trigger.
        ('insert-fence', sql, [('    OR EXISTS (SELECT 1 FROM orphan_object_fences WHERE storage_key = NEW.storage_key) THEN',
                                '    THEN')],
         t + 'test_prepare_first_refuses_waiting_insert_without_cycle[False]'),
        # Drop the shared key lock from the orphan routine: prepare no longer waits for an INSERT.
        ('key-lock', sql, [(" PERFORM pg_advisory_xact_lock(1918989163, hashtext(p_key));\n -- Zero references",
                            ' -- Zero references')],
         t + 'test_insert_first_holds_key_then_prepare_keeps_object'),
        # Continue to storage after an uncertain prepare commit.
        ('uncertain', 'api/app/ingest/runtime.py', [("        raise Halt('worker_commit_uncertain') from None",
                                                     '        return  # negative control')],
         t + 'test_prepare_commit_uncertain_no_storage_then_independent_resume[True]'),
        # Continue after a release fault following a known commit.
        ('release', 'api/app/ingest/runtime.py', [("        raise Halt('worker_release_unavailable') from None",
                                                   '        pass  # negative control')],
         t + 'test_release_fault_after_known_commit_keeps_mutation_and_stops[prepare]'),
        # Treat a 403/timeout/5xx HEAD as a missing object.
        ('403-as-missing', boundary, [("        if is_missing_object(exc):\n            return None\n"
                                       "        raise runtime.Halt('storage_unavailable') from None\n    seen = _seen(",
                                       '        return None\n    seen = _seen(')],
         t + 'test_storage_errors_halt_never_count_as_absent[pre_delete_head-403]'),
        # Drop the final gate of the completion checkpoint.
        ('final-gate', boundary, [('        _final_gate(db, writer=True)\n        with db.no_autoflush:',
                                   '        with db.no_autoflush:')],
         t + 'test_gate_closing_inside_finalize_refuses_commit[pending]'),
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
            command = [sys.executable, str(REPO / 'scripts/c2d5e2_test_host.py'), '--context', str(root),
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
