"""Stage 1 - get a PDF into the system exactly once.

Deliberately dumb: hash, store, record. Anything that requires reading the
document's contents belongs in ``classify``. Filename hints are treated as
hints only - the classifier confirms or overrides them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from bursa.db.enums import DocSource, DocStatus, DocType
from bursa.db.models import Company, Document
from bursa.storage import get_store
from bursa.storage.base import content_key, sha256_file

log = logging.getLogger(__name__)

_YEAR = re.compile(r"(?:^|[^\d])(19[89]\d|20[0-4]\d)(?:[^\d]|$)")
_QUARTER = re.compile(r"\bq([1-4])\b|\bquarter\s*([1-4])\b|\b([1-4])q\b", re.IGNORECASE)
_ANNUAL = re.compile(
    r"\b(ar|annual[\s_-]*report|annualreport|laporan[\s_-]*tahunan)\b", re.IGNORECASE
)
_QUARTERLY = re.compile(r"\b(qr|quarterly|interim|condensed)\b", re.IGNORECASE)
_STOCK_CODE = re.compile(r"(?:^|[^\d])(\d{4})(?:[^\d]|$)")
_ISO_DATE = re.compile(r"(20\d{2})[-_ ]?(0[1-9]|1[0-2])[-_ ]?(0[1-9]|[12]\d|3[01])")


@dataclass(frozen=True)
class FilenameHints:
    stock_code: str | None = None
    doc_type: DocType = DocType.UNKNOWN
    year: int | None = None
    quarter: int | None = None
    period_end: date | None = None


def parse_filename(name: str) -> FilenameHints:
    """Best-effort reading of a filename. Never authoritative.

        >>> parse_filename("5285_SIMEPLANT_Q3_2024.pdf").quarter
        3
        >>> parse_filename("1023-CIMB-AnnualReport-2023.pdf").doc_type
        <DocType.ANNUAL_REPORT: 'ANNUAL_REPORT'>
    """
    raw_stem = Path(name).stem
    # Underscores are word characters, so `\bQ3\b` would not match "..._Q3_2024".
    # Fold every separator to a space before the token patterns run.
    stem = raw_stem.replace("_", " ")

    doc_type = DocType.UNKNOWN
    # Check quarterly first: "Q3 2024 Annual..." is rare, but an annual report
    # never contains a quarter marker, while quarterlies often say "report".
    if _QUARTERLY.search(stem) or _QUARTER.search(stem):
        doc_type = DocType.QUARTERLY_REPORT
    elif _ANNUAL.search(stem):
        doc_type = DocType.ANNUAL_REPORT

    quarter = None
    if match := _QUARTER.search(stem):
        quarter = int(next(g for g in match.groups() if g))

    year = None
    if match := _YEAR.search(stem):
        year = int(match.group(1))

    period_end = None
    if match := _ISO_DATE.search(stem):
        try:
            period_end = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            period_end = None

    stock_code = None
    for match in _STOCK_CODE.finditer(stem):
        candidate = match.group(1)
        # A 4-digit year is not a stock code.
        if year and candidate == str(year):
            continue
        stock_code = candidate
        break

    return FilenameHints(
        stock_code=stock_code,
        doc_type=doc_type,
        year=year,
        quarter=quarter,
        period_end=period_end,
    )


def _page_count(path: Path) -> int | None:
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            return doc.page_count
    except Exception as exc:
        log.warning("could not read page count for %s: %s", path.name, exc)
        return None


def ingest_file(
    session: Session,
    path: Path,
    source: DocSource = DocSource.INBOX,
    company_id: int | None = None,
    source_url: str | None = None,
    doc_type: DocType | None = None,
    document_group_key: str | None = None,
) -> tuple[Document, bool]:
    """Ingest one PDF.

    Returns ``(document, created)``. ``created=False`` means the identical file
    is already in the system - re-running over the same inbox is safe.

    ``doc_type``, when given, is used directly instead of guessing from the
    filename. A caller that already knows what it fetched - a scraper that
    searched specifically for an annual report, say - should always pass this:
    filename sniffing on a server-named file (``ar-2025.pdf``, an opaque hashed
    path) is strictly worse than just being told.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)

    sha256, size = sha256_file(path)

    existing = session.execute(
        select(Document).where(Document.file_sha256 == sha256)
    ).scalar_one_or_none()
    if existing is not None:
        log.info("skipping duplicate %s (sha256=%s)", path.name, sha256[:12])
        return existing, False

    hints = parse_filename(path.name)

    if company_id is None and hints.stock_code:
        company_id = session.execute(
            select(Company.id).where(Company.stock_code == hints.stock_code)
        ).scalar_one_or_none()

    key = content_key(sha256)
    storage_path = get_store().put(key, path)

    document = Document(
        company_id=company_id,
        doc_type=doc_type if doc_type is not None else hints.doc_type,
        source=source,
        source_url=source_url,
        original_filename=path.name,
        file_sha256=sha256,
        file_size=size,
        storage_path=storage_path,
        period_end_hint=hints.period_end,
        page_count=_page_count(path),
        status=DocStatus.INGESTED,
        document_group_key=document_group_key,
    )
    session.add(document)
    session.flush()
    log.info("ingested %s as document %s", path.name, document.id)
    return document, True


def scan_inbox(
    session: Session, inbox_dir: Path, source: DocSource = DocSource.INBOX
) -> list[tuple[Document, bool]]:
    """Ingest every PDF in the drop folder. Idempotent by content hash."""
    inbox_dir = Path(inbox_dir)
    if not inbox_dir.exists():
        log.info("inbox %s does not exist yet", inbox_dir)
        return []

    results: list[tuple[Document, bool]] = []
    for path in sorted(inbox_dir.rglob("*.pdf")):
        try:
            results.append(ingest_file(session, path, source=source))
        except Exception as exc:
            log.exception("failed to ingest %s: %s", path, exc)
    return results
