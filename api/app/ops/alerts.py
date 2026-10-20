"""Minimal operational alerts (M8b): stable codes, thresholds, dedupe and recovery notices.

``evaluate`` turns collected facts into conditions; ``plan`` compares them with the
previous state (new, repeat after ``renotify_hours``, resolved); ``render`` makes one
plain-text email. Every value in a message passes ``safe``: integers, booleans,
ISO times and short codes only. Anything else (an email, a path, a token, a name)
becomes ``?``, so a fact that carries user data cannot leak into a mail.

Codes and their runbook section (docs/m8b-pilot-operations.md#alert-runbook):
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

CODES = {
    "gate_closed": "recovery gate/readiness closed (reason code attached)",
    "api_unavailable": "API not running or not answering, outside a maintenance window",
    "replica_error": "journal replicator stopped shipping (reason code attached)",
    "journal_rollback": "local journal is behind or diverged from the off-host replica",
    "replica_lag": "records not shipped for longer than the threshold",
    "replica_stale": "replicator status not updated (service down?)",
    "backup_failed": "latest backup run failed (step code attached)",
    "backup_stale": "no good off-host backup newer than the threshold",
    "maintenance_reopen_failed": "a maintenance window could not reopen writers",
    "maintenance_stuck": "maintenance lock held longer than its timeout",
    "cleanup_overdue": "revoked/deleted content past purge_due_at and not purged",
    "restore_drill_overdue": "no restore drill recorded within the threshold",
    "disk_low": "free space below threshold (which volume attached)",
    "check_incomplete": "a fact could not be collected (which one attached)",
}
_SAFE_CODE = re.compile(r"[a-z0-9_:.\-]{1,64}")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?([+-]\d{2}:\d{2}|Z)?")


def safe(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 1)
    if isinstance(value, str) and (_SAFE_CODE.fullmatch(value) or _ISO.fullmatch(value)):
        return value
    return "?"


@dataclass(frozen=True)
class Condition:
    code: str
    detail: dict

    @property
    def key(self) -> str:
        # Same code with another reason is a different alert (e.g. gate_closed:journal_torn).
        reason = self.detail.get("reason") or self.detail.get("which")
        return f"{self.code}:{safe(reason)}" if reason else self.code


def _age_seconds(now: datetime, iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return None


def evaluate(facts: dict, cfg, now: datetime) -> list[Condition]:
    """``facts`` from ``app.cli.ops check`` (host probe + container reads); ``cfg`` is ``AlertSettings``."""
    out: list[Condition] = []
    host = facts.get("host") or {}
    in_window = bool(host.get("maintenance_lock"))
    probe = facts.get("probe") or {}
    if probe.get("status") == "unavailable" and not in_window:
        out.append(Condition("api_unavailable", {}))
    elif probe.get("status") == "ok" and probe.get("gate") != "open":
        out.append(Condition("gate_closed", {"reason": probe.get("gate")}))
    for name in ("overdue_tombstones", "overdue_private_deletions"):
        if isinstance(probe.get(name), int) and probe[name] > 0:
            out.append(Condition("cleanup_overdue", {"which": name, "count": probe[name]}))
    replica = facts.get("replica") or {}
    rel = replica.get("relation")
    if rel in ("journal_behind_replica", "replica_fork", "replica_journal_mismatch"):
        out.append(Condition("journal_rollback", {"reason": rel, "local_seq": replica.get("local_seq"),
                                                  "replica_seq": replica.get("replica_seq")}))
    elif replica.get("error"):
        out.append(Condition("replica_error", {"reason": replica.get("error")}))
    status = facts.get("replicator") or {}
    status_age = _age_seconds(now, status.get("checked_at"))
    if status_age is None or status_age > cfg.replica_status_stale_seconds:
        out.append(Condition("replica_stale", {"age_seconds": int(status_age) if status_age is not None else None}))
    elif not status.get("ok") and not any(c.code == "journal_rollback" for c in out):
        out.append(Condition("replica_error", {"reason": status.get("code")}))
    lag = _age_seconds(now, status.get("unshipped_since"))
    pending = replica.get("unshipped_records")
    if lag is not None and lag > cfg.replica_lag_seconds:
        out.append(Condition("replica_lag", {"seconds": int(lag), "records": pending}))
    runs = facts.get("backup") or {}
    last = runs.get("last_run") or {}
    if last.get("status") == "failed":
        out.append(Condition("backup_failed", {"reason": last.get("code"), "at": last.get("finished_at")}))
    if last.get("reopen") == "failed":
        out.append(Condition("maintenance_reopen_failed", {"at": last.get("finished_at")}))
    newest = runs.get("newest_good_taken_at")
    age = _age_seconds(now, newest)
    if age is None or age > cfg.backup_max_age_hours * 3600:
        out.append(Condition("backup_stale", {"age_hours": round(age / 3600, 1) if age is not None else None,
                                              "good_count": runs.get("good_count")}))
    lock_age = _age_seconds(now, host.get("maintenance_lock_since"))
    if lock_age is not None and lock_age > host.get("maintenance_lock_timeout_seconds", 4 * 3600):
        out.append(Condition("maintenance_stuck", {"age_seconds": int(lock_age)}))
    drill = facts.get("drill") or {}
    drill_age = _age_seconds(now, drill.get("last_success_at"))
    if drill_age is None or drill_age > cfg.drill_max_age_days * 86400:
        out.append(Condition("restore_drill_overdue", {"age_days": round(drill_age / 86400, 1)
                                                       if drill_age is not None else None}))
    disks = facts.get("disk") or {}
    for which, minimum in (("journal", cfg.min_free_bytes_journal), ("staging", cfg.min_free_bytes_staging)):
        free = disks.get(which)
        if isinstance(free, int) and free < minimum:
            out.append(Condition("disk_low", {"which": which, "free_bytes": free}))
    for missing in facts.get("errors") or []:
        out.append(Condition("check_incomplete", {"which": missing}))
    return out


def plan(previous: dict, conditions: list[Condition], now: datetime, renotify: timedelta) -> tuple[list, list, list, dict]:
    """(new, repeat, resolved, next_state). ``previous`` maps key -> {first_seen, last_sent}."""
    current = {c.key: c for c in conditions}
    new, repeat, resolved, state = [], [], [], {}
    for key, cond in current.items():
        seen = previous.get(key)
        if seen is None:
            new.append(cond)
            state[key] = {"first_seen": now.isoformat(), "last_sent": None}
        else:
            state[key] = dict(seen)
            last = seen.get("last_sent")
            if last is None or now - datetime.fromisoformat(last) >= renotify:
                repeat.append(cond)
    for key, seen in previous.items():
        if key not in current:
            resolved.append(key)
    return new, repeat, resolved, state


def mark_sent(state: dict, sent: list[Condition], now: datetime) -> dict:
    for cond in sent:
        state[cond.key]["last_sent"] = now.isoformat()
    return state


def render(cfg, new: list, repeat: list, resolved: list, now: datetime, installation: str,
           recipient: str) -> EmailMessage | None:
    if not (new or repeat or resolved):
        return None
    smtp = cfg.smtp
    parts = []
    if new or repeat:
        parts.append("alert")
    if resolved:
        parts.append("resolved")
    codes = sorted({c.code for c in new + repeat} | {r.split(":", 1)[0] for r in resolved})
    msg = EmailMessage()
    msg["From"] = Address(smtp.sender_name or "", addr_spec=smtp.sender)
    msg["To"] = Address(addr_spec=recipient)
    msg["Subject"] = f"{cfg.subject_prefix} {'/'.join(parts)}: {', '.join(codes)}"[:200]
    msg["Date"] = format_datetime(now)
    msg["Message-ID"] = make_msgid(domain=smtp.sender.rsplit("@", 1)[1])
    msg["Auto-Submitted"] = "auto-generated"
    lines = [f"installation: {safe(installation)}", f"checked_at: {now.isoformat()}", ""]
    for title, items in (("NEW", new), ("STILL ACTIVE", repeat)):
        for cond in items:
            detail = " ".join(f"{k}={safe(v)}" for k, v in sorted(cond.detail.items()))
            lines.append(f"{title} {cond.code} {detail}".rstrip())
            lines.append(f"  runbook: docs/m8b-pilot-operations.md#alert-{cond.code.replace('_', '-')}")
    for key in resolved:
        lines.append(f"RESOLVED {safe(key)}")
    lines += ["", "Codes, counts and times only. Act per the runbook; do not reply to this message."]
    msg.set_content("\n".join(lines) + "\n")
    return msg
