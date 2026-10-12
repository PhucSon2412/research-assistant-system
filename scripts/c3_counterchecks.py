"""Sequential negative controls for C3; external logs, exact restoration even on failure.

Run only when no suite/process is using the source. Each control must fail a
business assertion (not setup/error). The final suite runs after restoration.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
T = 'api/tests/test_c3_operator_recovery.py::'
SQL = 'api/migrations/versions/0049_operator_auth_recovery.sql'
OPS = 'api/app/operator_recovery.py'
SPECS = [
    ('receipt-guard', [(SQL, "  RAISE EXCEPTION 'operator recovery receipt needs its case use' USING ERRCODE='insufficient_privilege'; END IF;",
                        "  NULL; END IF;")],
     T + 'test_generic_paths_cannot_mint_operator_reasons'),
    ('login-binding', [(SQL, "  WHERE o.login_role=session_user::text AND o.revoked_at IS NULL\n   AND r.rolcanlogin",
                        "  WHERE o.revoked_at IS NULL\n   AND r.rolcanlogin")],
     T + 'test_membership_set_role_and_unmapped_grants_never_impersonate'),
    ('status-disabled', [(OPS, '        if snap.status != "active":\n            raise OperatorRefused("status_disabled")\n', ''),
                         (SQL, "IF c.action='resolve_initial_status' AND (u.status IS DISTINCT FROM 'active' OR u.status_safety",
                          "IF c.action='resolve_initial_status' AND (u.status_safety")],
     T + 'test_disabled_snapshot_with_later_enable_intent_stays_disabled'),
    ('hold-reconciled', [(OPS, '    if not view.backfill_complete or view.projection_seq is None or view.projection_seq < begin.seq:\n        raise OperatorRefused("hold_not_reconciled")\n', '')],
     T + 'test_inspect_reports_codes_without_secrets_and_requires_hold_for_mutation'),
    ('batch-false', [(OPS, '                attempt.written = False  # absent/torn/prefix/unsynced: the whole batch stays false',
                      '                attempt.written = True')],
     T + 'test_t15_fault_matrix[commit-full]'),
    ('journal-case-reuse', [(OPS, '        if case_used(hold.state, case.case_id):\n            raise OperatorRefused("case_already_used")\n        registered_case(conn, case)\n        at = clock()',
                             '        registered_case(conn, case)\n        at = clock()')],
     T + 'test_two_dump_restore_rounds_never_reuse_case_or_rebuild_credential'),
    ('final-authority-after-append', [(OPS, '            _recheck(conn, case, settings, clock)
            if attempt.written:
                conn.execute(',
                                       '            if attempt.written:
                _recheck(conn, case, settings, clock)
                conn.execute(')],
     T + 'test_final_authority_rechecked_when_real_fsync_fails[set_credential-expire]'),
    ('release-reported', [(OPS, '    if not _release(conn):
        raise ConnectionReleaseFailed()
', '    _release(conn)
')],
     T + 'test_prepare_release_failure_stops_before_prompt[False]'),
]


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
    results = []
    code_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
    for name, replacements, node in SPECS:
        originals = {f: (REPO / f).read_bytes() for f, _, _ in replacements}
        before = {f: hashlib.sha256(b).hexdigest() for f, b in originals.items()}
        xml = out / ('mutant-' + name + '.xml')
        passed = False
        try:
            texts = {f: b.decode('utf-8') for f, b in originals.items()}
            for f, old, new in replacements:
                s = texts[f]
                crlf = '\r\n' in s
                old, new = (old.replace('\n', '\r\n'), new.replace('\n', '\r\n')) if crlf else (old, new)
                if s.count(old) != 1:
                    raise RuntimeError('mutation target must be unique: ' + name)
                texts[f] = s.replace(old, new, 1)
            for f, s in texts.items():
                (REPO / f).write_bytes(s.encode('utf-8'))
            command = [sys.executable, str(REPO / 'scripts/c3_test_host.py'), '--context', str(root),
                       'test', node, '-q', '--tb=short', '-p', 'no:cacheprovider', '-o', 'faulthandler_timeout=300',
                       '--junitxml=' + str(xml)]
            with (out / ('mutant-' + name + '.log')).open('w', encoding='utf-8') as log:
                completed = subprocess.run(command, cwd=REPO, stdout=log, stderr=subprocess.STDOUT)
            tree = ET.parse(xml)
            cases, failures, errors = tree.findall('.//testcase'), tree.findall('.//failure'), tree.findall('.//error')
            passed = (completed.returncode == 1 and len(cases) >= 1 and len(failures) >= 1 and not errors
                      and all(any(m in ''.join(f.itertext()) for m in ('AssertionError', 'assert ', 'DID NOT RAISE',
                                                                       'Regex pattern did not match'))
                              for f in failures))
            results.append(dict(name=name, node=node, valid_countercheck=passed, exit=completed.returncode,
                                failures=len(failures), cases=len(cases), code_sha=code_sha, original_sha256=before,
                                mutant_sha256={f: hashlib.sha256((REPO / f).read_bytes()).hexdigest() for f in originals}))
        finally:
            for f, b in originals.items():
                (REPO / f).write_bytes(b)
                assert hashlib.sha256((REPO / f).read_bytes()).hexdigest() == before[f]
        (out / 'counterchecks.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
        print(name + ': ' + ('assertion caught' if passed else 'NOT VALID'), flush=True)
    return 0 if all(r['valid_countercheck'] for r in results) and len(results) == len(SPECS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
