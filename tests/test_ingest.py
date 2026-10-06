from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from sqlalchemy.orm import Session

from bursa.db.enums import DocType
from bursa.db.models import Company
from bursa.pipeline import ingest as ingest_mod
from bursa.pipeline.ingest import ingest_file, parse_filename, scan_inbox
from bursa.storage.local import LocalBlobStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBlobStore:
    blob_store = LocalBlobStore(tmp_path / "storage")
    monkeypatch.setattr(ingest_mod, "get_store", lambda: blob_store)
    return blob_store


def make_pdf(path: Path, text: str = "Revenue 1,234") -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()
    return path


def test_filename_hints() -> None:
    hints = parse_filename("5285_SIMEPLANT_Q3_2024.pdf")
    assert hints.stock_code == "5285"
    assert hints.quarter == 3
    assert hints.year == 2024
    assert hints.doc_type is DocType.QUARTERLY_REPORT

    annual = parse_filename("1023-CIMB-AnnualReport-2023.pdf")
    assert annual.doc_type is DocType.ANNUAL_REPORT
    assert annual.stock_code == "1023"

    # A year must never be mistaken for a stock code.
    assert parse_filename("AnnualReport-2023.pdf").stock_code is None

    dated = parse_filename("7113_QR_2024-09-30.pdf")
    assert dated.period_end is not None
    assert (dated.period_end.year, dated.period_end.month) == (2024, 9)


def test_ingest_links_to_company_by_stock_code(
    session: Session, tmp_path: Path, store: LocalBlobStore
) -> None:
    company = Company(stock_code="5285", name="Test Plantations Bhd")
    session.add(company)
    session.flush()

    pdf = make_pdf(tmp_path / "5285_Q3_2024.pdf")
    document, created = ingest_file(session, pdf)

    assert created is True
    assert document.company_id == company.id
    assert document.doc_type is DocType.QUARTERLY_REPORT
    assert document.page_count == 1
    sha = document.file_sha256
    assert store.exists(f"{sha[:2]}/{sha[2:4]}/{sha}.pdf")


def test_reingesting_the_same_content_is_a_no_op(
    session: Session, tmp_path: Path, store: LocalBlobStore
) -> None:
    pdf = make_pdf(tmp_path / "report.pdf")
    first, created_first = ingest_file(session, pdf)
    session.flush()

    # Same bytes, different filename - still the same document.
    renamed = tmp_path / "report-copy.pdf"
    renamed.write_bytes(pdf.read_bytes())
    second, created_second = ingest_file(session, renamed)

    assert created_first is True
    assert created_second is False
    assert second.id == first.id


def test_scan_inbox_survives_a_corrupt_file(
    session: Session, tmp_path: Path, store: LocalBlobStore
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    make_pdf(inbox / "good_a.pdf", "Revenue 1")
    make_pdf(inbox / "good_b.pdf", "Revenue 2")
    (inbox / "broken.pdf").write_bytes(b"not actually a pdf")

    results = scan_inbox(session, inbox)

    # The broken file is still ingested (it is stored and recorded); what must
    # not happen is the batch aborting.
    assert len(results) == 3
    assert sum(1 for _, created in results if created) == 3
