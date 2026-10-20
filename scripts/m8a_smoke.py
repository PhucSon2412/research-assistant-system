"""M8a smoke of the real pilot deploy config on an isolated Compose project.

    python scripts/m8a_smoke.py --context <dir outside repo> setup
    python scripts/m8a_smoke.py --context <dir> compose <docker compose args>   # e.g. build migrate web / up -d --wait
    python scripts/m8a_smoke.py --context <dir> http       # HTTPS/cookie/Origin/flows/limits/faults/gate/logs
    python scripts/m8a_smoke.py --context <dir> browser    # Chrome over HTTPS (scripts/m8a_browser.py)
    python scripts/m8a_smoke.py --context <dir> cleanup    # only this project's labelled resources

The context holds a throwaway test CA (trusted only by the smoke's clients),
synthetic credentials and secret files, an explicit journal directory on the
same machine (NOT an independent failure domain), and an isolated SMTP
capture on an internal ``egress`` network: application containers have no
route out, so no provider, arXiv or real mailbox is reachable. Provider key
is empty and pricing false. It never reads .env and never touches ``ras``.
"""
from __future__ import annotations

import argparse
import email
import email.policy
import ipaddress
import json
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from c4_isolation import free_port  # noqa: E402

HOST = "ras.test"
ORIGIN = "https://" + HOST
SMTP_HOST = "smtp.pilot.test"
PREFIX = "rasm8a"
# Smoke-only limits (override file): short windows so "window ends" is observable.
LIMITS = {"LOGIN_SOURCE": (10, 30), "LOGIN_ACCOUNT": (5, 30), "RESET_REQUEST_SOURCE": (10, 30),
          "RESET_REQUEST_ACCOUNT": (3, 30), "TOKEN_SOURCE": (12, 30)}
WINDOW = 30


def guard_context(path: Path) -> Path:
    root = path.resolve()
    if root == REPO or REPO in root.parents:
        raise SystemExit("context must be outside the repository")
    return root


def load(root: Path) -> dict:
    c = json.loads((root / "context.json").read_text(encoding="utf-8"))
    if not c["project"].startswith(PREFIX) or c["project"] == "ras":
        raise SystemExit("not an M8a smoke context")
    return c


def compose_cmd(root: Path, c: dict, *args: str) -> list[str]:
    return ["docker", "compose", "--env-file", str(root / "pilot.env"), "-p", c["project"],
            "-f", str(REPO / "deploy" / "compose.yaml"), "-f", str(root / "override.json"), *args]


def clean_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("RAS_", "POSTGRES_", "REDIS_", "MINIO_", "OPENROUTER_"))}


def run(cmd, **kw):
    return subprocess.run(cmd, text=True, capture_output=True, env=clean_env(), **kw)


def used_subnets() -> list:
    nets = []
    for name in run(["docker", "network", "ls", "-q"]).stdout.split():
        info = json.loads(run(["docker", "network", "inspect", name]).stdout or "[]")
        for net in info:
            for cfg in (net.get("IPAM") or {}).get("Config") or []:
                if cfg.get("Subnet"):
                    nets.append(ipaddress.ip_network(cfg["Subnet"], strict=False))
    return nets


def setup(root: Path) -> int:
    from tests.m8a_tls import make_ca, make_leaf
    root.mkdir(parents=True, exist_ok=True)
    if (root / "context.json").exists():
        raise SystemExit("context already set up")
    project = PREFIX + secrets.token_hex(4)
    taken = used_subnets()
    for _ in range(200):
        subnet = ipaddress.ip_network(f"172.31.{100 + secrets.randbelow(150)}.0/24")
        if not any(subnet.overlaps(n) for n in taken):
            break
    else:
        raise SystemExit("no free private /24 for the backend network")
    web_ip = str(subnet.network_address + 10)
    for d in ("pki", "tls.d", "trust", "secrets", "journal", "capture", "evidence"):
        (root / d).mkdir()
    ca = make_ca(root / "pki", "ca", "RAS M8a smoke test root")
    site = make_leaf(root / "pki", ca, "site", [HOST])
    make_leaf(root / "pki", ca, "smtp", [SMTP_HOST])
    shutil.copyfile(site.cert, root / "tls.d" / "site.crt")
    shutil.copyfile(site.key, root / "tls.d" / "site.key")
    (root / "tls.d" / "site.caddy").write_text("tls /etc/caddy/tls.d/site.crt /etc/caddy/tls.d/site.key\n",
                                                encoding="utf-8")
    shutil.copyfile(ca.cert, root / "trust" / "ca.crt")
    secret = {"edge_token": secrets.token_urlsafe(32), "rate_limit_key": secrets.token_hex(32),
              "smtp_password": secrets.token_urlsafe(24)}
    for name, value in secret.items():
        (root / "secrets" / name).write_text(value + "\n", encoding="utf-8")
    pw = {k: secrets.token_hex(24) for k in ("pg", "runtime", "recovery", "redis", "minio")}
    ports = {"http": free_port(), "https": free_port()}
    env = {
        "RAS_DEPLOY_PROJECT": project, "RAS_API_IMAGE": f"{project}-api:local", "RAS_WEB_IMAGE": f"{project}-web:local",
        "RAS_PUBLIC_HOST": HOST, "RAS_EDGE_BIND": "127.0.0.1", "RAS_EDGE_HTTP_PORT": str(ports["http"]),
        "RAS_EDGE_HTTPS_PORT": str(ports["https"]), "RAS_HSTS_MAX_AGE": "300",
        "RAS_CADDY_TLS_DIR": (root / "tls.d").as_posix(), "RAS_BACKEND_SUBNET": str(subnet),
        "RAS_WEB_BACKEND_IP": web_ip, "RAS_SECRETS_DIR": (root / "secrets").as_posix(),
        "RAS_JOURNAL_DIR": (root / "journal").as_posix(), "RAS_TRUST_DIR": (root / "trust").as_posix(),
        "POSTGRES_USER": "m8a_migrator", "POSTGRES_DB": "m8a_pilot", "POSTGRES_PASSWORD": pw["pg"],
        "RAS_RUNTIME_DB_USER": "m8a_runtime", "RAS_RUNTIME_DB_PASSWORD": pw["runtime"],
        "RAS_RECOVERY_DB_USER": "m8a_recovery", "RAS_RECOVERY_DB_PASSWORD": pw["recovery"],
        "RAS_DEFINER_DB_ROLE": "m8a_definer", "REDIS_PASSWORD": pw["redis"],
        "MINIO_ROOT_USER": "m8a-minio", "MINIO_ROOT_PASSWORD": pw["minio"],
        "RAS_SMTP_HOST": SMTP_HOST, "RAS_SMTP_PORT": "465", "RAS_SMTP_SECURITY": "implicit_tls",
        "RAS_SMTP_SENDER": "no-reply@" + HOST, "RAS_SMTP_SENDER_NAME": "RAS pilot smoke",
        "RAS_SMTP_USERNAME": "pilot-relay", "RAS_SMTP_PASSWORD_FILE": "/run/secrets/smtp_password",
        "RAS_SMTP_CA_FILE": "/etc/ras/trust/ca.crt", "RAS_SMTP_CONNECT_TIMEOUT_SECONDS": "3",
        "RAS_SMTP_TOTAL_TIMEOUT_SECONDS": "6",
        "OPENROUTER_API_KEY": "", "RAS_EMBEDDING__PRICING_VERIFIED": "false", "RAS_RERANK__PRICING_VERIFIED": "false",
        "RAS_GENERATION__PRICING_VERIFIED": "false", "RAS_DISCOVERY__ENABLED": "false"}
    (root / "pilot.env").write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    label = {"ras.m8a.context": root.as_posix()}
    limits = {}
    for name, (limit, window) in LIMITS.items():
        limits[f"RAS_RATE_LIMIT__{name}__LIMIT"] = str(limit)
        limits[f"RAS_RATE_LIMIT__{name}__WINDOW_SECONDS"] = str(window)
    override = {
        "networks": {"egress": {"internal": True}},
        "services": {
            **{name: {"labels": label} for name in ("db", "redis", "minio", "migrate", "provision", "recovery-init",
                                                   "worker", "beat", "web")},
            "edge": {"labels": label, "networks": {"public": {"aliases": [HOST]}, "edge": {}}},
            "api": {"labels": label, "environment": limits},
            "smtp": {"image": "python:3.12-alpine", "labels": label,
                     "command": ["python", "/capture/smtp_capture.py", "--cert", "/pki/smtp.crt", "--key",
                                 "/pki/smtp.key", "--port", "465", "--mode", "implicit_tls", "--store", "/store",
                                 "--auth-user", "pilot-relay", "--auth-password-file", "/run/secrets/smtp_password"],
                     "volumes": [(REPO / "api/tests/smtp_capture.py").as_posix() + ":/capture/smtp_capture.py:ro",
                                 (root / "pki").as_posix() + ":/pki:ro", (root / "capture").as_posix() + ":/store"],
                     "secrets": ["smtp_password"], "networks": {"egress": {"aliases": [SMTP_HOST]}},
                     "restart": "unless-stopped"}}}
    (root / "override.json").write_text(json.dumps(override, indent=2), encoding="utf-8")
    (root / "context.json").write_text(json.dumps({"project": project, "ports": ports, "web_ip": web_ip,
                                                   "subnet": str(subnet)}), encoding="utf-8")
    print("created isolated M8a smoke context " + project)
    return 0


class Checks:
    def __init__(self, path: Path):
        self.path, self.items = path, []

    def __call__(self, label: str, ok, detail=None):
        self.items.append({"check": label, "pass": bool(ok), **({"detail": detail} if detail is not None else {})})
        self.path.write_text(json.dumps(self.items, indent=2, ensure_ascii=False), encoding="utf-8")
        print(("PASS " if ok else "FAIL ") + label + ("" if ok or detail is None else f" :: {detail}"), flush=True)
        if not ok:
            raise SystemExit(f"smoke check failed: {label}")


def http_phase(root: Path, c: dict) -> int:
    import httpx
    env = dict(line.split("=", 1) for line in (root / "pilot.env").read_text(encoding="utf-8").splitlines() if "=" in line)
    out = root / "evidence" / f"http-{time.time_ns()}"
    out.mkdir(parents=True)
    check = Checks(out / "result.json")
    ca = str(root / "pki" / "ca.crt")
    ctx = ssl.create_default_context(cafile=ca)
    base = f"https://127.0.0.1:{c['ports']['https']}"
    secret_values = {p.name: p.read_text(encoding="utf-8").strip() for p in (root / "secrets").iterdir()}
    seen_tokens: list[str] = []
    passwords: list[str] = []

    def compose(*args, **kw):
        return run(compose_cmd(root, c, *args), cwd=REPO, **kw)

    def edge_get(url, headers, extensions, verify, timeout=60):
        # SNI/Host name the public host while connecting to the published loopback port.
        with httpx.Client(verify=verify, timeout=timeout) as client:
            return client.get(url, headers=headers, extensions=extensions)

    class Browser:
        def __init__(self):
            self.c = httpx.Client(base_url=base, verify=ctx, timeout=60, headers={"host": HOST, "origin": ORIGIN})
            self.request("GET", "/api/auth/csrf")

        def request(self, method, path, **kw):
            return self.c.request(method, path, extensions={"sni_hostname": HOST}, **kw)

        def post(self, path, body=None, headers=None, **kw):
            h = {"x-csrf-token": self.c.cookies.get("ras_csrf", "")}
            h.update(headers or {})
            return self.request("POST", "/api" + path, json=body, headers=h, **kw)

        def get(self, path, **kw):
            return self.request("GET", "/api" + path, **kw)

        def login(self, email, password, **kw):
            return self.post("/auth/login", {"email": email, "password": password}, **kw)

    def mails():
        found = []
        for meta in sorted((root / "capture").glob("msg-*.json")):
            info = json.loads(meta.read_text(encoding="utf-8"))
            parsed = email.message_from_bytes(meta.with_suffix(".eml").read_bytes(), policy=email.policy.default)
            found.append((meta.stem, info, parsed))
        return found

    def wait_mail(to: str, before: int, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            fresh = [m for m in mails()[before:] if m[2]["To"] == to]
            if fresh:
                stem, info, parsed = fresh[-1]
                body = parsed.get_content()
                match = re.search(r"(https://\S+?)#token=([A-Za-z0-9_\-]+)", body)
                seen_tokens.append(match.group(2))
                return info, match.group(1), match.group(2)
            time.sleep(0.25)
        raise SystemExit(f"no mail for {to}")

    def probe(network: str, *args: str):
        cmd = ["docker", "run", "--rm", "--network", f"{c['project']}_{network}",
               "--label", f"ras.m8a.context={root.as_posix()}",
               "-v", (REPO / "scripts/m8a_probe.py").as_posix() + ":/probe.py:ro",
               "-v", (root / "pki").as_posix() + ":/pki:ro", "python:3.12-alpine", "python", "/probe.py", *args]
        result = run(cmd)
        if result.returncode != 0:
            raise SystemExit("probe failed: " + result.stderr[-400:])
        return json.loads(result.stdout.strip().splitlines()[-1])

    def readyz():
        r = compose("exec", "-T", "api", "python", "-c",
                    "import urllib.request,urllib.error\n"
                    "try: print(urllib.request.urlopen('http://127.0.0.1:8000/readyz',timeout=5).status)\n"
                    "except urllib.error.HTTPError as e: print(e.code)")
        return r.stdout.strip()

    def wait_window(note):
        print(f"... waiting {WINDOW + 2}s for the smoke rate-limit window ({note})", flush=True)
        time.sleep(WINDOW + 2)

    # 1. deploy contract, images, ports, networks -------------------------------------------------
    contract = run([sys.executable, str(REPO / "scripts/m8a_deploy_check.py"), "--env-file", str(root / "pilot.env"),
                    "--settings-image", env["RAS_API_IMAGE"]], cwd=REPO)
    check("deploy contract on the smoke env, Settings validated inside the API image",
          contract.returncode == 0 and "validated in image" in contract.stdout, contract.stdout[-600:])
    raw_ps = compose("ps", "--format", "json").stdout.strip()
    ps = json.loads(raw_ps) if raw_ps.startswith("[") else [json.loads(l) for l in raw_ps.splitlines() if l.strip()]
    publishers = {s["Service"]: sorted({p.get("PublishedPort") for p in s.get("Publishers") or [] if p.get("PublishedPort")})
                  for s in ps}
    check("only the edge publishes host ports (127.0.0.1)",
          {k for k, v in publishers.items() if v} == {"edge"}
          and publishers["edge"] == sorted([c["ports"]["http"], c["ports"]["https"]]), publishers)
    images = {}
    for svc in ("api", "worker", "beat"):
        cid = compose("ps", "-q", svc).stdout.strip()
        images[svc] = run(["docker", "inspect", cid, "--format", "{{.Image}}"]).stdout.strip()
    check("api/worker/beat run one API image digest", len(set(images.values())) == 1, images)
    listing = compose("exec", "-T", "api", "python", "-c",
                      "import hashlib,json,pathlib\nroot=pathlib.Path('/srv/api')\n"
                      "print(json.dumps({str(p.relative_to(root)).replace('\\\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()"
                      " for d in ('app','migrations') for p in sorted((root/d).rglob('*'))"
                      " if p.is_file() and '__pycache__' not in p.parts}))").stdout
    in_image = json.loads(listing)
    on_host = {str(p.relative_to(REPO / "api")).replace("\\", "/"): __import__("hashlib").sha256(p.read_bytes()).hexdigest()
               for d in ("app", "migrations") for p in sorted((REPO / "api" / d).rglob("*"))
               if p.is_file() and "__pycache__" not in p.parts}
    (out / "image-source-hashes.json").write_text(json.dumps({"image": images, "files": in_image}, indent=1), encoding="utf-8")
    check(f"API image code/migrations match host bytes ({len(on_host)} files)", in_image == on_host,
          sorted(set(in_image.items()) ^ set(on_host.items()))[:6])
    reach = probe("public", "connect", "api:8000", "web:3000", "db:5432", "redis:6379", "minio:9000",
                  f"{SMTP_HOST}:465", f"{HOST}:443", f"{HOST}:80")
    check("public network reaches only the edge",
          reach[f"{HOST}:443"] == "open" and reach[f"{HOST}:80"] == "open"
          and all(v != "open" for k, v in reach.items() if not k.startswith(HOST)), reach)
    reach_egress = probe("egress", "connect", "db:5432", "redis:6379", "minio:9000", "web:3000", "api:8000")
    check("egress network reaches only api (no db/redis/minio/web)",
          reach_egress["api:8000"] == "open"
          and all(reach_egress[k] != "open" for k in ("db:5432", "redis:6379", "minio:9000", "web:3000")), reach_egress)
    check("API readiness passes on real postconditions", readyz() == "200")

    # 2. TLS, redirect, headers --------------------------------------------------------------------
    plain = httpx.get(f"http://127.0.0.1:{c['ports']['http']}/login", headers={"host": HOST}, follow_redirects=False)
    check("HTTP redirects to HTTPS", plain.status_code in (301, 308)
          and plain.headers.get("location") == ORIGIN + "/login", plain.status_code)
    plain_post = httpx.post(f"http://127.0.0.1:{c['ports']['http']}/api/auth/login", headers={"host": HOST},
                            json={"email": "x@example.test", "password": "y"}, follow_redirects=False)
    check("HTTP write is only redirected, never served", plain_post.status_code in (301, 308)
          and "set-cookie" not in plain_post.headers, plain_post.status_code)
    try:
        edge_get(base + "/login", headers={"host": HOST}, extensions={"sni_hostname": HOST}, verify=True)
        system_trust = "accepted"
    except httpx.ConnectError as exc:
        system_trust = "refused:" + type(exc).__name__ + (":CERTIFICATE_VERIFY_FAILED" if "CERTIFICATE_VERIFY_FAILED" in str(exc) else "")
    check("system trust store does not trust the test CA", system_trust.endswith("CERTIFICATE_VERIFY_FAILED"), system_trust)
    try:
        edge_get(base + "/login", headers={"host": "other.test"}, extensions={"sni_hostname": "other.test"}, verify=ctx)
        wrong_name = "accepted"
    except httpx.ConnectError as exc:
        wrong_name = "refused:" + type(exc).__name__
    check("unknown SNI/host gets no certificate", wrong_name.startswith("refused"), wrong_name)
    page = edge_get(base + "/login", headers={"host": HOST}, extensions={"sni_hostname": HOST}, verify=ctx)
    check("HTTPS with the test CA serves the web app with HSTS", page.status_code == 200
          and page.headers.get("strict-transport-security") == "max-age=300" and "server" not in page.headers,
          dict(page.headers))
    blocked = {p: edge_get(base + p, headers={"host": HOST}, extensions={"sni_hostname": HOST}, verify=ctx).status_code
               for p in ("/api/admin/dev-outbox", "/openapi.json", "/docs", "/readyz", "/healthz", "/api/docs")}
    check("dev outbox, API docs and health are not public", all(v == 404 for v in blocked.values()), blocked)

    # 3. bootstrap, cookies, Origin ----------------------------------------------------------------
    accounts = {k: {"email": f"m8a-{k}@pilot.test", "password": secrets.token_urlsafe(18)} for k in ("admin", "a", "b")}
    passwords += [a["password"] for a in accounts.values()]
    (root / "evidence" / "accounts.json").write_text(json.dumps(accounts), encoding="utf-8")
    boot = compose("exec", "-T", "api", "python", "-m", "app.cli.bootstrap_admin", "--email",
                   accounts["admin"]["email"], "--password-stdin", input=accounts["admin"]["password"] + "\n")
    check("one-time bootstrap through the runtime CLI", boot.returncode == 0, boot.stderr[-300:])
    admin = Browser()
    r = admin.login(accounts["admin"]["email"], accounts["admin"]["password"])
    cookies = r.headers.get_list("set-cookie")
    session = next((x for x in cookies if x.startswith("__Host-ras_session=")), "")
    attrs = {a.strip().lower() for a in session.split(";")[1:]}
    csrf_cookie = next((x for x in cookies if x.startswith("ras_csrf=")), "")
    check("HTTPS login sets __Host- session cookie Secure/HttpOnly/SameSite=Lax/Path=/ without Domain",
          r.status_code == 200 and {"secure", "httponly", "samesite=lax", "path=/"} <= attrs
          and not any(a.startswith("domain") for a in attrs) and "secure" in csrf_cookie.lower(), cookies)
    for bad in ("http://" + HOST, "https://evil.test", None):
        h = {"origin": bad} if bad else {}
        req = admin.c.build_request("POST", "/api/auth/logout", headers={"x-csrf-token": admin.c.cookies.get("ras_csrf", ""), **h},
                                    extensions={"sni_hostname": HOST})
        if bad is None:
            del req.headers["origin"]
        resp = admin.c.send(req)
        check(f"write with Origin {bad!r} is refused", resp.status_code == 403 and resp.json() == {"error": "csrf_failed"},
              resp.status_code)
    check("Admin session still live after refused writes", admin.get("/auth/session").status_code == 200)

    # 4. invite -> SMTP capture -> accept -> login -------------------------------------------------
    users = {}
    for key in ("a", "b"):
        before = len(mails())
        inv = admin.post("/admin/invitations", {"email": accounts[key]["email"]})
        check(f"Admin invites {key} over HTTPS", inv.status_code == 201, inv.text[:200])
        info, link, token = wait_mail(accounts[key]["email"], before)
        check(f"invite mail for {key} went over verified TLS with AUTH; link is HTTPS fragment",
              info["tls"] and info["auth_user"] == "pilot-relay" and link == ORIGIN + "/invite", info)
        guest = Browser()
        check(f"{key} inspects then accepts the invitation",
              guest.post("/auth/invitations/inspect", {"token": token}).json().get("email") == accounts[key]["email"]
              and guest.post("/auth/invitations/accept", {"token": token, "password": accounts[key]["password"]}).status_code == 201)
        users[key] = Browser()
        check(f"{key} logs in", users[key].login(accounts[key]["email"], accounts[key]["password"]).status_code == 200)

    # 5. private ACL through the proxy --------------------------------------------------------------
    import hashlib
    data = f"# M8a private synthetic {time.time_ns()}\n\n## Methods\nSynthetic pilot evidence.\n".encode()
    a = users["a"]
    notice = a.get("/uploads/notice").json()["version"]
    intent = a.post("/uploads/intents", {"acknowledged": True, "notice_version": notice, "file_kind": "markdown",
                                         "filename": "m8a.md", "byte_size": len(data),
                                         "sha256": hashlib.sha256(data).hexdigest()})
    sent = a.request("PUT", "/api/uploads/original", content=data,
                     headers={"x-csrf-token": a.c.cookies.get("ras_csrf", ""), "x-upload-intent": intent.json()["intent_token"],
                              "content-type": "application/octet-stream"})
    check("A uploads a private Markdown file through the proxy", sent.status_code == 201, sent.text[:200])
    item, file_id = sent.json()["item_id"], None
    detail = a.get(f"/library/items/{item}").json()["item"]
    file_id = detail["files"][0]["id"]
    matrix = {"a": a.get(f"/library/items/{item}").status_code, "b": users["b"].get(f"/library/items/{item}").status_code,
              "admin": admin.get(f"/library/items/{item}").status_code,
              "b_file": users["b"].request("GET", f"/api/files/{file_id}/original").status_code,
              "admin_file": admin.request("GET", f"/api/files/{file_id}/original").status_code,
              "a_file": a.request("GET", f"/api/files/{file_id}/original").status_code}
    check("private ACL matrix: owner 200, other User and Admin 404", matrix == {
        "a": 200, "b": 404, "admin": 404, "b_file": 404, "admin_file": 404, "a_file": 200}, matrix)
    deadline = time.monotonic() + 90
    status = None
    while time.monotonic() < deadline:
        jobs = a.get(f"/library/items/{item}").json()["item"].get("jobs", [])
        status = jobs[0]["state"] if jobs else None
        if status == "awaiting_index":
            break
        time.sleep(1)
    check("worker extracts the upload and stops before paid embedding (pricing false)", status == "awaiting_index", status)
    # PR #66 P2-2: the storage container is the digest-pinned image and survives a restart with data.
    pinned = re.search(r"\$\{RAS_MINIO_IMAGE:-([^}]+)\}", (REPO / "deploy/compose.yaml").read_text(encoding="utf-8")).group(1)
    minio_id = compose("ps", "-q", "minio").stdout.strip()
    running = run(["docker", "inspect", minio_id, "--format", "{{.Config.Image}}"]).stdout.strip()
    check("storage runs the digest-pinned MinIO image", running == pinned, running)
    first = a.request("GET", f"/api/files/{file_id}/original")
    compose("restart", "minio")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        health = run(["docker", "inspect", minio_id, "--format", "{{.State.Health.Status}}"]).stdout.strip()
        if health == "healthy":
            break
        time.sleep(2)
    again = a.request("GET", f"/api/files/{file_id}/original")
    check("after a MinIO restart the original reads back byte-for-byte",
          first.status_code == again.status_code == 200 and first.content == again.content == data
          and hashlib.sha256(again.content).hexdigest() == hashlib.sha256(data).hexdigest(), (first.status_code, again.status_code))
    rag = a.post("/rag/ask", {"scope": "all", "question": "What does the pilot measure?", "mode": "keyword"})
    check("RAG answers the unverified-pricing 503, not an answer",
          rag.status_code == 503 and rag.json()["error"] == "rag_generation_unavailable_unverified_pricing", rag.text[:200])
    hybrid = a.post("/search", {"query": "pilot evidence", "mode": "hybrid"})
    # With pricing false nothing is vector-indexed, so hybrid has no vector scope:
    # it answers 0 hits without a query-embedding call (503 only arises with indexed scope).
    hybrid_body = hybrid.json()
    check("hybrid search over unindexed scope: 200, 0 hits, no provider cost",
          hybrid.status_code == 200 and hybrid_body["count"] == 0 and hybrid_body["timings"]["provider_ms"] == 0
          and hybrid_body.get("cost", {}).get("embed_query") is None, hybrid.text[:300])

    # 6. reset -> capture -> confirm; old credential/session/token blocked --------------------------
    before = len(mails())
    guest = Browser()
    rr = guest.post("/auth/password-reset/request", {"email": accounts["a"]["email"]})
    ghost = guest.post("/auth/password-reset/request", {"email": "nobody-here@pilot.test"})
    check("reset request answers the same for existing and unknown email",
          (rr.status_code, rr.text) == (ghost.status_code, ghost.text) == (202, '{"status":"accepted"}'))
    info, link, token = wait_mail(accounts["a"]["email"], before)
    time.sleep(1.5)
    check("no mail for the unknown address", not any(m[2]["To"] == "nobody-here@pilot.test" for m in mails()))
    new_password = secrets.token_urlsafe(18)
    passwords.append(new_password)
    check("A confirms the reset link", link == ORIGIN + "/reset-password"
          and guest.post("/auth/password-reset/confirm", {"token": token, "new_password": new_password}).status_code == 200)
    check("old password no longer logs in", Browser().login(accounts["a"]["email"], accounts["a"]["password"]).status_code == 401)
    check("A's earlier session ended", a.get("/auth/session").status_code == 401)
    check("used reset token is refused", guest.post("/auth/password-reset/confirm",
                                                    {"token": token, "new_password": secrets.token_urlsafe(18)}).status_code == 400)
    accounts["a"]["password"] = new_password
    a = users["a"] = Browser()
    check("A logs in with the new password", a.login(accounts["a"]["email"], new_password).status_code == 200)

    # 7. rate limits through the edge ------------------------------------------------------------------
    wait_window("before login limits")
    attacker = Browser()
    statuses = []
    for i in range(LIMITS["LOGIN_SOURCE"][0] + 2):
        forged = f"203.0.113.{i + 1}"
        resp = attacker.login(f"brute{i}@pilot.test", "wrong-password-xx",
                              headers={"x-ras-client-ip": forged, "x-forwarded-for": forged, "x-real-ip": forged,
                                       "forwarded": f"for={forged}", "x-ras-edge-token": "forged-" + forged})
        statuses.append((resp.status_code, resp.headers.get("retry-after"), resp.json().get("error")))
    limit = LIMITS["LOGIN_SOURCE"][0]
    check("source limit through edge ignores forged IP/edge-token headers: 429 + Retry-After after the quota",
          [s[0] for s in statuses] == [401] * limit + [429, 429]
          and all(s[2] == "rate_limited" and 1 <= int(s[1]) <= WINDOW for s in statuses[limit:]), statuses)
    blocked_now = admin.login(accounts["admin"]["email"], accounts["admin"]["password"])
    check("same source is blocked even with the right password", blocked_now.status_code == 429)
    other = probe("public", "logins", "--base", ORIGIN, "--csrf-path", "/api/auth/csrf", "--login-path", "/api/auth/login",
                  "--origin", ORIGIN, "--count", "2", "--ca", "/pki/ca.crt")
    check("another source is not blocked by this source's quota", [x["status"] for x in other["results"]] == [401, 401], other)
    retry = int(statuses[-1][1])
    time.sleep(retry + 1)
    deadline = time.monotonic() + 10
    while (again := admin.login(accounts["admin"]["email"], accounts["admin"]["password"])).status_code == 429:
        if time.monotonic() > deadline:
            break
        time.sleep(1)
    check("after Retry-After the window ends and login works again", again.status_code == 200, again.status_code)
    direct = probe("egress", "logins", "--base", "http://api:8000", "--csrf-path", "/auth/csrf", "--login-path", "/auth/login",
                   "--origin", ORIGIN, "--count", str(limit + 1), "--spoof")
    check("direct API connection outside the topology: forged client headers ignored, keyed by its own peer",
          [x["status"] for x in direct["results"]] == [401] * limit + [429], direct)
    via_web = probe("backend", "logins", "--base", "http://web:3000", "--csrf-path", "/api/auth/csrf",
                    "--login-path", "/api/auth/login", "--origin", ORIGIN, "--count", str(limit + 1), "--spoof",
                    "--edge-token", "forged-token-forged-token-forged-token")
    check("direct web connection with a forged edge token: client header not forwarded (one bucket)",
          [x["status"] for x in via_web["results"]] == [401] * limit + [429], via_web)
    wait_window("before account and reset limits")
    account_statuses = [Browser().login(accounts["b"]["email"], "wrong-password-xx").status_code
                        for _ in range(LIMITS["LOGIN_ACCOUNT"][0] + 1)]
    check("account bucket limits one email", account_statuses == [401] * LIMITS["LOGIN_ACCOUNT"][0] + [429], account_statuses)
    wait_window("before reset limits")
    reset_limit = LIMITS["RESET_REQUEST_ACCOUNT"][0]
    seq = {}
    resetter = Browser()
    before = len(mails())
    for target in (accounts["b"]["email"], "absent-person@pilot.test"):
        seq[target] = [(r.status_code, r.headers.get("retry-after") is not None)
                       for r in (resetter.post("/auth/password-reset/request", {"email": target}) for _ in range(reset_limit + 1))]
    check("reset limit is identical for an existing and an unknown email",
          seq[accounts["b"]["email"]] == seq["absent-person@pilot.test"] == [(202, False)] * reset_limit + [(429, True)], seq)
    time.sleep(2)
    fresh = [m for m in mails()[before:]]
    check("limited reset requests sent no extra mail", len(fresh) == reset_limit
          and all(m[2]["To"] == accounts["b"]["email"] for m in fresh), len(fresh))
    for m in fresh:
        seen_tokens.append(re.search(r"#token=([A-Za-z0-9_\-]+)", m[2].get_content()).group(1))

    # 8. SMTP and Redis failures ------------------------------------------------------------------------
    wait_window("before failure checks")
    admin = Browser()
    check("Admin logs in again", admin.login(accounts["admin"]["email"], accounts["admin"]["password"]).status_code == 200)
    (root / "capture" / "control.json").write_text(json.dumps({"reject_rcpt": True}), encoding="utf-8")
    try:
        before = len(mails())
        rejected = admin.post("/admin/invitations", {"email": "rejected-invitee@pilot.test"})
        listed = {i["email"]: i["status"] for i in admin.get("/admin/invitations").json()["invitations"]}
        check("SMTP reject: 503 delivery_failed and the invitation is revoked (compensated)",
              (rejected.status_code, rejected.json()) == (503, {"error": "delivery_failed"})
              and listed.get("rejected-invitee@pilot.test") == "revoked", (rejected.status_code, listed))
        reset_fail = Browser().post("/auth/password-reset/request", {"email": accounts["admin"]["email"]})
        time.sleep(1.5)
        check("SMTP reject during reset: same generic 202, nothing captured",
              (reset_fail.status_code, reset_fail.text) == (202, '{"status":"accepted"}') and len(mails()) == before)
    finally:
        (root / "capture" / "control.json").unlink()
    compose("stop", "smtp")
    try:
        down = admin.post("/admin/invitations", {"email": "smtp-down@pilot.test"})
        check("SMTP down: 503 delivery_failed (no retry, no token in response)",
              (down.status_code, down.json()) == (503, {"error": "delivery_failed"}), down.text)
    finally:
        compose("start", "smtp")
    compose("stop", "redis")
    try:
        limited = Browser().login(accounts["b"]["email"], accounts["b"]["password"])
        check("Redis down: auth answers 503 rate_limit_unavailable with Retry-After, no cookie",
              limited.status_code == 503 and limited.json() == {"error": "rate_limit_unavailable"}
              and limited.headers.get("retry-after") == "30" and "set-cookie" not in limited.headers, limited.text)
        check("Redis down: API readiness (liveness of gate) stays 200", readyz() == "200")
        check("Redis down: an existing session still reads", admin.get("/auth/session").status_code == 200)
    finally:
        compose("start", "redis")
    deadline = time.monotonic() + 60
    while (back := Browser().login(accounts["b"]["email"], accounts["b"]["password"])).status_code != 200:
        if time.monotonic() > deadline:
            break
        time.sleep(2)
    check("Redis back: login works, no account lockout", back.status_code == 200, back.status_code)
    check("connections/slots released after failures: 25 reads succeed",
          all(admin.get("/auth/session").status_code == 200 for _ in range(25)))

    # 9. recovery gate: hold, missing journal, torn journal -------------------------------------------------
    journal = root / "journal" / "revocations.jsonl"
    def gate_answer():
        r = edge_get(base + "/api/auth/csrf", headers={"host": HOST}, extensions={"sni_hostname": HOST},
                      verify=ctx, timeout=60)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return r.status_code, body.get("error")
    hold = compose("run", "--rm", "--no-deps", "recovery-init", "python", "-m", "app.cli.recovery", "hold")
    check("recovery hold CLI ran", hold.returncode == 0, hold.stderr[-300:])
    check("hold closes the gate for traffic through the edge", gate_answer() == (503, "service_recovering"))
    check("hold makes readiness fail", readyz() == "503")
    recover = compose("run", "--rm", "--no-deps", "recovery-init", "python", "-m", "app.cli.recovery", "recover")
    check("recover reopens after postconditions", recover.returncode == 0 and gate_answer() == (200, None),
          recover.stdout[-300:] + recover.stderr[-300:])
    original = journal.read_bytes()
    journal.rename(journal.with_suffix(".moved"))
    try:
        check("missing journal closes the gate", gate_answer() == (503, "service_recovering"))
    finally:
        journal.with_suffix(".moved").rename(journal)
    check("journal back: gate open", gate_answer() == (200, None))
    with open(journal, "ab") as handle:
        handle.write(b'{"torn":')
    try:
        check("torn journal closes the gate", gate_answer() == (503, "service_recovering"))
    finally:
        journal.write_bytes(original)
    check("journal restored byte-for-byte: gate open", gate_answer() == (200, None) and journal.read_bytes() == original)

    # 10. logs carry no token, password, secret or email ----------------------------------------------
    logs = compose("logs", "--no-color").stdout
    (out / "compose-logs.redacted.txt").write_text(redact(logs, seen_tokens, passwords, secret_values, env), encoding="utf-8")
    leaks = {"tokens": sum(t in logs for t in seen_tokens), "passwords": sum(p in logs for p in passwords),
             "secrets": sum(v in logs for v in secret_values.values()),
             "db_passwords": sum(env[k] in logs for k in ("POSTGRES_PASSWORD", "RAS_RUNTIME_DB_PASSWORD",
                                                         "RAS_RECOVERY_DB_PASSWORD", "REDIS_PASSWORD", "MINIO_ROOT_PASSWORD")),
             "emails": len(set(re.findall(r"[\w.+-]+@pilot\.test", logs))), "#token=": logs.count("#token=")}
    check("compose logs contain no token, password, secret, email or link", not any(leaks.values()), leaks)
    print(f"HTTP smoke: {len(check.items)} checks pass; evidence {out}")
    return 0


def redact(text: str, *groups) -> str:
    for group in groups:
        values = group.values() if isinstance(group, dict) else group
        for value in values:
            if value and len(value) >= 8:
                text = text.replace(value, "[redacted]")
    return re.sub(r"[\w.+-]+@pilot\.test", "[email]", text)


def browser_phase(root: Path, c: dict) -> int:
    out = root / "evidence" / f"chrome-{time.time_ns()}"
    out.mkdir(parents=True)
    accounts = json.loads((root / "evidence" / "accounts.json").read_text(encoding="utf-8"))
    (out / "input.json").write_text(json.dumps({"origin": ORIGIN, "admin": accounts["admin"]}), encoding="utf-8")
    cmd = ["docker", "run", "--rm", "--network", f"{c['project']}_public", "--label", f"ras.m8a.context={root.as_posix()}",
           "--shm-size", "1g", "-v", (REPO / "scripts/m8a_browser.py").as_posix() + ":/m8a_browser.py:ro",
           "-v", (root / "pki/ca.crt").as_posix() + ":/pki/ca.crt:ro",
           "-v", (root / "capture").as_posix() + ":/capture:ro", "-v", out.as_posix() + ":/out",
           "rasm8a-browser:local", "python3", "/m8a_browser.py", "--input", "/out/input.json", "--out", "/out"]
    result = subprocess.run(cmd, text=True, env=clean_env())
    print(f"Chrome phase exit {result.returncode}; evidence {out}")
    return result.returncode


def cleanup(root: Path, c: dict) -> int:
    ids = run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={c['project']}"]).stdout.split()
    for identifier in ids:
        labels = json.loads(run(["docker", "inspect", identifier, "--format", "{{json .Config.Labels}}"]).stdout)
        if labels.get("com.docker.compose.project") != c["project"] or labels.get("ras.m8a.context") != root.as_posix():
            raise SystemExit("container ownership/label mismatch; refusing cleanup")
    for volume in run(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={c['project']}"]).stdout.split():
        users = set(run(["docker", "ps", "-aq", "--filter", f"volume={volume}"]).stdout.split())
        if not users <= set(ids):
            raise SystemExit("volume has a foreign consumer; refusing cleanup")
    code = subprocess.call(compose_cmd(root, c, "down", "-v", "--remove-orphans"), cwd=REPO, env=clean_env())
    for tag in (f"{c['project']}-api:local", f"{c['project']}-web:local"):
        subprocess.call(["docker", "image", "rm", tag], env=clean_env())
    return code


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--context", type=Path, required=True)
    p.add_argument("action", choices=["setup", "compose", "http", "browser", "cleanup"])
    p.add_argument("args", nargs=argparse.REMAINDER)
    a = p.parse_args()
    root = guard_context(a.context)
    if a.action == "setup":
        return setup(root)
    c = load(root)
    if a.action == "compose":
        return subprocess.call(compose_cmd(root, c, *a.args), cwd=REPO, env=clean_env())
    if a.action == "http":
        return http_phase(root, c)
    if a.action == "browser":
        return browser_phase(root, c)
    return cleanup(root, c)


if __name__ == "__main__":
    raise SystemExit(main())
