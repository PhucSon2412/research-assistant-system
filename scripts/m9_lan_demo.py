"""M9 LAN demo on the owner's Windows machine (host Conda Python; never .env, never ``ras``, never a provider key).

    python scripts/m9_lan_demo.py inventory --out <file>             # Docker/firewall/task snapshot (no secrets)
    python scripts/m9_lan_demo.py prepare --lan-ip <ip>              # dirs, demo CA, secrets, age key, env, overlays
    python scripts/m9_lan_demo.py offsite-up                         # same-machine MinIO: buckets, users, policies
    python scripts/m9_lan_demo.py verify                             # binds, other interfaces closed, HTTPS via LAN IP
    python scripts/m9_lan_demo.py rebind --lan-ip <ip>               # DHCP changed: new leaf cert + env, then `up -d`
    python scripts/m9_lan_demo.py schedule install|remove            # "RAS pilot backup" 03:00, "RAS pilot check" 15 min
    python scripts/m9_lan_demo.py firewall-commands                  # elevated add/remove commands (printed, not run)
    python scripts/m9_lan_demo.py uninstall --yes                    # tasks, projects/volumes (label-checked), tags
    python scripts/m9_lan_demo.py provider --routes <r ...> [--max-output-tokens N] [--apply]  # M7c: key, pricing
    python scripts/m9_lan_demo.py provider --off [--apply]            # back to provider off
    python scripts/m9_lan_demo.py web-image --image <local-tag> [--apply]  # M10: recreate web only

Everything lives under ``--root`` (default ``%USERPROFILE%\\ras-pilot``), outside the repository. The
deploy itself is the reviewed ``deploy/compose.yaml`` of the release commit, copied with ``git archive``
into ``<root>\\release`` so a later branch checkout never changes what runs; this script only adds
an overlay (edge published on the Ethernet address and port 8443 only, site address with that port,
API origins with the port, the network to the same-machine MinIO) and never touches application code.

Secrets are generated once and never printed. The SMTP password is only mounted as a Compose secret;
this script reads ``smtp_username`` (the Gmail address) to fill the env file and ops config, both
outside the repo. Provider key stays empty and pricing false until ``provider`` (M7c) enables the routes
with live evidence; the key is then read from the owner's ``secrets/openrouter_api_key`` into the
owner-only env file, never printed. The replica/backup destination is a
MinIO on the SAME machine (own project/volume): it is not an independent failure domain, which the
owner accepted for this demo.
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import io
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RELEASE_COMMIT = "7009a8483e4f9891f11a966f114df136c90c4953"  # merge PR #68, rc2 source
IMAGES = {  # alias tag -> (source tag, expected image ID) of release candidate rc2 (docs/m8c-release-candidate.md)
    "ras-pilot-api:rc2": ("rasm8c-api:rc2", "sha256:c0936a8189df7335187c457ff56e8c4ff21aebbe338ed7e4240e5c14ec730a06"),
    "ras-pilot-web:rc2": ("rasm8c-web:rc", "sha256:59cb514163f9d7f8f5b5163977966ae456f83957e6912f72f59478c5c1dc2cb3"),
    "ras-pilot-ops:rc2": ("rasm8c-ops:rc2", "sha256:22656406b2a9ed9d1697baf4cf754216c0e2b481dd14d4a10268bfe349e066fc"),
}
PROJECT, DRILL, OFFSITE = "ras-pilot", "ras-pilot-drill", "ras-pilot-offsite"
LABEL = "ras.m9.demo"
TASKS = {"RAS pilot backup": "backup", "RAS pilot check": "check"}
FIREWALL_RULE = "RAS pilot LAN demo HTTPS"
SECRET_FILES = ("edge_token", "rate_limit_key", "journal_replica_credentials", "journal_replica_read_credentials",
                "backup_target_credentials", "backup_target_read_credentials")
CLEAN_PREFIXES = ("RAS_", "POSTGRES_", "MINIO_", "REDIS_", "OPENROUTER_", "COMPOSE_", "OFFSITE_")


def default_root() -> Path:
    return Path(os.environ["USERPROFILE"]) / "ras-pilot"


def clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(CLEAN_PREFIXES)}


def run(cmd, *, check=True, env_extra: dict | None = None, **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    if isinstance(kw.get("input"), str):  # bytes on stdin: text mode would turn LF into CRLF on Windows
        kw["input"] = kw["input"].encode("utf-8")
    result = subprocess.run(cmd, env={**clean_env(), **(env_extra or {})}, **kw)
    result.stdout = (result.stdout or b"").decode("utf-8", "replace")
    result.stderr = (result.stderr or b"").decode("utf-8", "replace")
    if check and result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {cmd[:4]} ... {result.stderr[-400:]}")
    return result


def powershell(script: str, *, check=True) -> subprocess.CompletedProcess:
    return run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], check=check)


def write(path: Path, data: str | bytes) -> None:
    """LF bytes, never the Windows text-mode CRLF (Compose secrets/env files are read inside Linux)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def restrict_to_user(path: Path) -> None:
    """Private keys: only the current user (no inherited Administrators/SYSTEM entries)."""
    user = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
    run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"])


# --- state -------------------------------------------------------------------------------------

def layout(root: Path) -> dict:
    return {"root": root, "secrets": root / "secrets", "ca": root / "ca", "public": root / "public",
            "tls": root / "tls.d", "release": root / "release", "pilot": root / "pilot", "drill": root / "drill",
            "offsite": root / "offsite", "handoff": root / "operator-handoff", "bin": root / "bin",
            "logs": root / "logs", "evidence": root / "evidence"}


def load_state(root: Path) -> dict:
    try:
        return json.loads((root / "state.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"{root} is not prepared (run prepare)") from None


def save_state(root: Path, state: dict) -> None:
    write(root / "state.json", json.dumps(state, indent=1, sort_keys=True) + "\n")


def compose_cmd(state: dict, which: str) -> list[str]:
    root = Path(state["root"])
    if which == OFFSITE:
        return ["docker", "compose", "--env-file", str(root / "offsite" / "offsite.env"), "-p", OFFSITE,
                "-f", str(root / "offsite" / "compose.yaml")]
    d = root / ("pilot" if which == PROJECT else "drill")
    return ["docker", "compose", "--env-file", str(d / f"{'pilot' if which == PROJECT else 'drill'}.env"), "-p", which,
            "-f", str(root / "release" / "deploy" / "compose.yaml"), "-f", str(d / "override.yaml")]


# --- inventory ---------------------------------------------------------------------------------

def inventory(args) -> int:
    def lines(cmd):
        return [line for line in run(cmd).stdout.splitlines() if line.strip()]
    snap = {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "containers": sorted(lines(["docker", "ps", "-a", "--no-trunc", "--format",
                                    "{{.ID}} {{.Names}} {{.State}} {{.Label \"com.docker.compose.project\"}}"])),
        "volumes": sorted(lines(["docker", "volume", "ls", "--format", "{{.Name}}"])),
        "networks": sorted(lines(["docker", "network", "ls", "--no-trunc", "--format", "{{.ID}} {{.Name}}"])),
        "images": sorted(set(lines(["docker", "image", "ls", "-q", "--no-trunc"]))),
        "image_tags": sorted(lines(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}",
                                    "--filter", "reference=ras-pilot-*"])),
        "firewall_rules": sorted(lines(["powershell.exe", "-NoProfile", "-Command",
                                        "Get-NetFirewallRule -DisplayName 'RAS pilot*' -ErrorAction SilentlyContinue"
                                        " | ForEach-Object { $_.DisplayName }"])),
        "scheduled_tasks": sorted(lines(["powershell.exe", "-NoProfile", "-Command",
                                         "Get-ScheduledTask -TaskName 'RAS pilot*' -ErrorAction SilentlyContinue"
                                         " | ForEach-Object { $_.TaskName }"])),
    }
    snap["counts"] = {k: len(snap[k]) for k in ("containers", "volumes", "networks", "images")}
    write(Path(args.out), json.dumps(snap, indent=1) + "\n")
    print(json.dumps({"inventory": str(args.out), **snap["counts"], "firewall_rules": snap["firewall_rules"],
                      "scheduled_tasks": snap["scheduled_tasks"]}))
    return 0


# --- PKI ---------------------------------------------------------------------------------------

PRIVATE_NETS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


def make_demo_ca(ca_dir: Path, public_dir: Path, host: str) -> None:
    """Root for this demo only, name-constrained to the demo host and private addresses."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "RAS LAN demo CA"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Research Assistant System demo")])
    constraints = x509.NameConstraints(
        permitted_subtrees=[x509.DNSName(host)] + [x509.IPAddress(ipaddress.ip_network(n)) for n in PRIVATE_NETS],
        excluded_subtrees=None)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=3 * 365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(constraints, critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    ca_dir.mkdir(parents=True, exist_ok=True)
    restrict_to_user(ca_dir)
    write(ca_dir / "demo-ca.key", key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    write(ca_dir / "demo-ca.crt", pem)
    write(public_dir / "ras-demo-ca.crt", pem)  # what demo devices install (certificate only)


def make_site_cert(ca_dir: Path, tls_dir: Path, host: str, lan_ip: str) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    ca_cert = x509.load_pem_x509_certificate((ca_dir / "demo-ca.crt").read_bytes())
    ca_key = serialization.load_pem_private_key((ca_dir / "demo-ca.key").read_bytes(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=397))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host),
                                                        x509.IPAddress(ipaddress.ip_address(lan_ip))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=False, crl_sign=False,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    write(tls_dir / "site.key", key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
    write(tls_dir / "site.crt", cert.public_bytes(serialization.Encoding.PEM))
    write(tls_dir / "site.caddy", "tls /etc/caddy/tls.d/site.crt /etc/caddy/tls.d/site.key\n")


# --- prepare -----------------------------------------------------------------------------------

def check_lan_ip(lan_ip: str) -> str:
    """The address must belong to the 'Ethernet' interface right now (never Tailscale/Radmin/0.0.0.0)."""
    out = powershell("Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { $_.InterfaceAlias + '|' + "
                     "$_.IPAddress + '|' + $_.PrefixLength }").stdout
    for line in out.splitlines():
        alias, _, rest = line.partition("|")
        address, _, prefix = rest.partition("|")
        if address.strip() == lan_ip:
            if alias.strip() != "Ethernet":
                raise SystemExit(f"{lan_ip} belongs to {alias!r}, not Ethernet")
            return f"{lan_ip}/{prefix.strip()}"
    raise SystemExit(f"{lan_ip} is not an address of this machine")


def verify_images() -> None:
    for alias, (source, expected) in IMAGES.items():
        for ref in (source, alias):
            found = run(["docker", "image", "inspect", "--format", "{{.Id}}", ref], check=False).stdout.strip()
            if ref == alias and not found:
                run(["docker", "image", "tag", expected, alias])
                found = run(["docker", "image", "inspect", "--format", "{{.Id}}", alias]).stdout.strip()
            if found != expected:
                raise SystemExit(f"{ref} is {found or 'missing'}, expected {expected} (never rebuild: stop and report)")


def export_release(release: Path) -> None:
    if release.exists():
        return
    commit = run(["git", "-C", str(REPO), "rev-parse", "--verify", RELEASE_COMMIT + "^{commit}"]).stdout.strip()
    data = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", commit, "deploy", "scripts/ras_ops.py"],
                          capture_output=True, check=True, env=clean_env()).stdout
    release.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(release, filter="data")
    write(release / "RELEASE", f"{commit}\n")


def used_networks() -> list:
    nets = []
    for ident in run(["docker", "network", "ls", "-q"]).stdout.split():
        for net in json.loads(run(["docker", "network", "inspect", ident]).stdout or "[]"):
            for cfg in (net.get("IPAM") or {}).get("Config") or []:
                if cfg.get("Subnet") and ":" not in cfg["Subnet"]:
                    nets.append(ipaddress.ip_network(cfg["Subnet"], strict=False))
    host = powershell("Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { $_.IPAddress + '/' + "
                      "$_.PrefixLength }").stdout.split()
    nets += [ipaddress.ip_interface(h).network for h in host]
    return nets


def free_subnet(taken: list) -> ipaddress.IPv4Network:
    for _ in range(300):
        subnet = ipaddress.ip_network(f"172.30.{10 + secrets.randbelow(240)}.0/24")
        if not any(subnet.overlaps(n) for n in taken):
            taken.append(subnet)
            return subnet
    raise SystemExit("no free private /24")


def free_port() -> int:
    for _ in range(200):
        candidate = 20000 + secrets.randbelow(25000)
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
        return candidate
    raise SystemExit("no free port")


def gmail_address(secrets_dir: Path) -> str:
    try:
        value = (secrets_dir / "smtp_username").read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        raise SystemExit("secrets/smtp_username is missing (the owner creates it)") from None
    if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value):
        raise SystemExit("secrets/smtp_username is not one e-mail address")
    pw = secrets_dir / "smtp_password"
    if not pw.exists() or pw.stat().st_size == 0:
        raise SystemExit("secrets/smtp_password is missing or empty (the owner creates it)")
    return value


def site_values(host: str, port: int, lan_ip: str) -> dict:
    # Hostname only: a bare-IP request carries no SNI and the reviewed Caddyfile has no default_sni,
    # so https://<ip>:<port> fails the handshake (verified); devices use the hosts line instead.
    origins = [f"https://{host}:{port}"]
    return {"RAS_PUBLIC_HOST": f"{host}:{port}", "RAS_EDGE_BIND": lan_ip, "RAS_EDGE_HTTPS_PORT": str(port),
            "RAS_EDGE_SITE_ADDRESSES": ", ".join(o.removeprefix("https://") for o in origins),
            "RAS_LAN_ALLOWED_ORIGINS": "'" + json.dumps(origins, separators=(",", ":")) + "'"}


def env_text(values: dict) -> str:
    return "# Generated by scripts/m9_lan_demo.py for the M9 LAN demo. Holds secrets: never commit/share.\n" + \
        "".join(f"{k}={v}\n" for k, v in values.items())


def pilot_env(state: dict, kind: str, gmail: str) -> dict:
    root = Path(state["root"])
    d = root / kind
    pw = state["passwords"]
    if kind == "pilot":
        site = site_values(state["host"], state["port"], state["lan_ip"])
        subnet = state["subnet"]
        ports = {"RAS_EDGE_HTTP_PORT": "8080"}  # rendered by the base file only; the overlay publishes 8443 alone
    else:
        site = {"RAS_PUBLIC_HOST": "drill." + state["host"], "RAS_EDGE_BIND": "127.0.0.1",
                "RAS_EDGE_HTTPS_PORT": str(state["drill_ports"][1])}
        subnet = state["drill_subnet"]
        ports = {"RAS_EDGE_HTTP_PORT": str(state["drill_ports"][0])}
    net = ipaddress.ip_network(subnet)
    return {
        "RAS_DEPLOY_PROJECT": PROJECT if kind == "pilot" else DRILL,
        **{k: alias for k, alias in (("RAS_API_IMAGE", "ras-pilot-api:rc2"),
                                     ("RAS_WEB_IMAGE", state.get("web_image", "ras-pilot-web:rc2") if kind == "pilot" else "ras-pilot-web:rc2"),
                                     ("RAS_OPS_IMAGE", "ras-pilot-ops:rc2"))},
        **site, **ports, "RAS_HSTS_MAX_AGE": "86400", "RAS_CADDY_TLS_DIR": (root / "tls.d").as_posix(),
        "RAS_BACKEND_SUBNET": str(net), "RAS_WEB_BACKEND_IP": str(net.network_address + 10),
        "RAS_SECRETS_DIR": (root / "secrets").as_posix(), "RAS_JOURNAL_DIR": (d / "journal").as_posix(),
        "RAS_OPS_CONFIG_DIR": (root / "pilot" / "ops-config").as_posix(),
        "RAS_OPS_STATE_DIR": (d / "ops-state").as_posix(), "RAS_OPS_STAGING_DIR": (d / "staging").as_posix(),
        "RAS_MAINTENANCE_DIR": (d / "maintenance").as_posix(),
        "POSTGRES_USER": "ras_migrator", "POSTGRES_DB": "ras", "POSTGRES_PASSWORD": pw["pg"],
        "RAS_RUNTIME_DB_USER": "ras_runtime", "RAS_RUNTIME_DB_PASSWORD": pw["runtime"],
        "RAS_RECOVERY_DB_USER": "ras_recovery", "RAS_RECOVERY_DB_PASSWORD": pw["recovery"],
        "RAS_DEFINER_DB_ROLE": "ras_definer", "REDIS_PASSWORD": pw["redis"],
        "MINIO_ROOT_USER": "ras-minio", "MINIO_ROOT_PASSWORD": pw["minio"],
        "RAS_SMTP_HOST": "smtp.gmail.com", "RAS_SMTP_PORT": "465", "RAS_SMTP_SECURITY": "implicit_tls",
        "RAS_SMTP_SENDER": gmail, "RAS_SMTP_SENDER_NAME": "Research Assistant System (demo)",
        "RAS_SMTP_USERNAME": gmail, "RAS_SMTP_PASSWORD_FILE": "/run/secrets/smtp_password", "RAS_SMTP_CA_FILE": "",
        "RAS_SMTP_CONNECT_TIMEOUT_SECONDS": "10", "RAS_SMTP_TOTAL_TIMEOUT_SECONDS": "20",
        **provider_values(state, kind), "RAS_DISCOVERY__ENABLED": "false"}


PROVIDER_ROUTES = ("embedding", "rerank", "generation")
KEY_FILE = "openrouter_api_key"


def provider_values(state: dict, kind: str) -> dict:
    """Provider off (M9 default) unless ``provider`` enabled routes (M7c); the drill always stays off.

    The key comes only from the owner's ``secrets/openrouter_api_key`` and is written only into the
    owner-only env file; it is never printed. A route's pricing gate opens only when it was enabled."""
    routes = ((state.get("provider") or {}).get("routes") or []) if kind == "pilot" else []
    key = ""
    if routes:
        try:
            key = (Path(state["root"]) / "secrets" / KEY_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            raise SystemExit("secrets/openrouter_api_key absent: the owner creates it; provider stays off") from None
        if not key or any(c.isspace() for c in key):
            raise SystemExit("secrets/openrouter_api_key malformed; provider stays off")
    values = {"OPENROUTER_API_KEY": key,
              **{f"RAS_{route.upper()}__PRICING_VERIFIED": str(route in routes).lower() for route in PROVIDER_ROUTES}}
    generation = (state.get("provider") or {}).get("generation") if kind == "pilot" else None
    if generation:  # M7c review: output limit of the demo only; the overlay passes these two to the API
        values["RAS_GENERATION__MAX_OUTPUT_TOKENS"] = str(generation["max_output_tokens"])
        values["RAS_GENERATION__MAX_REQUEST_USD"] = generation["max_request_usd"]
    return values


# GenerationPolicy defaults the admission ceiling is computed from (api/app/config.py): input token
# estimate and the input/output price ceilings per million tokens.
GENERATION_INPUT_TOKENS, GENERATION_INPUT_USD_PER_M, GENERATION_OUTPUT_USD_PER_M = 16_000, 2.5, 10.0


def generation_limits(max_output_tokens: int) -> dict:
    """Output limit and a request ceiling that still admits it (rounded up to the cent)."""
    if not 64 <= max_output_tokens <= 4096:
        raise SystemExit("max output tokens must be between 64 and 4096")
    ceiling = (GENERATION_INPUT_TOKENS * GENERATION_INPUT_USD_PER_M + max_output_tokens * GENERATION_OUTPUT_USD_PER_M) / 1e6
    cents = int(round(ceiling * 100, 9) // 1) + (round(ceiling * 100, 9) % 1 > 0)
    return {"max_output_tokens": max_output_tokens, "max_request_usd": f"{cents / 100:.2f}",
            "admission_ceiling_usd": round(ceiling, 6)}


def override_yaml(kind: str) -> str:
    label = f'{{{LABEL}: "{PROJECT}"}}'
    services = ["db", "redis", "minio", "migrate", "provision", "recovery-init", "web", "worker", "beat"]
    out = ["# Generated by scripts/m9_lan_demo.py: LAN demo overlay on the reviewed deploy/compose.yaml.", "services:"]
    for name in services:
        out += [f"  {name}:", f"    labels: {label}"]
    if kind == "pilot":
        out += ["  edge:", f"    labels: {label}",
                "    # Site addresses carry the port, so Caddy listens on it; only the Ethernet address publishes.",
                "    environment:", "      RAS_PUBLIC_HOST: ${RAS_EDGE_SITE_ADDRESSES:?set the LAN site addresses}",
                "    ports: !override",
                '      - "${RAS_EDGE_BIND:?set the Ethernet address}:${RAS_EDGE_HTTPS_PORT:?}:${RAS_EDGE_HTTPS_PORT:?}"',
                "  api:", f"    labels: {label}", "    environment:",
                "      RAS_HTTP__ALLOWED_ORIGINS: ${RAS_LAN_ALLOWED_ORIGINS:?set the LAN origins}",
                "      # M7c review: demo-only generation output limit (defaults = code defaults when unset).",
                "      RAS_GENERATION__MAX_OUTPUT_TOKENS: ${RAS_GENERATION__MAX_OUTPUT_TOKENS:-512}",
                "      RAS_GENERATION__MAX_REQUEST_USD: ${RAS_GENERATION__MAX_REQUEST_USD:-0.05}"]
    else:
        out += ["  edge:", f"    labels: {label}", "  api:", f"    labels: {label}"]
    out += ["  journal-replicator:", f"    labels: {label}", "    networks: {egress: {}, offsite: {}}",
            "  ops:", f"    labels: {label}", "    networks: {backend: {}, egress: {}, offsite: {}}",
            "  ops-check:", f"    labels: {label}", "    networks: {egress: {}, offsite: {}}",
            "networks:", "  offsite:", "    external: true", f"    name: {OFFSITE}_offsite", ""]
    return "\n".join(out)


def offsite_compose() -> str:
    minio = re.search(r"\$\{RAS_MINIO_IMAGE:-([^}]+@sha256:[0-9a-f]{64})\}",
                      (REPO / "deploy/compose.yaml").read_text(encoding="utf-8")).group(1)
    return f"""# Generated by scripts/m9_lan_demo.py: replica + backup destination of the M9 LAN demo.
# SAME MACHINE as the pilot (own project/volume): NOT an independent failure domain (owner accepted).
name: {OFFSITE}
services:
  offsite-minio:
    image: {minio}
    command: ["server", "/data"]
    environment:
      MINIO_ROOT_USER: ${{OFFSITE_ROOT_USER:?}}
      MINIO_ROOT_PASSWORD: ${{OFFSITE_ROOT_PASSWORD:?}}
    volumes: ["offsite-data:/data"]
    networks: {{offsite: {{aliases: [offsite-minio]}}}}
    labels: {{{LABEL}: "{PROJECT}"}}
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 5s
      retries: 30
    restart: unless-stopped
networks:
  offsite:
    internal: true
    labels: {{{LABEL}: "{PROJECT}"}}
volumes:
  offsite-data:
    labels: {{{LABEL}: "{PROJECT}"}}
"""


def ops_config(state: dict, gmail: str) -> dict:
    target = lambda which, name: {  # noqa: E731
        "endpoint_url": "http://offsite-minio:9000", "region": "us-east-1", "bucket": state["buckets"][which],
        "prefix": PROJECT, "credentials_file": f"/run/secrets/{name}_credentials",
        "read_credentials_file": f"/run/secrets/{name}_read_credentials", "addressing_style": "path",
        "require_versioning": True}
    example = json.loads((REPO / "deploy" / "ops.example.json").read_text(encoding="utf-8"))
    alerts = dict(example["alerts"])
    alerts.update({"recipients": [gmail], "subject_prefix": "[RAS demo]",
                   "smtp": {"host": "smtp.gmail.com", "port": 465, "security": "implicit_tls", "sender": gmail,
                            "sender_name": "RAS demo alerts", "username": gmail,
                            "password_file": "/run/secrets/smtp_password", "connect_timeout_seconds": 10,
                            "total_timeout_seconds": 20}})
    return {**example, "journal_replica": target("journal", "journal_replica"),
            "backup_target": target("backup", "backup_target"), "alerts": alerts}


def prepare(args) -> int:
    root = Path(args.root).resolve()
    if root == REPO or REPO in root.parents:
        raise SystemExit("root must be outside the repository")
    paths = layout(root)
    if (root / "state.json").exists():
        raise SystemExit("already prepared (use rebind for a new address)")
    lan = check_lan_ip(args.lan_ip)
    gmail = gmail_address(paths["secrets"])
    verify_images()
    export_release(paths["release"])
    for key in ("ca", "public", "tls", "handoff", "bin", "logs", "evidence", "offsite"):
        paths[key].mkdir(parents=True, exist_ok=True)
    for kind in ("pilot", "drill"):
        for sub in ("journal", "ops-state", "staging", "maintenance"):
            (paths[kind] / sub).mkdir(parents=True, exist_ok=True)
    (paths["pilot"] / "ops-config").mkdir(exist_ok=True)
    taken = used_networks()
    tag = secrets.token_hex(3)
    state = {"root": str(root), "host": args.host, "port": args.port, "lan_ip": args.lan_ip, "lan_cidr": lan,
             "subnet": str(free_subnet(taken)), "drill_subnet": str(free_subnet(taken)),
             "drill_ports": [free_port(), free_port()], "release_commit": RELEASE_COMMIT,
             "buckets": {"journal": f"ras-pilot-journal-{tag}", "backup": f"ras-pilot-backups-{tag}"},
             "passwords": {k: secrets.token_hex(24) for k in ("pg", "runtime", "recovery", "redis", "minio")}}
    # Demo CA (key restricted to the user) and the site leaf for host + LAN IP.
    if not (paths["ca"] / "demo-ca.key").exists():
        make_demo_ca(paths["ca"], paths["public"], args.host)
    make_site_cert(paths["ca"], paths["tls"], args.host, args.lan_ip)
    # Operator age key pair: only the public key goes to the deploy.
    import pyrage
    if not (paths["handoff"] / "age-identity.txt").exists():
        restrict_to_user(paths["handoff"])
        identity = pyrage.x25519.Identity.generate()
        write(paths["handoff"] / "age-identity.txt", f"# RAS demo backup key: move OFF this machine\n{identity}\n")
        write(paths["pilot"] / "ops-config" / "age-recipients.txt",
              f"# RAS demo backup recipient (public key)\n{identity.to_public()}\n")
    # Same-machine MinIO credentials and the deploy's generated secrets (never overwritten, never printed).
    off = {"root_user": "offsite-root", "root_password": secrets.token_hex(24)}
    creds = {}
    for which, name in (("journal", "journal_replica"), ("backup", "backup_target")):
        for role, suffix in (("rw", "credentials"), ("ro", "read_credentials")):
            user, password = f"{which}-{role}-{tag}", secrets.token_hex(24)
            off[f"{which}_{role}"] = user
            creds[f"{name}_{suffix}"] = json.dumps({"access_key_id": user, "secret_access_key": password})
    generated = {"edge_token": secrets.token_urlsafe(32), "rate_limit_key": secrets.token_hex(32), **creds}
    for name in SECRET_FILES:
        if (paths["secrets"] / name).exists():
            raise SystemExit(f"secrets/{name} already exists: refusing to overwrite")
    for name in SECRET_FILES:
        write(paths["secrets"] / name, generated[name] + "\n")
    write(paths["offsite"] / "offsite.env", env_text({"OFFSITE_ROOT_USER": off["root_user"],
                                                       "OFFSITE_ROOT_PASSWORD": off["root_password"]}))
    write(paths["offsite"] / "compose.yaml", offsite_compose())
    state["offsite_users"] = {k: v for k, v in off.items() if k != "root_password"}
    save_state(root, state)
    for kind in ("pilot", "drill"):
        write(paths[kind] / f"{kind}.env", env_text(pilot_env(state, kind, gmail)))
        write(paths[kind] / "override.yaml", override_yaml(kind))
    write(paths["pilot"] / "ops-config" / "ops.json", json.dumps(ops_config(state, gmail), indent=1) + "\n")
    shutil.copyfile(REPO / "deploy" / "lan-demo" / "run_ops_task.py", paths["bin"] / "run_ops_task.py")
    write(paths["bin"] / "task.json", json.dumps({
        "python": str(Path(sys.executable)), "ras_ops": str(paths["release"] / "scripts" / "ras_ops.py"),
        "env_file": str(paths["pilot"] / "pilot.env"), "project": PROJECT, "log_dir": str(paths["logs"]),
        "compose_files": [str(paths["release"] / "deploy" / "compose.yaml"), str(paths["pilot"] / "override.yaml")]},
        indent=1) + "\n")
    print(json.dumps({"prepared": str(root), "lan": lan, "subnet": state["subnet"], "drill_subnet":
                      state["drill_subnet"], "release": RELEASE_COMMIT}))
    return 0


def offsite_up(args) -> int:
    root = Path(args.root).resolve()
    state = load_state(root)
    run(compose_cmd(state, OFFSITE) + ["up", "-d", "--wait"], timeout=600)
    name = f"{OFFSITE}-offsite-minio-1"
    env = {}
    for line in (root / "offsite" / "offsite.env").read_text(encoding="utf-8").splitlines():
        k, sep, v = line.partition("=")
        if sep and not k.startswith("#"):
            env[k] = v
    creds = {n: json.loads((root / "secrets" / n).read_text(encoding="utf-8"))
             for n in SECRET_FILES if n.endswith("credentials")}
    # Root credential only through the container's own environment (MC_HOST_*), never argv.
    alias = f"http://{env['OFFSITE_ROOT_USER']}:{env['OFFSITE_ROOT_PASSWORD']}@127.0.0.1:9000"

    def mc(*a, check=True):
        return run(["docker", "exec", "-i", "-e", "MC_HOST_local", name, "mc", *a], check=check,
                   env_extra={"MC_HOST_local": alias})
    for which, name_ in (("journal", "journal_replica"), ("backup", "backup_target")):
        bucket = state["buckets"][which]
        if mc("ls", f"local/{bucket}", check=False).returncode != 0:
            if which == "journal":  # versioned + object lock: shipped records cannot be deleted for a day
                mc("mb", "--with-lock", f"local/{bucket}")
                mc("retention", "set", "--default", "GOVERNANCE", "1d", f"local/{bucket}")
            else:
                mc("mb", f"local/{bucket}")
                mc("version", "enable", f"local/{bucket}")
        write_actions = ["s3:GetObject", "s3:PutObject"] + (["s3:DeleteObject"] if which == "backup" else [])
        for role, suffix, actions in (("rw", "credentials", write_actions), ("ro", "read_credentials", ["s3:GetObject"])):
            policy = {"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": ["s3:GetBucketVersioning", "s3:GetBucketObjectLockConfiguration",
                                               "s3:ListBucket"], "Resource": [f"arn:aws:s3:::{bucket}"]},
                {"Effect": "Allow", "Action": actions, "Resource": [f"arn:aws:s3:::{bucket}/*"]}]}
            run(["docker", "exec", "-i", name, "sh", "-c", f"cat > /tmp/{which}-{role}.json"],
                input=json.dumps(policy))
            mc("admin", "policy", "create", "local", f"ras-{which}-{role}", f"/tmp/{which}-{role}.json")
            cred = creds[f"{name_}_{suffix}"]
            # User secret over stdin-fed shell variable, not argv of the host process.
            run(["docker", "exec", "-i", "-e", "MC_HOST_local", name, "sh", "-c",
                 'read -r u; read -r p; mc admin user add local "$u" "$p"'],
                input=f"{cred['access_key_id']}\n{cred['secret_access_key']}\n", env_extra={"MC_HOST_local": alias})
            mc("admin", "policy", "attach", "local", f"ras-{which}-{role}", "--user", cred["access_key_id"],
               check=False)
    print(json.dumps({"offsite": OFFSITE, "buckets": state["buckets"]}))
    return 0


# --- verify ------------------------------------------------------------------------------------

def verify(args) -> int:
    root = Path(args.root).resolve()
    state = load_state(root)
    results = {}
    out = powershell("Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { $_.InterfaceAlias + '|' + "
                     "$_.IPAddress }").stdout
    addrs = {line.split("|")[1]: line.split("|")[0] for line in out.splitlines() if "|" in line}
    results["ethernet_ip_still_ours"] = addrs.get(state["lan_ip"]) == "Ethernet"
    listen = powershell(f"Get-NetTCPConnection -State Listen -LocalPort {state['port']} -ErrorAction SilentlyContinue"
                        " | ForEach-Object { $_.LocalAddress }").stdout.split()
    results["listen_addresses"] = sorted(set(listen))
    results["only_lan_ip_listens"] = set(listen) == {state["lan_ip"]}
    others = {}
    for address, alias in sorted(addrs.items()):
        if address in (state["lan_ip"],) or address.startswith("169.254."):
            continue
        with socket.socket() as s:
            s.settimeout(2)
            others[f"{alias}:{address}"] = "open" if s.connect_ex((address, state["port"])) == 0 else "closed"
    results["other_interfaces"] = others
    results["other_interfaces_closed"] = all(v == "closed" for v in others.values())
    ctx = ssl.create_default_context(cafile=str(root / "public" / "ras-demo-ca.crt"))
    for label, host in (("hostname", state["host"]), ("ip", state["lan_ip"])):
        # Connect to the LAN address; for the hostname send it as SNI/Host exactly as a device with the hosts line does.
        try:
            with socket.create_connection((state["lan_ip"], state["port"]), timeout=10) as raw:
                with ctx.wrap_socket(raw, server_hostname=host) as tls:
                    tls.sendall(f"GET /login HTTP/1.1\r\nHost: {host}:{state['port']}\r\nConnection: close\r\n\r\n"
                                .encode())
                    head = tls.recv(4096).decode("latin-1").split("\r\n")[0]
            results[f"https_{label}"] = head
        except (OSError, ssl.SSLError) as exc:
            results[f"https_{label}"] = f"error: {type(exc).__name__}: {exc}"  # expected for "ip": no SNI
    results["ok"] = (results["ethernet_ip_still_ours"] and results["only_lan_ip_listens"]
                     and results["other_interfaces_closed"] and " 200" in results.get("https_hostname", ""))
    print(json.dumps(results, indent=1))
    return 0 if results["ok"] else 1


# --- rebind / schedule / firewall / uninstall --------------------------------------------------

def rebind(args) -> int:
    root = Path(args.root).resolve()
    state = load_state(root)
    state["lan_cidr"] = check_lan_ip(args.lan_ip)
    state["lan_ip"] = args.lan_ip
    gmail = gmail_address(root / "secrets")
    make_site_cert(root / "ca", root / "tls.d", state["host"], args.lan_ip)
    save_state(root, state)
    write(root / "pilot" / "pilot.env", env_text(pilot_env(state, "pilot", gmail)))
    print(json.dumps({"rebound": args.lan_ip, "next": "compose up -d edge api (see handoff); update the hosts line "
                      "on demo devices; then firewall-commands for the new address"}))
    return 0


def web_image(args) -> int:
    """M10: change one env line, persist the selection and recreate ONLY web.

    Does not regenerate provider configuration or read its key. The old env
    bytes (including credentials) are preserved except for RAS_WEB_IMAGE.
    Only local, explicitly tagged demo web images are accepted; never pull.
    """
    root = Path(args.root).resolve()
    state = load_state(root)
    image = args.image
    if not re.fullmatch(r"ras-pilot-web:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", image):
        raise SystemExit("expected a local ras-pilot-web:<tag> image")
    image_id = run(["docker", "image", "inspect", image, "--format", "{{.Id}}"]).stdout.strip()
    cmd = compose_cmd(state, PROJECT)
    ids = run(cmd + ["ps", "-aq"]).stdout.split()
    for identifier in ids:
        info = json.loads(run(["docker", "inspect", identifier]).stdout)[0]
        labels = info["Config"].get("Labels") or {}
        if labels.get(LABEL) != PROJECT or labels.get("com.docker.compose.project") != PROJECT:
            raise SystemExit("demo container ownership changed; no file written")
    env_path = root / "pilot" / "pilot.env"
    original = env_path.read_bytes()
    pattern = rb"(?m)^RAS_WEB_IMAGE=[^\r\n]*"
    if len(re.findall(pattern, original)) != 1:
        raise SystemExit("expected exactly one RAS_WEB_IMAGE line; no file written")
    updated = re.sub(pattern, b"RAS_WEB_IMAGE=" + image.encode("ascii"), original)
    previous = state.get("web_image", "ras-pilot-web:rc2")
    write(env_path, updated)  # existing owner-only ACL is retained
    state["web_image"] = image
    save_state(root, state)
    result = {"web_image": image, "image_id": image_id, "previous": previous}
    if args.apply:
        up = run(cmd + ["up", "-d", "--no-deps", "--wait", "--pull", "never", "--force-recreate", "web"], check=False)
        result["recreate_exit"] = up.returncode
        if up.returncode:
            result["rollback"] = f"web-image --image {previous} --apply"
    print(json.dumps(result))
    return 0 if result.get("recreate_exit", 0) == 0 else 1


def provider(args) -> int:
    """M7c: enable the provider for the routes with live evidence (or ``--off``); recreate api/worker/beat.

    Rewrites only the provider lines of pilot.env (all other values regenerate from state as in
    ``rebind``); image, deploy files and the drill env stay unchanged. Budget and quotas keep the
    application defaults ($10/day app budget, per-user quotas)."""
    root = Path(args.root).resolve()
    state = load_state(root)
    routes = [] if args.off else sorted(set(args.routes))
    unknown = set(routes) - set(PROVIDER_ROUTES)
    if unknown:
        raise SystemExit(f"unknown route(s) {sorted(unknown)}")
    previous = state.get("provider") or {}
    state["provider"] = {"routes": routes, "changed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    if "generation" in routes and (args.max_output_tokens or previous.get("generation")):
        state["provider"]["generation"] = (generation_limits(args.max_output_tokens) if args.max_output_tokens
                                           else previous["generation"])
    gmail = gmail_address(root / "secrets")
    values = pilot_env(state, "pilot", gmail)  # validates the key file before anything is written
    env_path = root / "pilot" / "pilot.env"
    shutil.copyfile(env_path, env_path.with_name("pilot.env.before-provider"))
    write(env_path, env_text(values))
    write(root / "pilot" / "override.yaml", override_yaml("pilot"))  # passes the generation limits to the API
    save_state(root, state)
    user = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
    for name in ("pilot.env", "pilot.env.before-provider"):  # the key file's neighbours: owner-only (0600)
        run(["icacls", str(root / "pilot" / name), "/inheritance:r", "/grant:r", f"{user}:F"])
    result = {"routes": routes, "key_present": bool(values["OPENROUTER_API_KEY"]),
              "pricing": {r: values[f"RAS_{r.upper()}__PRICING_VERIFIED"] for r in PROVIDER_ROUTES},
              "generation": state["provider"].get("generation")}
    if args.apply:
        up = run(compose_cmd(state, PROJECT) + ["up", "-d", "--wait", "--force-recreate", "api", "worker", "beat"],
                 check=False)
        result["recreate_exit"] = up.returncode
        if up.returncode:
            result["recreate_error"] = up.stderr[-400:]
    print(json.dumps(result))
    return 0 if result.get("recreate_exit", 0) == 0 else 1


def schedule(args) -> int:
    root = Path(args.root).resolve()
    load_state(root)
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    wrapper = root / "bin" / "run_ops_task.py"
    if args.action == "remove":
        for name in TASKS:
            powershell(f"Unregister-ScheduledTask -TaskName '{name}' -Confirm:$false -ErrorAction SilentlyContinue",
                       check=False)
        print(json.dumps({"removed": list(TASKS)}))
        return 0
    principal = ("$p = New-ScheduledTaskPrincipal -UserId \"$env:USERDOMAIN\\$env:USERNAME\" "
                 "-LogonType Interactive -RunLevel Limited;")
    for name, command in TASKS.items():
        if command == "backup":
            trigger = "$t = New-ScheduledTaskTrigger -Daily -At 03:00;"
            limit = "(New-TimeSpan -Hours 3)"
        else:
            trigger = ("$t = New-ScheduledTaskTrigger -Once -At (Get-Date).Date "
                       "-RepetitionInterval (New-TimeSpan -Minutes 15);")
            limit = "(New-TimeSpan -Minutes 10)"
        powershell(
            f"$a = New-ScheduledTaskAction -Execute '{pythonw}' -Argument '\"{wrapper}\" {command}' "
            f"-WorkingDirectory '{root}';" + trigger + principal +
            f"$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew "
            f"-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit {limit};"
            f"Register-ScheduledTask -TaskName '{name}' -Action $a -Trigger $t -Principal $p -Settings $s "
            f"-Description 'M9 LAN demo: scripts/ras_ops.py {command} (remove: m9_lan_demo.py schedule remove)' "
            f"-Force | Out-Null")
    print(json.dumps({"installed": list(TASKS)}))
    return 0


def firewall_commands(args) -> int:
    state = load_state(Path(args.root).resolve())
    print("# Run in an ELEVATED PowerShell (Administrator). One inbound rule, Ethernet only, LAN subnet only.")
    print(f"New-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -Direction Inbound -Action Allow -Protocol TCP "
          f"-LocalPort {state['port']} -LocalAddress {state['lan_ip']} -RemoteAddress LocalSubnet "
          f"-InterfaceAlias Ethernet -Profile Public")
    print("# Remove:")
    print(f"Remove-NetFirewallRule -DisplayName '{FIREWALL_RULE}'")
    return 0


def uninstall(args) -> int:
    if not args.yes:
        raise SystemExit("pass --yes: removes the RAS pilot tasks, the three projects with their volumes and tags")
    root = Path(args.root).resolve()
    state = load_state(root)
    schedule(argparse.Namespace(root=str(root), action="remove"))
    for project in (PROJECT, DRILL, OFFSITE):
        ids = run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]).stdout.split()
        for ident in ids:
            labels = json.loads(run(["docker", "inspect", ident, "--format", "{{json .Config.Labels}}"]).stdout)
            if labels.get("com.docker.compose.project") != project or labels.get(LABEL) != PROJECT:
                raise SystemExit(f"container {ident} of {project} lacks the demo label: stop, inspect by hand")
        extra = ["--profile", "ops"] if project != OFFSITE else []
        run(compose_cmd(state, project) + extra + ["down", "-v", "--remove-orphans"], timeout=900)
    for alias in IMAGES:
        run(["docker", "image", "rm", alias], check=False)
    print(json.dumps({"removed": [PROJECT, DRILL, OFFSITE], "tags": list(IMAGES),
                      "left": f"{root} (delete after the owner keeps what they need) and the firewall rule "
                              "(firewall-commands, elevated)"}))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=str(default_root()))
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("inventory")
    s.add_argument("--out", required=True)
    s = sub.add_parser("prepare")
    s.add_argument("--lan-ip", required=True)
    s.add_argument("--host", default="ras-demo.lan")
    s.add_argument("--port", type=int, default=8443)
    sub.add_parser("offsite-up")
    sub.add_parser("verify")
    s = sub.add_parser("rebind")
    s.add_argument("--lan-ip", required=True)
    s = sub.add_parser("web-image")
    s.add_argument("--image", required=True)
    s.add_argument("--apply", action="store_true", help="recreate only web, with --no-deps and no pull")
    s = sub.add_parser("schedule")
    s.add_argument("action", choices=["install", "remove"])
    sub.add_parser("firewall-commands")
    s = sub.add_parser("uninstall")
    s.add_argument("--yes", action="store_true")
    s = sub.add_parser("provider")
    group = s.add_mutually_exclusive_group(required=True)
    group.add_argument("--routes", nargs="+", choices=PROVIDER_ROUTES)
    group.add_argument("--off", action="store_true")
    s.add_argument("--apply", action="store_true", help="recreate api/worker/beat now (short maintenance window)")
    s.add_argument("--max-output-tokens", type=int, help="demo-only generation output limit (request ceiling follows)")
    args = p.parse_args()
    return {"inventory": inventory, "prepare": prepare, "offsite-up": offsite_up, "verify": verify,
            "rebind": rebind, "schedule": schedule, "firewall-commands": firewall_commands,
            "uninstall": uninstall, "provider": provider, "web-image": web_image}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
