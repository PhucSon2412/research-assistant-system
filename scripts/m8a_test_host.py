"""Isolated PostgreSQL/Redis/MinIO for the API tests (host Conda Python).

    python scripts/m8a_test_host.py --context <dir outside repo> setup [--workers N]
    python scripts/m8a_test_host.py --context <dir> test [--junit <file.xml>] -- <pytest args>
    python scripts/m8a_test_host.py --context <dir> cleanup

Never reads .env and never touches the dev project ``ras`` or the demo
``ras-pilot``: every container carries the context's own Compose project
(``rasm8at<hex>``), synthetic credentials and loopback-only random ports.
PostgreSQL runs on tmpfs (speed, not durability evidence). Outbound HTTP from
pytest is refused except to loopback/test hosts, so no provider or arXiv call
can happen.

M12: ``setup --workers N`` starts N independent PostgreSQL/Redis/MinIO sets in
the one project; ``test`` then runs pytest-xdist with ``-n N`` and gives each
worker its own set (cluster roles, limiter keys, buckets and the DB container
for dump/restore rounds never cross workers), followed by the tests marked
``timing`` alone on set 0; both phases go to one JUnit file. Passing ``-n`` or
``-p no:xdist`` yourself runs one plain pytest instead. The credentials are
random per context and shared by the sets.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from c4_isolation import free_port  # noqa: E402

GUARD = """import httpx, ipaddress
def guard(original):
    def checked(self, request, *a, **kw):
        try:
            allowed = ipaddress.ip_address(request.url.host).is_loopback
        except ValueError:
            allowed = request.url.host in ('testserver', 'web.test', 'localhost')
        if not allowed:
            raise AssertionError('external HTTP blocked in M8a tests')
        return original(self, request, *a, **kw)
    return checked
httpx.HTTPTransport.handle_request = guard(httpx.HTTPTransport.handle_request)
"""


def deploy_image(service: str) -> str:
    """The digest-pinned image of one deploy/compose.yaml service (one source for tests and deploy).

    Reads the ``image:`` line of the service block, plain or as a ``${RAS_*_IMAGE:-<default>}``
    default; anything not pinned by digest is refused rather than replaced by a tag."""
    text = (REPO / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    block = re.search(rf"^  {re.escape(service)}:\n(.*?)(?=^  \S|^\S|\Z)", text, re.M | re.S)
    match = block and re.search(r"^    image: (?:\$\{RAS_\w+_IMAGE:-)?([^\s}]+@sha256:[0-9a-f]{64})\}?\s*$",
                                block.group(1), re.M)
    if not match:
        raise SystemExit(f"deploy/compose.yaml does not pin the {service} image by digest")
    return match.group(1)


def deploy_minio_image() -> str:
    return deploy_image("minio")


def wait_http(url: str, seconds: float) -> bool:
    import time
    import urllib.request
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def context(root: Path) -> dict:
    return json.loads((root / "context.json").read_text(encoding="utf-8"))


def compose(root: Path, c: dict, *args: str) -> int:
    return subprocess.call(["docker", "compose", "-p", c["project"], "-f", str(root / "compose.json"), *args],
                           cwd=root)


def stack_env(root: Path, c: dict, n: int) -> dict[str, str]:
    """What differs between the isolated stacks: the ports and the DB container."""
    from sqlalchemy.engine import URL
    stack = c["stacks"][n]
    container = subprocess.run(["docker", "compose", "-p", c["project"], "-f", str(root / "compose.json"),
                                "ps", "-q", f"db{n}"], cwd=root, capture_output=True, text=True).stdout.strip()
    if not container:
        raise SystemExit(f"isolated db{n} is not running; run setup again")
    return {
        "RAS_TEST_DATABASE_ADMIN_URL": URL.create("postgresql+psycopg", username="task_admin", password=c["pg"],
                                                  host="127.0.0.1", port=stack["pg_port"], database="m8a")
        .render_as_string(hide_password=False),
        "RAS_DB__PORT": str(stack["pg_port"]),
        "MINIO_API_HOST_PORT": str(stack["minio_port"]),
        "RAS_STORAGE__ENDPOINT_URL": f"http://127.0.0.1:{stack['minio_port']}",
        "RAS_REDIS_PORT": str(stack["redis_port"]), "RAS_TEST_REDIS_PORT": str(stack["redis_port"]),
        "RAS_C3_TEST_CONTAINER": container, "RAS_C3_TEST_PROJECT": c["project"]}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--context", type=Path, required=True)
    p.add_argument("action", choices=["setup", "test", "cleanup", "env"])
    p.add_argument("--junit", type=Path)
    p.add_argument("--workers", type=int, default=1, help="setup: isolated stacks, one per xdist worker")
    p.add_argument("--api-dir", type=Path, default=REPO / "api", help="countercheck copies only")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    a = p.parse_args()
    # REMAINDER also takes options written after the action: lift --junit/--workers out.
    while a.rest[:1] in (["--junit"], ["--workers"]) and len(a.rest) > 1:
        if a.rest[0] == "--junit":
            a.junit = Path(a.rest[1])
        else:
            a.workers = int(a.rest[1])
        a.rest = a.rest[2:]
    root = a.context.resolve()
    if root == REPO or REPO in root.parents:
        raise SystemExit("context must be outside the repository")
    if sys.platform == "win32" and len(str(root)) > 40:
        # Journal "aside" files under pytest-tmp then pass MAX_PATH (FileNotFoundError, not a test failure).
        print(f"WARNING: context path has {len(str(root))} characters; use a short one such as %USERPROFILE%\\rt\\m12",
              file=sys.stderr)
    if a.action == "setup":
        root.mkdir(parents=True, exist_ok=True)
        if (root / "context.json").exists():
            raise SystemExit("context already set up")
        if not 1 <= a.workers <= 16:
            raise SystemExit("--workers must be 1..16")
        c = {"project": "rasm8at" + secrets.token_hex(4), "workers": a.workers, **{k: secrets.token_hex(24) for k in (
            "pg", "redis", "minio", "runtime", "recovery", "operator", "operator2")},
            "stacks": [{"pg_port": free_port(), "redis_port": free_port(), "minio_port": free_port()}
                       for _ in range(a.workers)]}
        # ras.task lets the C3 dump/restore rounds check that the DB container is task-owned.
        label = {"ras.m8a.context": root.as_posix(), "ras.task": c["project"]}
        services = {}
        for n, stack in enumerate(c["stacks"]):
            services.update({
                # PostgreSQL/Redis/MinIO: the digests pinned in deploy/compose.yaml, so CI and the
                # pilot deploy exercise the same bytes (M7c, P3 of the PR #68 review). The admin role
                # is task_admin, the name the C3 dump/restore rounds use inside the container.
                f"db{n}": {"image": deploy_image("db"), "labels": label, "tmpfs": ["/var/lib/postgresql/data"],
                           "environment": {"POSTGRES_USER": "task_admin", "POSTGRES_PASSWORD": c["pg"],
                                           "POSTGRES_DB": "m8a"},
                           "ports": [f"127.0.0.1:{stack['pg_port']}:5432"],
                           "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U task_admin -d m8a"], "interval": "2s",
                                           "retries": 30}},
                f"redis{n}": {"image": deploy_image("redis"), "labels": label,
                              "command": ["redis-server", "--save", "", "--appendonly", "no", "--requirepass",
                                          c["redis"]],
                              "ports": [f"127.0.0.1:{stack['redis_port']}:6379"]},
                # Same digest-pinned image as the pilot deploy (PR #66); readiness is the
                # S3 ready endpoint polled below. RAS_TEST_MINIO_IMAGE may name another digest.
                f"minio{n}": {"image": os.environ.get("RAS_TEST_MINIO_IMAGE") or deploy_minio_image(),
                              "labels": label, "command": ["server", "/data"], "tmpfs": ["/data"],
                              "environment": {"MINIO_ROOT_USER": "m8a_minio", "MINIO_ROOT_PASSWORD": c["minio"]},
                              "ports": [f"127.0.0.1:{stack['minio_port']}:9000"]}})
        spec = {"name": c["project"], "services": services}
        (root / "compose.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
        (root / "context.json").write_text(json.dumps(c), encoding="utf-8")
        (root / "guard").mkdir(exist_ok=True)
        (root / "guard" / "sitecustomize.py").write_text(GUARD, encoding="utf-8")
        code = compose(root, c, "up", "-d", "--wait")
        for stack in c["stacks"]:
            if code == 0 and not wait_http(f"http://127.0.0.1:{stack['minio_port']}/minio/health/ready", 90):
                code = 1
        print(f"isolated test infra {c['project']} ({a.workers} stack(s))" + (" up" if code == 0 else " FAILED"))
        return code
    c = context(root)
    if not c["project"].startswith("rasm8at"):
        raise SystemExit("not an M8a test context")
    if a.action == "cleanup":
        return compose(root, c, "down", "-v", "--remove-orphans")
    if "stacks" not in c:
        raise SystemExit("context set up before M12; run cleanup and setup again")
    blocks = {f"gw{n}": stack_env(root, c, n) for n in range(len(c["stacks"]))}
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("RAS_", "OPENROUTER_", "POSTGRES_", "MINIO_", "REDIS_", "PYTEST_XDIST_"))}
    env.update(blocks["gw0"])  # without xdist (-n 0, -p no:xdist) every test uses stack 0
    env.update({
        "RAS_C1_RUNTIME_PASSWORD": c["runtime"], "RAS_C1_RECOVERY_PASSWORD": c["recovery"],
        "RAS_C3_OPERATOR_PASSWORD": c["operator"], "RAS_C3_OPERATOR2_PASSWORD": c["operator2"],
        # Default control-DB connection, as the earlier task hosts provide it (port per stack).
        "RAS_DB__HOST": "127.0.0.1", "RAS_DB__USER": "task_admin", "RAS_DB__PASSWORD": c["pg"], "RAS_DB__NAME": "m8a",
        "MINIO_ROOT_USER": "m8a_minio", "MINIO_ROOT_PASSWORD": c["minio"],
        "RAS_STORAGE__ACCESS_KEY": "m8a_minio", "RAS_STORAGE__SECRET_KEY": c["minio"],
        "RAS_REDIS_HOST": "127.0.0.1", "RAS_REDIS_PASSWORD": c["redis"],
        "RAS_TEST_REDIS_HOST": "127.0.0.1", "RAS_TEST_REDIS_PASSWORD": c["redis"],
        "RAS_EMBEDDING__API_KEY": "", "RAS_EMBEDDING__PRICING_VERIFIED": "false",
        # The repository root last: some suites import tooling as `from scripts import ...`.
        "PYTHONPATH": os.pathsep.join([str(root / "guard"), str(a.api_dir), str(REPO)]), "PYTHONUTF8": "1"})
    if a.action == "env":
        print(json.dumps(sorted(k for k in env if k.startswith("RAS_"))))
        return 0
    args = [x for x in a.rest if x != "--"]
    chosen = any(x in ("-n", "--numprocesses", "no:xdist") or x.startswith(("-n", "--numprocesses=")) for x in args)
    worker_file = root / "worker-env.json"
    worker_file.write_text(json.dumps(blocks), encoding="utf-8")
    env["RAS_TEST_WORKER_ENV_FILE"] = str(worker_file)
    # Fixture files (journals, case files, synthetic credentials) stay under this context.
    basetemp = root / "pytest-tmp" / str(time.time_ns())
    basetemp.mkdir(parents=True)
    base =[sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"]

    def pytest(extra: list[str], temp: Path, junit: Path | None) -> int:
        return subprocess.call(base + ["--basetemp", str(temp), *extra] + (["--junitxml", str(junit)] if junit else []),
                               cwd=a.api_dir, env=env)

    if len(c["stacks"]) == 1 or chosen:
        return pytest(args, basetemp, a.junit)
    # One xdist worker per isolated stack (worksteal rebalances the uneven file durations), then the
    # tests marked `timing` (wall-clock bounds) alone, without parallel load; one merged JUnit.
    expr, rest = marker_expression(args)
    timing_junit = a.junit.with_name(a.junit.stem + "-timing.xml") if a.junit else None
    first = pytest(["-n", str(len(c["stacks"])), "--dist", "worksteal", "-m",
                    f"({expr}) and not timing" if expr else "not timing", *rest], basetemp / "parallel", a.junit)
    second = pytest(["-p", "no:xdist", "-m", f"({expr}) and timing" if expr else "timing", *without_dist(rest)],
                    basetemp / "timing", timing_junit)
    if a.junit and timing_junit.exists():
        merge_junit(a.junit, timing_junit)
    if first == second == 5:
        return 5  # nothing collected in either phase
    return next((code for code in (first, second) if code not in (0, 5)), 0)


def without_dist(args: list[str]) -> list[str]:
    """Remove xdist scheduling flags the serial timing phase does not know."""
    out, skip = [], False
    for x in args:
        if skip:
            skip = False
        elif x in ("--dist", "--maxschedchunk"):
            skip = True
        elif not x.startswith(("--dist=", "--maxschedchunk=")):
            out.append(x)
    return out


def marker_expression(args: list[str]) -> tuple[str | None, list[str]]:
    """The last ``-m EXPR`` of the pytest arguments (as pytest uses it) and the arguments without any."""
    expr, rest, skip = None, [], False
    for n, x in enumerate(args):
        if skip:
            skip = False
        elif x == "-m" and n + 1 < len(args):
            expr, skip = args[n + 1], True
        elif x.startswith("-m") and len(x) > 2:
            expr = x[2:].lstrip("=")
        else:
            rest.append(x)
    return expr, rest


def merge_junit(target: Path, extra: Path) -> None:
    import xml.etree.ElementTree as ET
    if not target.exists():
        extra.replace(target)
        return
    tree = ET.parse(target)
    tree.getroot().extend(list(ET.parse(extra).getroot()))
    tree.write(target, encoding="utf-8", xml_declaration=True)
    extra.unlink()

if __name__ == "__main__":
    raise SystemExit(main())
