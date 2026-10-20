"""Storage dependency check for the pilot MinIO image (PR #66 P2-2).

    python scripts/m8a_minio_check.py --context <dir outside repo> [--fresh-pull] [--legacy-image <tag>]

Uses the image pinned in deploy/compose.yaml. ``--fresh-pull`` removes only
that digest-pinned image (refusing if any container uses it) and pulls it
again by digest, so the run proves the registry fetch rather than a cache.
Then, on throwaway labelled containers/volumes with synthetic credentials:

1. new install: ``mc ready local`` (the compose healthcheck) and the S3 ready
   endpoint; ensure bucket, put three objects through ``app.storage``, read
   back each SHA-256, list, delete one, restart, read back the rest;
2. upgrade (with ``--legacy-image``): objects written by the old root-run
   image on a volume, then the new non-root image on the same volume: read,
   write, and the documented ownership step if the write is refused.

Never touches the dev project, other volumes or real data. Results are
written to <context>/minio-check.json.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from c4_isolation import free_port  # noqa: E402

LABEL = "ras.m8a.context"


def pinned_image() -> str:
    text = (REPO / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    match = re.search(r"\$\{RAS_MINIO_IMAGE:-([^}]+@sha256:[0-9a-f]{64})\}", text)
    if not match:
        raise SystemExit("deploy/compose.yaml does not pin the MinIO image by digest")
    return match.group(1)


def docker(*args, check=True, **kw):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RAS_", "MINIO_"))}
    env["MSYS_NO_PATHCONV"] = "1"
    r = subprocess.run(["docker", *args], capture_output=True, text=True, env=env, **kw)
    if check and r.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {r.stderr.strip()[-300:]}")
    return r


def wait_ready(name: str, port: int, seconds: float = 90) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        mc = docker("exec", name, "mc", "ready", "local", check=False)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/minio/health/ready", timeout=3) as r:
                http = r.status
        except OSError:
            http = None
        if mc.returncode == 0 and http == 200:
            return {"mc_ready": True, "http_ready": 200}
        time.sleep(1)
    raise RuntimeError("MinIO never became ready")


def storage(port: int, user: str, password: str, bucket: str):
    from app.config import Settings
    from app.storage import get_storage
    return get_storage(Settings(database_url="postgresql+psycopg://u@h/db", storage={
        "endpoint_url": f"http://127.0.0.1:{port}", "access_key": user, "secret_key": password, "bucket": bucket}))


def sha(store, key: str) -> str:
    return hashlib.sha256(store.open(key).read()).hexdigest()


def start(name: str, image: str, volume: str, port: int, user: str, password: str, root: Path, as_root=False):
    args = ["run", "-d", "--name", name, "--label", f"{LABEL}={root.as_posix()}", "-p", f"127.0.0.1:{port}:9000",
            "-e", f"MINIO_ROOT_USER={user}", "-e", f"MINIO_ROOT_PASSWORD={password}", "-v", f"{volume}:/data"]
    if as_root:
        args += ["-u", "0"]
    docker(*args, image, "server", "/data")


def remove(name: str, root: Path):
    r = docker("inspect", name, "--format", "{{index .Config.Labels \"" + LABEL + "\"}}", check=False)
    if r.returncode == 0:
        if r.stdout.strip() != root.as_posix():
            raise SystemExit(f"{name} is not owned by this context")
        docker("rm", "-f", name)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--context", type=Path, required=True)
    p.add_argument("--fresh-pull", action="store_true")
    p.add_argument("--legacy-image")
    a = p.parse_args()
    root = a.context.resolve()
    if root == REPO or REPO in root.parents:
        raise SystemExit("context must be outside the repository")
    root.mkdir(parents=True, exist_ok=True)
    image = pinned_image()
    report = {"image": image, "steps": []}
    out = root / "minio-check.json"

    def step(label, ok, detail=None):
        report["steps"].append({"step": label, "pass": bool(ok), **({"detail": detail} if detail is not None else {})})
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(("PASS " if ok else "FAIL ") + label + ("" if detail is None else f" :: {detail}"), flush=True)
        if not ok:
            raise SystemExit("storage check failed: " + label)

    if a.fresh_pull:
        users = docker("ps", "-aq", "--filter", f"ancestor={image}").stdout.split()
        if users:
            raise SystemExit("pinned image is in use; refusing to remove it for a fresh pull")
        removed = docker("image", "rm", image, check=False)
        step("pinned image absent before pull (removed task-pulled copy or none cached)",
             docker("image", "inspect", image, check=False).returncode != 0,
             "removed" if removed.returncode == 0 else "not cached")
        pulled = docker("pull", image)
        step("pulled by digest from the registry", f"Digest: {image.split('@')[1]}" in pulled.stdout, pulled.stdout.strip()[-200:])
    version = docker("run", "--rm", "--network", "none", image, "--version").stdout.splitlines()
    labels = json.loads(docker("image", "inspect", image, "--format", "{{json .Config.Labels}}").stdout)
    user_cfg = docker("image", "inspect", image, "--format", "{{.Config.User}}").stdout.strip()
    report["image_facts"] = {"version": version[:3], "user": user_cfg,
                             "source": labels.get("org.opencontainers.image.source"),
                             "created": labels.get("org.opencontainers.image.created")}
    step("image reports MinIO version and AGPLv3 license", version and version[0].startswith("minio version RELEASE.")
         and any("AGPL" in line for line in version), version[:3])

    tag = "rasm8am" + secrets.token_hex(3)
    user, password = "m8a-minio", secrets.token_hex(20)
    names, volumes = [f"{tag}-new", f"{tag}-old-legacy", f"{tag}-old-new"], [f"{tag}-new-data", f"{tag}-old-data"]
    try:
        for v in volumes:
            docker("volume", "create", "--label", f"{LABEL}={root.as_posix()}", v)
        port = free_port()
        start(names[0], image, volumes[0], port, user, password, root)
        step("new install: compose healthcheck (mc ready local) and S3 ready endpoint", wait_ready(names[0], port))
        store = storage(port, user, password, "ras-originals")
        store.ensure_bucket()
        blobs = {f"originals/m8a/{i}": secrets.token_bytes(64 * 1024 + i) for i in range(3)}
        for key, data in blobs.items():
            store.put(key, io.BytesIO(data), "application/octet-stream")
        step("put + read back SHA-256 of three objects",
             all(sha(store, k) == hashlib.sha256(d).hexdigest() for k, d in blobs.items()))
        listed, truncated = store.list_originals(start_after=None, max_keys=100)
        step("list originals", sorted(o.key for o in listed) == sorted(blobs) and truncated is False)
        gone = next(iter(blobs))
        store.delete(gone)
        listed, _ = store.list_originals(start_after=None, max_keys=100)
        step("delete one object", gone not in {o.key for o in listed} and len(listed) == 2)
        docker("restart", names[0])
        wait_ready(names[0], port)
        store = storage(port, user, password, "ras-originals")
        step("after restart the remaining objects read back byte-for-byte",
             all(sha(store, k) == hashlib.sha256(d).hexdigest() for k, d in blobs.items() if k != gone))
        remove(names[0], root)

        if a.legacy_image:
            port = free_port()
            start(names[1], a.legacy_image, volumes[1], port, user, password, root)
            wait_ready(names[1], port)
            old = storage(port, user, password, "ras-originals")
            legacy = {f"originals/legacy/{i}": secrets.token_bytes(32 * 1024) for i in range(2)}
            for key, data in legacy.items():
                old.put(key, io.BytesIO(data), "application/octet-stream")
            remove(names[1], root)
            port = free_port()
            start(names[2], image, volumes[1], port, user, password, root)
            try:
                wait_ready(names[2], port, seconds=45)
                new = storage(port, user, password, "ras-originals")
                read_ok = all(sha(new, k) == hashlib.sha256(d).hexdigest() for k, d in legacy.items())
                try:
                    new.put("originals/legacy/after-upgrade", io.BytesIO(b"after"), "text/plain")
                    write_ok = True
                except Exception as exc:  # noqa: BLE001
                    write_ok = type(exc).__name__
            except RuntimeError:
                read_ok, write_ok = "not_ready", "not_ready"
            logs = docker("logs", "--tail", "15", names[2], check=False)
            report["upgrade_without_ownership_step"] = {
                "read": read_ok, "write": write_ok, "state": docker(
                    "inspect", names[2], "--format", "{{.State.Status}} exit={{.State.ExitCode}}", check=False).stdout.strip(),
                "log_tail": [line[:200] for line in (logs.stdout + logs.stderr).splitlines()[-15:]
                             if password not in line]}
            out.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"upgrade without ownership step: read={read_ok} write={write_ok}", flush=True)
            remove(names[2], root)
            if write_ok is not True or read_ok is not True:
                # Documented one-shot step: the new image runs as 65532.
                docker("run", "--rm", "--label", f"{LABEL}={root.as_posix()}", "--network", "none", "-u", "0",
                       "--entrypoint", "/usr/bin/chown", "-v", f"{volumes[1]}:/data", image, "-R", "65532:65532", "/data")
                port = free_port()
                start(names[2], image, volumes[1], port, user, password, root)
                wait_ready(names[2], port)
                new = storage(port, user, password, "ras-originals")
                new.put("originals/legacy/after-upgrade", io.BytesIO(b"after"), "text/plain")
                step("upgrade after the ownership step: legacy objects read back and new writes work",
                     all(sha(new, k) == hashlib.sha256(d).hexdigest() for k, d in legacy.items())
                     and sha(new, "originals/legacy/after-upgrade") == hashlib.sha256(b"after").hexdigest())
            else:
                step("upgrade: legacy objects read back and new writes work without extra step", True)
    finally:
        for name in names:
            remove(name, root)
        for v in volumes:
            r = docker("volume", "inspect", v, "--format", "{{index .Labels \"" + LABEL + "\"}}", check=False)
            if r.returncode == 0 and r.stdout.strip() == root.as_posix():
                docker("volume", "rm", v, check=False)
    print(f"storage check: {sum(s['pass'] for s in report['steps'])} steps pass; {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
