"""API test tiers and shards for CI and local runs (M12).

    python scripts/ci_tiers.py files --tier fast|full [--shard I --shards N]
    python scripts/ci_tiers.py check
    python scripts/ci_tiers.py durations --tier fast|full <junit.xml> ...   # refresh scripts/ci_durations.json

``fast`` (pull requests) runs FAST with ``-m "not slow"``; ``full`` (push main,
nightly, workflow_dispatch) runs every file except NOT_IN_CI, slow tests
included. Shards split a tier's files greedily by the per-file seconds of an
earlier JUnit (scripts/ci_durations.json; a file without a measurement counts
as DEFAULT_SECONDS), so every shard gets about the same work. ``check`` fails
when a test file is in no tier, FAST names a missing/excluded file, or a shard
split loses or repeats a file.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "api" / "tests"
DURATIONS = REPO / "scripts" / "ci_durations.json"
DEFAULT_SECONDS = 120.0

# Pull-request tier, in addition to the M8a selection: ACL, search/RAG, ingest,
# library, review and revocation first (docs/tasks/m12-fast-ci-and-test-tiers.md).
FAST = [
    # M8a selection (config, auth/session/CSRF/Origin, invitation/reset/delivery, limiter,
    # provenance/receipts/auth safety, migrations/ORM, C4 contract/health, M8b ops, M7c/M11 tooling).
    "test_config.py", "test_m8a_smtp_delivery.py", "test_m8a_rate_limit.py", "test_m8a_smtp_deadline.py",
    "test_m8a_ci_guard.py", "test_m8a_auth_delivery.py", "test_csrf_and_cookies.py", "test_c2a_origin.py",
    "test_auth_sessions.py", "test_c2a_login_sessions.py", "test_auth_secrets.py", "test_invitations.py",
    "test_invitation_journal.py", "test_c2c2_invitations.py", "test_password_reset.py", "test_c2b_reset_status.py",
    "test_reset_token_journal.py", "test_session_revocation_journal.py", "test_pre_c4_initial_receipts.py",
    "test_auth_safety_foundation.py", "test_c2c1_bootstrap.py", "test_migrations.py", "test_c4_contract.py",
    "test_health.py", "test_m8b_replica.py", "test_m8b_artifacts.py", "test_m8b_alerts.py", "test_m8b_ops_host.py",
    "test_m8b_s3_minio.py", "test_evaluation.py", "test_m7c_ai_review.py", "test_m7c_tooling.py",
    "test_m11_tooling.py",
    # M12
    "test_m12_ci.py",
    # M12 expansion within the PR budget (one more shard): ACL before retrieval, search,
    # review requests, upload intent/original, collections, ingest worker.
    "test_library_acl.py", "test_search.py", "test_review_requests.py", "test_uploads.py", "test_collections.py",
    "test_ingest_worker.py",
]

# Never in CI, with the reason (docs/m12-fast-ci.md lists them for the orchestrator).
NOT_IN_CI = {
    **{f"test_{n}_proxy_smoke.py": "installed Windows Chrome + a built Next server (C:\\Program Files path)"
       for n in ("c2d4c", "c2d5a", "c2d5b", "c2d5c1", "c2d5c2", "c2d5d1")},
    **{f"test_{n}_browser.py": "headless Chrome against the Next web app + task context"
       for n in ("c2d6", "c2d7")},
    **{f"test_{n}_process_smoke.py": "frozen API image (RAS_TEST_PROCESS_IMAGE) + Linux Celery compose context"
       for n in ("c2d5d2a", "c2d5d2b", "c2d5e1", "c2d5e2", "c2d5f1", "c2d5f2")},
}


def all_files() -> list[str]:
    return sorted(p.name for p in TESTS.glob("test_*.py"))


def tier_files(tier: str) -> list[str]:
    if tier == "fast":
        return list(FAST)
    return [f for f in all_files() if f not in NOT_IN_CI]


def durations(tier: str = "fast") -> dict[str, float]:
    """Seconds per file for a tier: ``fast`` excludes slow tests; ``full`` falls back to ``fast``."""
    data = json.loads(DURATIONS.read_text(encoding="utf-8")) if DURATIONS.exists() else {}
    return data.get("fast", {}) | (data.get("full", {}) if tier == "full" else {})


def shard(files: list[str], index: int, count: int, seconds: dict[str, float] | None = None,
          tier: str = "fast") -> list[str]:
    """Greedy longest-first split; deterministic for the same files and durations."""
    seconds = durations(tier) if seconds is None else seconds
    loads, names = [0.0] * count, [[] for _ in range(count)]
    for name in sorted(files, key=lambda f: (-seconds.get(f, DEFAULT_SECONDS), f)):
        n = min(range(count), key=lambda i: (loads[i], i))
        loads[n] += seconds.get(name, DEFAULT_SECONDS)
        names[n].append(name)
    return sorted(names[index])


def check() -> list[str]:
    problems = []
    files = set(all_files())
    problems += [f"FAST names a missing file: {f}" for f in FAST if f not in files]
    problems += [f"FAST names an excluded file: {f}" for f in FAST if f in NOT_IN_CI]
    problems += [f"NOT_IN_CI names a missing file: {f}" for f in NOT_IN_CI if f not in files]
    problems += [f"FAST repeats {f}" for f, k in collections.Counter(FAST).items() if k > 1]
    for tier in ("fast", "full"):
        for count in range(1, 9):
            split = [f for i in range(count) for f in shard(tier_files(tier), i, count, tier=tier)]
            if sorted(split) != sorted(tier_files(tier)):
                problems.append(f"{tier} split into {count} loses or repeats a file")
    return problems


def shard_nodes(nodes: list[str], index: int, count: int, seconds: dict[str, float]) -> list[str]:
    """Spread large files across runners; estimate each node from its file's measured total.

    Membership is independent of collection order and shuffle seed. Pytest applies
    marker deselection first, so a tier's duration is divided over its selected nodes.
    """
    file_of = lambda node: Path(node.split("::", 1)[0]).name
    sizes = collections.Counter(file_of(node) for node in nodes)
    # Keep only sparse measured hotspots in the source duration file, not a full
    # node inventory. Subtract them from the file total before averaging its rest.
    known_seconds = collections.Counter()
    known_count = collections.Counter()
    for node in nodes:
        if node in seconds:
            known_seconds[file_of(node)] += seconds[node]
            known_count[file_of(node)] += 1
    average = {file: max(0.001, (seconds.get(file, DEFAULT_SECONDS) - known_seconds[file]) /
                            max(1, size - known_count[file])) for file, size in sizes.items()}
    weight = {node: seconds.get(node, average[file_of(node)]) for node in nodes}
    loads, names = [0.0] * count, [[] for _ in range(count)]
    for node in sorted(nodes, key=lambda n: (-weight[n], n)):
        target = min(range(count), key=lambda i: (loads[i], i))
        loads[target] += weight[node]
        names[target].append(node)
    return names[index]


def from_junit(paths: list[Path]) -> dict[str, float]:
    seconds: dict[str, float] = collections.defaultdict(float)
    hotspots: dict[str, float] = {}
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            module = (case.get("classname") or "").split(".")
            if len(module) >= 2 and module[0] == "tests":
                elapsed = float(case.get("time") or 0)
                seconds[module[1] + ".py"] += elapsed
                if elapsed >= 600:
                    node = "tests/" + module[1] + ".py::" + "::".join([*module[2:], case.get("name")])
                    hotspots[node] = elapsed
    return {k: round(v, 1) for k, v in sorted((seconds | hotspots).items())}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["files", "check", "durations"])
    p.add_argument("--tier", choices=["fast", "full"], default="fast")
    p.add_argument("--shard", type=int, default=1, help="1-based")
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("junit", nargs="*", type=Path)
    a = p.parse_args()
    if a.action == "check":
        problems = check()
        print("\n".join(problems) or f"ok: {len(tier_files('fast'))} fast, {len(tier_files('full'))} full, "
                                     f"{len(NOT_IN_CI)} not in CI")
        return 1 if problems else 0
    if a.action == "durations":
        data = json.loads(DURATIONS.read_text(encoding="utf-8")) if DURATIONS.exists() else {}
        data[a.tier] = data.get(a.tier, {}) | from_junit(a.junit)
        DURATIONS.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"{a.tier}: {len(data[a.tier])} duration entries (file totals and sparse hotspots)")
        return 0
    if not 1 <= a.shard <= a.shards:
        raise SystemExit("--shard must be 1..--shards")
    print(" ".join("tests/" + f for f in shard(tier_files(a.tier), a.shard - 1, a.shards, tier=a.tier)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
