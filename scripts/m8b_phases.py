"""Phases of the M8b isolated drills (driven by scripts/m8b_drill.py ``run <phase>``).

Every phase records its checks/metrics under <context>/evidence/<phase>-<time>/result.json.
Accounts, cookies and IDs used across phases live in <context>/run.json (synthetic only).
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import ssl
import threading
import time
from pathlib import Path

import m8b_drill as d
from m8b_drill import ORIGIN, HOST, OPS_RECIPIENTS, Checks, Project, new_project, run, save_ctx, ctx


# --- HTTP through the edge (test CA only; SNI ras.test) ---------------------------------------

class Client:
    def __init__(self, p: Project, root: Path, cookies: dict | None = None):
        import httpx
        self.c = httpx.Client(base_url=f"https://127.0.0.1:{p.info['ports']['https']}",
                              verify=ssl.create_default_context(cafile=str(root / "pki" / "ca.crt")), timeout=90,
                              headers={"host": HOST, "origin": ORIGIN})
        for name, value in (cookies or {}).items():
            self.c.cookies.set(name, value)
        if not cookies:
            self.request("GET", "/api/auth/csrf")

    def request(self, method, path, **kw):
        return self.c.request(method, path, extensions={"sni_hostname": HOST}, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", "/api" + path, json=body,
                            headers={"x-csrf-token": self.c.cookies.get("ras_csrf", "")}, **kw)

    def get(self, path, **kw):
        return self.request("GET", "/api" + path, **kw)

    def login(self, account):
        return self.post("/auth/login", {"email": account["email"], "password": account["password"]})

    def jar(self) -> dict:
        return {k: v for k, v in self.c.cookies.items()}


def mails(root: Path) -> list:
    import email
    import email.policy
    out = []
    for meta in sorted((root / "capture").glob("msg-*.json")):
        out.append((meta.stem, email.message_from_bytes(meta.with_suffix(".eml").read_bytes(),
                                                        policy=email.policy.default)))
    return out


def wait_token_mail(root: Path, to: str, before: int, timeout=30) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for _, m in mails(root)[before:]:
            if m["To"] == to:
                return re.search(r"#token=([A-Za-z0-9_\-]+)", m.get_content()).group(1)
        time.sleep(0.3)
    raise SystemExit("no mail for " + to)


def load_run(root: Path) -> dict:
    path = root / "run.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_run(root: Path, data: dict) -> None:
    (root / "run.json").write_text(json.dumps(data, indent=1), encoding="utf-8")


def accounts_for(data: dict, installation: str) -> dict:
    accounts = data.setdefault("accounts", {}).setdefault(installation, {})
    for key in ("admin", "a", "b"):
        accounts.setdefault(key, {"email": f"m8b-{installation}-{key}-{secrets.token_hex(2)}@pilot.test",
                                  "password": secrets.token_urlsafe(18)})
    return accounts


def bootstrap_and_users(root: Path, p: Project, check: Checks, accounts: dict) -> Client:
    boot = p.compose("exec", "-T", "api", "python", "-m", "app.cli.bootstrap_admin", "--email",
                     accounts["admin"]["email"], "--password-stdin", input=accounts["admin"]["password"] + "\n")
    check(f"[{p.key}] one-time bootstrap through the runtime CLI", boot.returncode == 0, boot.stderr[-300:])
    admin = Client(p, root)
    check(f"[{p.key}] Admin logs in over HTTPS", admin.login(accounts["admin"]).status_code == 200)
    for key in ("a", "b"):
        before = len(mails(root))
        r = admin.post("/admin/invitations", {"email": accounts[key]["email"]})
        token = wait_token_mail(root, accounts[key]["email"], before)
        guest = Client(p, root)
        accepted = guest.post("/auth/invitations/accept", {"token": token, "password": accounts[key]["password"]})
        check(f"[{p.key}] invite {key} via SMTP capture and accept", r.status_code == 201 and accepted.status_code == 201)
    return admin


def login(p: Project, root: Path, account: dict, check: Checks | None = None, label: str = "") -> Client:
    client = Client(p, root)
    r = client.login(account)
    if check is not None:
        check(f"[{p.key}] {label or account['email'].split('@')[0]} logs in", r.status_code == 200, r.status_code)
    elif r.status_code != 200:
        raise SystemExit(f"login failed {r.status_code}")
    return client


def upload(user: Client, label: str) -> dict:
    content = f"# M8b {label} {secrets.token_hex(8)}\n\n## Methods\nSynthetic drill evidence {label}.\n".encode()
    notice = user.get("/uploads/notice").json()["version"]
    intent = user.post("/uploads/intents", {"acknowledged": True, "notice_version": notice, "file_kind": "markdown",
                                            "filename": f"{label}.md", "byte_size": len(content),
                                            "sha256": hashlib.sha256(content).hexdigest()})
    sent = user.request("PUT", "/api/uploads/original", content=content,
                        headers={"x-csrf-token": user.c.cookies.get("ras_csrf", ""),
                                 "x-upload-intent": intent.json()["intent_token"],
                                 "content-type": "application/octet-stream"})
    if sent.status_code != 201:
        raise SystemExit(f"upload {label}: {sent.status_code} {sent.text[:200]}")
    item = sent.json()["item_id"]
    deadline = time.monotonic() + 180
    detail = {}
    while time.monotonic() < deadline:
        detail = user.get(f"/library/items/{item}").json()["item"]
        jobs = detail.get("jobs", [])
        if jobs and jobs[0]["state"] == "awaiting_index":
            break
        time.sleep(1)
    f = detail["files"][0]
    return {"item": item, "file": f["id"], "version": f.get("version_id"), "sha256": hashlib.sha256(content).hexdigest(),
            "label": label}


def journal_state(path: Path):
    from app import journal
    return journal.read(path)


def replica_head(c: dict, installation: str) -> int:
    """The replica's head seq read from the destination itself (0 when empty)."""
    s3 = d.offsite_client(c, "journal")
    bucket = c["offsite"]["journal"]["bucket"]
    prefix = f"pilot-{installation}/journal/"
    try:
        install = json.loads(s3.get_object(Bucket=bucket, Key=prefix + "install.json")["Body"].read())
        head = json.loads(s3.get_object(Bucket=bucket, Key=f"{prefix}{install['journal_id']}/head.json")["Body"].read())
    except Exception:  # noqa: BLE001
        return 0
    return int(head["seq"])


def wait_shipped(p: Project, timeout=120) -> float:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        state = journal_state(p.journal)
        if state.torn is None and replica_head(p.c, p.info["installation"]) == state.head.seq:
            return round(time.monotonic() - start, 2)
        time.sleep(0.5)
    raise SystemExit(f"[{p.key}] replica did not catch up within {timeout}s")


def edge(p: Project, root: Path, path: str):
    import httpx
    with httpx.Client(verify=ssl.create_default_context(cafile=str(root / "pki" / "ca.crt")), timeout=30) as client:
        return client.get(f"https://127.0.0.1:{p.info['ports']['https']}{path}", headers={"host": HOST},
                          extensions={"sni_hostname": HOST})


def check_run(p: Project, root: Path):
    before = len(mails(root))
    code, out = p.ras_ops("check", timeout=600)
    time.sleep(1)
    fresh = [m for _, m in mails(root)[before:] if str(m["To"]) in OPS_RECIPIENTS]
    return code, out.get("check", out), fresh


def scan_mails(root: Path, data: dict, check: Checks):
    """Alert mails: codes/counts/times only (no user email, secret, token, path, object key)."""
    secret_values = [f.read_text(encoding="utf-8").strip() for f in (root / "secrets").iterdir()]
    people = [a for inst in data.get("accounts", {}).values() for a in inst.values()]
    forbidden = [a["email"] for a in people] + [a["password"] for a in people]
    forbidden += [v for v in secret_values if len(v) >= 8]
    forbidden += [str(root), root.as_posix(), "originals/", "#token=", "AGE-SECRET-KEY", "@pilot.test"]
    alerts = [(stem, m) for stem, m in mails(root) if str(m["To"]) in OPS_RECIPIENTS]
    leaks = []
    for stem, m in alerts:
        text = m.as_string() + m.get_content()
        leaks += [stem for f in forbidden if f and f in text]
    check(f"{len(alerts)} alert mails carry no user email, password, secret, token, path or object key",
          bool(alerts) and not leaks, leaks[:5])


def secrets_in(text: str, root: Path, c: dict) -> list[str]:
    values = [f.read_text(encoding="utf-8").strip() for f in (root / "secrets").iterdir()]
    values += list(c["pw"].values())
    values += [c["offsite"][w][k] for w in ("journal", "backup") for k in ("root_password", "rw_password", "ro_password")]
    values += [(root / "operator" / "identity.txt").read_text().strip()]
    return [v[:4] + "…" for v in values if len(v) >= 8 and v in text]


def scan_logs(p: Project, root: Path, check: Checks):
    logs = p.compose("--profile", "ops", "logs", "--no-color", timeout=300).stdout
    host_logs = "".join(f.read_text(encoding="utf-8", errors="replace") for f in (p.dir / "ops-state" / "logs").glob("*"))
    argv_log = (p.dir / "ras_ops.log").read_text(encoding="utf-8") if (p.dir / "ras_ops.log").exists() else ""
    found = secrets_in(logs + host_logs + argv_log, root, p.c)
    check(f"[{p.key}] compose logs, ops logs and recorded argv hold no secret value", not found, found)


def stage_poll(p: Project, root: Path, stop: threading.Event, out: list):
    while not stop.is_set():
        try:
            r = edge(p, root, "/api/auth/csrf")
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            out.append((r.status_code, body.get("error")))
        except Exception as exc:  # noqa: BLE001
            out.append((0, type(exc).__name__))
        time.sleep(0.5)


# --- phases ----------------------------------------------------------------------------------

def phase_upgrade(root: Path) -> None:
    """An M8a deploy with data upgraded to M8b per the runbook (hold -> migrate/provision -> recover)."""
    c = ctx(root)
    check = Checks(root, "upgrade")
    data = load_run(root)
    p = new_project(root, c, "u1", installation="u1", m8a=True)
    r = p.compose("up", "-d", "--wait", timeout=1800)
    check("[u1] M8a deploy (merged base compose + M8a API image) starts", r.returncode == 0, r.stderr[-500:])
    check("[u1] M8a readiness 200", p.readyz() == "200")
    acc = accounts_for(data, "u1")
    save_run(root, data)
    bootstrap_and_users(root, p, check, acc)
    a = login(p, root, acc["a"], check)
    keep, gone = upload(a, "u1-keep"), upload(a, "u1-gone")
    check("[u1] A deletes one private item on M8a", a.post(f"/library/items/{gone['item']}/delete",
                                                            {"confirm_delete": True}).status_code == 200)
    extra = login(p, root, acc["a"])
    old_cookie = extra.jar()
    check("[u1] A ends an extra session on M8a", extra.post("/auth/logout").status_code in (200, 204))
    before_seq = journal_state(p.journal).head.seq
    # --- runbook: stop writers and edge, hold with the OLD image, then switch compose/env/images ---
    t0 = time.monotonic()
    r = p.compose("stop", "edge", "web", "api", "worker", "beat")
    check("[u1] writers and edge stopped", r.returncode == 0)
    r = p.compose("run", "--rm", "-T", "--no-deps", "recovery-init", "python", "-m", "app.cli.recovery", "hold")
    check("[u1] hold with the serving (M8a) image before migrate", r.returncode == 0, r.stderr[-300:])
    m8b_env = {
        "RAS_API_IMAGE": c["images"]["api"], "RAS_OPS_IMAGE": c["images"]["ops"],
        "RAS_OPS_CONFIG_DIR": (root / "ops-config-u1").as_posix(), "RAS_OPS_STATE_DIR": (p.dir / "ops-state").as_posix(),
        "RAS_OPS_STAGING_DIR": (p.dir / "staging").as_posix(), "RAS_MAINTENANCE_DIR": (p.dir / "maintenance").as_posix()}
    lines = [line for line in p.env.read_text(encoding="utf-8").splitlines() if not line.startswith("RAS_API_IMAGE=")]
    p.env.write_text("\n".join(lines + [f"{k}={v}" for k, v in m8b_env.items()]) + "\n", encoding="utf-8")
    d.write_override(root, c, p.dir, m8a=False)
    c["projects"]["u1"]["compose_file"] = str(d.REPO / "deploy" / "compose.yaml")
    save_ctx(root, c)
    p = Project(root, c, "u1")
    (p.dir / "maintenance" / "enabled").write_text('{"reason":"upgrade"}', encoding="utf-8")
    for step in (("migrate",), ("provision",)):
        r = p.compose("run", "--rm", "-T", "--no-deps", *step, timeout=1800)
        check(f"[u1] {step[0]} with the M8b image (source 0052, no new migration)", r.returncode == 0, r.stderr[-300:])
    r = p.compose("run", "--rm", "-T", "--no-deps", "recovery-init", "python", "-m", "app.cli.recovery", "recover",
                  timeout=1800)
    check("[u1] recover closes the hold after postconditions", r.returncode == 0 and "safety_ready=True" in r.stdout,
          (r.stdout + r.stderr)[-400:])
    code, out = p.ras_ops("open", timeout=1800)
    check("[u1] ras_ops open: replica compare, start everything, readiness, flag cleared",
          code == 0 and not (p.dir / "maintenance" / "enabled").exists(), out)
    check.metric("upgrade_downtime_seconds", round(time.monotonic() - t0, 2))
    a = login(p, root, acc["a"], check, "A after upgrade")
    lib = a.get("/library/items").json()
    ids = {i["id"] for i in lib.get("items", [])}
    check("[u1] data intact: library keeps the item, the deleted one stays gone",
          keep["item"] in ids and gone["item"] not in ids
          and a.get(f"/library/items/{gone['item']}").status_code == 404, sorted(ids))
    original = a.get(f"/files/{keep['file']}/original")
    check("[u1] original reads back byte-exact", original.status_code == 200
          and hashlib.sha256(original.content).hexdigest() == keep["sha256"])
    check("[u1] the session ended before the upgrade stays ended",
          Client(p, root, old_cookie).get("/auth/session").status_code == 401)
    seconds = wait_shipped(p)
    check.metric("upgrade_first_ship_seconds", seconds)
    check("[u1] the replicator shipped the whole pre-existing journal", replica_head(c, "u1") == journal_state(p.journal).head.seq
          and journal_state(p.journal).head.seq > before_seq)
    code, out = p.ras_ops("backup", timeout=3600)
    check("[u1] first scheduled backup after the upgrade", code == 0 and out.get("status") == "ok", out)
    check.metric("upgrade_backup_window_seconds", out.get("window_seconds"))
    scan_logs(p, root, check)
    r = p.compose("--profile", "ops", "down", "-v", "--remove-orphans", timeout=900)
    check("[u1] upgrade project torn down (its own volumes only)", r.returncode == 0)


def phase_install(root: Path) -> None:
    c = ctx(root)
    check = Checks(root, "install")
    data = load_run(root)
    p = new_project(root, c, "p1", installation="i1")
    contract = run([d.PY, str(d.REPO / "scripts/m8a_deploy_check.py"), "--env-file", str(p.env),
                    "--settings-image", c["images"]["api"]], timeout=600)
    check("[p1] deploy contract (M8b services) on this env, Settings and ops example in image",
          contract.returncode == 0 and "ops.example.json validated" in contract.stdout, contract.stdout[-800:])
    t0 = time.monotonic()
    r = p.compose("up", "-d", "--wait", timeout=1800)
    check("[p1] fresh install of the M8b deploy starts (incl. journal-replicator)", r.returncode == 0, r.stderr[-600:])
    check.metric("install_up_seconds", round(time.monotonic() - t0, 2))
    check("[p1] readiness on real postconditions", p.readyz() == "200")
    running = set(p.compose("ps", "--status", "running", "--services").stdout.split())
    check("[p1] replicator runs; ops one-shots are not long-running services",
          "journal-replicator" in running and not {"ops", "ops-check"} & running, sorted(running))
    rep_id = p.compose("ps", "-q", "journal-replicator").stdout.strip()
    inspect = json.loads(run(["docker", "inspect", rep_id]).stdout)[0]
    mounts = {m["Destination"]: m["RW"] for m in inspect["Mounts"]}
    env_names = {e.split("=", 1)[0] for e in inspect["Config"]["Env"]}
    check("[p1] replicator: journal read-only, read-only rootfs, uid 10001, no DB/MinIO settings",
          mounts.get("/var/lib/ras/journal") is False and inspect["HostConfig"]["ReadonlyRootfs"]
          and inspect["Config"]["User"] in ("ras", "10001")
          and not any(n.startswith(("RAS_DB__", "RAS_STORAGE__", "POSTGRES", "MINIO")) for n in env_names),
          {"mounts": mounts, "user": inspect["Config"]["User"]})
    check.metric("first_ship_seconds", wait_shipped(p))
    # Alerts before any backup/drill: two stable codes, mailed once, not again.
    code, out, fresh = check_run(p, root)
    check("[p1] first check mails backup_stale + restore_drill_overdue to both recipients",
          code == 0 and {"backup_stale", "restore_drill_overdue"} <= set(out.get("new", []))
          and len(fresh) == 2, out)
    code, out, fresh = check_run(p, root)
    check("[p1] same conditions next run: deduplicated, no mail", code == 0 and not out.get("new") and not fresh, out)
    # Maintenance page and upstream failure page at the edge.
    flag = p.dir / "maintenance" / "enabled"
    flag.write_text('{"reason":"manual"}', encoding="utf-8")
    api, page = edge(p, root, "/api/auth/csrf"), edge(p, root, "/login")
    check("[p1] maintenance flag: edge answers 503 maintenance (JSON for /api, HTML page) with Retry-After",
          api.status_code == 503 and api.json() == {"error": "maintenance"} and api.headers.get("retry-after") == "300"
          and api.headers.get("content-type") == "application/json"
          and page.status_code == 503 and "bảo trì" in page.text and page.headers.get("content-type", "").startswith(
              "text/html"), (api.status_code, api.headers.get("content-type"), page.status_code))
    flag.unlink()
    check("[p1] flag removed: traffic again", edge(p, root, "/api/auth/csrf").status_code == 200)
    p.compose("stop", "web")
    api, page = edge(p, root, "/api/auth/csrf"), edge(p, root, "/login")
    check("[p1] web down outside a window: clear 503 service_unavailable, not 502",
          api.status_code == 503 and api.json() == {"error": "service_unavailable"} and page.status_code == 503
          and api.headers.get("content-type") == "application/json",
          (api.status_code, page.status_code))
    r = p.compose("up", "-d", "--wait", "--no-deps", "web")
    check("[p1] web back", r.returncode == 0 and edge(p, root, "/api/auth/csrf").status_code == 200)
    # Data that later phases revoke/delete/close.
    acc = accounts_for(data, "i1")
    save_run(root, data)
    admin = bootstrap_and_users(root, p, check, acc)
    a, b = login(p, root, acc["a"], check), login(p, root, acc["b"], check)
    items = {k: upload(a, k) for k in ("keep", "del1", "del2", "del3", "del4", "common", "review")}
    notice = a.get("/review-requests/notice").json()["version"]

    def review(item):
        r = a.post("/review-requests", {"acknowledged": True, "notice_version": notice, "item_id": item["item"],
                                        "version_id": item["version"], "source_file_id": item["file"],
                                        "declared_source": "synthetic m8b", "declared_rights": "synthetic permission"})
        if r.status_code not in (200, 201):
            raise SystemExit("review request: " + r.text[:300])
        return r.json()["request"]["id"]
    rr_open = review(items["review"])
    admin_read = admin.get(f"/admin/review-requests/{rr_open}/files/{items['review']['file']}/original")
    check("[p1] open review request: the Admin may read that private file", admin_read.status_code == 200)
    rr_common = review(items["common"])
    approved = admin.post(f"/admin/review-requests/{rr_common}/approve", {
        "confirm_verified": True, "file_checksum_sha256": items["common"]["sha256"], "title": "M8b common synthetic",
        "verified_source": "synthetic m8b", "evidence_note": "synthetic file only",
        "checked_on": time.strftime("%Y-%m-%d"), "scope": {"audience": "instance_users", "original_download": True,
                                                           "fulltext_search": False, "external_processing": False,
                                                           "conditions": None}})
    common = approved.json()["request"]["pending_publication_id"]
    published = admin.post(f"/admin/common-copies/{common}/publish",
                           {"confirm_publish": True, "file_checksum_sha256": items["common"]["sha256"]})
    detail = b.get(f"/library/items/{common}").json()["item"]
    common_file = detail["files"][0]["id"]
    check("[p1] common copy approved + published; B downloads it", approved.status_code == 200
          and published.status_code == 200 and b.get(f"/files/{common_file}/original").status_code == 200)
    # Sessions S/T/U/V are opened right before the backup that must contain them (idle timeout 30 min).
    data["i1"] = {"items": items, "review_open": rr_open, "common": common, "common_file": common_file,
                  "sessions": {}}
    save_run(root, data)
    check.metric("ship_after_data_seconds", wait_shipped(p))


def phase_replica(root: Path) -> None:
    c = ctx(root)
    check = Checks(root, "replica")
    data = load_run(root)
    p = Project(root, c, "p1")
    acc = data["accounts"]["i1"]
    s3 = d.offsite_client(c, "journal")
    bucket = c["offsite"]["journal"]["bucket"]
    prefix = "pilot-i1/journal/"

    def listing():
        v = s3.list_object_versions(Bucket=bucket, Prefix=prefix)
        return sorted((x["Key"], x["VersionId"]) for x in v.get("Versions", []) if "/segments/" in x["Key"])
    # Latency from a journaled action to the off-host head.
    throwaway = login(p, root, acc["b"])
    start = time.monotonic()
    throwaway.post("/auth/logout")
    wait_shipped(p)
    check.metric("ship_latency_after_logout_seconds", round(time.monotonic() - start, 2))
    # Torn tail: never shipped; the local gate closes as before (M8a behaviour).
    original = p.journal.read_bytes()
    before = listing()
    with open(p.journal, "ab") as handle:
        handle.write(b'{"body":{"torn-tail-marker')
    time.sleep(12)
    shipped = b"".join(s3.get_object(Bucket=bucket, Key=k)["Body"].read() for k, _ in listing())
    status = p.state("replica-status.json")
    check("[p1] torn tail: nothing shipped, marker absent off-host, status records local_torn",
          listing() == before and b"torn-tail-marker" not in shipped and status.get("local_torn") is True, status)
    p.journal.write_bytes(original)
    time.sleep(6)
    check("[p1] journal bytes restored: gate open again", p.readyz() == "200")
    # Destination outage: serving continues, lag grows, alerts, then catch-up and recovery mail.
    r = d.offsite_compose(root, c, "stop", "offsite-journal")
    check("[p1] off-host journal destination stopped", r.returncode == 0)
    a = login(p, root, acc["a"])
    timings = []
    for _ in range(5):
        t = time.monotonic()
        ok = a.get("/library/items").status_code == 200
        timings.append(round(time.monotonic() - t, 3))
        if not ok:
            break
    logout_user = login(p, root, acc["b"])
    check("[p1] destination down: requests still served (library x5) and a journaled logout succeeds",
          len(timings) == 5 and logout_user.post("/auth/logout").status_code in (200, 204), timings)
    check.metric("library_latency_seconds_while_destination_down", timings)
    local_seq = journal_state(p.journal).head.seq
    time.sleep(36)
    code, out, fresh = check_run(p, root)
    new = set(out.get("new", []))
    check("[p1] alerts: replica_error:target_unavailable and replica_lag mailed",
          {"replica_error:target_unavailable", "replica_lag"} <= new and len(fresh) == 2, out)
    check.metric("lag_records_while_destination_down", local_seq - replica_head_safe(c))
    t = time.monotonic()
    d.offsite_compose(root, c, "start", "offsite-journal")
    d.offsite_compose(root, c, "up", "-d", "--wait", "offsite-journal")
    check.metric("catch_up_seconds_after_destination_back", round(time.monotonic() - t + wait_shipped(p), 2))
    code, out, fresh = check_run(p, root)
    check("[p1] destination back: replica caught up, recovery mail", "replica_lag" in out.get("resolved", [])
          and "replica_error:target_unavailable" in out.get("resolved", []) and len(fresh) == 2, out)
    # Replicator stopped: stale status alert.
    p.compose("stop", "journal-replicator")
    time.sleep(36)
    code, out, fresh = check_run(p, root)
    check("[p1] replicator down: replica_stale mailed", "replica_stale" in out.get("new", []), out)
    p.compose("start", "journal-replicator")
    wait_shipped(p)
    time.sleep(6)
    code, out, fresh = check_run(p, root)
    check("[p1] replicator back: replica_stale resolved", "replica_stale" in out.get("resolved", []), out)
    # Tampered destination: stop, alert, never overwritten; heal by removing the bad version.
    key = listing()[-1][0]
    good = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    tampered = s3.put_object(Bucket=bucket, Key=key, Body=good.replace(b'"seq":', b'"seq": ', 1))
    time.sleep(12)
    status = p.state("replica-status.json")
    versions_before = listing()
    check("[p1] tampered off-host segment: replicator stops with replica_corrupt", status.get("code") == "replica_corrupt",
          status)
    throwaway = login(p, root, acc["b"])
    throwaway.post("/auth/logout")
    time.sleep(12)
    check("[p1] while corrupt nothing new is written off-host", listing() == versions_before)
    code, out, fresh = check_run(p, root)
    check("[p1] alert replica_error:replica_corrupt mailed", "replica_error:replica_corrupt" in out.get("new", []), out)
    s3.delete_object(Bucket=bucket, Key=key, VersionId=tampered["VersionId"], BypassGovernanceRetention=True)
    check.metric("ship_after_heal_seconds", wait_shipped(p))
    # Local fork: same genesis, different record -> replica_fork, no write.
    from app import journal
    original = p.journal.read_bytes()
    state = journal.read(p.journal)
    keep = list(state.records[:-1])
    if keep[-1].kind == journal.RECOVERY_BEGIN:
        keep = keep[:-1]
    fork_path = p.dir / "fork.jsonl"
    journal.create(fork_path, keep)
    forked = journal.read(fork_path)
    rid = "00000000-0000-4000-8000-" + secrets.token_hex(6)
    begin = journal.next_record(forked.head, journal.RECOVERY_BEGIN,
                                {"recovery_id": rid, "started_at": d.now_iso(), "db_head_seq": 0})
    end = journal.next_record(begin, journal.RECOVERY_END, {"recovery_id": rid, "finished_at": d.now_iso(), "replayed": 0,
                                                            "tombstones_purged": 0, "objects_removed": 0})
    journal.append(fork_path, forked, [begin, end])
    versions_before = listing()
    p.journal.write_bytes(fork_path.read_bytes())
    time.sleep(12)
    status = p.state("replica-status.json")
    check("[p1] forked local journal: replica_fork, nothing written off-host",
          status.get("code") == "replica_fork" and listing() == versions_before, status)
    code, out, fresh = check_run(p, root)
    check("[p1] fork is a journal_rollback alert and fences the edge",
          "journal_rollback:replica_fork" in out.get("new", []) and out.get("maintenance_flag_set")
          and edge(p, root, "/api/auth/csrf").json() == {"error": "maintenance"}, out)
    p.journal.write_bytes(original)
    fork_path.unlink()
    wait_shipped(p)
    (p.dir / "maintenance" / "enabled").unlink()
    check("[p1] original journal back: shipping resumes, operator lifts the fence", p.readyz() == "200"
          and edge(p, root, "/api/auth/csrf").status_code == 200)
    # Another journal (other genesis/journal_id) through a one-off replicator run: refused, no write.
    other = p.dir / "other-journal"
    other.mkdir()
    journal.create(other / "revocations.jsonl",
                   [journal.genesis(__import__("uuid").uuid4(), __import__("datetime").datetime.now(
                       __import__("datetime").timezone.utc), "new_install")])
    alt = p.dir / "other-config"
    alt.mkdir()
    cfg = json.loads((root / "ops-config-i1" / "ops.json").read_text(encoding="utf-8"))
    cfg["state_dir"] = "/tmp/other-state"  # the live replica-status.json is not touched
    (alt / "ops.json").write_text(json.dumps(cfg), encoding="utf-8")
    versions_before = listing()
    r = p.compose("run", "--rm", "-T", "--no-deps", "-v", f"{other.as_posix()}:/var/lib/ras/other:ro",
                  "-v", f"{alt.as_posix()}:/etc/ras/ops-alt:ro", "-e",
                  "RAS_RECOVERY__JOURNAL_PATH=/var/lib/ras/other/revocations.jsonl", "-e",
                  "RAS_OPS_CONFIG=/etc/ras/ops-alt/ops.json", "journal-replicator", "python", "-m",
                  "app.cli.journal_replica", "ship", "--once")
    check("[p1] another journal_id is refused (exit 3, replica_journal_mismatch), nothing written",
          r.returncode == 3 and "replica_journal_mismatch" in r.stdout and listing() == versions_before,
          (r.returncode, r.stdout[-200:]))
    # The replicator's own credential cannot delete or overwrite shipped records.
    import boto3
    from botocore.config import Config
    o = c["offsite"]["journal"]
    rw = boto3.client("s3", endpoint_url=f"http://127.0.0.1:{o['port']}", aws_access_key_id=o["rw_user"],
                      aws_secret_access_key=o["rw_password"], region_name="us-east-1",
                      config=Config(s3={"addressing_style": "path"}))
    seg_key, seg_version = listing()[0]
    denied = []
    for attempt in (lambda: rw.put_object(Bucket=bucket, Key=seg_key, Body=b"x\n", IfNoneMatch="*"),
                    lambda: rw.delete_object(Bucket=bucket, Key=seg_key, VersionId=seg_version)):
        try:
            attempt()
            denied.append("allowed")
        except Exception as exc:  # noqa: BLE001
            denied.append(getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__))
    check("[p1] replica writer credential: conditional overwrite refused (412), version delete denied",
          denied[0] in ("PreconditionFailed", "412") and denied[1] in ("AccessDenied",), denied)
    scan_mails(root, data, check)


def replica_head_safe(c: dict) -> int:
    try:
        return replica_head(c, "i1")
    except Exception:  # noqa: BLE001
        return -1


def backup_with_poll(p: Project, root: Path, check: Checks, label: str):
    stop, seen = threading.Event(), []
    poller = threading.Thread(target=stage_poll, args=(p, root, stop, seen), daemon=True)
    poller.start()
    code, out = p.ras_ops("backup", timeout=3600)
    stop.set()
    poller.join()
    statuses = sorted({s for s in seen})
    check.metric(f"{label}_edge_statuses_during_run", statuses)
    return code, out, seen


def phase_backup(root: Path) -> None:
    c = ctx(root)
    check = Checks(root, "backup")
    data = load_run(root)
    p = Project(root, c, "p1")
    code, out, seen = backup_with_poll(p, root, check, "b1")
    check("[p1] scheduled backup (same argv as the systemd unit) succeeds", code == 0 and out.get("status") == "ok", out)
    check("[p1] during the window the edge served 503 maintenance, never a 502",
          (503, "maintenance") in seen and not any(s == 502 for s, _ in seen), sorted(set(seen)))
    check.metric("b1_window_seconds", out.get("window_seconds"))
    check.metric("b1_total_seconds", out.get("seconds"))
    check("[p1] writers back and ready, flag removed", p.readyz() == "200" and not (p.dir / "maintenance" / "enabled").exists())
    check("[p1] staging holds no plaintext or ciphertext after the run",
          [x for x in (p.dir / "staging").rglob("*") if x.is_file()] == [])
    b1 = out["backup_id"]
    s3 = d.offsite_client(c, "backup")
    bucket = c["offsite"]["backup"]["bucket"]
    cipher = s3.get_object(Bucket=bucket, Key=f"pilot-i1/backups/{b1}.tar.age")["Body"].read()
    meta = json.loads(s3.get_object(Bucket=bucket, Key=f"pilot-i1/backups/{b1}.json")["Body"].read())
    check("[p1] off-host object is age ciphertext, hash read back = metadata, no dump/object bytes in clear",
          cipher.startswith(b"age-encryption.org/v1\n") and hashlib.sha256(cipher).hexdigest() == meta["ciphertext_sha256"]
          and b"PGDMP" not in cipher and b"Synthetic drill evidence" not in cipher and meta["verified"] is True,
          {k: meta[k] for k in ("ciphertext_bytes", "journal_db_head_seq")})
    data.setdefault("backups", {})["b1"] = b1
    save_run(root, data)
    # Failure inside the window, writers reopen fine (object store down during capture).
    p.compose("stop", "minio")
    code, out, seen = backup_with_poll(p, root, check, "fail_minio")
    p.compose("up", "-d", "--wait", "--no-deps", "minio")
    check("[p1] capture fails mid-window (MinIO down): run failed, writers reopened, flag cleared",
          code == 1 and out.get("status") == "failed" and out.get("reopen") == "ok"
          and not (p.dir / "maintenance" / "enabled").exists(), out)
    check("[p1] no plaintext left after the failed capture", [x for x in (p.dir / "staging").rglob("*") if x.is_file()] == [])
    alerts = [m for _, m in mails(root)[-4:]]
    check("[p1] the failed run mailed backup_failed at once", any("backup_failed" in str(m["Subject"]) for m in alerts),
          [str(m["Subject"]) for m in alerts])
    # A database stopped mid-window is NOT a reopen failure: `compose start` brings dependencies back (kept as a check).
    p.compose("stop", "db")
    code, out, seen = backup_with_poll(p, root, check, "db_stopped")
    check("[p1] database stopped before the window: capture fails, start brings db back, writers ready, flag cleared",
          code == 1 and out.get("reopen") == "ok" and p.readyz() == "200"
          and not (p.dir / "maintenance" / "enabled").exists(), {k: out.get(k) for k in ("code", "reopen")})
    # Writers cannot become ready again (a recovery hold is open): page stays up, alert, operator reopens.
    r = p.compose("run", "--rm", "-T", "--no-deps", "recovery-init", "python", "-m", "app.cli.recovery", "hold")
    check("[p1] recovery hold opened before the run (readiness will stay closed)", r.returncode == 0)
    code, out, seen = backup_with_poll(p, root, check, "fail_reopen")
    flag = (p.dir / "maintenance" / "enabled").exists()
    page = edge(p, root, "/api/auth/csrf")
    check("[p1] readiness closed after the window: run failed, reopen failed, maintenance page stays (no silent outage)",
          code == 1 and out.get("reopen") == "failed" and flag and page.json() == {"error": "maintenance"},
          {k: out.get(k) for k in ("code", "status", "reopen")})
    check("[p1] maintenance_reopen_failed mailed at once",
          "maintenance_reopen_failed" in (out.get("alert") or {}).get("new", []), out.get("alert"))
    r = p.compose("run", "--rm", "-T", "--no-deps", "recovery-init", "python", "-m", "app.cli.recovery", "recover",
                  timeout=1800)
    code, out = p.ras_ops("open", timeout=1800)
    check("[p1] operator recovers, then ras_ops open lifts the page", r.returncode == 0 and code == 0
          and not (p.dir / "maintenance" / "enabled").exists() and p.readyz() == "200", out)
    # Retention with synthetic expired good backups and an incomplete upload.
    synthetic = [f"202608{day:02d}T020000Z-5e1f{day:04x}" for day in range(1, 7)]
    for sid in synthetic:
        s3.put_object(Bucket=bucket, Key=f"pilot-i1/backups/{sid}.tar.age", Body=b"synthetic ciphertext")
        s3.put_object(Bucket=bucket, Key=f"pilot-i1/backups/{sid}.json", Body=json.dumps({
            "format": "ras-backup-offsite/1", "backup_id": sid, "verified": True,
            "ciphertext_bytes": len(b"synthetic ciphertext")}).encode())
    s3.put_object(Bucket=bucket, Key="pilot-i1/backups/20260701T020000Z-0dd0dd00.tar.age", Body=b"incomplete")
    d.offsite_compose(root, c, "stop", "offsite-backup")
    code, out, seen = backup_with_poll(p, root, check, "fail_target")
    d.offsite_compose(root, c, "up", "-d", "--wait", "offsite-backup")
    current = {o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix="pilot-i1/backups/").get("Contents", [])}
    check("[p1] destination down: preflight refuses before any window, nothing pruned",
          code == 1 and str(out.get("code", "")).startswith("preflight_") and "window_seconds" not in out
          and all(f"pilot-i1/backups/{sid}.json" in current for sid in synthetic), out)
    data["i1"]["sessions"]["S"] = login(p, root, data["accounts"]["i1"]["a"]).jar()
    code, out, seen = backup_with_poll(p, root, check, "b2")
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix="pilot-i1/backups/").get("Contents", [])}
    good = sorted(k.rsplit("/", 1)[1][:-5] for k in keys if k.endswith(".json"))
    real = {k.rsplit("/", 1)[1][:-5] for k in current if k.endswith(".json")} - set(synthetic) | {out.get("backup_id")}
    expected = real | {sid for sid in synthetic if sid in sorted(real | set(synthetic))[-3:]}
    check("[p1] retention after a good run: every real (in-retention) backup kept, expired ones beyond the newest "
          "3 good pruned, expired incomplete upload pruned",
          code == 0 and out["status"] == "ok" and set(good) == expected and b1 in good
          and "pilot-i1/backups/20260701T020000Z-0dd0dd00.tar.age" not in keys, {"kept": sorted(good)})
    check.metric("retention_kept_ids", sorted(good))
    data["backups"]["b2"] = out["backup_id"]
    save_run(root, data)
    code, out, fresh = check_run(p, root)
    check("[p1] backup alerts resolved after the good run",
          not any(k.startswith(("backup_", "maintenance_")) for k in out.get("conditions", [])), out)
    scan_logs(p, root, check)


def restore_into(root: Path, c: dict, key: str, backup_id: str, check: Checks, *, local: Path | None = None,
                 without_replica: bool = False, expect: str = "ok") -> tuple[Project, dict, float]:
    p = new_project(root, c, key, installation="i1")
    args = ["restore", "--backup-id", backup_id, "--identity-file", str(root / "operator" / "identity.txt")]
    if local is not None:
        args += ["--local-journal", str(local)]
    if without_replica:
        args.append("--without-replica")
    t = time.monotonic()
    code, out = p.ras_ops(*args, timeout=3600)
    seconds = round(time.monotonic() - t, 2)
    if expect == "ok":
        check(f"[{key}] restore {backup_id} -> gate held closed", code == 0 and out.get("gate") not in (None, "open"), out)
    else:
        check(f"[{key}] restore refused with {expect}", code != 0 and out.get("refused") == expect, out)
        stage = [x for x in (p.dir / "staging").rglob("*") if x.is_file()]
        check(f"[{key}] refused restore leaves no plaintext and no journal", not stage and not p.journal.exists(),
              [str(x) for x in stage][:3])
    return p, out, seconds


def teardown(p: Project, check: Checks | None = None):
    r = p.compose("--profile", "ops", "down", "-v", "--remove-orphans", timeout=900)
    if check:
        check(f"[{p.key}] project torn down (own volumes only)", r.returncode == 0)


def phase_disaster(root: Path) -> None:
    c = ctx(root)
    check = Checks(root, "disaster")
    data = load_run(root)
    acc, i1 = data["accounts"]["i1"], data["i1"]
    p1 = Project(root, c, "p1")
    a = login(p1, root, acc["a"])
    admin = login(p1, root, acc["admin"])
    # (a) after b1: delete, revoke, reject, logout -- then the DB goes back to b1 with the current journal.
    del1 = i1["items"]["del1"]
    r = [a.post(f"/library/items/{del1['item']}/delete", {"confirm_delete": True}).status_code,
         admin.post(f"/admin/common-copies/{i1['common']}/revoke", {"confirm_revoke": True,
                                                                    "reason_code": "rights_withdrawn", "retain": []}).status_code,
         admin.post(f"/admin/review-requests/{i1['review_open']}/reject",
                    {"reason_code": "rights_not_established", "note": "synthetic"}).status_code,
         Client(p1, root, i1["sessions"]["S"]).post("/auth/logout").status_code]
    check("[a] after b2: private delete, common revoke, review reject, session logout", r[:3] == [200] * 3
          and r[3] in (200, 204), r)
    wait_shipped(p1)
    p1.compose("--profile", "ops", "stop", timeout=900)
    p2, out, seconds = restore_into(root, c, "p2", data["backups"]["b2"], check, local=p1.dir / "journal")
    check("[a] journal sources compared: local copy and replica equal", out.get("journal_relation") == "equal", out)
    t = time.monotonic()
    code, opened = p2.ras_ops("open", "--recover", timeout=3600)
    check("[a] recover replays the journal and opens", code == 0, opened)
    check.metric("a_restore_seconds", seconds)
    check.metric("a_recover_open_seconds", round(time.monotonic() - t, 2))
    a2, admin2, b2 = login(p2, root, acc["a"]), login(p2, root, acc["admin"]), login(p2, root, acc["b"])
    results = {"deleted_private": a2.get(f"/library/items/{del1['item']}").status_code,
               "revoked_common": b2.get(f"/files/{i1['common_file']}/original").status_code,
               "closed_review": admin2.get(f"/admin/review-requests/{i1['review_open']}/files/"
                                           f"{i1['items']['review']['file']}/original").status_code,
               "logged_out_session": Client(p2, root, i1["sessions"]["S"]).get("/auth/session").status_code,
               "kept_original": a2.get(f"/files/{i1['items']['keep']['file']}/original").status_code}
    check("[a] nothing resurrected: delete/revoke/closure/session hold, kept item readable",
          results == {"deleted_private": 404, "revoked_common": 404, "closed_review": 404, "logged_out_session": 401,
                      "kept_original": 200}, results)
    teardown(p1, check)
    # (b) host loss: backup on p2, more decisions shipped, one decision NOT shipped, then everything local is gone.
    i1["sessions"].update({k: login(p2, root, acc["a"]).jar() for k in ("T", "U")})
    code, out = p2.ras_ops("backup", timeout=3600)
    check("[b] backup on the restored deployment", code == 0 and out.get("status") == "ok", out)
    data["backups"]["b3"] = out["backup_id"]
    del2 = i1["items"]["del2"]
    r = [a2.post(f"/library/items/{del2['item']}/delete", {"confirm_delete": True}).status_code,
         Client(p2, root, i1["sessions"]["T"]).post("/auth/logout").status_code]
    check("[b] after b3: delete + logout, shipped", r[0] == 200 and r[1] in (200, 204))
    wait_shipped(p2)
    p2.compose("stop", "journal-replicator")
    unshipped = Client(p2, root, i1["sessions"]["U"]).post("/auth/logout").status_code
    lost = journal_state(p2.journal).head.seq - replica_head(c, "i1")
    check.metric("b_records_not_shipped_at_host_loss", lost)
    check("[b] one more logout while the replicator is stopped (the measured loss window)", unshipped in (200, 204)
          and lost >= 1)
    teardown(p2, check)
    for sub in ("journal", "ops-state", "staging", "maintenance"):
        shutil.rmtree(p2.dir / sub)
    check("[b] host lost: volumes, journal, state and staging of p2 are gone", not (p2.dir / "journal").exists())
    p3, out, seconds = restore_into(root, c, "p3", data["backups"]["b3"], check)
    check("[b] only the replica survived: journal source replica", out.get("journal_source") == "replica"
          and out.get("journal_relation") == "replica_only", out)
    t = time.monotonic()
    code, opened = p3.ras_ops("open", "--recover", timeout=3600)
    check("[b] recover + open on the new host", code == 0 and p3.readyz() == "200", opened)
    check.metric("b_restore_seconds", seconds)
    check.metric("b_recover_open_seconds", round(time.monotonic() - t, 2))
    check.metric("b_total_rto_seconds_measured", round(seconds + time.monotonic() - t, 2))
    a3 = login(p3, root, acc["a"])
    results = {"deleted_after_backup": a3.get(f"/library/items/{del2['item']}").status_code,
               "deleted_before_backup": a3.get(f"/library/items/{del1['item']}").status_code,
               "logged_out_shipped": Client(p3, root, i1["sessions"]["T"]).get("/auth/session").status_code,
               "kept_original": a3.get(f"/files/{i1['items']['keep']['file']}/original").status_code}
    check("[b] shipped decisions hold after host loss", results == {"deleted_after_backup": 404,
                                                                    "deleted_before_backup": 404,
                                                                    "logged_out_shipped": 401, "kept_original": 200},
          results)
    resurrected = Client(p3, root, i1["sessions"]["U"]).get("/auth/session").status_code
    check("[b] LIMIT (expected): the logout that was never shipped is lost with the host; the session works again",
          resurrected == 200, resurrected)
    i1["browser"] = {"project": "p3", "revoked_session": i1["sessions"]["T"], "deleted_item": del2["item"],
                     "deleted_item_before_backup": del1["item"], "keep": i1["items"]["keep"]}
    save_run(root, data)
    browser_exit = d.browser(root, "p3")
    check("[b] Chrome over HTTPS on the restored host: login, library, original, deleted 404, revoked session refused",
          browser_exit == 0, browser_exit)
    # (c) replica older than the backup: refused.
    p3.compose("stop", "journal-replicator")
    del3 = i1["items"]["del3"]
    check("[c] while the replicator is stopped: one more private delete", a3.post(
        f"/library/items/{del3['item']}/delete", {"confirm_delete": True}).status_code == 200)
    code, out = p3.ras_ops("backup", timeout=3600)
    check("[c] backup taken with the replica behind", code == 0, out)
    data["backups"]["b4"] = out["backup_id"]
    save_run(root, data)
    p4, out, _ = restore_into(root, c, "p4", data["backups"]["b4"], check, expect="journal_behind_backup")
    teardown(p4, check)
    p3.compose("start", "journal-replicator")
    wait_shipped(p3)
    # (d1) DB and local journal rolled back together, replica newer: detected, replica used, nothing resurrected.
    i1["sessions"]["V"] = login(p3, root, acc["a"]).jar()
    old = root / "journal-p3-at-b5"
    shutil.copytree(p3.dir / "journal", old)
    code, out = p3.ras_ops("backup", timeout=3600)
    check("[d] backup b5 with a copy of the journal taken at the same time", code == 0, out)
    data["backups"]["b5"] = out["backup_id"]
    save_run(root, data)
    # The copy taken just before b5 may lack nothing b5 needs: b5's window wrote no record.
    a3 = login(p3, root, acc["a"])
    del4 = i1["items"]["del4"]
    r = [a3.post(f"/library/items/{del4['item']}/delete", {"confirm_delete": True}).status_code,
         Client(p3, root, i1["sessions"]["V"]).post("/auth/logout").status_code]
    check("[d] after b5: delete + logout, shipped", r[0] == 200 and r[1] in (200, 204))
    wait_shipped(p3)
    p3.compose("--profile", "ops", "stop", timeout=900)
    p5, out, _ = restore_into(root, c, "p5", data["backups"]["b5"], check, local=old)
    check("[d] old DB + old local journal: replica is ahead -> detected and the replica chain is used",
          out.get("journal_relation") == "replica_ahead" and out.get("journal_source") == "replica", out)
    code, opened = p5.ras_ops("open", "--recover", timeout=3600)
    check("[d] recover replays the newer records", code == 0, opened)
    a5 = login(p5, root, acc["a"])
    results = {"deleted": a5.get(f"/library/items/{del4['item']}").status_code,
               "logged_out": Client(p5, root, i1["sessions"]["V"]).get("/auth/session").status_code}
    check("[d] nothing resurrected", results == {"deleted": 404, "logged_out": 401}, results)
    teardown(p3, check)
    # (d2) the same rollback forced with --without-replica: opens stale, then the replicator/check catch it.
    p6, out, _ = restore_into(root, c, "p6", data["backups"]["b5"], check, local=old, without_replica=True)
    check("[d2] operator override without the replica: chain = old local copy", out.get("journal_relation") == "local_only")
    code, opened = p6.ras_ops("open", "--recover", "--without-replica", timeout=3600)
    t_open = time.monotonic()
    stale = Client(p6, root, i1["sessions"]["V"]).get("/auth/session").status_code
    check("[d2] LIMIT (expected): the override opened a stale state (the logout after b5 is not applied)",
          code == 0 and stale == 200, (code, stale))
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and p6.state("replica-status.json").get("code") != "replica_fork":
        time.sleep(1)
    check("[d2] the replicator detects the fork and writes nothing",
          p6.state("replica-status.json").get("code") == "replica_fork")
    code, out, fresh = check_run(p6, root)
    fence = edge(p6, root, "/api/auth/csrf")
    check("[d2] check mails journal_rollback and fences the edge (503 maintenance)",
          "journal_rollback:replica_fork" in out.get("new", []) and fence.json() == {"error": "maintenance"}, out)
    check.metric("d2_seconds_from_open_to_fence", round(time.monotonic() - t_open, 2))
    code, out = p6.ras_ops("open", timeout=900)
    check("[d2] ras_ops open refuses while the journal diverges from the replica",
          code != 0 and str(out.get("refused", "")).startswith("journal_"), out)
    teardown(p6, check)
    # (e) tampered replica: refused.
    s3 = d.offsite_client(c, "journal")
    bucket = c["offsite"]["journal"]["bucket"]
    seg = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix="pilot-i1/journal/").get("Contents", [])
                 if "/segments/" in o["Key"])[0]
    body = s3.get_object(Bucket=bucket, Key=seg)["Body"].read()
    bad = s3.put_object(Bucket=bucket, Key=seg, Body=body.replace(b'"seq":2', b'"seq":3', 1))
    p7, out, _ = restore_into(root, c, "p7", data["backups"]["b5"], check, expect="replica_corrupt")
    s3.delete_object(Bucket=bucket, Key=seg, VersionId=bad["VersionId"], BypassGovernanceRetention=True)
    teardown(p7, check)
    data["live"] = "p5"
    save_run(root, data)
    scan_mails(root, data, check)


def phase_alerts(root: Path) -> None:
    c = ctx(root)
    check = Checks(root, "alerts")
    data = load_run(root)
    p = Project(root, c, data["live"])
    acc = data["accounts"]["i1"]
    code, out, fresh = check_run(p, root)
    check.metric("baseline_conditions", out.get("conditions"))

    def expect(label, key, *, resolved=False):
        code, out, fresh = check_run(p, root)
        bucket = out.get("resolved" if resolved else "new", [])
        check(f"[{p.key}] {label}", key in bucket and len(fresh) == 2 and code == 0, out)
    p.compose("run", "--rm", "-T", "--no-deps", "recovery-init", "python", "-m", "app.cli.recovery", "hold")
    expect("hold -> gate_closed:db_behind_journal mailed (hold record not mirrored until recover)", "gate_closed:db_behind_journal")
    p.compose("run", "--rm", "-T", "--no-deps", "recovery-init", "python", "-m", "app.cli.recovery", "recover",
              timeout=1800)
    expect("recover -> gate_closed resolved", "gate_closed:db_behind_journal", resolved=True)
    p.compose("stop", "api")
    expect("api stopped outside a window -> api_unavailable", "api_unavailable")
    p.compose("up", "-d", "--wait", "--no-deps", "api")
    expect("api back -> resolved", "api_unavailable", resolved=True)
    p.compose("stop", "worker", "beat")
    a = login(p, root, acc["a"])
    item = upload_without_worker(a)
    a.post(f"/library/items/{item}/delete", {"confirm_delete": True})
    time.sleep(70)
    expect("deletion past purge_due_at with cleanup stopped -> cleanup_overdue", "cleanup_overdue:overdue_private_deletions")
    p.compose("start", "worker", "beat")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        code, out, fresh = check_run(p, root)
        if "cleanup_overdue:overdue_private_deletions" in out.get("resolved", []):
            break
        time.sleep(15)
    check(f"[{p.key}] cleanup ran -> cleanup_overdue resolved", "cleanup_overdue:overdue_private_deletions"
          in out.get("resolved", []), out)
    lock = p.dir / "ops-state" / "maintenance.lock"
    lock.write_text(json.dumps({"operation": "backup", "since": "2026-01-01T00:00:00+00:00", "pid": 1}))
    expect("stale maintenance lock -> maintenance_stuck", "maintenance_stuck")
    lock.unlink()
    expect("lock gone -> resolved", "maintenance_stuck", resolved=True)
    cfg = root / "ops-config-i1" / "ops.json"
    original = cfg.read_text(encoding="utf-8")
    d.write_ops_config(root, c, "i1", min_free_bytes_staging=1 << 60)
    expect("staging free space below threshold -> disk_low:staging", "disk_low:staging")
    cfg.write_text(original, encoding="utf-8")
    expect("threshold back -> resolved", "disk_low:staging", resolved=True)
    creds = root / "secrets" / "backup_target_read_credentials"
    good = creds.read_text(encoding="utf-8")
    creds.write_text(json.dumps({"access_key_id": "m8b-nobody", "secret_access_key": "wrong-secret-value"}) + "\n")
    expect("backup destination unreadable -> check_incomplete", "check_incomplete:backup_target_target_denied")
    creds.write_text(good, encoding="utf-8")
    expect("credential back -> resolved", "check_incomplete:backup_target_target_denied", resolved=True)
    scan_mails(root, data, check)


def upload_without_worker(user: Client) -> str:
    content = f"# M8b overdue {secrets.token_hex(8)}\n\nSynthetic.\n".encode()
    notice = user.get("/uploads/notice").json()["version"]
    intent = user.post("/uploads/intents", {"acknowledged": True, "notice_version": notice, "file_kind": "markdown",
                                            "filename": "overdue.md", "byte_size": len(content),
                                            "sha256": hashlib.sha256(content).hexdigest()})
    sent = user.request("PUT", "/api/uploads/original", content=content,
                        headers={"x-csrf-token": user.c.cookies.get("ras_csrf", ""),
                                 "x-upload-intent": intent.json()["intent_token"],
                                 "content-type": "application/octet-stream"})
    return sent.json()["item_id"]


def phase_drill(root: Path) -> None:
    c = ctx(root)
    check = Checks(root, "drill")
    data = load_run(root)
    live = Project(root, c, data["live"])
    p = new_project(root, c, "drill", installation="i1")
    code, out = p.ras_ops("drill", "--identity-file", str(root / "operator" / "identity.txt"), "--record-state-dir",
                          str(live.dir / "ops-state"), timeout=3600)
    check("[drill] monthly drill: latest good backup restored into a scratch project, recovered, ready, torn down",
          code == 0 and out.get("drill") == "ok" and out["probe"].get("gate") == "open", out)
    check.metric("drill_restore_seconds", out.get("restore_seconds"))
    left = run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={p.name}"]).stdout.split()
    vols = run(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={p.name}"]).stdout.split()
    check("[drill] scratch project left nothing behind (containers, volumes, journal)", not left and not vols
          and not p.journal.exists(), {"containers": left, "volumes": vols})
    check("[drill] the scratch journal never shipped (replica head unchanged by the drill)",
          replica_head(c, "i1") == journal_state(live.journal).head.seq)
    code, out, fresh = check_run(live, root)
    check("[drill] restore_drill_overdue resolved on the live deployment",
          "restore_drill_overdue" not in out.get("conditions", []), out)


PHASES = {"upgrade": phase_upgrade, "install": phase_install, "replica": phase_replica, "backup": phase_backup,
          "disaster": phase_disaster, "alerts": phase_alerts, "drill": phase_drill}
