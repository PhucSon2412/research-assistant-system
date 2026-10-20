"""M8b isolated operations drills on the real deploy/ topology (host Conda Python; never .env, never ``ras``).

    python scripts/m8b_drill.py --context <dir outside repo> setup        # PKI, secrets, age keys, offsite MinIO x2
    python scripts/m8b_drill.py --context <dir> build                     # API/web/ops images (+ M8a API for upgrade)
    python scripts/m8b_drill.py --context <dir> run <phase> [...]         # upgrade install replica backup disaster alerts drill
    python scripts/m8b_drill.py --context <dir> browser <project-key>     # Chrome over HTTPS (scripts/m8b_browser.py)
    python scripts/m8b_drill.py --context <dir> cleanup                   # only this context's labelled projects

Two MinIO containers in their own Compose project stand in for the two OFF-HOST destinations
(journal replica with object lock, backups with versioning). They run on the same machine:
that only simulates another failure domain, it proves nothing about one. Deploy projects reach
them over a dedicated internal network; application networks stay internal (no route out), the
provider key is empty, pricing false, SMTP is the isolated TLS capture. Every container/volume
carries ``ras.m8b.context=<context>``; cleanup removes only those after checking the label.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from c4_isolation import free_port  # noqa: E402

PY = sys.executable
HOST = "ras.test"
ORIGIN = "https://" + HOST
SMTP_HOST = "smtp.pilot.test"
PREFIX = "rasm8b"
LABEL = "ras.m8b.context"
OPS_RECIPIENTS = ["oncall-a@ops.test", "oncall-b@ops.test"]
M8A_BASE = "0de10f4c7d119bd0190a673a52cc99b0e98ca99f"
MINIO_IMAGE = re.search(r"\$\{RAS_MINIO_IMAGE:-([^}]+@sha256:[0-9a-f]{64})\}",
                        (REPO / "deploy/compose.yaml").read_text(encoding="utf-8")).group(1)


def guard_context(path: Path) -> Path:
    root = path.resolve()
    if root == REPO or REPO in root.parents:
        raise SystemExit("context must be outside the repository")
    return root


def clean_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if not k.upper().startswith(("RAS_", "POSTGRES_", "REDIS_", "MINIO_", "OPENROUTER_", "COMPOSE_"))}


def run(cmd, **kw):
    kw.setdefault("capture_output", True)
    if "input" not in kw or isinstance(kw.get("input"), str):
        kw.setdefault("encoding", "utf-8")
        kw.setdefault("errors", "replace")
    result = subprocess.run(cmd, env=clean_env(), **kw)
    if result.stdout is None:
        result.stdout = ""
    if result.stderr is None:
        result.stderr = ""
    return result


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Checks:
    """Every check is kept (pass and fail) with its phase; a failed check stops the phase."""

    def __init__(self, root: Path, phase: str):
        self.dir = root / "evidence" / f"{phase}-{time.strftime('%Y%m%dT%H%M%S')}"
        self.dir.mkdir(parents=True)
        self.items, self.metrics = [], {}

    def __call__(self, label, ok, detail=None):
        self.items.append({"check": label, "pass": bool(ok), "at": now_iso(),
                           **({"detail": detail} if detail is not None else {})})
        self.save()
        print(("PASS " if ok else "FAIL ") + label + ("" if ok or detail is None else f" :: {detail}"), flush=True)
        if not ok:
            raise SystemExit(f"check failed: {label}")

    def metric(self, name, value):
        self.metrics[name] = value
        self.save()
        print(f"METRIC {name} = {value}", flush=True)

    def save(self):
        (self.dir / "result.json").write_text(json.dumps({"checks": self.items, "metrics": self.metrics}, indent=1,
                                                         ensure_ascii=False), encoding="utf-8")


# --- context ----------------------------------------------------------------------------------

def ctx(root: Path) -> dict:
    return json.loads((root / "context.json").read_text(encoding="utf-8"))


def save_ctx(root: Path, c: dict) -> None:
    (root / "context.json").write_text(json.dumps(c, indent=1), encoding="utf-8")


def used_subnets() -> list:
    nets = []
    for name in run(["docker", "network", "ls", "-q"]).stdout.split():
        for net in json.loads(run(["docker", "network", "inspect", name]).stdout or "[]"):
            for cfg in (net.get("IPAM") or {}).get("Config") or []:
                if cfg.get("Subnet"):
                    nets.append(ipaddress.ip_network(cfg["Subnet"], strict=False))
    return nets


def free_subnet(taken: list) -> ipaddress.IPv4Network:
    for _ in range(300):
        subnet = ipaddress.ip_network(f"172.30.{10 + secrets.randbelow(240)}.0/24")
        if not any(subnet.overlaps(n) for n in taken):
            taken.append(subnet)
            return subnet
    raise SystemExit("no free private /24")


def offsite_compose(root: Path, c: dict, *args, **kw):
    return run(["docker", "compose", "-p", c["offsite"]["project"], "-f", str(root / "offsite" / "compose.json"),
                *args], cwd=root, **kw)


def offsite_client(c: dict, which: str):
    import boto3
    from botocore.config import Config
    o = c["offsite"][which]
    return boto3.client("s3", endpoint_url=f"http://127.0.0.1:{o['port']}", aws_access_key_id=o["root_user"],
                        aws_secret_access_key=o["root_password"], region_name="us-east-1",
                        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 2}))


def setup(root: Path) -> int:
    from tests.m8a_tls import make_ca, make_leaf
    import pyrage
    root.mkdir(parents=True, exist_ok=True)
    if (root / "context.json").exists():
        raise SystemExit("context already set up")
    tag = secrets.token_hex(3)
    for d in ("pki", "tls.d", "trust", "secrets", "operator", "capture", "evidence", "projects", "offsite"):
        (root / d).mkdir()
    ca = make_ca(root / "pki", "ca", "RAS M8b drill test root")
    site = make_leaf(root / "pki", ca, "site", [HOST], days=30)
    make_leaf(root / "pki", ca, "smtp", [SMTP_HOST], days=30)
    shutil.copyfile(site.cert, root / "tls.d" / "site.crt")
    shutil.copyfile(site.key, root / "tls.d" / "site.key")
    (root / "tls.d" / "site.caddy").write_text("tls /etc/caddy/tls.d/site.crt /etc/caddy/tls.d/site.key\n")
    shutil.copyfile(ca.cert, root / "trust" / "ca.crt")
    # Operator key pair: only the PUBLIC key goes into the server's ops config; the identity stays
    # in operator/ (stands for the operator's own machine) and is mounted for a restore run only.
    identity = pyrage.x25519.Identity.generate()
    (root / "operator" / "identity.txt").write_text(str(identity) + "\n", encoding="ascii")
    recipient = str(identity.to_public())
    offsite = {"project": f"{PREFIX}off{tag}"}
    for which in ("journal", "backup"):
        offsite[which] = {"port": free_port(), "root_user": f"m8b-{which}-root", "root_password": secrets.token_hex(20),
                          "rw_user": f"m8b-{which}-rw", "rw_password": secrets.token_hex(20),
                          "ro_user": f"m8b-{which}-ro", "ro_password": secrets.token_hex(20)}
    label = {LABEL: root.as_posix()}
    spec = {"name": offsite["project"],
            "networks": {"offsite": {"internal": True}, "hostside": {}},
            "volumes": {"journal-data": {"labels": label}, "backup-data": {"labels": label}},
            "services": {f"offsite-{which}": {
                "image": MINIO_IMAGE, "command": ["server", "/data"], "labels": label,
                "environment": {"MINIO_ROOT_USER": offsite[which]["root_user"],
                                "MINIO_ROOT_PASSWORD": offsite[which]["root_password"]},
                "volumes": [f"{which}-data:/data"],
                "networks": {"offsite": {"aliases": [f"offsite-{which}"]}, "hostside": {}},
                "ports": [f"127.0.0.1:{offsite[which]['port']}:9000"],
                "healthcheck": {"test": ["CMD", "mc", "ready", "local"], "interval": "3s", "retries": 30}}
                for which in ("journal", "backup")}}
    (root / "offsite" / "compose.json").write_text(json.dumps(spec, indent=1), encoding="utf-8")
    secret = {"edge_token": secrets.token_urlsafe(32), "rate_limit_key": secrets.token_hex(32),
              "smtp_password": secrets.token_urlsafe(24)}
    for which, name in (("journal", "journal_replica"), ("backup", "backup_target")):
        o = offsite[which]
        secret[f"{name}_credentials"] = json.dumps({"access_key_id": o["rw_user"], "secret_access_key": o["rw_password"]})
        secret[f"{name}_read_credentials"] = json.dumps({"access_key_id": o["ro_user"],
                                                         "secret_access_key": o["ro_password"]})
    for name, value in secret.items():
        (root / "secrets" / name).write_text(value + "\n", encoding="utf-8")
    c = {"tag": tag, "offsite": offsite, "recipient": recipient, "projects": {},
         "images": {"api": f"{PREFIX}{tag}-api:local", "web": f"{PREFIX}{tag}-web:local",
                    "ops": f"{PREFIX}{tag}-ops:local", "m8a_api": f"{PREFIX}{tag}-m8a-api:local"},
         "subnets": [], "pw": {k: secrets.token_hex(24) for k in ("pg", "runtime", "recovery", "redis", "minio")}}
    save_ctx(root, c)
    code = offsite_compose(root, c, "up", "-d", "--wait").returncode
    if code != 0:
        raise SystemExit("offsite MinIO did not start")
    for which in ("journal", "backup"):
        o = offsite[which]
        name = f"{offsite['project']}-offsite-{which}-1"

        def mc(*args):
            r = run(["docker", "exec", name, "mc", *args])
            if r.returncode != 0:
                raise SystemExit(f"mc {args[:3]} failed: {r.stderr[-300:]}")
        mc("alias", "set", "local", "http://127.0.0.1:9000", o["root_user"], o["root_password"])
        bucket = f"m8b-{which}-{tag}"
        if which == "journal":  # versioned + object lock (governance, 1 day): shipped versions cannot be deleted
            mc("mb", "--with-lock", f"local/{bucket}")
            mc("retention", "set", "--default", "GOVERNANCE", "1d", f"local/{bucket}")
        else:
            mc("mb", f"local/{bucket}")
            mc("version", "enable", f"local/{bucket}")
        # Least privilege: the replica writer cannot delete (the replica is never pruned); the backup
        # writer may delete (retention); the read-only users can only list/read.
        write_actions = ["s3:GetObject", "s3:PutObject"] + (["s3:DeleteObject"] if which == "backup" else [])
        for role, actions in (("rw", write_actions), ("ro", ["s3:GetObject"])):
            policy = {"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": ["s3:GetBucketVersioning", "s3:GetBucketObjectLockConfiguration",
                                               "s3:ListBucket"], "Resource": [f"arn:aws:s3:::{bucket}"]},
                {"Effect": "Allow", "Action": actions, "Resource": [f"arn:aws:s3:::{bucket}/*"]}]}
            policy_file = root / "offsite" / f"{which}-{role}-policy.json"
            policy_file.write_text(json.dumps(policy), encoding="utf-8")
            run(["docker", "cp", str(policy_file), f"{name}:/tmp/{role}.json"])
            mc("admin", "policy", "create", "local", f"m8b-{which}-{role}", f"/tmp/{role}.json")
            mc("admin", "user", "add", "local", o[f"{role}_user"], o[f"{role}_password"])
            mc("admin", "policy", "attach", "local", f"m8b-{which}-{role}", "--user", o[f"{role}_user"])
        o["bucket"] = bucket
    save_ctx(root, c)
    for key in ("i1", "u1"):
        write_ops_config(root, c, key)
    print(f"context {root} ready; offsite project {offsite['project']} (same machine: simulation only)")
    return 0


def write_ops_config(root: Path, c: dict, installation: str, **alert_over) -> Path:
    """One ops config per installation (its own replica/backup prefix). Test thresholds are short."""
    d = root / f"ops-config-{installation}"
    d.mkdir(exist_ok=True)
    (d / "age-recipients.txt").write_text("# operator public key (test)\n" + c["recipient"] + "\n", encoding="ascii")
    o = c["offsite"]
    target = lambda which, name: {  # noqa: E731
        "endpoint_url": f"http://offsite-{which}:9000", "bucket": o[which]["bucket"], "prefix": f"pilot-{installation}",
        "credentials_file": f"/run/secrets/{name}_credentials",
        "read_credentials_file": f"/run/secrets/{name}_read_credentials", "connect_timeout_seconds": 3,
        "read_timeout_seconds": 15}
    alerts = {"recipients": OPS_RECIPIENTS, "subject_prefix": "[RAS m8b drill]", "renotify_hours": 24,
              "smtp": {"host": SMTP_HOST, "port": 465, "security": "implicit_tls", "sender": "alerts@" + HOST,
                       "sender_name": "RAS drill alerts", "username": "pilot-relay",
                       "password_file": "/run/secrets/smtp_password", "ca_file": "/etc/ras/trust/ca.crt",
                       "connect_timeout_seconds": 3, "total_timeout_seconds": 8},
              "replica_lag_seconds": 30, "replica_status_stale_seconds": 30, "backup_max_age_hours": 2,
              "drill_max_age_days": 31, "min_free_bytes_journal": 1 << 30, "min_free_bytes_staging": 2 << 30,
              "fence_on_journal_rollback": True, **alert_over}
    cfg = {"journal_replica": target("journal", "journal_replica"), "backup_target": target("backup", "backup_target"),
           "backup": {"recipients_file": "/etc/ras/ops/age-recipients.txt", "retention_days": 30, "min_keep": 3,
                      "staging_dir": "/var/lib/ras/staging"},
           "replicator": {"interval_seconds": 5, "max_segment_records": 500, "max_segments_per_cycle": 20},
           "alerts": alerts,
           "window": {"stop_timeout_seconds": 120, "capture_timeout_seconds": 900, "start_timeout_seconds": 300,
                      "readiness_timeout_seconds": 180, "ship_timeout_seconds": 900, "restore_timeout_seconds": 1200,
                      "step_timeout_seconds": 600},
           "state_dir": "/var/lib/ras/ops-state"}
    (d / "ops.json").write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    return d


# --- deploy projects ---------------------------------------------------------------------------

class Project:
    """One deploy/ Compose project of this context, with its own dirs/ports/subnet."""

    def __init__(self, root: Path, c: dict, key: str):
        self.root, self.c, self.key = root, c, key
        self.info = c["projects"][key]
        self.name = self.info["project"]
        self.dir = root / "projects" / key
        self.env = self.dir / "pilot.env"
        self.override = self.dir / "override.json"
        self.compose_file = Path(self.info["compose_file"])

    def files(self) -> list[str]:
        return ["-f", str(self.compose_file), "-f", str(self.override)]

    def compose(self, *args, timeout=900, **kw):
        cmd = ["docker", "compose", "--env-file", str(self.env), "-p", self.name, *self.files(), *args]
        return run(cmd, cwd=self.compose_file.parent, timeout=timeout, **kw)

    def ras_ops(self, *args, timeout=3600):
        cmd = [PY, str(REPO / "scripts/ras_ops.py"), args[0], "--env-file", str(self.env), "--project", self.name,
               "--compose-file", str(self.compose_file), "--compose-file", str(self.override), *args[1:]]
        started = time.monotonic()
        r = run(cmd, timeout=timeout)
        out = {}
        for line in reversed(r.stdout.splitlines()):
            if line.strip().startswith("{"):
                out = json.loads(line)
                break
        (self.dir / "ras_ops.log").open("a", encoding="utf-8").write(
            json.dumps({"at": now_iso(), "args": list(args), "exit": r.returncode, "out": out,
                        "seconds": round(time.monotonic() - started, 2)}) + "\n")
        return r.returncode, out

    def readyz(self) -> str:
        r = self.compose("exec", "-T", "api", "python", "-c",
                         "import urllib.request,urllib.error\n"
                         "try: print(urllib.request.urlopen('http://127.0.0.1:8000/readyz',timeout=5).status)\n"
                         "except urllib.error.HTTPError as e: print(e.code)\nexcept Exception: print(0)", timeout=60)
        return r.stdout.strip() or "unavailable"

    @property
    def journal(self) -> Path:
        return self.dir / "journal" / "revocations.jsonl"

    def state(self, name) -> dict:
        try:
            return json.loads((self.dir / "ops-state" / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}


def new_project(root: Path, c: dict, key: str, *, installation: str, m8a: bool = False) -> Project:
    """Create dirs, env and override for a new deploy project (nothing is started)."""
    if key in c["projects"]:
        raise SystemExit(f"project {key} exists")
    name = f"{PREFIX}{key}{c['tag']}"
    d = root / "projects" / key
    for sub in ("journal", "ops-state", "staging", "maintenance"):
        (d / sub).mkdir(parents=True)
    taken = used_subnets() + [ipaddress.ip_network(s) for s in c["subnets"]]
    subnet = free_subnet(taken)
    c["subnets"].append(str(subnet))
    ports = {"http": free_port(), "https": free_port()}
    compose_file = (root / "m8a-worktree" / "deploy" / "compose.yaml") if m8a else (REPO / "deploy" / "compose.yaml")
    pw = c["pw"]
    env = {
        "RAS_DEPLOY_PROJECT": name, "RAS_API_IMAGE": c["images"]["m8a_api" if m8a else "api"],
        "RAS_WEB_IMAGE": c["images"]["web"], "RAS_OPS_IMAGE": c["images"]["ops"],
        "RAS_PUBLIC_HOST": HOST, "RAS_EDGE_BIND": "127.0.0.1", "RAS_EDGE_HTTP_PORT": str(ports["http"]),
        "RAS_EDGE_HTTPS_PORT": str(ports["https"]), "RAS_HSTS_MAX_AGE": "300",
        "RAS_CADDY_TLS_DIR": (root / "tls.d").as_posix(), "RAS_BACKEND_SUBNET": str(subnet),
        "RAS_WEB_BACKEND_IP": str(subnet.network_address + 10), "RAS_SECRETS_DIR": (root / "secrets").as_posix(),
        "RAS_JOURNAL_DIR": (d / "journal").as_posix(), "RAS_TRUST_DIR": (root / "trust").as_posix(),
        "RAS_OPS_CONFIG_DIR": (root / f"ops-config-{installation}").as_posix(),
        "RAS_OPS_STATE_DIR": (d / "ops-state").as_posix(), "RAS_OPS_STAGING_DIR": (d / "staging").as_posix(),
        "RAS_MAINTENANCE_DIR": (d / "maintenance").as_posix(),
        "POSTGRES_USER": "m8b_migrator", "POSTGRES_DB": "m8b_pilot", "POSTGRES_PASSWORD": pw["pg"],
        "RAS_RUNTIME_DB_USER": "m8b_runtime", "RAS_RUNTIME_DB_PASSWORD": pw["runtime"],
        "RAS_RECOVERY_DB_USER": "m8b_recovery", "RAS_RECOVERY_DB_PASSWORD": pw["recovery"],
        "RAS_DEFINER_DB_ROLE": "m8b_definer", "REDIS_PASSWORD": pw["redis"],
        "MINIO_ROOT_USER": "m8b-minio", "MINIO_ROOT_PASSWORD": pw["minio"],
        "RAS_SMTP_HOST": SMTP_HOST, "RAS_SMTP_PORT": "465", "RAS_SMTP_SECURITY": "implicit_tls",
        "RAS_SMTP_SENDER": "no-reply@" + HOST, "RAS_SMTP_SENDER_NAME": "RAS m8b drill",
        "RAS_SMTP_USERNAME": "pilot-relay", "RAS_SMTP_PASSWORD_FILE": "/run/secrets/smtp_password",
        "RAS_SMTP_CA_FILE": "/etc/ras/trust/ca.crt", "RAS_SMTP_CONNECT_TIMEOUT_SECONDS": "3",
        "RAS_SMTP_TOTAL_TIMEOUT_SECONDS": "6", "OPENROUTER_API_KEY": "",
        "RAS_EMBEDDING__PRICING_VERIFIED": "false", "RAS_RERANK__PRICING_VERIFIED": "false",
        "RAS_GENERATION__PRICING_VERIFIED": "false", "RAS_DISCOVERY__ENABLED": "false"}
    if m8a:  # the M8a env file knows none of the M8b names
        for k in ("RAS_OPS_IMAGE", "RAS_OPS_CONFIG_DIR", "RAS_OPS_STATE_DIR", "RAS_OPS_STAGING_DIR",
                  "RAS_MAINTENANCE_DIR"):
            env.pop(k)
    (d / "pilot.env").write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    write_override(root, c, d, m8a=m8a)
    c["projects"][key] = {"project": name, "ports": ports, "subnet": str(subnet), "installation": installation,
                          "compose_file": str(compose_file)}
    save_ctx(root, c)
    return Project(root, c, key)


def write_override(root: Path, c: dict, d: Path, *, m8a: bool) -> None:
    label = {LABEL: root.as_posix()}
    # Short cleanup deadline so "cleanup overdue" is observable; poll allows 4 attempts (Retention rule).
    retention = {"RAS_RETENTION__DELETED_CLEANUP_DEADLINE": "PT60S", "RAS_RETENTION__CLEANUP_POLL_SECONDS": "15"}
    services = {name: {"labels": label} for name in ("db", "redis", "minio", "migrate", "provision", "recovery-init",
                                                     "web")}
    services["edge"] = {"labels": label, "networks": {"public": {"aliases": [HOST]}, "edge": {}}}
    for name in ("api", "worker", "beat"):
        services[name] = {"labels": label, "environment": retention}
    services["smtp"] = {"image": "python:3.12-alpine", "labels": label,
                        "command": ["python", "/capture/smtp_capture.py", "--cert", "/pki/smtp.crt", "--key",
                                    "/pki/smtp.key", "--port", "465", "--mode", "implicit_tls", "--store", "/store",
                                    "--auth-user", "pilot-relay", "--auth-password-file", "/run/secrets/smtp_password"],
                        "volumes": [(REPO / "api/tests/smtp_capture.py").as_posix() + ":/capture/smtp_capture.py:ro",
                                    (root / "pki").as_posix() + ":/pki:ro", (root / "capture").as_posix() + ":/store"],
                        "secrets": ["smtp_password"], "networks": {"egress": {"aliases": [SMTP_HOST]}},
                        "restart": "unless-stopped"}
    networks = {"egress": {"internal": True}}
    if not m8a:
        networks["offsite"] = {"external": True, "name": f"{c['offsite']['project']}_offsite"}
        services["journal-replicator"] = {"labels": label, "networks": {"egress": {}, "offsite": {}}}
        services["ops"] = {"labels": label, "networks": {"backend": {}, "egress": {}, "offsite": {}}}
        services["ops-check"] = {"labels": label, "networks": {"egress": {}, "offsite": {}}}
    (d / "override.json").write_text(json.dumps({"networks": networks, "services": services}, indent=1),
                                     encoding="utf-8")


def build(root: Path) -> int:
    c = ctx(root)
    log = root / "evidence" / "build.log"
    probe_key = "build-probe"
    if probe_key not in c["projects"]:
        new_project(root, c, probe_key, installation="i1")
    p = Project(root, c, probe_key)
    for args in (("build", "migrate"), ("build", "web"), ("--profile", "ops", "build", "ops")):
        r = p.compose(*args, timeout=3600)
        log.open("a", encoding="utf-8").write(f"$ compose {' '.join(args)} -> {r.returncode}\n{r.stdout[-3000:]}\n"
                                              f"{r.stderr[-3000:]}\n")
        if r.returncode != 0:
            raise SystemExit(f"build {args} failed (see {log})")
    # M8a API image for the upgrade drill, from a worktree of the merged base (outside the repo).
    wt = root / "m8a-worktree"
    if not wt.exists():
        r = run(["git", "-C", str(REPO), "worktree", "add", "--detach", str(wt), M8A_BASE])
        if r.returncode != 0:
            raise SystemExit("worktree failed: " + r.stderr)
    r = run(["docker", "build", "-t", c["images"]["m8a_api"], "--label", f"{LABEL}={root.as_posix()}",
             str(wt / "api")], timeout=3600)
    log.open("a", encoding="utf-8").write(f"$ docker build m8a api -> {r.returncode}\n{r.stderr[-3000:]}\n")
    if r.returncode != 0:
        raise SystemExit("M8a API build failed")
    digests = {k: run(["docker", "image", "inspect", v, "--format", "{{.Id}}"]).stdout.strip()
               for k, v in c["images"].items()}
    c["image_ids"] = digests
    del c["projects"][probe_key]
    shutil.rmtree(root / "projects" / probe_key)
    save_ctx(root, c)
    print(json.dumps(digests, indent=1))
    return 0


def browser(root: Path, key: str) -> int:
    """Chrome inside rasm8a-browser:local on the project's public network, after the host-loss restore."""
    c = ctx(root)
    p = Project(root, c, key)
    data = json.loads((root / "run.json").read_text(encoding="utf-8"))
    out = root / "evidence" / f"chrome-{key}-{time.strftime('%Y%m%dT%H%M%S')}"
    out.mkdir(parents=True)
    acc = data["accounts"][p.info["installation"]]
    info = data["i1"]["browser"]
    (out / "input.json").write_text(json.dumps({"origin": ORIGIN, "admin": acc["admin"], "a": acc["a"],
                                                "revoked_session": info["revoked_session"],
                                                "deleted_items": [info["deleted_item"], info["deleted_item_before_backup"]],
                                                "keep": info["keep"]}), encoding="utf-8")
    cmd = ["docker", "run", "--rm", "--network", f"{p.name}_public", "--label", f"{LABEL}={root.as_posix()}",
           "--shm-size", "1g", "-v", (REPO / "scripts/m8b_browser.py").as_posix() + ":/m8b_browser.py:ro",
           "-v", (root / "pki/ca.crt").as_posix() + ":/pki/ca.crt:ro", "-v", out.as_posix() + ":/out",
           "rasm8a-browser:local", "python3", "/m8b_browser.py", "--input", "/out/input.json", "--out", "/out"]
    result = subprocess.run(cmd, text=True, env=clean_env())
    print(f"Chrome exit {result.returncode}; evidence {out}")
    return result.returncode


def cleanup(root: Path) -> int:
    """Only this context's projects: every container/volume must carry ras.m8b.context=<root>."""
    c = ctx(root)
    projects = [info["project"] for info in c["projects"].values()] + [c["offsite"]["project"]]
    for name in projects:
        ids = run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={name}"]).stdout.split()
        for identifier in ids:
            labels = json.loads(run(["docker", "inspect", identifier, "--format", "{{json .Config.Labels}}"]).stdout)
            if labels.get(LABEL) != root.as_posix() and not labels.get("com.docker.compose.oneoff"):
                raise SystemExit(f"{name}: container without this context's label; refusing cleanup")
        vols = run(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={name}"]).stdout.split()
        for volume in vols:
            users = set(run(["docker", "ps", "-aq", "--filter", f"volume={volume}"]).stdout.split())
            if not users <= set(ids):
                raise SystemExit(f"{volume}: foreign consumer; refusing cleanup")
    for key, info in c["projects"].items():
        p = Project(root, c, key)
        r = p.compose("--profile", "ops", "down", "-v", "--remove-orphans", timeout=900)
        print(f"down {info['project']}: {r.returncode}")
    r = offsite_compose(root, c, "down", "-v", "--remove-orphans")
    print(f"down offsite: {r.returncode}")
    for tag in c["images"].values():
        run(["docker", "image", "rm", tag])
    if (root / "m8a-worktree").exists():
        run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(root / "m8a-worktree")])
    return 0


def forget(root: Path, key: str) -> int:
    """Tear down one project of this context (label-checked) and drop it, to rerun a phase."""
    c = ctx(root)
    p = Project(root, c, key)
    ids = run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={p.name}"]).stdout.split()
    for identifier in ids:
        labels = json.loads(run(["docker", "inspect", identifier, "--format", "{{json .Config.Labels}}"]).stdout)
        if labels.get(LABEL) != root.as_posix() and not labels.get("com.docker.compose.oneoff"):
            raise SystemExit("container without this context's label; refusing")
    r = p.compose("--profile", "ops", "down", "-v", "--remove-orphans", timeout=900)
    if r.returncode != 0:
        raise SystemExit("down failed: " + r.stderr[-300:])
    shutil.move(str(p.dir), str(root / "evidence" / f"forgotten-{key}-{time.strftime('%H%M%S')}"))
    del c["projects"][key]
    save_ctx(root, c)
    print(f"forgot {key} ({p.name})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("action", choices=["setup", "build", "run", "browser", "cleanup", "forget"])
    parser.add_argument("args", nargs="*")
    a = parser.parse_args()
    root = guard_context(a.context)
    if a.action == "setup":
        return setup(root)
    if a.action == "build":
        return build(root)
    if a.action == "browser":
        return browser(root, a.args[0])
    if a.action == "cleanup":
        return cleanup(root)
    if a.action == "forget":
        return forget(root, a.args[0])
    import m8b_phases
    for phase in a.args:
        m8b_phases.PHASES[phase](root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
