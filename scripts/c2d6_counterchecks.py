"""Sequential negative controls for C2d-6; external logs, exact restoration even on failure.

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
    t = 'api/tests/test_c2d6_search_runtime.py::'
    boundary = 'api/app/search_runtime.py'
    specs = [
        ('final-clock', 'api/app/identity/final_caller.py', [('    at = clock()\n', '    at = row.last_activity_at\n')],
         t+'test_final_clock_after_predicate_sql_including_empty[idle-response]'),
        ('presend', boundary, [("    boundary.checkpoint(engine, 'presend', presend)\n", '')],
         t+'test_new_proof_after_commit_not_precommit_dto[presend]'),
        ('nonce', 'api/app/search_accounting.py', [("        and all(getattr(row, k) == getattr(proof, k) for k in FIELDS))", '        and row.id == proof.id)')],
         t+'test_nonce_tuple_cannot_renew_or_settle_another_attempt[reservation_token]'),
        ('late-settlement', 'api/app/search_accounting.py', [("        row = db.scalar(select(SearchUsage).where(SearchUsage.id == proof.id).with_for_update())\n        if not matches(row, proof):\n", "        if clock().date() != proof.day: raise s.ApiError(503, 'expired_financial_owner')\n        row = db.scalar(select(SearchUsage).where(SearchUsage.id == proof.id).with_for_update())\n        if not matches(row, proof):\n")],
         t+'test_expiry_old_day_late_duplicate_settlement_and_no_legacy_adoption'),
        ('capacity', 'api/app/search_accounting.py', [('if active_document + active_query >= settings.embedding.max_concurrent_requests:', 'if False:')],
         t+'test_document_query_share_slots_but_rerank_is_separate'),
        ('acl-before-topk', 'api/app/search.py', [(' WHERE {_audience(include_common)} AND i.content_status=', " WHERE TRUE AND i.content_status=")],
         'api/tests/test_search.py::test_search_acl_before_keyword_vector_topk_and_rerank_payload'),
        ('final-scope', boundary, [(' or (expected is not None and scope != expected) or (before is not None and fresh != before)', '')],
         t+'test_new_proof_after_commit_not_precommit_dto[response]'),
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
            command = [sys.executable, str(REPO / 'scripts/c2d6_test_host.py'), '--context', str(root),
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
