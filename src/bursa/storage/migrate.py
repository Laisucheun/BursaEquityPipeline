"""Copy local PDF blobs to Cloudflare R2, verify them, optionally repoint the DB.

Required environment (``.env`` or process env; see ``bursa.config.Settings``):

    R2_ACCOUNT_ID         Cloudflare account id; endpoint becomes
                          https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com
    R2_ENDPOINT_URL       optional; overrides the endpoint derived above
                          (then R2_ACCOUNT_ID is not needed)
    R2_BUCKET             bucket name
    R2_ACCESS_KEY_ID      R2 API token access key (Object Read & Write)
    R2_SECRET_ACCESS_KEY  R2 API token secret
    BLOB_CACHE_DIR        optional; local cache for downloaded blobs
                          (default <system temp>/bursa-blobs)
    STORAGE_BACKEND=r2    only once the migration is done, so *new* ingests
                          go to R2 too. Reads dispatch on the stored path
                          (r2:// vs local), so mixed databases keep working.

Usage (default is a dry run that touches neither R2 nor the DB):

    bursa storage migrate-to-r2                      # report only
    bursa storage migrate-to-r2 --execute            # upload + verify
    bursa storage migrate-to-r2 --execute --rewrite-paths
                                                     # ...and repoint Document.storage_path
    bursa storage verify

Idempotent: an object already at the content-addressed key whose ``sha256``
metadata (or, failing that, size + single-part MD5 ETag) matches is skipped.
Local files are never deleted. ``storage_path`` is only rewritten for a
document whose R2 copy has just been verified.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from bursa.db.models import Document
from bursa.storage import is_remote
from bursa.storage.base import CHUNK, content_key, sha256_file
from bursa.storage.r2 import R2BlobStore, parse_r2_uri, r2_uri


@dataclass
class DocResult:
    document_id: int
    status: str
    key: str | None = None
    detail: str = ""


@dataclass
class MigrationReport:
    execute: bool
    results: list[DocResult] = field(default_factory=list)

    @property
    def counts(self) -> Counter[str]:
        return Counter(r.status for r in self.results)

    @property
    def ok(self) -> bool:
        bad = {"missing_local", "hash_mismatch_local", "verify_failed", "missing_remote", "mismatch"}
        return not any(r.status in bad for r in self.results)


def _md5_file(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - comparing against S3 ETag, not security
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _remote_matches(head: dict[str, Any] | None, doc: Document, local: Path | None) -> bool:
    """Does the R2 object (head_object response) hold this document's bytes?"""
    if head is None:
        return False
    if int(head.get("ContentLength", -1)) != doc.file_size:
        return False
    meta_sha = (head.get("Metadata") or {}).get("sha256")
    if meta_sha:
        return meta_sha == doc.file_sha256
    # Uploaded by something else without metadata: fall back to the ETag,
    # which is the MD5 only for single-part uploads (no '-' suffix).
    etag = str(head.get("ETag", "")).strip('"')
    if local is None or not etag or "-" in etag:
        return False
    return etag == _md5_file(local)


def migrate_to_r2(
    session: Session,
    store: R2BlobStore,
    *,
    execute: bool = False,
    rewrite_paths: bool = False,
    limit: int | None = None,
) -> MigrationReport:
    """Upload every local blob to R2. Dry run unless ``execute``.

    Statuses: already_remote, missing_local, hash_mismatch_local, present,
    would_upload, uploaded, verify_failed; ``+rewritten`` suffix when
    ``storage_path`` was repointed.
    """
    if rewrite_paths and not execute:
        raise ValueError("rewrite_paths requires execute")

    report = MigrationReport(execute=execute)
    query = select(Document).order_by(Document.id)
    docs = list(session.scalars(query))
    processed = 0

    for doc in docs:
        if is_remote(doc):
            report.results.append(DocResult(doc.id, "already_remote", doc.storage_path))
            continue
        if limit is not None and processed >= limit:
            break
        processed += 1

        key = content_key(doc.file_sha256)
        local = Path(doc.storage_path)
        if not local.is_file():
            report.results.append(DocResult(doc.id, "missing_local", key, str(local)))
            continue

        if _remote_matches(store.head(key), doc, local):
            status = "present"
        elif not execute:
            report.results.append(DocResult(doc.id, "would_upload", key))
            continue
        else:
            sha, size = sha256_file(local)
            if sha != doc.file_sha256 or size != doc.file_size:
                report.results.append(
                    DocResult(doc.id, "hash_mismatch_local", key, f"local file sha256={sha}")
                )
                continue
            store.put(key, local, sha256=sha)
            if not _remote_matches(store.head(key), doc, local):
                report.results.append(DocResult(doc.id, "verify_failed", key))
                continue
            status = "uploaded"

        if rewrite_paths:
            doc.storage_path = r2_uri(store.bucket, key)
            status += "+rewritten"
        report.results.append(DocResult(doc.id, status, key))

    if rewrite_paths:
        session.flush()
    return report


def verify(session: Session, store: R2BlobStore) -> MigrationReport:
    """Check every document has a matching R2 object (read-only).

    Statuses: ok, missing_remote, mismatch.
    """
    report = MigrationReport(execute=False)
    for doc in session.scalars(select(Document).order_by(Document.id)):
        if is_remote(doc):
            bucket, key = parse_r2_uri(doc.storage_path)
            if bucket != store.bucket:
                report.results.append(DocResult(doc.id, "mismatch", key, f"bucket {bucket}"))
                continue
            local = None
        else:
            key = content_key(doc.file_sha256)
            local = Path(doc.storage_path) if Path(doc.storage_path).is_file() else None
        head = store.head(key)
        if head is None:
            report.results.append(DocResult(doc.id, "missing_remote", key))
        elif _remote_matches(head, doc, local):
            report.results.append(DocResult(doc.id, "ok", key))
        else:
            report.results.append(DocResult(doc.id, "mismatch", key))
    return report


# -- CLI ----------------------------------------------------------------------

app = typer.Typer(help="Blob storage maintenance (local -> R2 migration).", no_args_is_help=True)


def _store() -> R2BlobStore:
    from bursa.config import get_settings

    return R2BlobStore(get_settings())


def _print(report: MigrationReport, verbose: bool) -> None:
    from rich.console import Console

    console = Console()
    if verbose:
        for r in report.results:
            console.print(f"  doc {r.document_id}: {r.status} {r.key or ''} {r.detail}".rstrip())
    summary = ", ".join(f"{k}={v}" for k, v in sorted(report.counts.items())) or "no documents"
    console.print(f"{'EXECUTED' if report.execute else 'DRY RUN'}: {summary}")
    if not report.ok:
        console.print("[red]problems found - see statuses above[/]")


@app.command("migrate-to-r2")
def migrate_cmd(
    execute: Annotated[bool, typer.Option("--execute", help="Actually upload (default: dry run).")] = False,
    rewrite_paths: Annotated[
        bool,
        typer.Option("--rewrite-paths", help="After verifying, repoint Document.storage_path to r2://."),
    ] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="Process at most N local documents.")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Copy local blobs to R2 (idempotent). Never deletes local files."""
    from bursa.db.session import session_scope

    if rewrite_paths and not execute:
        raise typer.BadParameter("--rewrite-paths requires --execute")
    store = _store()
    with session_scope() as session:
        report = migrate_to_r2(
            session, store, execute=execute, rewrite_paths=rewrite_paths, limit=limit
        )
    _print(report, verbose)
    if not report.ok:
        raise typer.Exit(1)


@app.command("verify")
def verify_cmd(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    """Check every document has a matching object in R2 (read-only)."""
    from bursa.db.session import session_scope

    with session_scope() as session:
        report = verify(session, _store())
    _print(report, verbose)
    if not report.ok:
        raise typer.Exit(1)
