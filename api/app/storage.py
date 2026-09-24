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
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
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

    def orphan_keys(self, prefix: str = "originals/") -> Iterator[tuple[str, object]]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                yield item["Key"], item["LastModified"]


def get_storage(settings: Settings) -> OriginalStorage:
    return OriginalStorage(settings)
