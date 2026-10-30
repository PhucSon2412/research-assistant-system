"""M12: path filter for pull requests, test tiers and the per-worker test stacks.

Offline: reads scripts/ci_paths.py, .github/workflows/ci.yml and pyproject.toml;
no container, no network.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ci_paths", REPO / "scripts" / "ci_paths.py")
ci_paths = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_paths)

NONE = {"api": False, "web": False, "deploy": False}
ALL = {"api": True, "web": True, "deploy": True}


@pytest.mark.parametrize("paths, expected", [
    (["docs/decisions.md", "docs/tasks/m12-fast-ci-and-test-tiers.md", "AGENTS.md", "CLAUDE.md", "README.md"], NONE),
    (["docs/screenshots/a.png"], NONE),
    (["web/app/page.tsx", "web/package-lock.json"], {"api": False, "web": True, "deploy": False}),
    (["deploy/compose.yaml"], {"api": True, "web": False, "deploy": True}),
    (["api/app/rag.py"], {"api": True, "web": False, "deploy": True}),
    (["api/constraints-rc2.txt"], {"api": True, "web": False, "deploy": True}),
    (["scripts/m8a_test_host.py"], {"api": True, "web": False, "deploy": True}),
    # Markdown outside the repository root can be test data (evaluation corpus).
    (["evaluation/corpus/markdown/p01-gated-sparse-routing.md"], {"api": True, "web": False, "deploy": True}),
    (["api/README.md"], {"api": True, "web": False, "deploy": True}),
    ([".github/workflows/ci.yml"], ALL),
    (["docker-compose.yml"], ALL),
    (["docs/README.md", "web/app/page.tsx"], {"api": False, "web": True, "deploy": False}),
    ([], ALL),
    (["", "  "], ALL),
])
def test_changed_paths_select_jobs(paths, expected):
    assert ci_paths.needed(paths) == expected


def test_cli_appends_every_job_to_github_output(tmp_path):
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "ci_paths.py")], input="docs/x.md\nweb/a.ts\n",
                       capture_output=True, text=True, env={**os.environ, "GITHUB_OUTPUT": str(out)}, check=True)
    assert r.stdout.split() == out.read_text(encoding="utf-8").split() == ["api=false", "web=true", "deploy=false"]


def test_slow_marker_is_declared_and_strict():
    text = (REPO / "api" / "pyproject.toml").read_text(encoding="utf-8")
    assert "--strict-markers" in text and '"slow:' in text


TIERS_SPEC = importlib.util.spec_from_file_location("ci_tiers", REPO / "scripts" / "ci_tiers.py")
ci_tiers = importlib.util.module_from_spec(TIERS_SPEC)
TIERS_SPEC.loader.exec_module(ci_tiers)


def test_every_test_file_is_in_a_tier_and_shards_partition_it():
    assert ci_tiers.check() == []
    assert set(ci_tiers.FAST) <= set(ci_tiers.tier_files("full"))
    assert set(ci_tiers.tier_files("full")) | set(ci_tiers.NOT_IN_CI) == set(ci_tiers.all_files())


def test_shards_balance_by_measured_seconds():
    seconds = {"a.py": 100, "b.py": 60, "c.py": 50, "d.py": 10}
    split = [ci_tiers.shard(list(seconds), i, 2, seconds) for i in range(2)]
    assert split == [["a.py", "d.py"], ["b.py", "c.py"]]  # 110 s and 110 s


def _workflow():
    import yaml
    return yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def test_workflow_tiers_triggers_and_conditional_jobs():
    wf = _workflow()
    on = wf[True] if True in wf else wf["on"]  # PyYAML reads the bare key `on` as True
    assert {"pull_request", "push", "schedule", "workflow_dispatch"} <= set(on)
    jobs = wf["jobs"]
    paths = next(s for s in jobs["changes"]["steps"] if s.get("id") == "paths")["run"]
    assert "git diff --name-only HEAD^1 HEAD | python3 scripts/ci_paths.py" in paths
    for job, output in (("api", "api"), ("web", "web"), ("deploy-config", "deploy")):
        assert jobs[job]["needs"] == "changes"
        assert jobs[job]["if"] == f"needs.changes.outputs.{output} == 'true'"
    run = next(s for s in jobs["api"]["steps"] if s.get("name") == "Run API tests of this tier and shard")["run"]
    assert 'marker=(-m "not slow")' in run and "ci_tiers.py files" in run and 'exit 1' in run
    assert jobs["api"]["strategy"]["fail-fast"] is False
    for job in jobs.values():
        assert "continue-on-error" not in job
        assert all("continue-on-error" not in step for step in job["steps"])
    # Third-party actions only from the GitHub-owned actions/ namespace.
    uses = {s["uses"].split("@")[0] for job in jobs.values() for s in job["steps"] if "uses" in s}
    assert all(u.startswith("actions/") for u in uses), uses


HOST_SPEC = importlib.util.spec_from_file_location("m8a_test_host_m12", REPO / "scripts" / "m8a_test_host.py")
host = importlib.util.module_from_spec(HOST_SPEC)
sys.path.insert(0, str(REPO / "scripts"))  # the harness imports c4_isolation next to it
HOST_SPEC.loader.exec_module(host)


@pytest.mark.parametrize("args, expected", [
    (["tests/a.py", "-m", "not slow", "-x"], ("not slow", ["tests/a.py", "-x"])),
    (["-m", "a", "-m", "b"], ("b", [])),  # pytest keeps the last -m
    (["-mslow"], ("slow", [])),
    (["tests/a.py"], (None, ["tests/a.py"])),
])
def test_harness_lifts_the_marker_expression_for_both_phases(args, expected):
    assert host.marker_expression(args) == expected


def test_harness_merges_the_timing_phase_into_one_junit(tmp_path):
    import xml.etree.ElementTree as ET
    main, timing = tmp_path / "junit-api.xml", tmp_path / "junit-api-timing.xml"
    main.write_text('<testsuites><testsuite name="pytest"><testcase name="a"/></testsuite></testsuites>')
    timing.write_text('<testsuites><testsuite name="pytest"><testcase name="t"><failure/></testcase>'
                      '</testsuite></testsuites>')
    host.merge_junit(main, timing)
    cases = {c.get("name"): c.find("failure") is not None for c in ET.parse(main).getroot().iter("testcase")}
    assert cases == {"a": False, "t": True} and not timing.exists()
