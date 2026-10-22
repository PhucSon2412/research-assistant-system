"""M7c counterchecks: each deliberate mutation must make its targeted offline tests fail.

    python scripts/m7c_counterchecks.py --out <dir outside repo>

Every mutation is applied to a fresh copy of api/, scripts/ and evaluation/ outside the repository
and the targeted tests run against that copy (PYTHONPATH puts the copy first). The source tree is
hashed before and after and must not change. A mutation HOLDS when the targeted tests fail on the
mutated copy and pass on the clean copy. No provider, arXiv, DB or network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AI = 'api/app/evaluation/ai_review.py'
T_AI, T_TOOL = 'tests/test_m7c_ai_review.py', 'tests/test_m7c_tooling.py'

MUTATIONS = [
    # AI records written into the M7b human tree or carrying human_* fields
    ('ai_output_into_human_tree', AI,
     '        raise AiReviewError(f"{path}: AI judge output never goes into the M7b human-review tree")\n', '',
     [T_AI + '::test_ai_output_never_goes_into_the_human_review_tree']),
    ('ai_record_with_human_fields', AI, '    bad = sorted(k for k in _keys(record) if _HUMAN_FIELDS.match(k))\n',
     '    bad = []\n', [T_AI + '::test_record_guard_refuses_human_fields_missing_provenance_and_unbound_records']),
    # judge records counted without their binding
    ('judge_counted_unbound', AI,
     '        if binding is not None and record["binding"] == binding and record["binding_sha256"] == human.digest(binding):\n',
     '        if binding is not None:\n', [T_AI + '::test_report_counts_only_live_bindings_and_measures_two_run_agreement']),
    ('judge_record_without_binding', AI,
     '    if binding is None:\n        raise AiReviewError(f"{target}: no live binding; nothing to record")\n', '',
     [T_AI + '::test_no_live_binding_records_nothing']),
    ('m7c_binding_ignores_corpus', AI,
     '    if current is None or current["corpus_matches_run"] is not True:\n', '    if current is None:\n',
     [T_AI + '::test_m7c_binding_follows_question_response_snapshot_and_corpus']),
    # papers with a wrong licence accepted
    ('accept_arxiv_default_licence', 'scripts/m7c_papers.py',
     "    'https://creativecommons.org/publicdomain/zero/1.0/': 'CC0 1.0',\n",
     "    'https://creativecommons.org/publicdomain/zero/1.0/': 'CC0 1.0',\n"
     "    'https://arxiv.org/licenses/nonexclusive-distrib/1.0/': 'arXiv default',\n",
     [T_TOOL + '::test_licence_policy_accepts_only_open_licences_of_the_exact_latest_version']),
    ('accept_any_version', 'scripts/m7c_papers.py',
     "    if not oai_versions or version != max(oai_versions):\n        reasons.append('not_latest_version')\n", '',
     [T_TOOL + '::test_licence_policy_accepts_only_open_licences_of_the_exact_latest_version']),
    ('accept_licence_disagreement', 'scripts/m7c_papers.py',
     "        reasons.append('licence_sources_disagree')\n", '        pass\n',
     [T_TOOL + '::test_licence_policy_accepts_only_open_licences_of_the_exact_latest_version']),
    # ceiling exceeded
    ('no_80_percent_stop', 'scripts/m7c_live.py', '        usd, requests = self.limits()\n',
     '        usd, requests = self.usd, self.requests\n',
     [T_TOOL + '::test_ledger_stops_new_calls_at_80_percent_of_requests',
      T_TOOL + '::test_ledger_stops_at_80_percent_of_usd_and_counts_open_calls_at_ceiling']),
    ('judge_content_on_invalid_cost', 'scripts/m7c_live.py',
     '                content = None  # only a clean, pinned, costed reply is ever judged\n', '                pass\n',
     [T_TOOL + '::test_judge_failures_record_no_content_and_keep_cost_conservative']),
    ('judge_unpinned_provider', 'scripts/m7c_live.py',
     "                'provider': {'only': [self.provider], 'allow_fallbacks': False, 'data_collection': 'deny',\n",
     "                'provider': {'only': [self.provider], 'allow_fallbacks': True, 'data_collection': 'deny',\n",
     [T_TOOL + '::test_judge_call_pins_zdr_provider_and_settles_from_reported_cost']),
    ('key_from_environment', 'scripts/m7c_live.py', '    path = Path(path)\n',
     "    if os.environ.get('OPENROUTER_API_KEY'):\n        return os.environ['OPENROUTER_API_KEY']\n    path = Path(path)\n",
     [T_TOOL + '::test_key_only_from_the_owner_file_never_env_or_dotenv']),
    # settlement below the provider charge not flagged
    ('settlement_below_charge_accepted', 'scripts/m7c_runtime.py',
     "                                                     and app + 1e-12 >= charge)})\n",
     "                                                     and app >= 0)})\n",
     [T_TOOL + '::test_settlement_pairing_flags_a_settlement_below_the_charge']),
    ('generation_settles_at_prompt_price', 'api/app/config.py',
     '        return max(self.max_input_usd_per_million_tokens, self.max_input_cache_write_usd_per_million_tokens)\n',
     '        return self.max_input_usd_per_million_tokens\n',
     [T_TOOL + '::test_generation_settlement_covers_every_m7c_live_charge']),
    # demo drill getting the key
    ('drill_gets_provider_key', 'scripts/m9_lan_demo.py',
     '    routes = ((state.get("provider") or {}).get("routes") or []) if kind == "pilot" else []\n',
     '    routes = (state.get("provider") or {}).get("routes") or []\n',
     [T_TOOL + '::test_demo_provider_values_read_only_the_secret_file_and_keep_the_drill_off']),
]


def tree_hash() -> str:
    names = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', 'api', 'scripts',
                                     'evaluation'], cwd=REPO, text=True).splitlines()
    digest = hashlib.sha256()
    for name in sorted(names):
        path = REPO / name
        if path.is_file():
            digest.update(name.encode() + b'\0' + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def copy_tree(dest: Path) -> None:
    ignore = shutil.ignore_patterns('__pycache__', '*.egg-info', '.pytest_cache')
    for name in ('api', 'scripts', 'evaluation'):
        shutil.copytree(REPO / name, dest / name, ignore=ignore)


def run_tests(root: Path, tests: list[str], log: Path) -> int:
    env = {k: v for k, v in os.environ.items() if not k.startswith(('RAS_', 'OPENROUTER_'))}
    env.update(PYTHONPATH=str(root / 'api'), PYTHONUTF8='1')
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *tests],
                            cwd=root / 'api', env=env, capture_output=True, text=True, encoding='utf-8',
                            errors='replace')
    log.write_text(result.stdout + result.stderr, encoding='utf-8')
    return result.returncode


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    out = a.out.resolve()
    if out == REPO or REPO in out.parents:
        raise SystemExit('output must be outside the repository')
    out.mkdir(parents=True, exist_ok=False)
    before = tree_hash()
    clean = out / 'clean'
    copy_tree(clean)
    targets = sorted({t for *_, tests in MUTATIONS for t in tests})
    results = {'source_before': before, 'clean_exit': run_tests(clean, targets, out / 'clean.log'), 'mutations': []}
    for name, path, old, new, tests in MUTATIONS:
        root = out / name
        copy_tree(root)
        target = root / path
        text = target.read_text(encoding='utf-8')
        if text.count(old) != 1:
            results['mutations'].append({'name': name, 'status': 'NOT_APPLIED', 'occurrences': text.count(old)})
            continue
        target.write_text(text.replace(old, new), encoding='utf-8')
        code = run_tests(root, tests, out / f'{name}.log')
        results['mutations'].append({'name': name, 'tests': tests, 'exit': code,
                                     'status': 'HOLDS' if code != 0 else 'MISSED'})
        shutil.rmtree(root)
    results['source_after'] = tree_hash()
    results['source_unchanged'] = results['source_before'] == results['source_after']
    (out / 'counterchecks.json').write_text(json.dumps(results, indent=1), encoding='utf-8')
    held = sum(m['status'] == 'HOLDS' for m in results['mutations'])
    print(json.dumps({'clean_exit': results['clean_exit'], 'holds': f"{held}/{len(MUTATIONS)}",
                      'source_unchanged': results['source_unchanged'],
                      'not_holding': [m['name'] for m in results['mutations'] if m['status'] != 'HOLDS']}))
    return 0 if results['clean_exit'] == 0 and held == len(MUTATIONS) and results['source_unchanged'] else 1


if __name__ == '__main__':
    sys.exit(main())
