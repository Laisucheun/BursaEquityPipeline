"""Blob storage for source PDFs.

Originals are kept forever: every published figure must be traceable back to a
page and a bounding box in the filing it came from, or it cannot be defended
when it looks wrong.

``Document.storage_path`` is either a local filesystem path (LocalBlobStore) or
an ``r2://<bucket>/<key>`` URI (R2BlobStore). Code that needs to *read* a PDF
must go through :func:`materialize` rather than ``Path(doc.storage_path)``, which
only works for the local case.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from bursa.config import get_settings
from bursa.storage.base import BlobStore
from bursa.storage.local import LocalBlobStore

if TYPE_CHECKING:
    from bursa.storage.r2 import R2BlobStore

R2_SCHEME = "r2://"


def get_store() -> BlobStore:
    settings = get_settings()
    backend = settings.storage_backend.lower()

    if backend == "local":
        return LocalBlobStore(settings.storage_local_root)
    if backend == "r2":
        from bursa.storage.r2 import R2BlobStore  # imported lazily: needs boto3

        return R2BlobStore(settings)

    raise ValueError(f"unknown STORAGE_BACKEND: {settings.storage_backend!r}")


def _location(doc_or_path: Any) -> str:
    return str(getattr(doc_or_path, "storage_path", doc_or_path))


def is_remote(doc_or_path: Any) -> bool:
    """True when the stored location is a remote (r2://) URI, not a local file."""
    return _location(doc_or_path).startswith(R2_SCHEME)


_r2_stores: dict[str, R2BlobStore] = {}


def _r2_store_for(bucket: str) -> R2BlobStore:
    # Dispatch is on the stored URI, not STORAGE_BACKEND: after a migration a
    # database may hold r2:// paths while new ingests still go local.
    if bucket not in _r2_stores:
        from bursa.storage.r2 import R2BlobStore

        settings = get_settings().model_copy(update={"r2_bucket": bucket})
        _r2_stores[bucket] = R2BlobStore(settings)
    return _r2_stores[bucket]


def materialize(doc_or_path: Any, *, store: R2BlobStore | None = None) -> Path:
    """A local filesystem path for a Document (or a raw ``storage_path``).

    - local path: returned as-is (it may not exist; callers keep their
      ``.is_file()`` checks).
    - ``r2://bucket/key``: downloaded once into the blob cache (keyed by the
      content-addressed key, i.e. by sha256) and that cached path returned.
      A missing object yields a non-existent path rather than raising, so
      ``materialize(doc).is_file()`` behaves like the local case. Credential
      and network errors propagate.

    ``store`` overrides the R2 store (tests / callers holding a client).
    """
    location = _location(doc_or_path)
    if not location.startswith(R2_SCHEME):
        return Path(location)

    from bursa.storage.r2 import _is_not_found, parse_r2_uri

    bucket, key = parse_r2_uri(location)
    r2 = store if store is not None else _r2_store_for(bucket)
    try:
        return r2.open_local(key)
    except Exception as exc:
        if _is_not_found(exc):
            return r2._cache / key
        raise


__all__ = ["BlobStore", "LocalBlobStore", "get_store", "is_remote", "materialize"]
