"""PR #66 P2-3: CI publishes only scanned bytes after a clean scan.

Scanner cases use a synthetic fixture context outside the repo; nothing is
uploaded anywhere. The workflow cases read .github/workflows/ci.yml and check
that the upload step depends on the scan step's clean output (not on
``always()``), publishes only the scan's publish directory, and still runs
for clean JUnit of a failed test run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCANNER = REPO / "scripts" / "m8a_ci_scan.py"
SECRET = "synthetic-only-context-password-for-tests"


@pytest.fixture
def ctx(tmp_path):
    (tmp_path / "context.json").write_text(json.dumps({"pg": SECRET, "project": "rasm8atfixture"}), encoding="utf-8")
    return tmp_path


def scan(ctx, *files, publish=True):
    out = ctx / "github-output"
    out.write_text("", encoding="utf-8")
    cmd = [sys.executable, str(SCANNER), "--context", str(ctx)]
    if publish:
        cmd += ["--publish-dir", str(ctx / "publish")]
    r = subprocess.run(cmd + [str(f) for f in files], capture_output=True, text=True,
                       env={**os.environ, "GITHUB_OUTPUT": str(out)})
    assert SECRET not in r.stdout + r.stderr
    published = sorted(p.name for p in (ctx / "publish").iterdir()) if (ctx / "publish").exists() else []
    return r.returncode, out.read_text(encoding="utf-8"), published


def test_clean_file_is_published_with_the_scanned_bytes(ctx):
    junit = ctx / "junit-api.xml"
    junit.write_bytes(b"<testsuites><testcase name='a'/></testsuites>")
    code, output, published = scan(ctx, junit)
    assert (code, output.strip(), published) == (0, "clean=true", ["junit-api.xml"])
    assert (ctx / "publish" / "junit-api.xml").read_bytes() == junit.read_bytes()


def test_failed_tests_with_clean_junit_still_publish_evidence(ctx):
    junit = ctx / "junit-api.xml"
    junit.write_text("<testsuites><testcase name='a'><failure message='assert 1 == 2'/></testcase></testsuites>")
    assert scan(ctx, junit)[0] == 0


# Explicit ids: a payload in the test id would itself land in the JUnit and trip the scan.
@pytest.mark.parametrize("payload", [f"<failure>{SECRET}</failure>",
                                     "<failure>https://ras.test/invite#token=" + "AbCdEfGhIjKlMnOpQrStUvWx</failure>"],
                         ids=["context-secret", "link-token"])
def test_leak_publishes_nothing(ctx, payload):
    junit = ctx / "junit-api.xml"
    junit.write_text(payload, encoding="utf-8")
    code, output, published = scan(ctx, junit)
    assert code == 1 and output == "" and published == []


def test_missing_file_is_not_clean(ctx):
    code, output, published = scan(ctx, ctx / "junit-api.xml")
    assert code == 1 and output == "" and published == []


def test_one_bad_file_blocks_every_file(ctx):
    good, bad = ctx / "good.xml", ctx / "bad.xml"
    good.write_text("<ok/>")
    bad.write_text(SECRET)
    code, output, published = scan(ctx, good, bad)
    assert code == 1 and output == "" and published == []


@pytest.mark.parametrize("context", [None, "not json", json.dumps({"project": "short"})])
def test_unreadable_or_empty_context_fails_closed(tmp_path, context):
    if context is not None:
        (tmp_path / "context.json").write_text(context, encoding="utf-8")
    junit = tmp_path / "junit-api.xml"
    junit.write_text("<ok/>")
    code, output, published = scan(tmp_path, junit)
    assert code == 2 and output == "" and published == []


def test_stale_publish_directory_is_refused(ctx):
    (ctx / "publish").mkdir()
    (ctx / "publish" / "old.xml").write_text("raw")
    junit = ctx / "junit-api.xml"
    junit.write_text("<ok/>")
    code, output, _ = scan(ctx, junit)
    assert code == 2 and output == ""


def _steps():
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    return {s.get("name"): s for s in workflow["jobs"]["api"]["steps"]}


def _upload_runs(condition: str, clean_output: str | None, cancelled: bool = False) -> bool:
    """Evaluate the exact upload expression form used by the workflow."""
    expr = condition.strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2].strip()
    assert re.fullmatch(r"!cancelled\(\) && steps\.scan\.outputs\.clean == 'true'", expr), expr
    return (not cancelled) and clean_output == "true"


def test_workflow_upload_depends_on_a_clean_scan_of_the_published_file():
    steps = _steps()
    scan_step, upload = steps["Scan JUnit for context secrets before upload"], steps["Upload scanned JUnit"]
    assert scan_step["id"] == "scan" and scan_step["if"] == "always()"
    assert "--publish-dir \"$RUNNER_TEMP/m8a-publish\"" in scan_step["run"]
    assert "always()" not in upload["if"]
    assert upload["with"]["path"] == "${{ runner.temp }}/m8a-publish/junit-api.xml"
    assert upload["with"]["if-no-files-found"] == "error"
    assert "continue-on-error" not in upload and "continue-on-error" not in scan_step
    # pytest failed but JUnit scanned clean: publish; scan failed/missing/skipped: never.
    assert _upload_runs(upload["if"], "true")
    for output in (None, "", "false"):
        assert not _upload_runs(upload["if"], output)
    assert not _upload_runs(upload["if"], "true", cancelled=True)
    assert steps["Stop test containers"]["if"] == "always()"
