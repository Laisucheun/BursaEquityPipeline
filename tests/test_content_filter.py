"""content_filter.has_financial_statements is the precision backstop for
ir_fallback's now-deliberately-wide link collection (see that module's
docstring) - these tests check it actually separates a genuine statement PDF
from a narrative-only decoy, using the same synthetic-PDF machinery the
page-selection tests already rely on."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from bursa.scrapers.content_filter import has_financial_statements
from tests.fixtures.synthetic import BALANCE_SHEET, build_statement_pdf


def _build_narrative_pdf(path: Path) -> Path:
    """A page of pure prose - a chairman's statement, roughly - with no
    table structure and no statement vocabulary at all, the shape of the
    real decoys (chairman's/MD's statements, sustainability reports) this
    check exists to reject."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((40, 60), "Chairman's Statement", fontname="hebo", fontsize=14)
    lines = [
        "On behalf of the Board, I am pleased to present this year's review",
        "of our Group's performance and strategic direction. It has been a",
        "year of resilience amid a challenging global operating environment,",
        "and I would like to thank our shareholders, customers and staff for",
        "their continued support throughout the year.",
    ]
    y = 100.0
    for line in lines:
        page.insert_text((40, y), line, fontname="helv", fontsize=10)
        y += 16.0
    doc.save(path)
    doc.close()
    return path


def test_a_genuine_balance_sheet_is_recognised(tmp_path: Path) -> None:
    pdf_path = build_statement_pdf(tmp_path / "real.pdf", BALANCE_SHEET)
    assert has_financial_statements(pdf_path) is True


def test_a_narrative_only_page_is_not_mistaken_for_a_statement(tmp_path: Path) -> None:
    pdf_path = _build_narrative_pdf(tmp_path / "chairman.pdf")
    assert has_financial_statements(pdf_path) is False


def test_a_corrupt_or_unopenable_file_is_treated_as_no_statements_not_an_error(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "corrupt.pdf"
    pdf_path.write_bytes(b"not actually a pdf")
    assert has_financial_statements(pdf_path) is False
