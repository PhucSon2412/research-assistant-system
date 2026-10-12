"""C3 recovery case file: strict schema, kept outside the repo and backups.

The file is the human-managed record of one approved decision; trusted
provisioning registers the same values in ``auth_recovery_cases`` and the CLI
requires both to match exactly. Neither alone is authority: a self-declared
``verified`` flag or UUID cannot replace the registered binding to a real
operator login. Only IDs, codes and times belong here; identity evidence,
documents, emails and contact details stay in the restricted record outside
the journal and repo.
"""
from __future__ import annotations

import json
import os
import re
import stat
import uuid
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path

from app.journal import CASE_ACTIONS

SCHEMA = "ras.auth_recovery_case.v1"
FIELDS = ("schema", "case_id", "action", "installation_journal_id", "user_id", "expected_role",
          "expected_credential_operation_id", "expected_status_operation_id", "operator_id", "approver_id",
          "verification_method", "approved_at", "expires_at")
ROLES = ("admin", "user")
_METHOD = re.compile(r"[a-z][a-z0-9_]{2,40}")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")
_API = Path(__file__).resolve().parents[1]
# The source checkout (repo root) when present; in an image only the API tree exists.
_ROOTS = tuple(p for p in (_API, _API.parent) if p == _API or (p / "AGENTS.md").is_file())


class CaseError(Exception):
    """The case file or its values are unusable; ``code`` never contains a value."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RecoveryCase:
    case_id: uuid.UUID
    action: str
    journal_id: uuid.UUID
    user_id: uuid.UUID
    expected_role: str
    expected_credential_operation_id: uuid.UUID | None
    expected_status_operation_id: uuid.UUID | None
    operator_id: uuid.UUID
    approver_id: uuid.UUID
    verification_method: str
    approved_at: datetime
    expires_at: datetime

    @classmethod
    def from_row(cls, row) -> "RecoveryCase":
        """From an ``auth_recovery_cases`` row (registered by trusted provisioning)."""
        values = row._mapping
        return cls(**{f.name: values[f.name] for f in fields(cls)})


def _uuid(value, *, nullable=False, v4=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("uuid")
    parsed = uuid.UUID(value)
    if v4 and parsed.version != 4:
        raise ValueError("uuid_version")
    return parsed


def _time(value):
    if not isinstance(value, str) or not _STAMP.fullmatch(value):
        raise ValueError("time")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("time")
    return parsed


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate")
    return dict(pairs)


def _no_constants(_value):
    raise ValueError("constant")


def parse_case(data: bytes) -> RecoveryCase:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
        if not isinstance(value, dict) or set(value) != set(FIELDS) or value["schema"] != SCHEMA:
            raise ValueError("fields")
        if value["action"] not in CASE_ACTIONS or value["expected_role"] not in ROLES:
            raise ValueError("code")
        if not isinstance(value["verification_method"], str) or not _METHOD.fullmatch(value["verification_method"]):
            raise ValueError("method")
        case = RecoveryCase(
            case_id=_uuid(value["case_id"], v4=True), action=value["action"],
            journal_id=_uuid(value["installation_journal_id"]), user_id=_uuid(value["user_id"]),
            expected_role=value["expected_role"],
            expected_credential_operation_id=_uuid(value["expected_credential_operation_id"], nullable=True),
            expected_status_operation_id=_uuid(value["expected_status_operation_id"], nullable=True),
            operator_id=_uuid(value["operator_id"]), approver_id=_uuid(value["approver_id"]),
            verification_method=value["verification_method"],
            approved_at=_time(value["approved_at"]), expires_at=_time(value["expires_at"]))
    except (UnicodeDecodeError, ValueError, TypeError, AttributeError, KeyError):
        raise CaseError("case_file_invalid") from None
    if case.expires_at <= case.approved_at:
        raise CaseError("case_window_invalid")
    return case


def load_case_file(path: Path, *, max_bytes: int) -> RecoveryCase:
    """Read a restricted case file: regular, not a link, outside the repo, owner-only on POSIX.

    Windows ACLs are not inspected here; deployment must restrict the folder
    (documented limit). The path itself is never printed.
    """
    try:
        if path.is_symlink():
            raise CaseError("case_file_unsafe")
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except CaseError:
        raise
    except OSError:
        raise CaseError("case_file_unreadable") from None
    if not stat.S_ISREG(info.st_mode):
        raise CaseError("case_file_unsafe")
    if any(resolved == root or root in resolved.parents for root in _ROOTS):
        raise CaseError("case_file_inside_repo")
    if os.name == "posix" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
        raise CaseError("case_file_permissions")
    if info.st_size > max_bytes:
        raise CaseError("case_file_too_large")
    try:
        data = resolved.read_bytes()
    except OSError:
        raise CaseError("case_file_unreadable") from None
    if len(data) > max_bytes:
        raise CaseError("case_file_too_large")
    return parse_case(data)
