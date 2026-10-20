"""M8b counterchecks: each deliberate mutation must make its targeted tests fail.

    python scripts/m8b_counterchecks.py --host-context <m8a_test_host context> --out <dir outside repo>

Every mutation is applied to a fresh copy (api/, scripts/ras_ops.py, deploy/, docker-compose.yml)
outside the repository; the source tree is hashed before and after and must not change. A
mutation HOLDS when the targeted tests fail on the mutated copy (and they pass on the clean copy).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOST = REPO / "scripts" / "m8a_test_host.py"

MUTATIONS = [
    ("ship_torn_tail", "api/app/ops/replica.py", "        data = chain_bytes(batch)\n",
     "        data = chain_bytes(batch) + (local.torn or b'')\n", ["tests/test_m8b_replica.py::test_torn_tail_is_never_shipped"]),
    ("overwrite_replica", "api/app/ops/s3.py",
     '        self._call("put_object", Key=key, Body=data, IfNoneMatch="*", ContentType=content_type,\n',
     '        self._call("put_object", Key=key, Body=data, ContentType=content_type,\n',
     ["tests/test_m8b_replica.py::test_existing_segment_is_never_overwritten",
      "tests/test_m8b_s3_minio.py::test_conditional_create_never_overwrites"]),
    # Round 2 mutated only the in-loop raise; a second check after the loop still refused (MISSED, kept in
    # the inventory). The fork check proper lives in relation(): removing it must be caught.
    ("skip_fork_check", "api/app/ops/replica.py",
     '    if local.records[common - 1].hash != remote.records[common - 1].hash:\n        return "replica_fork"\n', '',
     ["tests/test_m8b_replica.py::test_fork_stops_shipping_without_writing",
      "tests/test_m8b_replica.py::test_rolled_back_local_journal_is_detected_not_shipped"]),
    ("choose_shorter_chain", "api/app/ops/replica.py",
     '        return Choice(local, "local", l_seq, r_seq, "local_ahead")\n    return Choice(replica, "replica", l_seq, r_seq, "replica_ahead")\n',
     '        return Choice(replica, "replica", l_seq, r_seq, "local_ahead")\n    return Choice(local, "local", l_seq, r_seq, "replica_ahead")\n',
     ["tests/test_m8b_replica.py::test_choose_longer_only_when_prefix",
      "tests/test_m8b_replica.py::test_db_and_local_journal_rolled_back_but_replica_newer_is_caught"]),
    ("splice_diverged_chains", "api/app/ops/replica.py",
     '    if local.records[common - 1].hash != replica.records[common - 1].hash:\n        raise ReplicaError("journal_sources_diverged")\n',
     '', ["tests/test_m8b_replica.py::test_choose_longer_only_when_prefix"]),
    ("backup_reads_dotenv", "scripts/ras_ops.py",
     '    values = {}\n    try:\n        text = path.read_text(encoding="utf-8")\n',
     '    values = {}\n    try:\n        open(Path(__file__).resolve().parents[1] / ".env", encoding="utf-8").close()\n'
     '    except OSError:\n        pass\n    try:\n        text = path.read_text(encoding="utf-8")\n',
     ["tests/test_m8b_ops_host.py::test_no_dotenv_is_ever_opened"]),
    ("keep_plaintext_after_upload", "api/app/ops/artifacts.py",
     "    finally:\n        plain_tar.unlink(missing_ok=True)\n        remove_tree(artifact)\n",
     "    finally:\n        pass\n",
     ["tests/test_m8b_artifacts.py::test_ship_encrypts_uploads_reads_back_and_leaves_no_plaintext"]),
    ("private_key_accepted", "api/app/ops/artifacts.py",
     '    if "AGE-SECRET-KEY" in text.upper():\n        raise OffsiteError("private_key_on_server")\n', '',
     ["tests/test_m8b_artifacts.py::test_private_key_on_server_is_refused"]),
    ("prune_last_good", "api/app/ops/artifacts.py",
     "    keep = {s.backup_id for s in good[:max(1, min_keep)]}\n", "    keep = set()\n",
     ["tests/test_m8b_artifacts.py::test_retention_keeps_good_backups_and_needs_a_successful_last_run"]),
    ("prune_after_failed_run", "api/app/ops/artifacts.py",
     "    if not last_run_ok:\n        return []\n", "",
     ["tests/test_m8b_artifacts.py::test_retention_keeps_good_backups_and_needs_a_successful_last_run"]),
    ("forget_reopen_writers", "scripts/ras_ops.py",
     '                if window_open:\n                    run["reopen"] = reopen(d)\n',
     '                if window_open and "code" not in run:\n                    run["reopen"] = reopen(d)\n',
     ["tests/test_m8b_ops_host.py::test_failure_mid_window_still_reopens_writers_and_alerts"]),
    ("alert_leaks_data", "api/app/ops/alerts.py",
     '    if isinstance(value, str) and (_SAFE_CODE.fullmatch(value) or _ISO.fullmatch(value)):\n',
     '    if isinstance(value, str):\n',
     ["tests/test_m8b_alerts.py::test_rendered_mail_carries_codes_counts_and_times_only"]),
]


def tree_hash() -> dict:
    out = {}
    for base in ("api/app", "api/tests", "scripts", "deploy"):
        for path in sorted((REPO / base).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                out[path.relative_to(REPO).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def copy_tree(dest: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.egg-info", ".pytest_cache")
    shutil.copytree(REPO / "api", dest / "api", ignore=ignore)
    (dest / "scripts").mkdir()
    shutil.copy2(REPO / "scripts" / "ras_ops.py", dest / "scripts" / "ras_ops.py")
    shutil.copytree(REPO / "deploy", dest / "deploy", ignore=ignore)
    shutil.copy2(REPO / "docker-compose.yml", dest / "docker-compose.yml")


def run_tests(host_ctx: Path, copy: Path, tests: list[str], junit: Path) -> int:
    # --api-dir before the action: the host tool's REMAINDER would swallow it otherwise.
    cmd = [sys.executable, str(HOST), "--context", str(host_ctx), "--api-dir", str(copy / "api"), "test",
           "--junit", str(junit), "--", *tests]
    return subprocess.run(cmd, capture_output=True, text=True).returncode


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host-context", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    out = a.out.resolve()
    if out == REPO or REPO in out.parents:
        raise SystemExit("output must be outside the repository")
    out.mkdir(parents=True, exist_ok=True)
    before = tree_hash()
    results = []
    clean = out / "clean"
    copy_tree(clean)
    targets = sorted({t for m in MUTATIONS for t in m[4]})
    baseline = run_tests(a.host_context, clean, targets, out / "clean.xml")
    results.append({"mutation": "none (clean copy)", "exit": baseline, "result": "PASS" if baseline == 0 else "FAIL"})
    print(f"clean copy: exit {baseline}", flush=True)
    for name, rel, old, new, tests in MUTATIONS:
        copy = out / name
        copy_tree(copy)
        target = copy / rel
        text = target.read_text(encoding="utf-8")
        if text.count(old) != 1:
            results.append({"mutation": name, "result": "NOT-APPLIED", "occurrences": text.count(old)})
            print(f"{name}: NOT APPLIED ({text.count(old)} matches)", flush=True)
            continue
        target.write_text(text.replace(old, new), encoding="utf-8")
        code = run_tests(a.host_context, copy, tests, out / f"{name}.xml")
        # pytest exit 1 = tests ran and failed; any other non-zero (usage/collection) proves nothing.
        verdict = "HOLDS" if code == 1 else ("MISSED" if code == 0 else f"INVALID(exit {code})")
        results.append({"mutation": name, "file": rel, "tests": tests, "exit": code, "result": verdict})
        print(f"{name}: {verdict} (exit {code})", flush=True)
        shutil.rmtree(copy)
    after = tree_hash()
    summary = {"results": results, "source_files": len(before), "source_unchanged": before == after,
               "source_sha256": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()}
    (out / "counterchecks.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps({"holds": sum(r["result"] == "HOLDS" for r in results), "total": len(MUTATIONS),
                      "clean_exit": baseline, "source_unchanged": before == after}))
    ok = baseline == 0 and all(r["result"] == "HOLDS" for r in results[1:]) and before == after
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
