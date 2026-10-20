"""M8b alerts: each condition has a stable code; dedupe, repeat and recovery; mail carries no data.

``app.cli.ops.check`` runs end to end against a real journal file, in-memory off-host
buckets and the isolated SMTPS capture server (verified TLS, AUTH); nothing leaves the host.
"""

from __future__ import annotations

import email
import email.policy
import json
from datetime import timedelta

import pytest

from app.cli import ops as ops_cli
from app.ops import alerts, replica
from app.ops import state as state_file
from app.ops.config import AlertSettings, OpsSettings
from tests.m8a_tls import make_ca, make_leaf
from tests.m8b_fakes import T0, FakeS3, extend, new_journal, target_cfg
from tests.smtp_capture import Behaviour, CaptureServer

RECIPIENTS = ["oncall-a@ops.test", "oncall-b@ops.test"]


def alert_cfg(**over) -> AlertSettings:
    smtp = {"host": "127.0.0.1", "port": 465, "security": "implicit_tls", "sender": "alerts@ras.test"}
    return AlertSettings(recipients=RECIPIENTS, smtp=smtp, **over)


def healthy_facts(now=T0):
    return {"host": {"maintenance_lock": False}, "probe": {"status": "ok", "gate": "open", "overdue_tombstones": 0,
                                                          "overdue_private_deletions": 0},
            "replica": {"relation": "equal", "local_seq": 9, "replica_seq": 9, "unshipped_records": 0},
            "replicator": {"ok": True, "code": "ok", "checked_at": now.isoformat(), "unshipped_since": None},
            "backup": {"last_run": {"status": "ok", "reopen": "ok"}, "newest_good_taken_at": now.isoformat(),
                       "good_count": 3},
            "drill": {"last_success_at": now.isoformat()}, "disk": {"journal": 10 << 30, "staging": 50 << 30},
            "errors": []}


CASES = {
    "gate_closed:journal_torn": lambda f: f["probe"].update(gate="journal_torn"),
    "gate_closed:recovery_in_progress": lambda f: f["probe"].update(gate="recovery_in_progress"),
    "api_unavailable": lambda f: f.update(probe={"status": "unavailable"}),
    "cleanup_overdue:overdue_tombstones": lambda f: f["probe"].update(overdue_tombstones=2),
    "cleanup_overdue:overdue_private_deletions": lambda f: f["probe"].update(overdue_private_deletions=1),
    "journal_rollback:journal_behind_replica": lambda f: f["replica"].update(relation="journal_behind_replica"),
    "journal_rollback:replica_fork": lambda f: f["replica"].update(relation="replica_fork"),
    "replica_error:target_unavailable": lambda f: f["replica"].update(error="target_unavailable", relation=None),
    "replica_stale": lambda f: f["replicator"].update(checked_at=(T0 - timedelta(hours=1)).isoformat()),
    "replica_lag": lambda f: f["replicator"].update(unshipped_since=(T0 - timedelta(hours=1)).isoformat()),
    "backup_failed:capture_timeout": lambda f: f["backup"]["last_run"].update(status="failed", code="capture_timeout"),
    "maintenance_reopen_failed": lambda f: f["backup"]["last_run"].update(reopen="failed"),
    "backup_stale": lambda f: f["backup"].update(newest_good_taken_at=(T0 - timedelta(days=2)).isoformat()),
    "maintenance_stuck": lambda f: f["host"].update(maintenance_lock=True, maintenance_lock_timeout_seconds=600,
                                                    maintenance_lock_since=(T0 - timedelta(hours=2)).isoformat()),
    "restore_drill_overdue": lambda f: f["drill"].update(last_success_at=(T0 - timedelta(days=40)).isoformat()),
    "disk_low:journal": lambda f: f["disk"].update(journal=1000),
    "disk_low:staging": lambda f: f["disk"].update(staging=1000),
    "check_incomplete:backup_target_target_denied": lambda f: f["errors"].append("backup_target_target_denied"),
}


def test_healthy_facts_raise_nothing():
    assert alerts.evaluate(healthy_facts(), alert_cfg(), T0) == []


@pytest.mark.parametrize("key", sorted(CASES))
def test_each_condition_has_its_code(key):
    facts = healthy_facts()
    CASES[key](facts)
    found = [c.key for c in alerts.evaluate(facts, alert_cfg(), T0)]
    assert found == [key], found
    assert key.split(":")[0] in alerts.CODES


def test_api_down_inside_a_window_is_not_an_alert():
    facts = healthy_facts()
    facts["probe"] = {"status": "unavailable"}
    facts["host"]["maintenance_lock"] = True
    facts["host"]["maintenance_lock_since"] = T0.isoformat()
    assert alerts.evaluate(facts, alert_cfg(), T0) == []


def test_dedupe_repeat_and_recovery():
    renotify = timedelta(hours=24)
    cond = [alerts.Condition("backup_stale", {"age_hours": 30})]
    new, repeat, resolved, state = alerts.plan({}, cond, T0, renotify)
    assert [c.key for c in new] == ["backup_stale"] and not repeat and not resolved
    state = alerts.mark_sent(state, new, T0)
    new, repeat, resolved, state = alerts.plan(state, cond, T0 + timedelta(hours=1), renotify)
    assert not new and not repeat and not resolved
    new, repeat, resolved, state = alerts.plan(state, cond, T0 + timedelta(hours=25), renotify)
    assert [c.key for c in repeat] == ["backup_stale"]
    new, repeat, resolved, state = alerts.plan(state, [], T0 + timedelta(hours=26), renotify)
    assert resolved == ["backup_stale"] and state == {}


LEAKS = ["user@example.org", "/srv/ras-pilot/journal/revocations.jsonl", "#token=Abc123", "Paper Title Secret",
         "originals/0123456789abcdef0123456789abcdef", "AGE-SECRET-KEY-1XYZ"]


def test_rendered_mail_carries_codes_counts_and_times_only():
    conds = [alerts.Condition("gate_closed", {"reason": LEAKS[0]}),
             alerts.Condition("disk_low", {"which": LEAKS[1], "free_bytes": 12}),
             alerts.Condition("backup_failed", {"reason": LEAKS[2], "at": LEAKS[3]}),
             alerts.Condition("replica_error", {"reason": LEAKS[4]}),
             alerts.Condition("check_incomplete", {"which": LEAKS[5]})]
    msg = alerts.render(alert_cfg(), conds, [], ["gate_closed:" + LEAKS[0]], T0, LEAKS[0], RECIPIENTS[0])
    text = msg.as_string() + str(msg["Subject"]) + msg.get_content()
    for leak in LEAKS:
        assert leak not in text
    assert "free_bytes=12" in msg.get_content() and "gate_closed" in text and str(msg["To"]) == RECIPIENTS[0]


# --- end to end through app.cli.ops.check with the TLS capture ---------------------------------

@pytest.fixture
def smtp_capture(tmp_path):
    pki = tmp_path / "pki"
    ca = make_ca(pki)
    leaf = make_leaf(pki, ca, "smtp", ["127.0.0.1", "smtp.ops.test"])
    password = tmp_path / "smtp_password"
    password.write_text("synthetic-relay-password\n", encoding="utf-8")
    server = CaptureServer(str(leaf.cert), str(leaf.key), behaviour=Behaviour(
        implicit_tls=True, require_auth=("ops-relay", "synthetic-relay-password"))).start()
    yield server, ca.cert, password
    server.stop()


@pytest.fixture
def ops_env(tmp_path, smtp_capture, monkeypatch):
    server, ca_file, password = smtp_capture
    journal_path = tmp_path / "journal" / "revocations.jsonl"
    state = new_journal(journal_path, 2)
    recipients = tmp_path / "age-recipients.txt"
    recipients.write_text("age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq\n", encoding="ascii")
    cfg = OpsSettings(
        journal_replica=target_cfg(tmp_path, "ras-journal-replica"), backup_target=target_cfg(tmp_path, "ras-backups"),
        backup={"recipients_file": recipients, "staging_dir": tmp_path / "staging"},
        alerts={"recipients": RECIPIENTS, "renotify_hours": 24, "smtp": {
            "host": "127.0.0.1", "port": server.port, "security": "implicit_tls", "sender": "alerts@ras.test",
            "username": "ops-relay", "password_file": password, "ca_file": ca_file,
            "connect_timeout_seconds": 3, "total_timeout_seconds": 6}},
        state_dir=tmp_path / "ops-state")
    (tmp_path / "staging").mkdir()
    buckets = {"ras-journal-replica": FakeS3(), "ras-backups": FakeS3()}
    monkeypatch.setattr(ops_cli, "Target", lambda c, read_only=False: __import__("app.ops.s3", fromlist=["Target"])
                        .Target(c, client=buckets[c.bucket]))

    class Settings:
        class recovery:
            pass
    Settings.recovery.journal_path = journal_path
    clock = {"now": T0}
    monkeypatch.setattr(ops_cli, "utcnow", lambda: clock["now"])
    return cfg, Settings, buckets, server, journal_path, state, clock


def mails(server):
    return [email.message_from_bytes(m.data, policy=email.policy.default) for m in server.messages]


def host_ok():
    return {"host": {"maintenance_lock": False},
            "probe": {"status": "ok", "gate": "open", "overdue_tombstones": 0, "overdue_private_deletions": 0}}


def test_check_mails_dedupes_and_announces_recovery(ops_env):
    cfg, settings, buckets, server, journal_path, state, clock = ops_env
    code, summary = ops_cli.check(cfg, settings, host_ok())
    assert code == 0 and summary["mailed"] == 2 and summary["delivery_error"] is None
    assert {"backup_stale", "replica_stale", "restore_drill_overdue"} <= set(summary["new"])
    sent = mails(server)
    assert sorted(str(m["To"]) for m in sent) == sorted(RECIPIENTS)
    assert all(m.tls and m.auth_user == "ops-relay" for m in server.messages)
    body = sent[0].get_content()
    assert "runbook: docs/m8b-pilot-operations.md#alert-backup-stale" in body and str(journal_path) not in body
    # Same conditions a minute later: nothing new is mailed.
    clock["now"] = T0 + timedelta(minutes=1)
    code, summary = ops_cli.check(cfg, settings, host_ok())
    assert code == 0 and summary["mailed"] == 0 and summary["new"] == [] and summary["repeat"] == []
    # Make every condition true-good: replicate, record a drill and a good backup -> recovery notice.
    target = ops_cli.Target(cfg.journal_replica)
    replica.record_status(cfg.state_dir, clock["now"], replica.ship_once(journal_path, target, clock["now"]), None)
    state_file.write(cfg.state_dir / "drill-state.json", {"last_success_at": clock["now"].isoformat()})
    buckets["ras-backups"].objects["pilot/backups/20261020T085900Z-00000001.tar.age"] = b"c" * 10
    buckets["ras-backups"].objects["pilot/backups/20261020T085900Z-00000001.json"] = json.dumps(
        {"format": "ras-backup-offsite/1", "backup_id": "20261020T085900Z-00000001", "verified": True,
         "ciphertext_bytes": 10}).encode()
    state_file.write(cfg.state_dir / "backup-state.json", {"last_run": {"status": "ok", "reopen": "ok"}})
    before = len(server.messages)
    code, summary = ops_cli.check(cfg, settings, host_ok())
    assert code == 0 and summary["conditions"] == [] and len(summary["resolved"]) >= 3
    resolved_mail = mails(server)[before:]
    assert len(resolved_mail) == 2 and "RESOLVED backup_stale" in resolved_mail[0].get_content()


def test_check_flags_journal_rollback_for_the_fence(ops_env):
    cfg, settings, buckets, server, journal_path, state, clock = ops_env
    target = ops_cli.Target(cfg.journal_replica)
    old = journal_path.read_bytes()
    extend(journal_path, state, 1)
    replica.ship_once(journal_path, target, T0)
    journal_path.write_bytes(old)  # DB + local journal rolled back together; the replica is newer
    code, summary = ops_cli.check(cfg, settings, host_ok())
    assert "journal_rollback:journal_behind_replica" in summary["conditions"] and summary["fence"] is True


def test_failed_alert_delivery_exits_nonzero_and_retries_next_time(ops_env):
    cfg, settings, buckets, server, journal_path, state, clock = ops_env
    server.stop()
    code, summary = ops_cli.check(cfg, settings, host_ok())
    assert code == 5 and summary["delivery_error"] in ("smtp_connection_failed", "smtp_timeout")
    saved = state_file.read(cfg.state_dir / "alerts-state.json")
    assert summary["failed"] == 2 and saved["recipients"] and all(v == {} for v in saved["recipients"].values())
    # nothing marked as sent: both recipients are tried again next run
    assert "@" not in json.dumps(saved)  # recipients are keyed by hash, not address


def test_ops_check_needs_no_database_settings(tmp_path, monkeypatch):
    """ops-check carries no DB secret: the journal path comes from its own variable only."""
    monkeypatch.setenv("RAS_RECOVERY__JOURNAL_PATH", str(tmp_path / "revocations.jsonl"))
    assert ops_cli._journal_path() == tmp_path / "revocations.jsonl"
    monkeypatch.delenv("RAS_RECOVERY__JOURNAL_PATH")
    with pytest.raises(ops_cli.Refused) as refused:
        ops_cli._journal_path()
    assert refused.value.code == "journal_not_configured"


# --- PR #67 P2-2: every recipient independently, dedupe per recipient -------------------------------

def _flaky_send(monkeypatch, failing: set, log: list):
    from app import delivery

    def send(self, msg, recipient):
        log.append((recipient, str(msg["Subject"])))
        if recipient in failing:
            raise delivery.DeliveryFailed("smtp_rejected")
    monkeypatch.setattr(delivery.SmtpDelivery, "send_email", send)


def test_a_rejected_recipient_does_not_silence_the_others(ops_env, monkeypatch):
    cfg, settings, buckets, server, journal_path, state, clock = ops_env
    a, b = cfg.alerts.recipients
    log = []
    _flaky_send(monkeypatch, {a}, log)
    code, summary = ops_cli.check(cfg, settings, host_ok())
    assert code == 5 and summary["mailed"] == 1 and summary["failed"] == 1
    assert summary["delivery_errors"] == {"smtp_rejected": 1}
    assert [r for r, _ in log] == [a, b]
    clock["now"] = T0 + timedelta(minutes=5)
    log.clear()
    code, summary = ops_cli.check(cfg, settings, host_ok())
    assert code == 5 and [r for r, _ in log] == [a]  # A retried, B not mailed again


def test_a_failing_second_recipient_does_not_repeat_mail_to_the_first(ops_env, monkeypatch):
    cfg, settings, buckets, server, journal_path, state, clock = ops_env
    a, b = cfg.alerts.recipients
    log = []
    _flaky_send(monkeypatch, {b}, log)
    ops_cli.check(cfg, settings, host_ok())
    for minutes in (5, 10):
        clock["now"] = T0 + timedelta(minutes=minutes)
        log.clear()
        code, _ = ops_cli.check(cfg, settings, host_ok())
        assert code == 5 and [r for r, _ in log] == [b]


def test_resolved_goes_only_to_recipients_who_were_alerted(ops_env, monkeypatch):
    cfg, settings, buckets, server, journal_path, state, clock = ops_env
    a, b = cfg.alerts.recipients
    log = []
    _flaky_send(monkeypatch, {b}, log)
    ops_cli.check(cfg, settings, host_ok())  # A alerted, B failed
    # Every condition clears; B's relay works again.
    clock["now"] = T0 + timedelta(minutes=1)
    target = ops_cli.Target(cfg.journal_replica)
    replica.record_status(cfg.state_dir, clock["now"], replica.ship_once(journal_path, target, clock["now"]), None)
    state_file.write(cfg.state_dir / "drill-state.json", {"last_success_at": clock["now"].isoformat()})
    buckets["ras-backups"].objects["pilot/backups/20261020T085900Z-00000001.tar.age"] = b"c" * 10
    buckets["ras-backups"].objects["pilot/backups/20261020T085900Z-00000001.json"] = json.dumps(
        {"format": "ras-backup-offsite/1", "backup_id": "20261020T085900Z-00000001", "verified": True,
         "ciphertext_bytes": 10}).encode()
    state_file.write(cfg.state_dir / "backup-state.json", {"last_run": {"status": "ok", "reopen": "ok"}})
    log.clear()
    _flaky_send(monkeypatch, set(), log)
    code, summary = ops_cli.check(cfg, settings, host_ok())
    assert code == 0 and summary["conditions"] == []
    assert [r for r, _ in log] == [a] and "resolved" in log[0][1]  # B never got the alert, so no RESOLVED
