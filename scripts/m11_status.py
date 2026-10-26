"""Read-only M11 background status: local checkpoints, log activity and helper liveness.

No API/provider calls, credential reads, .env reads, process stops or file writes.
Run from any directory with the same Python used for the demo tools.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess

REPO = Path(__file__).resolve().parents[1]
LOCAL_TZ = timezone(timedelta(hours=7), 'Asia/Saigon')


def read(path, default):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default


def stamp(path):
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, LOCAL_TZ).isoformat(timespec='seconds')


def helper_alive(pid, script_name='output/m11_budget_finish.py'):
    if not isinstance(pid, int) or pid <= 0 or os.name != 'nt':
        return None
    # Never return the command line: an unrelated process could have reused a PID.
    script = ("try {"
              f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' -ErrorAction Stop; "
              "if($null -eq $p){'no'}"
              "elseif([string]::IsNullOrWhiteSpace($p.CommandLine)){'unknown'}"
              f"elseif($p.CommandLine.Replace('\\','/') -like '*{script_name}*')"
              "{'yes'}else{'no'}"
              "}catch{'unknown'}")
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-Command', script],
                           capture_output=True, text=True, timeout=15, check=True)
        return {'yes': True, 'no': False}.get(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None


def status(root):
    evidence = root / 'evidence/m11'
    state = read(evidence / 'finish-budget50-state.json', {})
    launch = read(evidence / 'finish-budget50-launch.json', {})
    phase = state.get('status', 'not_started')
    alive = helper_alive(launch.get('pid'))
    runtime_done = phase == 'runtime_complete_pending_agent_review_docs_and_final_ci'
    stopped = phase == 'stopped_for_agent_review'
    result = ('not_started' if phase == 'not_started' else
              'paused_by_owner' if phase == 'paused_by_owner' else
              'runtime_done_review_pending' if runtime_done else 'stopped_needs_review' if stopped else
              'helper_not_running_check_logs' if alive is False else 'running' if alive else 'unknown')
    manifest = read(REPO / 'evaluation/m11-papers/manifest.json', {'papers': []})
    tags = {p['tagged'] for p in manifest['papers']}
    rows = {k: v for k, v in read(evidence / 'uploads.json', {}).items() if k in tags}
    committed, ledger_error = Decimal(0), None
    try:
        ledger = evidence / 'cost/ledger.jsonl'
        if not ledger.exists():
            raise ValueError('ledger_missing')
        for line in ledger.read_text(encoding='utf-8').splitlines():
            record = json.loads(line)
            if record.get('event') == 'sending':
                value = Decimal(record['ceiling_usd'])
                if not value.is_finite() or value <= 0:
                    raise ValueError('invalid_ceiling')
                committed += value
    except (OSError, ValueError, KeyError, ArithmeticError):
        committed, ledger_error = None, 'ledger_unreadable_no_cost_estimate'
    return {
        'checked_at': datetime.now(LOCAL_TZ).isoformat(timespec='seconds'),
        'result': result, 'phase': phase, 'helper_pid': launch.get('pid'), 'helper_alive': alive,
        'workers': {name: {'pid': state.get('steps', {}).get(name, {}).get('pid'),
                          'alive': helper_alive(state.get('steps', {}).get(name, {}).get('pid'), script)}
                    for name, script in [('index', 'scripts/m11_library.py'), ('discover', 'scripts/m11_papers.py')]},
        'manifest_papers': len(tags), 'target_receipts': sum(bool(r.get('item_id')) for r in rows.values()),
        'checkpoint_states': dict(Counter(r.get('state', r.get('status')) for r in rows.values())),
        'committed_usd_not_billed': str(committed) if committed is not None else None,
        'ledger_error': ledger_error, 'hard_cap_usd': '50', 'total_stop_usd': '40',
        'smoke_records': len(read(evidence / 'smoke.json', [])),
        'last_updates': {name: stamp(evidence / name) for name in
                         ('uploads.json', 'budget-index.log', 'budget-discover.log')},
        'stop_reason': read(evidence / 'index-stop.json', {}).get('reason'),
        'upload_admission_closed': (evidence / 'upload-admission-closed.json').exists(),
        'helper_error': state.get('error'),
        'scope': 'runtime_only_docs_pr_review_and_final_ci_not_checked',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.home() / 'ras-pilot')
    args = parser.parse_args()
    print(json.dumps(status(args.root), indent=2))


if __name__ == '__main__':
    main()
