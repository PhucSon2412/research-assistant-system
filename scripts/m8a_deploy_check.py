"""Static contract check of the M8a pilot deployment config (CI and local).

    python scripts/m8a_deploy_check.py [--env-file <filled pilot env>]

Renders deploy/compose.yaml with ``docker compose config`` (placeholders from
deploy/pilot.env.example when no env file is given; nothing is started) and
checks: only the edge publishes ports; internal networks; production-only
API settings validated by the real ``Settings``; C4 role split (runtime
services carry no migrator/recovery password); explicit journal bind mount;
secret files; pricing off; and the Caddyfile trust-boundary lines. It also
checks the dev compose still is a dev-only file. Exit 1 lists every failure.
It is not a running smoke: see scripts/m8a_smoke.py.

M8b adds the operations services (rendered with ``--profile ops``): the journal
replicator (API image, read-only journal, replica write credential only, egress
only), the ``ops`` backup/restore one-shot (the only non-init service with the
migrator password) and the ``ops-check`` alert one-shot (read-only credentials,
no DB/MinIO secret); the edge maintenance flag mount; the pinned ops image
requirements; and the example ops config against ``OpsSettings`` in the image.
"""
from __future__ import annotations

import argparse
import json
import re
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
RUNTIME_SERVICES = ("api", "worker", "beat")
PY_SERVICES = ("migrate", "provision", "recovery-init", "api", "worker", "beat", "journal-replicator")
OPS_SERVICES = ("ops", "ops-check")
MIGRATOR_SERVICES = ("db", "migrate", "provision", "recovery-init", "ops")


def placeholder_env() -> dict[str, str]:
    env = {}
    for line in (DEPLOY / "pilot.env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key] = value
    for key in ("POSTGRES_PASSWORD", "RAS_RUNTIME_DB_PASSWORD", "RAS_RECOVERY_DB_PASSWORD", "REDIS_PASSWORD",
                "MINIO_ROOT_PASSWORD"):
        env[key] = "placeholder-" + key.lower()
    env["RAS_API_IMAGE"], env["RAS_WEB_IMAGE"] = "ras-pilot-api:check", "ras-pilot-web:check"
    env["RAS_OPS_IMAGE"] = "ras-pilot-ops:check"
    return env


def render(env_file: Path) -> dict:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("RAS_", "POSTGRES_", "REDIS_", "MINIO_",
                                                                       "OPENROUTER_"))}
    out = subprocess.run(["docker", "compose", "--env-file", str(env_file), "-p", "ras-m8a-check",
                          "-f", str(DEPLOY / "compose.yaml"), "--profile", "ops", "config", "--format", "json"],
                         capture_output=True, text=True, env=clean, cwd=DEPLOY)
    if out.returncode != 0:
        raise SystemExit("docker compose config failed:\n" + out.stderr)
    return json.loads(out.stdout)


VALIDATE = (
    "import json,sys\n"
    "from pydantic import ValidationError\n"
    "from app.config import Settings\n"
    "try:\n"
    "    s = Settings()\n"
    "except ValidationError as exc:\n"
    "    print(json.dumps(['.'.join(map(str, e['loc'])) + ': ' + e['type'] for e in exc.errors()])); sys.exit(1)\n"
    "print(json.dumps([] if s.environment == 'production' and s.delivery.smtp is not None\n"
    "                 and s.rate_limit.mode == 'redis' else ['not production smtp/redis']))\n"
    "sys.exit(0)\n")


def validate_settings(api_env: dict, image: str | None, failures: list[str], notes: list[str]) -> None:
    """The API's own Settings on the rendered env: inside the image when given,
    else in-process on a POSIX host (container paths are POSIX)."""
    if image:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "api.env"
            env_file.write_text("".join(f"{k}={v}\n" for k, v in api_env.items()), encoding="utf-8")
            out = subprocess.run(["docker", "run", "--rm", "--network", "none", "--env-file", str(env_file),
                                  image, "python", "-c", VALIDATE], capture_output=True, text=True)
        where = "image " + image
    elif os.name == "posix":
        env = {k: v for k, v in os.environ.items() if not k.startswith("RAS_")}
        env.update(api_env, PYTHONPATH=str(REPO / "api"))
        out = subprocess.run([sys.executable, "-c", VALIDATE], capture_output=True, text=True, env=env)
        where = "host"
    else:
        notes.append("NOT-RUN api Settings validation (Windows host; pass --settings-image <api image>)")
        return
    try:
        problems = json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        problems = ["validator crashed: " + (out.stderr.strip().splitlines() or ["?"])[-1]]
    if problems:
        failures.append(f"api settings ({where}) do not validate: " + " / ".join(problems))
    else:
        notes.append(f"api Settings validated in {where}")


OPS_VALIDATE = (
    "import json,sys\n"
    "from app.ops.config import OpsSettings\n"
    "OpsSettings.model_validate(json.load(sys.stdin)); print('ok')\n")


def validate_ops_example(image: str, failures: list[str], notes: list[str]) -> None:
    """deploy/ops.example.json against the real OpsSettings inside the API image (POSIX paths)."""
    out = subprocess.run(["docker", "run", "--rm", "-i", "--network", "none", image, "python", "-c", OPS_VALIDATE],
                         input=(DEPLOY / "ops.example.json").read_text(encoding="utf-8"), capture_output=True, text=True)
    if out.returncode == 0 and out.stdout.strip() == "ok":
        notes.append("ops.example.json validated by OpsSettings in image " + image)
    else:
        failures.append("deploy/ops.example.json does not validate: " + (out.stderr.strip().splitlines() or ["?"])[-1])


def check(cfg: dict, env: dict, failures: list[str], notes: list[str], settings_image: str | None) -> None:
    def need(cond, message):
        if not cond:
            failures.append(message)

    services = cfg["services"]
    for name, svc in services.items():
        ports = svc.get("ports") or []
        if name == "edge":
            published = sorted(int(p["target"]) for p in ports)
            need(published == [80, 443], f"edge must publish exactly 80/443, got {published}")
        else:
            need(not ports, f"{name} must not publish ports")
            need(not svc.get("network_mode"), f"{name} must not use network_mode")
    nets = cfg["networks"]
    need(nets["edge"].get("internal") is True, "edge network must be internal")
    need(nets["backend"].get("internal") is True, "backend network must be internal")
    need(bool(nets["backend"].get("ipam", {}).get("config")), "backend network needs a fixed subnet")
    expect_nets = {"edge": {"public", "edge"}, "web": {"edge", "backend"}, "api": {"backend", "egress"},
                   "worker": {"backend", "egress"}, "beat": {"backend"}, "db": {"backend"}, "redis": {"backend"},
                   "minio": {"backend"}, "migrate": {"backend"}, "provision": {"backend"},
                   "recovery-init": {"backend"}, "journal-replicator": {"egress"}, "ops": {"backend", "egress"},
                   "ops-check": {"egress"}}
    for name, want in expect_nets.items():
        need(set(services[name].get("networks", {})) == want, f"{name} networks must be {sorted(want)}")
    web_ip = services["web"]["networks"]["backend"].get("ipv4_address")
    need(web_ip == env["RAS_WEB_BACKEND_IP"], "web needs its fixed backend address")

    images = {services[s]["image"] for s in PY_SERVICES}
    need(len(images) == 1, f"python services must share one image, got {images}")
    need(all("build" not in services[s] for s in PY_SERVICES if s != "migrate"),
         "only migrate builds the API image")
    need({services[s]["image"] for s in OPS_SERVICES} == {env["RAS_OPS_IMAGE"]}, "ops services run the ops image")
    need(all(services[s].get("profiles") == ["ops"] for s in OPS_SERVICES), "ops one-shots only under profile ops")
    need("build" in services["ops"] and "build" not in services["ops-check"], "only ops builds the ops image")

    def journal_mount(name):
        return [v for v in services[name].get("volumes", []) if v.get("target") == "/var/lib/ras/journal"]

    def secrets_of(name):
        return {s["source"] for s in services[name].get("secrets", [])}

    rep = services["journal-replicator"]
    need([m.get("read_only") for m in journal_mount("journal-replicator")] == [True]
         and journal_mount("journal-replicator")[0]["type"] == "bind", "replicator mounts the journal read-only (bind)")
    need(not any(k.startswith(("RAS_DB__", "RAS_STORAGE__")) for k in rep.get("environment", {})),
         "replicator has no database or object-store settings")
    need(secrets_of("journal-replicator") == {"journal_replica_credentials"}, "replicator: replica write secret only")
    need(rep.get("read_only") is True and "ALL" in (rep.get("cap_drop") or []), "replicator read-only rootfs, no caps")
    need([m.get("read_only") for m in journal_mount("ops-check")] == [True], "ops-check reads the journal only")
    need(secrets_of("ops-check") == {"journal_replica_read_credentials", "backup_target_read_credentials",
                                     "smtp_password"}, "ops-check: read-only destination secrets + SMTP")
    need(secrets_of("ops") == {"backup_target_credentials", "journal_replica_read_credentials"},
         "ops: backup write + replica read secrets")
    for name, svc in services.items():
        values = json.dumps(svc.get("environment", {}))
        if env["POSTGRES_PASSWORD"] in values:
            need(name in MIGRATOR_SERVICES, f"{name} must not receive the migrator password")
        if name in ("journal-replicator", "ops-check"):
            need(env["MINIO_ROOT_PASSWORD"] not in values and env["RAS_RUNTIME_DB_PASSWORD"] not in values,
                 f"{name} must not receive DB/MinIO passwords")
    maint = [v for v in services["edge"].get("volumes", []) if v.get("target") == "/etc/caddy/maintenance"]
    need(len(maint) == 1 and maint[0].get("read_only") is True, "edge mounts the maintenance flag dir read-only")
    reqs = (DEPLOY / "ops" / "requirements.txt").read_text(encoding="utf-8")
    need(re.search(r"^pyrage==\d+\.\d+\.\d+ \\$", reqs, re.M) is not None and reqs.count("--hash=sha256:") >= 2,
         "ops image pins pyrage by version and hash")
    need("--require-hashes" in (DEPLOY / "ops" / "Dockerfile").read_text(encoding="utf-8"), "ops image requires hashes")

    api = services["api"]["environment"]
    fixed = {"RAS_ENVIRONMENT": "production", "RAS_HTTP__COOKIE_SECURE": "true", "RAS_DELIVERY__MODE": "smtp",
             "RAS_RATE_LIMIT__MODE": "redis", "RAS_RATE_LIMIT__KEY_SECRET_FILE": "/run/secrets/rate_limit_key"}
    for key, value in fixed.items():
        need(api.get(key) == value, f"api {key} must be {value}")
    need(json.loads(api["RAS_HTTP__TRUSTED_PROXY_CIDRS"]) == [web_ip + "/32"], "api trusts exactly web /32")
    need(api["RAS_HTTP__PUBLIC_BASE_URL"] == "https://" + env["RAS_PUBLIC_HOST"], "https public base URL")
    for name in ("worker", "beat"):
        need(services[name]["environment"].get("RAS_ENVIRONMENT") == "production", f"{name} production")
    for name, svc in services.items():
        values = json.dumps(svc.get("environment", {}))
        need("dev_outbox" not in values, f"{name} must not mention dev_outbox")
        if name in RUNTIME_SERVICES or name in ("web", "edge"):
            environment = svc.get("environment", {})
            need(env["POSTGRES_PASSWORD"] not in values, f"{name} must not receive the migrator password")
            need(env["RAS_RECOVERY_DB_PASSWORD"] not in values, f"{name} must not receive the recovery password")
            if name in RUNTIME_SERVICES:
                need(environment.get("RAS_DB__USER") == env["RAS_RUNTIME_DB_USER"], f"{name} uses runtime LOGIN")
        for key in ("RAS_EMBEDDING__PRICING_VERIFIED", "RAS_RERANK__PRICING_VERIFIED",
                    "RAS_GENERATION__PRICING_VERIFIED"):
            if key in svc.get("environment", {}):
                need(svc["environment"][key] == "false", f"{name} {key} must default to false")
    for name in ("recovery-init", "api", "worker", "beat"):
        mounts = [v for v in services[name].get("volumes", []) if v.get("target") == "/var/lib/ras/journal"]
        need(len(mounts) == 1 and mounts[0]["type"] == "bind", f"{name} journal must be an explicit bind mount")
    need({s["source"] for s in services["api"].get("secrets", [])} == {"rate_limit_key", "smtp_password"},
         "api secrets")
    need({s["source"] for s in services["edge"].get("secrets", [])} == {"edge_token"}, "edge secret")
    need({s["source"] for s in services["web"].get("secrets", [])} == {"edge_token"}, "web secret")
    need(services["web"]["environment"].get("WEB_EDGE_TOKEN_FILE") == "/run/secrets/edge_token", "web token file")
    # PR #66: storage image pinned by digest; its healthcheck was verified in that image.
    minio = services["minio"]
    need(re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", minio["image"]) is not None, "minio image must be pinned by digest")
    need(minio.get("healthcheck", {}).get("test") == ["CMD", "mc", "ready", "local"], "minio readiness healthcheck")
    need(services["recovery-init"]["depends_on"].get("minio", {}).get("condition") == "service_healthy"
         and services["api"]["depends_on"].get("minio", {}).get("condition") == "service_healthy",
         "storage users wait for a ready MinIO")

    validate_settings({k: v for k, v in api.items() if k.startswith("RAS_")}, settings_image, failures, notes)

    caddy = (DEPLOY / "Caddyfile").read_text(encoding="utf-8")
    for line in ("header_up X-RAS-Client-IP {remote_host}", "header_up X-RAS-Edge-Token {file./run/secrets/edge_token}",
                 "header_up -X-Forwarded-For", "header_up -X-Real-IP", "header_up -Forwarded",
                 "respond @internal 404", "reverse_proxy web:3000", "admin off", "Strict-Transport-Security"):
        need(line in caddy, f"Caddyfile must contain: {line}")
    need("/api/admin/dev-outbox" in caddy and "/openapi.json" in caddy, "Caddyfile blocks dev/admin docs paths")
    for line in ("try_files /enabled", '`{"error":"maintenance"}` 503', "handle_errors 502 503 504"):
        need(line in caddy, f"Caddyfile must contain (M8b maintenance): {line}")
    if settings_image:
        validate_ops_example(settings_image, failures, notes)

    dev = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    need("RAS_ENVIRONMENT: development" in dev and "127.0.0.1:${API_HOST_PORT" in dev,
         "dev compose must stay a development-only file")


def registry_check(cfg: dict, failures: list[str], notes: list[str]) -> None:
    """Every image the deployment pulls must resolve in its registry (not only in a local cache)."""
    pulled = sorted({svc["image"] for svc in cfg["services"].values() if svc.get("pull_policy") != "never"})
    for image in pulled:
        r = subprocess.run(["docker", "manifest", "inspect", image], capture_output=True, text=True)
        if r.returncode == 0:
            notes.append(f"registry ok: {image}")
        else:
            failures.append(f"image not resolvable in its registry: {image}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--env-file", type=Path)
    p.add_argument("--settings-image")
    p.add_argument("--check-registry", action="store_true", help="docker manifest inspect every pulled image")
    a = p.parse_args()
    failures: list[str] = []
    notes: list[str] = []
    if a.env_file:
        env_file = a.env_file
        env = {}
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
        cfg = render(env_file)
    else:
        env = placeholder_env()
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "pilot.env"
            env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
            cfg = render(env_file)
    check(cfg, env, failures, notes, a.settings_image)
    if a.check_registry:
        registry_check(cfg, failures, notes)
    for note in notes:
        print(note)
    for failure in failures:
        print("FAIL " + failure)
    print(f"deploy contract: {'ok' if not failures else str(len(failures)) + ' failure(s)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
