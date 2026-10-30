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
for dump/restore rounds never cross workers). The credentials are random per
context and shared by the sets.
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
        "PYTHONPATH": os.pathsep.join([str(root / "guard"), str(a.api_dir)]), "PYTHONUTF8": "1"})
    if a.action == "env":
        print(json.dumps(sorted(k for k in env if k.startswith("RAS_"))))
        return 0
    args = [x for x in a.rest if x != "--"]
    chosen = any(x in ("-n", "--numprocesses", "no:xdist") or x.startswith(("-n", "--numprocesses=")) for x in args)
    if len(c["stacks"]) > 1 and not chosen:
        # One worker per isolated stack; worksteal rebalances the uneven file durations.
        args = ["-n", str(len(c["stacks"])), "--dist", "worksteal", *args]
    worker_file = root / "worker-env.json"
    worker_file.write_text(json.dumps(blocks), encoding="utf-8")
    env["RAS_TEST_WORKER_ENV_FILE"] = str(worker_file)
    # Fixture files (journals, case files, synthetic credentials) stay under this context.
    basetemp = root / "pytest-tmp" / str(time.time_ns())
    basetemp.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--basetemp", str(basetemp), *args]
    if a.junit:
        cmd += ["--junitxml", str(a.junit)]
    return subprocess.call(cmd, cwd=a.api_dir, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
