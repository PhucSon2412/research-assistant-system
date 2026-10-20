"""A small S3-compatible client wrapper for the off-host destinations (M8b).

Errors become stable codes (``TargetError.code``); botocore messages are never
kept because they can name hosts, buckets or keys. Writes that must never
replace an object use a conditional create (``If-None-Match: *``): an existing
key answers ``target_conflict`` instead of being overwritten.
"""

from __future__ import annotations

import hashlib

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ConnectionError as BotoConnectionError

from app.ops.config import S3Target, read_credentials

CHUNK = 1024 * 1024


class TargetError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {}) or {}
        status = (exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        name = str(error.get("Code") or "")
        if status == 412 or name in ("PreconditionFailed", "ConditionalRequestConflict"):
            return "target_conflict"
        if name in ("NoSuchKey", "404", "NotFound"):
            return "target_object_missing"
        if name == "NoSuchBucket":
            return "target_bucket_missing"
        if status in (401, 403) or name in ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
            return "target_denied"
        return "target_error"
    if isinstance(exc, (BotoConnectionError, BotoCoreError, OSError)):
        return "target_unavailable"
    return "target_error"


class Target:
    """One bucket/prefix with its own credentials (read from the secret file at construction)."""

    def __init__(self, cfg: S3Target, client=None, *, read_only: bool = False):
        self.cfg = cfg
        self.bucket = cfg.bucket
        self.prefix = cfg.prefix.rstrip("/")
        if client is None:
            key, secret = read_credentials(cfg.read_credentials_file if read_only else cfg.credentials_file)
            client = boto3.client(
                "s3", endpoint_url=cfg.endpoint_url, region_name=cfg.region, aws_access_key_id=key,
                aws_secret_access_key=secret, verify=str(cfg.ca_file) if cfg.ca_file else True,
                config=Config(connect_timeout=cfg.connect_timeout_seconds, read_timeout=cfg.read_timeout_seconds,
                              retries={"max_attempts": 2, "mode": "standard"}, signature_version="s3v4",
                              s3={"addressing_style": cfg.addressing_style}))
        self.client = client

    def key(self, *parts: str) -> str:
        return "/".join((self.prefix, *parts))

    def _call(self, name: str, **kwargs):
        try:
            return getattr(self.client, name)(Bucket=self.bucket, **kwargs)
        except Exception as exc:  # noqa: BLE001 - mapped to a code, message dropped
            raise TargetError(_code(exc)) from None

    def properties(self) -> dict:
        """Versioning and object-lock state of the bucket (codes only)."""
        versioning = self._call("get_bucket_versioning").get("Status") or "Disabled"
        try:
            lock = self.client.get_object_lock_configuration(Bucket=self.bucket)
            lock_state = (lock.get("ObjectLockConfiguration") or {}).get("ObjectLockEnabled") or "Disabled"
        except Exception as exc:  # noqa: BLE001
            code = _code(exc)
            if code in ("target_unavailable", "target_denied", "target_bucket_missing"):
                raise TargetError(code) from None
            lock_state = "Disabled"
        return {"versioning": versioning, "object_lock": lock_state}

    def require_ready(self) -> dict:
        props = self.properties()
        if self.cfg.require_versioning and props["versioning"] != "Enabled":
            raise TargetError("target_versioning_disabled")
        return props

    def get(self, key: str) -> bytes | None:
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        except Exception as exc:  # noqa: BLE001
            code = _code(exc)
            if code == "target_object_missing":
                return None
            raise TargetError(code) from None
        try:
            return body.read()
        except Exception as exc:  # noqa: BLE001
            raise TargetError(_code(exc)) from None
        finally:
            body.close()

    def digest(self, key: str) -> tuple[int, str]:
        """Size and SHA-256 of the object as the destination returns it now (read back, streamed)."""
        response = self._call("get_object", Key=key)
        digest, size = hashlib.sha256(), 0
        body = response["Body"]
        try:
            while block := body.read(CHUNK):
                digest.update(block)
                size += len(block)
        except Exception as exc:  # noqa: BLE001
            raise TargetError(_code(exc)) from None
        finally:
            body.close()
        return size, digest.hexdigest()

    def download(self, key: str, handle) -> None:
        response = self._call("get_object", Key=key)
        body = response["Body"]
        try:
            while block := body.read(CHUNK):
                handle.write(block)
        except Exception as exc:  # noqa: BLE001
            raise TargetError(_code(exc)) from None
        finally:
            body.close()

    def create(self, key: str, data: bytes, *, content_type: str = "application/octet-stream",
               metadata: dict[str, str] | None = None) -> None:
        """Write ``key`` only if it does not exist (``target_conflict`` otherwise): never an overwrite."""
        self._call("put_object", Key=key, Body=data, IfNoneMatch="*", ContentType=content_type,
                   Metadata=metadata or {})

    def create_file(self, key: str, path, *, content_type: str = "application/octet-stream") -> None:
        with open(path, "rb") as handle:
            self._call("put_object", Key=key, Body=handle, IfNoneMatch="*", ContentType=content_type)

    def replace(self, key: str, data: bytes, *, content_type: str = "application/json") -> None:
        """For pointer records only (a head); the bucket's versioning keeps every earlier version."""
        self._call("put_object", Key=key, Body=data, ContentType=content_type)

    def delete(self, key: str) -> None:
        self._call("delete_object", Key=key)

    def list(self, prefix: str) -> dict[str, tuple[int, str]]:
        """Current objects under ``prefix``: key -> (size, ETag)."""
        out: dict[str, tuple[int, str]] = {}
        try:
            for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
                for item in page.get("Contents", []):
                    out[item["Key"]] = (int(item["Size"]), str(item.get("ETag")))
        except Exception as exc:  # noqa: BLE001
            raise TargetError(_code(exc)) from None
        return out
