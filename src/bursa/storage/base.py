from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

CHUNK = 1024 * 1024


def sha256_file(path: Path) -> tuple[str, int]:
    """Content hash and byte size. The hash is the document dedupe key."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_key(sha256: str, suffix: str = ".pdf") -> str:
    """Content-addressed storage key, fanned out so no directory gets huge."""
    return f"{sha256[:2]}/{sha256[2:4]}/{sha256}{suffix}"


class BlobStore(Protocol):
    def put(self, key: str, source: Path) -> str:
        """Store a file and return its resolvable path/URI."""
        ...

    def put_bytes(self, key: str, data: bytes) -> str: ...

    def get_bytes(self, key: str) -> bytes: ...

    def open_local(self, key: str) -> Path:
        """A real filesystem path for the blob, downloading it if remote."""
        ...

    def exists(self, key: str) -> bool: ...
