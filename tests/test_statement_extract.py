from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from bursa.db.enums import Basis, Statement
from bursa.extract.layout import Cell, ColumnBand, ExtractedRow, ExtractedTable
from bursa.extract.statement_extract import RowInfo, _column_year, _resolve_columns, extract_statements
from bursa.mapping.synonyms import seed_concepts
from tests.fixtures.synthetic import (
    BALANCE_SHEET,
    INCOME_STATEMENT_WITH_DUPLICATE_OWNERS_SPLIT,
    INCOME_STATEMENT_WITH_GROUPED_OWNERS_SPLIT,
    QUARTERLY_INCOME_STATEMENT,
    build_statement_pdf,
)

# Page-selection itself (the two-layer keyword/density scorer) is tested in
# tests/test_page_scoring.py, including the regression cases adapted from
# real filings. What's tested here is what extract_statements does with the
# page once selected: row extraction, concept mapping, scale, column years.


# --------------------------------------------------------------------------
# _column_year: never attribute a year found in a different column
# --------------------------------------------------------------------------


def test_column_year_reads_its_own_header() -> None:
    assert _column_year("30.09.2024", "irrelevant") == 2024


def test_column_year_falls_back_to_the_table_header_when_the_column_has_none() -> None:
    assert _column_year("", "For the year ended 31 December 2024 (RM'000)") == 2024


def test_column_year_prefers_its_own_header_over_the_table_header() -> None:
    # A column-specific date must win over an unrelated table-wide year
    # (e.g. an audit date, or the wrong comparative column's own text).
    assert _column_year("31.12.2023", "For the year ended 31 December 2024") == 2023


def test_column_year_is_none_when_no_year_appears_anywhere() -> None:
    assert _column_year("Note", "Statement of Financial Position") is None


def test_column_year_never_falls_back_for_a_non_numeric_own_header() -> None:
    # A column headed "Note" (or stray page-title text bled into the column
    # band, e.g. "BERHAD OR COMPREHENSIVE" - both confirmed real) must not
    # borrow the table-wide year even when the table header does carry one -
    # that is exactly what previously turned a footnote-reference column
    # into a dated one.
    assert _column_year("Note", "For the year ended 31 December 2024 (RM'000)") is None
    assert _column_year("BERHAD OR COMPREHENSIVE", "Statement for 2024") is None


# --------------------------------------------------------------------------
# _resolve_columns: whole-table column year/basis/note resolution
# --------------------------------------------------------------------------


def _table(head: str, headers: dict[int, str]) -> ExtractedTable:
    header_row = ExtractedRow(
        row_index=0, label="",
        cells=[Cell(col_index=i, text=t, bbox=(0, 0, 0, 0)) for i, t in headers.items() if t],
        bbox=(0, 0, 0, 0),
    )
    return ExtractedTable(
        page_no=1, table_index=0, statement=Statement.INCOME_STATEMENT,
        columns=[ColumnBand(index=i, x_min=0, x_max=0, support=1) for i in headers],
        header_rows=[header_row], page_text_head=head,
    )


def _row(values: dict[int, str]) -> RowInfo:
    return RowInfo(row_index=1, label="x", concept_key=None, indent_level=0, values=values)


def test_headerless_note_column_is_recognised_by_its_content() -> None:
    # Ajinomoto: blank Note header used to inherit the page year, writing "4" as revenue.
    table = _table("FOR THE FINANCIAL YEAR ENDED 31 MARCH 2021", {0: "", 1: "2021 RM", 2: "2020 RM"})
    rows = [_row({0: "4", 1: "443,119,251", 2: "461,689,082"}), _row({0: "13.1", 1: "(1,287)", 2: "(1,151)"})]

    cols = _resolve_columns(table, rows)

    assert cols[0].is_note and cols[0].year is None
    assert [c.year for c in cols[1:]] == [2021, 2020]


def test_a_value_column_of_small_numbers_is_not_a_note_column_unless_leftmost() -> None:
    table = _table("2024 2023", {0: "2024", 1: "2023"})
    cols = _resolve_columns(table, [_row({0: "1,000", 1: "2"}), _row({0: "3,000", 1: "4"})])
    assert not any(c.is_note for c in cols)


def test_header_year_line_overrides_page_title_bleed() -> None:
    # Ajinomoto/Vitrox: "REPORT 2021 INCOME 2021 2020" and a page number "105"
    # bled into the comparative column's own header.
    table = _table("ANNUAL REPORT 2021\nNote 2021 2020", {0: "Note", 1: "2021", 2: "REPORT 2021 INCOME 2021 2020"})
    cols = _resolve_columns(table, [_row({1: "1", 2: "2"})])
    assert [c.year for c in cols] == [None, 2021, 2020]


def test_group_company_band_assigns_basis_by_position() -> None:
    # Three-A: "Group Company" on its own line above four year columns.
    table = _table(
        "Group Company\n2020 2019 2020 2019",
        {0: "Note", 1: "2020", 2: "2020 2019", 3: "OTHER 2020", 4: "59 2019"},
    )
    cols = _resolve_columns(table, [_row({1: "1", 2: "2", 3: "3", 4: "4"})])
    assert [c.year for c in cols[1:]] == [2020, 2019, 2020, 2019]
    assert [c.basis for c in cols[1:]] == [Basis.CONSOLIDATED] * 2 + [Basis.COMPANY] * 2


def test_no_year_line_keeps_per_column_years() -> None:
    table = _table("As at 31 December 2024", {0: "31.12.2024", 1: "1.1.2024"})
    cols = _resolve_columns(table, [_row({0: "1", 1: "2"})])
    assert [c.year for c in cols] == [2024, 2024]
    assert all(c.basis is None for c in cols)


# --------------------------------------------------------------------------
# extract_statements: end to end against the synthetic fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def balance_sheet_pdf(tmp_path: Path) -> Path:
    return build_statement_pdf(tmp_path / "bs.pdf", BALANCE_SHEET)


@pytest.fixture
def income_statement_pdf(tmp_path: Path) -> Path:
    return build_statement_pdf(tmp_path / "is.pdf", QUARTERLY_INCOME_STATEMENT)


def test_extract_statements_finds_the_balance_sheet(
    session: Session, balance_sheet_pdf: Path
) -> None:
    seed_concepts(session)
    session.commit()

    result = extract_statements(session, document_id=1, pdf_path=balance_sheet_pdf)

    assert Statement.BALANCE_SHEET in result.statements
    bs = result.statements[Statement.BALANCE_SHEET]
    assert bs.mapped_row_count > 0

    total_assets = next(r for r in bs.rows if r.concept_key == "bs.total_assets")
    assert total_assets.values[0] == "465,600"


def test_extract_statements_reports_unmapped_rows_honestly(
    session: Session, balance_sheet_pdf: Path
) -> None:
    seed_concepts(session)
    session.commit()

    result = extract_statements(session, document_id=1, pdf_path=balance_sheet_pdf)
    bs = result.statements[Statement.BALANCE_SHEET]

    # "Net assets per share (RM)" is not in the seeded synonym table -
    # it must appear with its raw label, not a fabricated concept key.
    unmapped = [r for r in bs.rows if r.concept_key is None]
    assert any("net assets per share" in r.label.lower() for r in unmapped)


def test_extract_statements_detects_the_scale(session: Session, balance_sheet_pdf: Path) -> None:
    seed_concepts(session)
    session.commit()

    result = extract_statements(session, document_id=1, pdf_path=balance_sheet_pdf)
    assert result.statements[Statement.BALANCE_SHEET].scale.multiplier == 1_000


def test_a_repeated_owners_and_nci_split_is_mapped_to_its_own_section(
    session: Session, tmp_path: Path
) -> None:
    """"Owners of the Company" / "Non-controlling interests" appear twice,
    identically labelled, under two different subtotals - confirmed real on
    Vitrox Corporation's filing. The first pair (under "Profit for the
    financial year") must map to is.pat_owners/is.pat_nci; the second (under
    "Total comprehensive income") must map to is.tci_owners/is.tci_nci, not
    silently collide with the first and overwrite it in `facts`."""
    seed_concepts(session)
    session.commit()

    pdf = build_statement_pdf(tmp_path / "is.pdf", INCOME_STATEMENT_WITH_DUPLICATE_OWNERS_SPLIT)
    result = extract_statements(session, document_id=1, pdf_path=pdf)
    is_ = result.statements[Statement.INCOME_STATEMENT]

    owners_rows = [r for r in is_.rows if r.label == "Owners of the Company"]
    nci_rows = [r for r in is_.rows if r.label == "Non-controlling interests"]
    assert [r.concept_key for r in owners_rows] == ["is.pat_owners", "is.tci_owners"]
    assert [r.concept_key for r in nci_rows] == ["is.pat_nci", "is.tci_nci"]
    assert owners_rows[0].values[0] == "9,845"
    assert owners_rows[1].values[0] == "10,220"


def test_a_repeated_owners_and_nci_split_grouped_at_the_end_pairs_by_occurrence_order(
    session: Session, tmp_path: Path
) -> None:
    """The real layout confirmed on Vitrox Corporation's own filing: both
    subtotals are stated first, with both owners/NCI breakdown blocks
    grouped together afterwards - not interleaved with their own subtotal
    the way INCOME_STATEMENT_WITH_DUPLICATE_OWNERS_SPLIT is. "Whichever
    subtotal was most recently seen" gets this wrong (both anchors have
    already been seen by the time either breakdown appears); pairing by
    occurrence order gets it right."""
    seed_concepts(session)
    session.commit()

    pdf = build_statement_pdf(tmp_path / "is.pdf", INCOME_STATEMENT_WITH_GROUPED_OWNERS_SPLIT)
    result = extract_statements(session, document_id=1, pdf_path=pdf)
    is_ = result.statements[Statement.INCOME_STATEMENT]

    owners_rows = [r for r in is_.rows if r.label == "Owners of the Company"]
    nci_rows = [r for r in is_.rows if r.label == "Non-controlling interests"]
    assert [r.concept_key for r in owners_rows] == ["is.pat_owners", "is.tci_owners"]
    assert [r.concept_key for r in nci_rows] == ["is.pat_nci", "is.tci_nci"]


def test_extract_statements_skips_missing_statements_without_crashing(
    session: Session, income_statement_pdf: Path
) -> None:
    """A one-page fixture with only an income statement - the balance sheet
    and cash flow must be reported as skipped, not raise."""
    seed_concepts(session)
    session.commit()

    result = extract_statements(session, document_id=1, pdf_path=income_statement_pdf)

    assert Statement.INCOME_STATEMENT in result.statements
    assert Statement.BALANCE_SHEET in result.skipped_pages
    assert Statement.CASH_FLOW in result.skipped_pages
