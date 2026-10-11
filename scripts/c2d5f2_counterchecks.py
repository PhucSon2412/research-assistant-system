"""Sequential negative controls for C2d-5f2; external logs, exact restoration even on failure.

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
    t = 'api/tests/test_c2d5f2_discovery_runtime.py::'
    k = 'api/tests/test_c2d5f2_retarget.py::'
    boundary = 'api/app/discovery.py'
    specs = [
        ('identity', boundary, [('        runtime.require_runtime(db)\n', '')],
         t+'test_privileged_login_refused_before_dml_or_http[owner]'),
        ('final-gate', boundary, [('            auth_safety.require_final(db, writer=True)', '            pass # negative control')],
         t+'test_final_gate_closing_during_page_rolls_back_everything'),
        ('lease', boundary, [('elif clock.lease_token != nonce or previous_until is None or previous_until <= at:', 'elif False:')],
         t+'test_old_holder_cannot_write_renew_or_release_new_holder'),
        ('cursor', boundary, [('if source is None or (expected is not None and _cursor(source) != expected):', 'if source is None:')],
         t+'test_cursor_binding_rejects_changed_topic_window_token'),
        ('io-pool', boundary, [('    fetcher = fetcher or fetch_page', '    retained = engine.connect()\n    fetcher = fetcher or fetch_page')],
         t+'test_parser_has_no_pool_connection'),
        ('atomic-page', boundary, [('    reconcile_arxiv_ids(db, [p.remote_id',
            '    db.commit() # negative control\n    reconcile_arxiv_ids(db, [p.remote_id')],
         t+'test_failed_page_rolls_back_metadata_and_cursor_then_error_checkpoint'),
        ('cap', 'api/app/citations.py', [('if limit is not None and len(rows) > limit:', 'if False:')],
         k+'test_cap_exact_boundary_is_atomic[2-1-False]'),
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
            command = [sys.executable, str(REPO / 'scripts/c2d5f2_test_host.py'), '--context', str(root),
                       'test', node, '-q', '--tb=short', '-p', 'no:cacheprovider', '-o', 'faulthandler_timeout=120',
                       '--junitxml=' + str(xml)]
            with (out / ('mutant-' + name + '.log')).open('w', encoding='utf-8') as log:
                completed = subprocess.run(command, cwd=REPO, stdout=log, stderr=subprocess.STDOUT)
            tree = ET.parse(xml)
            cases, failures, errors = tree.findall('.//testcase'), tree.findall('.//failure'), tree.findall('.//error')
            passed = (completed.returncode == 1 and len(cases) == 1 and len(failures) == 1 and not errors
                      and any(marker in ''.join(failures[0].itertext()) for marker in
                              ('AssertionError', 'assert ', 'DID NOT RAISE', 'Failed: HTTP reached')))
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
