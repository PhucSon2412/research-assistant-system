"""Sequential negative controls for C2d-7; external logs, exact restoration even on failure.

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
    t = 'api/tests/test_c2d7_rag_runtime.py::'
    specs = [
        ('final-clock', 'api/app/api/resource_writes.py', [('    at = clock()  # after waits, resource SQL, and profile/CSRF SQL\n', '    at = row.last_activity_at\n')],
         t+'test_final_clock_after_last_predicate_sql_for_every_response[idle-response-measured-True]'),
        ('presend', 'api/app/rag_runtime.py', [("    checkpoint(engine, 'presend', presend)\n", '')],
         t+'test_exact_scope_and_caller_at_each_external_boundary[after_admission-epoch]'),
        ('nonce', 'api/app/search_accounting.py', [("        and all(getattr(row, k) == getattr(proof, k) for k in FIELDS))", '        and row.id == proof.id)')],
         t+'test_generation_nonce_cannot_adopt_another_financial_tuple[reservation_token]'),
        ('late-settlement', 'api/app/search_accounting.py', [("        row = db.scalar(select(SearchUsage).where(SearchUsage.id == proof.id).with_for_update())\n        if not matches(row, proof):\n", "        if clock().date() != proof.day: return {'status': 'unknown'}\n        row = db.scalar(select(SearchUsage).where(SearchUsage.id == proof.id).with_for_update())\n        if not matches(row, proof):\n")],
         t+'test_lease_old_day_late_duplicate_settlement_and_legacy_refusal'),
        ('capacity', 'api/app/search_accounting.py', [('if active >= settings.generation.max_concurrent_requests:', 'if False:')],
         t+'test_concurrent_generation_has_one_slot_and_no_second_dispatch'),
        ('acl-before-topk', 'api/app/search.py', [(" WHERE {_audience(include_common)} AND i.content_status='indexed'\n   AND i.rights_epoch=", " WHERE TRUE AND i.content_status='indexed'\n   AND i.rights_epoch=")],
         'api/tests/test_search.py::test_search_acl_before_keyword_vector_topk_and_rerank_payload'),
        ('exact-citation', 'api/app/rag.py', [(' or quote not in row["text"]:', ':')],
         'api/tests/test_rag.py::test_wrong_citation_quote_and_prompt_injection_fail_closed'),
        ('final-evidence', 'api/app/search_runtime.py', [(' or (expected is not None and scope != expected) or (before is not None and immutable(fresh) != immutable(before))', '')],
         t+'test_exact_scope_and_caller_at_each_external_boundary[provider-epoch]'),
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
            command = [sys.executable, str(REPO / 'scripts/c2d7_test_host.py'), '--context', str(root),
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
