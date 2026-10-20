"""Ops configuration: one explicit JSON file plus secret files (M8b).

The file is given by ``RAS_OPS_CONFIG`` (or ``--config``); there is no default
location and no fallback to ``.env`` or process environment for credentials.
Values are initial operational settings, not measured RPO/RTO or SLOs.
"""

from __future__ import annotations

import json
import os
import re
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.config import Retention, SmtpSettings

MAX_CONFIG_BYTES = 64 * 1024
MAX_SECRET_BYTES = 4096


class OpsConfigError(Exception):
    """The ops configuration or a secret file cannot be used; ``code`` names why (no values)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _absolute(path: Path) -> bool:
    # Container paths are POSIX; a host-side test may use native absolute paths.
    return path.is_absolute() or PurePosixPath(path.as_posix()).is_absolute()


class S3Target(BaseModel):
    """An S3-compatible destination the operator provides (journal replica or backups).

    The two destinations must differ (bucket and credentials) and live in another
    failure domain than the host; that is a deployment fact the config cannot prove.
    """

    model_config = ConfigDict(extra="forbid")

    endpoint_url: str = Field(min_length=8, max_length=500)
    region: str = Field(default="us-east-1", min_length=1, max_length=64)
    bucket: str = Field(min_length=3, max_length=63)
    prefix: str = Field(default="ras", min_length=1, max_length=100)
    credentials_file: Path  # write credential: the replicator (replica) / the ops one-shot (backups)
    read_credentials_file: Path  # read-only credential: alert check, restore (may be the same key if none exists)
    ca_file: Path | None = None
    addressing_style: Literal["path", "virtual"] = "path"
    connect_timeout_seconds: float = Field(default=5, gt=0, le=60)
    read_timeout_seconds: float = Field(default=30, gt=0, le=300)
    require_versioning: bool = True

    @field_validator("ca_file", mode="before")
    @classmethod
    def _empty_is_absent(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def _check(self) -> "S3Target":
        if not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d{1,5})?/?", self.endpoint_url):
            raise ValueError("endpoint_url must be a bare http(s) origin")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]", self.bucket):
            raise ValueError("bucket name")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]*(/[A-Za-z0-9][A-Za-z0-9._\-]*)*", self.prefix):
            raise ValueError("prefix")
        for name in ("credentials_file", "read_credentials_file", "ca_file"):
            path = getattr(self, name)
            if path is not None and not _absolute(path):
                raise ValueError(f"{name} must be an absolute path")
        return self


class BackupSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipients_file: Path
    retention_days: int = Field(default=Retention().backup_retention.days, ge=1, le=3650)
    min_keep: int = Field(default=7, ge=1, le=365)
    staging_dir: Path = Path("/var/lib/ras/staging")


class ReplicatorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_seconds: float = Field(default=15, ge=1, le=3600)
    max_segment_records: int = Field(default=1000, ge=1, le=100000)
    max_segments_per_cycle: int = Field(default=20, ge=1, le=10000)


class AlertSettings(BaseModel):
    """Thresholds are starting operational values, not SLOs."""

    model_config = ConfigDict(extra="forbid")

    recipients: list[str] = Field(min_length=1, max_length=20)
    subject_prefix: str = Field(default="[RAS pilot]", max_length=60)
    renotify_hours: float = Field(default=24, gt=0, le=24 * 30)
    smtp: SmtpSettings
    replica_lag_seconds: int = Field(default=900, ge=30)
    replica_status_stale_seconds: int = Field(default=300, ge=30)
    backup_max_age_hours: float = Field(default=26, gt=0)
    drill_max_age_days: float = Field(default=31, gt=0)
    min_free_bytes_journal: int = Field(default=1 << 30, ge=0)
    min_free_bytes_staging: int = Field(default=5 << 30, ge=0)
    fence_on_journal_rollback: bool = True

    @field_validator("recipients")
    @classmethod
    def _addresses(cls, value: list[str]) -> list[str]:
        for address in value:
            if not re.fullmatch(r"[^@\s<>,;\"]+@[^@\s<>,;\"]+", address):
                raise ValueError("alert recipient must be a bare email address")
        return value

    @field_validator("subject_prefix")
    @classmethod
    def _one_line(cls, value: str) -> str:
        if any(c in value for c in "\r\n\0"):
            raise ValueError("subject prefix must be one line")
        return value


class WindowSettings(BaseModel):
    """Host-side timeouts for the maintenance window and restore (``scripts/ras_ops.py``)."""

    model_config = ConfigDict(extra="forbid")

    stop_timeout_seconds: int = Field(default=180, ge=5)
    capture_timeout_seconds: int = Field(default=3600, ge=10)
    start_timeout_seconds: int = Field(default=600, ge=10)
    readiness_timeout_seconds: int = Field(default=300, ge=5)
    ship_timeout_seconds: int = Field(default=7200, ge=10)
    restore_timeout_seconds: int = Field(default=7200, ge=10)
    step_timeout_seconds: int = Field(default=600, ge=5)


class OpsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    journal_replica: S3Target
    backup_target: S3Target
    backup: BackupSettings
    replicator: ReplicatorSettings = ReplicatorSettings()
    alerts: AlertSettings
    window: WindowSettings = WindowSettings()
    state_dir: Path = Path("/var/lib/ras/ops-state")

    @model_validator(mode="after")
    def _distinct_targets(self) -> "OpsSettings":
        a, b = self.journal_replica, self.backup_target
        if (a.endpoint_url.rstrip("/"), a.bucket) == (b.endpoint_url.rstrip("/"), b.bucket):
            raise ValueError("journal replica and backups need different buckets")
        if a.credentials_file == b.credentials_file:
            raise ValueError("journal replica and backups need different credentials")
        return self

    @property
    def backup_retention(self) -> timedelta:
        return timedelta(days=self.backup.retention_days)


def load(path: Path | str | None = None) -> OpsSettings:
    """Read the explicit ops config; refuses a missing variable instead of guessing a path."""
    raw_path = path if path is not None else os.environ.get("RAS_OPS_CONFIG")
    if not raw_path:
        raise OpsConfigError("ops_config_not_given")
    try:
        with open(raw_path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        raise OpsConfigError("ops_config_unreadable") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise OpsConfigError("ops_config_too_large")
    try:
        return OpsSettings.model_validate(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError, ValidationError):
        raise OpsConfigError("ops_config_invalid") from None


def read_secret(path: Path) -> bytes:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_SECRET_BYTES + 1)
    except OSError:
        raise OpsConfigError("secret_unreadable") from None
    if not raw or len(raw) > MAX_SECRET_BYTES:
        raise OpsConfigError("secret_invalid")
    return raw


def read_credentials(path: Path) -> tuple[str, str]:
    """``{"access_key_id": ..., "secret_access_key": ...}`` from a secret file (never argv/env/log)."""
    try:
        value = json.loads(read_secret(path).decode("utf-8"))
        key, secret = value["access_key_id"], value["secret_access_key"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        raise OpsConfigError("credentials_invalid") from None
    if not (isinstance(key, str) and isinstance(secret, str) and 3 <= len(key) <= 256 and 8 <= len(secret) <= 512):
        raise OpsConfigError("credentials_invalid")
    return key, secret
