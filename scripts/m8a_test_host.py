"""Isolated PostgreSQL/Redis/MinIO for the M8a API selection (host Conda Python).

    python scripts/m8a_test_host.py --context <dir outside repo> setup
    python scripts/m8a_test_host.py --context <dir> test --junit <file.xml> -- <pytest args>
    python scripts/m8a_test_host.py --context <dir> cleanup

Never reads .env and never touches the dev project ``ras``: every container
carries the context's own Compose project (``rasm8at<hex>``), synthetic
credentials and loopback-only random ports. PostgreSQL runs on tmpfs (speed,
not durability evidence). Outbound HTTP from pytest is refused except to
loopback/test hosts, so no provider or arXiv call can happen.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
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


def context(root: Path) -> dict:
    return json.loads((root / "context.json").read_text(encoding="utf-8"))


def compose(root: Path, c: dict, *args: str) -> int:
    return subprocess.call(["docker", "compose", "-p", c["project"], "-f", str(root / "compose.json"), *args],
                           cwd=root)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--context", type=Path, required=True)
    p.add_argument("action", choices=["setup", "test", "cleanup", "env"])
    p.add_argument("--junit", type=Path)
    p.add_argument("--api-dir", type=Path, default=REPO / "api", help="countercheck copies only")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    a = p.parse_args()
    # REMAINDER also takes options written after the action: lift --junit out.
    if a.rest[:1] == ["--junit"] and len(a.rest) > 1:
        a.junit, a.rest = Path(a.rest[1]), a.rest[2:]
    root = a.context.resolve()
    if root == REPO or REPO in root.parents:
        raise SystemExit("context must be outside the repository")
    if a.action == "setup":
        root.mkdir(parents=True, exist_ok=True)
        if (root / "context.json").exists():
            raise SystemExit("context already set up")
        c = {"project": "rasm8at" + secrets.token_hex(4), **{k: secrets.token_hex(24) for k in (
            "pg", "redis", "minio", "runtime", "recovery", "operator")},
            "pg_port": free_port(), "redis_port": free_port(), "minio_port": free_port()}
        label = {"ras.m8a.context": root.as_posix()}
        spec = {"name": c["project"], "services": {
            "db": {"image": "pgvector/pgvector:pg16", "labels": label, "tmpfs": ["/var/lib/postgresql/data"],
                   "environment": {"POSTGRES_USER": "m8a_admin", "POSTGRES_PASSWORD": c["pg"], "POSTGRES_DB": "m8a"},
                   "ports": [f"127.0.0.1:{c['pg_port']}:5432"],
                   "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U m8a_admin -d m8a"], "interval": "2s",
                                   "retries": 30}},
            "redis": {"image": "redis:7-alpine", "labels": label,
                      "command": ["redis-server", "--save", "", "--appendonly", "no", "--requirepass", c["redis"]],
                      "ports": [f"127.0.0.1:{c['redis_port']}:6379"]},
            "minio": {"image": "quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z", "labels": label,
                      "command": ["server", "/data"], "tmpfs": ["/data"],
                      "environment": {"MINIO_ROOT_USER": "m8a_minio", "MINIO_ROOT_PASSWORD": c["minio"]},
                      "ports": [f"127.0.0.1:{c['minio_port']}:9000"],
                      "healthcheck": {"test": ["CMD", "mc", "ready", "local"], "interval": "2s", "retries": 30}}}}
        (root / "compose.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
        (root / "context.json").write_text(json.dumps(c), encoding="utf-8")
        (root / "guard").mkdir(exist_ok=True)
        (root / "guard" / "sitecustomize.py").write_text(GUARD, encoding="utf-8")
        code = compose(root, c, "up", "-d", "--wait")
        print("isolated test infra " + c["project"] + (" up" if code == 0 else " FAILED"))
        return code
    c = context(root)
    if not c["project"].startswith("rasm8at"):
        raise SystemExit("not an M8a test context")
    if a.action == "cleanup":
        return compose(root, c, "down", "-v", "--remove-orphans")
    from sqlalchemy.engine import URL
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("RAS_", "OPENROUTER_", "POSTGRES_", "MINIO_", "REDIS_"))}
    env.update({
        "RAS_TEST_DATABASE_ADMIN_URL": URL.create("postgresql+psycopg", username="m8a_admin", password=c["pg"],
                                                  host="127.0.0.1", port=c["pg_port"], database="m8a")
        .render_as_string(hide_password=False),
        "RAS_C1_RUNTIME_PASSWORD": c["runtime"], "RAS_C1_RECOVERY_PASSWORD": c["recovery"],
        "RAS_C3_OPERATOR_PASSWORD": c["operator"],
        # Default control-DB connection, as the earlier task hosts provide it.
        "RAS_DB__HOST": "127.0.0.1", "RAS_DB__PORT": str(c["pg_port"]), "RAS_DB__USER": "m8a_admin",
        "RAS_DB__PASSWORD": c["pg"], "RAS_DB__NAME": "m8a",
        "MINIO_ROOT_USER": "m8a_minio", "MINIO_ROOT_PASSWORD": c["minio"], "MINIO_API_HOST_PORT": str(c["minio_port"]),
        "RAS_STORAGE__ENDPOINT_URL": f"http://127.0.0.1:{c['minio_port']}",
        "RAS_STORAGE__ACCESS_KEY": "m8a_minio", "RAS_STORAGE__SECRET_KEY": c["minio"],
        "RAS_REDIS_HOST": "127.0.0.1", "RAS_REDIS_PORT": str(c["redis_port"]), "RAS_REDIS_PASSWORD": c["redis"],
        "RAS_TEST_REDIS_HOST": "127.0.0.1", "RAS_TEST_REDIS_PORT": str(c["redis_port"]),
        "RAS_TEST_REDIS_PASSWORD": c["redis"],
        "RAS_EMBEDDING__API_KEY": "", "RAS_EMBEDDING__PRICING_VERIFIED": "false",
        "PYTHONPATH": os.pathsep.join([str(root / "guard"), str(a.api_dir)]), "PYTHONUTF8": "1"})
    if a.action == "env":
        print(json.dumps(sorted(k for k in env if k.startswith("RAS_"))))
        return 0
    args = [x for x in a.rest if x != "--"]
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *args]
    if a.junit:
        cmd += ["--junitxml", str(a.junit)]
    return subprocess.call(cmd, cwd=a.api_dir, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
