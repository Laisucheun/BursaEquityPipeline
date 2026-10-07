"""Cloudflare R2 (S3-compatible) blob store. Requires the ``aws`` extra.

Stored locations are recorded as ``r2://<bucket>/<key>`` URIs.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from bursa.config import Settings
from bursa.storage.base import sha256_file

R2_SCHEME = "r2://"


def r2_uri(bucket: str, key: str) -> str:
    return f"{R2_SCHEME}{bucket}/{key}"


def parse_r2_uri(uri: str) -> tuple[str, str]:
    """``r2://bucket/a/b/c.pdf`` -> ``("bucket", "a/b/c.pdf")``."""
    if not uri.startswith(R2_SCHEME):
        raise ValueError(f"not an r2:// URI: {uri!r}")
    bucket, _, key = uri[len(R2_SCHEME) :].partition("/")
    if not bucket or not key:
        raise ValueError(f"malformed r2:// URI: {uri!r}")
    return bucket, key


def _is_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


def default_cache_dir(settings: Settings) -> Path:
    return Path(settings.blob_cache_dir or Path(tempfile.gettempdir()) / "bursa-blobs")


class R2BlobStore:
    def __init__(
        self,
        settings: Settings,
        *,
        client: Any | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        if client is None:
            required = [
                ("R2_BUCKET", settings.r2_bucket),
                ("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
                ("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
            ]
            if not settings.r2_endpoint_url:
                required.insert(0, ("R2_ACCOUNT_ID", settings.r2_account_id))
            missing = [name for name, value in required if not value]
            if missing:
                raise ValueError(f"STORAGE_BACKEND=r2 requires: {', '.join(missing)}")

            import boto3

            client = boto3.client(
                "s3",
                endpoint_url=settings.r2_endpoint_url
                or f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=settings.r2_access_key_id,
                aws_secret_access_key=settings.r2_secret_access_key,
                region_name="auto",
            )
        elif not settings.r2_bucket:
            raise ValueError("R2BlobStore requires R2_BUCKET")

        self.bucket: str = settings.r2_bucket  # type: ignore[assignment]
        self.client = client
        self._cache = Path(cache_dir) if cache_dir is not None else default_cache_dir(settings)

    # -- writes ---------------------------------------------------------------

    def put(self, key: str, source: Path, *, sha256: str | None = None) -> str:
        """Upload ``source`` under ``key``; a no-op if an identical object exists.

        The sha256 is stored as object metadata so later checks don't depend
        on the ETag (which is not an MD5 for multipart uploads).
        """
        source = Path(source)
        if sha256 is None:
            sha256, _ = sha256_file(source)
        head = self.head(key)
        if head is not None and head.get("Metadata", {}).get("sha256") == sha256:
            return r2_uri(self.bucket, key)
        with source.open("rb") as body:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType="application/pdf",
                Metadata={"sha256": sha256},
            )
        return r2_uri(self.bucket, key)

    def put_bytes(self, key: str, data: bytes) -> str:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return r2_uri(self.bucket, key)

    # -- reads ----------------------------------------------------------------

    def head(self, key: str) -> dict[str, Any] | None:
        """``head_object`` response, or None when the object does not exist."""
        from botocore.exceptions import ClientError

        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                return None
            raise

    def get_bytes(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        # Only "not found" means absent; auth/network errors propagate rather
        # than silently reading as a missing blob.
        return self.head(key) is not None

    def open_local(self, key: str) -> Path:
        """Download to the local cache (once) and return the cached path.

        Cache layout mirrors the key, so a content-addressed key is also a
        sha256-keyed cache entry. When the key's stem is a sha256 the bytes are
        verified before the file is published; the write is atomic, so a crash
        never leaves a truncated PDF that later reads as cached.
        """
        dest = (self._cache / key).resolve()
        if not dest.is_relative_to(self._cache.resolve()):
            raise ValueError(f"key escapes cache root: {key!r}")
        if dest.is_file():
            return dest
        data = self.get_bytes(key)  # raises ClientError (NoSuchKey) if absent
        expected = Path(key).stem
        if len(expected) == 64 and all(c in "0123456789abcdef" for c in expected):
            import hashlib

            actual = hashlib.sha256(data).hexdigest()
            if actual != expected:
                raise OSError(f"r2 object {key} sha256 mismatch: got {actual}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, dest)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return dest
