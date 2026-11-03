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


def test_node_shards_split_a_large_file_without_losing_or_repeating_tests():
    nodes = [f"tests/large.py::test_case[{i}]" for i in range(100)] + ["tests/small.py::test_a"]
    seconds = {"large.py": 1000, "small.py": 10}
    split = [ci_tiers.shard_nodes(nodes, i, 18, seconds) for i in range(18)]
    assert sorted(n for group in split for n in group) == sorted(nodes)
    assert all(any(n.startswith("tests/large.py") for n in group) for group in split)
    assert max(map(len, split)) - min(map(len, split)) <= 1
    assert split == [ci_tiers.shard_nodes(list(reversed(nodes)), i, 18, seconds) for i in range(18)]


def test_measured_hotspot_runs_first_without_double_counting_its_file_time():
    nodes = ["tests/a.py::test_small", "tests/a.py::test_big", "tests/b.py::test_b"]
    seconds = {"a.py": 101, "b.py": 50, "tests/a.py::test_big": 100}
    split = [ci_tiers.shard_nodes(nodes, i, 2, seconds) for i in range(2)]
    assert split == [["tests/a.py::test_big"], ["tests/b.py::test_b", "tests/a.py::test_small"]]
    assert ci_tiers.shard_nodes(nodes, 0, 1, seconds)[0] == "tests/a.py::test_big"


def test_collection_hook_keeps_heavy_first_execution_order(monkeypatch):
    from types import SimpleNamespace
    from tests.conftest import pytest_collection_modifyitems
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setitem(sys.modules, "ci_tiers", ci_tiers)
    monkeypatch.setattr(ci_tiers, "durations", lambda tier: {
        "a.py": 101, "b.py": 50, "tests/a.py::test_big": 100,
    })
    config = SimpleNamespace(getoption=lambda key: "1/1" if key == "--ras-shard" else None,
                             hook=SimpleNamespace(pytest_deselected=lambda **kw: None))
    items = [SimpleNamespace(nodeid=n) for n in ("tests/a.py::test_small", "tests/a.py::test_big", "tests/b.py::test_b")]
    pytest_collection_modifyitems(config, items)
    assert [item.nodeid for item in items] == ["tests/a.py::test_big", "tests/b.py::test_b", "tests/a.py::test_small"]


def _workflow():
    import yaml
    return yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def test_workflow_tiers_triggers_and_conditional_jobs():
    wf = _workflow()
    on = wf[True] if True in wf else wf["on"]  # PyYAML reads the bare key `on` as True
    assert {"pull_request", "push", "workflow_dispatch"} <= set(on)
    assert "schedule" not in on and "pull_request_target" not in on
    assert wf["permissions"] == {"contents": "read"}
    jobs = wf["jobs"]
    paths = next(s for s in jobs["changes"]["steps"] if s.get("id") == "paths")["run"]
    assert 'git diff --name-only "$base" HEAD | python3 scripts/ci_paths.py' in paths
    assert 'base=HEAD^1' in paths
    assert '--event pull_request --status success' in paths
    for job, output in (("api", "api"), ("web", "web"), ("deploy-config", "deploy")):
        assert jobs[job]["needs"] == "changes"
        assert jobs[job]["if"] == f"needs.changes.outputs.{output} == 'true'"
    run = next(s for s in jobs["api"]["steps"] if s.get("name") == "Run API tests of this tier and shard")["run"]
    assert 'marker=(-m "not slow")' in run and "ci_tiers.py files" in run and 'exit 1' in run
    assert jobs["api"]["strategy"]["fail-fast"] is False
    assert int(wf["env"]["FULL_SHARDS"]) <= 18
    assert jobs["api"]["strategy"]["max-parallel"] <= 18
    assert all(job["runs-on"] == "ubuntu-24.04" for job in jobs.values())
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


def test_a_manual_dispatch_never_cancels_another_run():
    group = _workflow()["concurrency"]["group"]
    assert "github.run_id" in group and "workflow_dispatch" in group


def test_harness_timing_phase_drops_xdist_distribution():
    assert host.without_dist(["a.py", "--dist", "loadfile", "--dist=load", "--maxschedchunk", "2",
                              "--maxschedchunk=4", "-x"]) == ["a.py", "-x"]


@pytest.mark.parametrize("ending", ["switch", "teardown"])
def test_cached_database_connection_is_closed_at_fixture_boundary(migrated_db_url, monkeypatch, ending):
    from app import db as app_db
    from tests.conftest import app_env

    scope = app_env.__wrapped__(monkeypatch)
    select_database = next(scope)
    select_database(migrated_db_url)
    pooled = app_db.get_engine().raw_connection()
    driver = pooled.driver_connection
    pooled.close()
    assert not driver.closed  # returned to QueuePool, not to the server
    finished = False
    try:
        if ending == "switch":
            select_database(migrated_db_url)
        else:
            with pytest.raises(StopIteration):
                next(scope)
            finished = True
        assert driver.closed, "previous test database pool still owns a live DBAPI connection"
    finally:
        driver.close()
        if not finished:
            with pytest.raises(StopIteration):
                next(scope)


def test_original_storage_socket_is_closed_at_fixture_teardown(installation, monkeypatch):
    from app.config import get_settings
    from tests.test_uploads import upload_api

    scope = upload_api.__wrapped__(installation, monkeypatch)
    _, storage = next(scope)
    manager = storage.client._endpoint.http_session._manager
    pool = manager.connection_from_url(get_settings().storage.endpoint_url)
    with pool.pool.mutex:
        sockets = [c.sock for c in pool.pool.queue if c is not None and c.sock is not None]
    # Observe the sockets only: holding the pool itself would prolong its
    # lifetime even after botocore removes it from the client's pool manager.
    del pool, manager
    assert sockets and all(s.fileno() >= 0 for s in sockets)
    finished = False
    try:
        with pytest.raises(StopIteration):
            next(scope)
        finished = True
        assert all(s.fileno() == -1 for s in sockets), "fixture still owns an open S3 HTTP socket"
    finally:
        storage.client.close()
        if not finished:
            with pytest.raises(StopIteration):
                next(scope)
