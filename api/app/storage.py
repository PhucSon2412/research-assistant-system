"""Private S3-compatible original storage. Object keys never leave the API."""

from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.config import Settings


class OriginalStorage:
    def __init__(self, settings: Settings):
        if settings.storage is None:
            raise RuntimeError("object storage is not configured")
        self.bucket = settings.storage.bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.storage.endpoint_url,
            aws_access_key_id=settings.storage.access_key.get_secret_value(),
            aws_secret_access_key=settings.storage.secret_key.get_secret_value(),
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                connect_timeout=settings.storage.connect_timeout_seconds,
                read_timeout=settings.storage.read_timeout_seconds,
                retries={"mode": "standard", "total_max_attempts": settings.storage.max_attempts}),
        )

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket"):
                raise
            try:
                self.client.create_bucket(Bucket=self.bucket)
            except ClientError as create_exc:
                if create_exc.response.get("Error", {}).get("Code") not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                    raise

    def put(self, key: str, file: BinaryIO, mime: str) -> None:
        self.ensure_bucket()
        file.seek(0)
        self.client.upload_fileobj(file, self.bucket, key, ExtraArgs={"ContentType": mime})

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def open(self, key: str):
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"]

    def size(self, key: str) -> int:
        return int(self.client.head_object(Bucket=self.bucket, Key=key)["ContentLength"])

    def fingerprint(self, key: str) -> tuple[int, str, str]:
        """Size, ETag and Last-Modified of the stored object (a HEAD request, no body).

        Only a cheap early reject: a *changed* fingerprint means the object changed, but an
        unchanged one proves nothing about the bytes. The S3-compatible contract of this
        project does not promise that every overwrite changes these values, and an ETag is
        not a whole-object checksum for every upload or encryption mode. Integrity is the
        caller's full SHA-256 read.
        """
        head = self.client.head_object(Bucket=self.bucket, Key=key)
        return int(head["ContentLength"]), str(head.get("ETag")), str(head.get("LastModified"))

    def orphan_keys(self, prefix: str = "originals/") -> Iterator[tuple[str, object]]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                yield item["Key"], item["LastModified"]


def get_storage(settings: Settings) -> OriginalStorage:
    return OriginalStorage(settings)


def is_missing_object(exc: BaseException) -> bool:
    """True when S3 says the key does not exist (as opposed to being unreachable)."""
    if not isinstance(exc, ClientError):
        return False
    return exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound")
