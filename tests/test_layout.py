"""The core bet: column structure survives arbitrary visual styling."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest

from bursa.db.enums import Statement
from bursa.extract.layout import _find_gutter, extract_page, words_from_page
from bursa.normalize.numbers import parse_number
from tests.fixtures.synthetic import (
    BALANCE_SHEET,
    QUARTERLY_INCOME_STATEMENT,
    QUARTERLY_INCOME_STATEMENT_UGLY,
    BlockSpec,
    Row,
    StatementSpec,
    TwoBlockStatementSpec,
    build_spread_pdf,
    build_statement_pdf,
    build_two_block_pdf,
)


def extract(tmp_path: Path, spec: StatementSpec, name: str = "s.pdf"):
    pdf = build_statement_pdf(tmp_path / name, spec)
    with pymupdf.open(pdf) as doc:
        return extract_page(doc[0], page_no=1, statement=Statement.INCOME_STATEMENT)


def row_by_label(table, needle: str):
    # Exact match first: "Taxation" must not resolve to "Profit before taxation".
    for row in table.rows:
        if row.label.lower() == needle.lower():
            return row
    for row in table.rows:
        if needle.lower() in row.label.lower():
            return row
    raise AssertionError(f"no row matching {needle!r}; got {[r.label for r in table.rows]}")


def values(row) -> list[Decimal | None]:
    return [parse_number(cell.text) for cell in sorted(row.cells, key=lambda c: c.col_index)]


def test_four_column_quarterly_is_recovered(tmp_path: Path) -> None:
    table = extract(tmp_path, QUARTERLY_INCOME_STATEMENT)
    assert table is not None
    assert len(table.columns) == 4

    assert values(row_by_label(table, "Revenue")) == [
        Decimal("125430"),
        Decimal("110220"),
        Decimal("362890"),
        Decimal("318455"),
    ]


def test_accounting_negatives_survive_extraction(tmp_path: Path) -> None:
    table = extract(tmp_path, QUARTERLY_INCOME_STATEMENT)
    assert values(row_by_label(table, "Cost of sales"))[0] == Decimal("-92318")
    assert values(row_by_label(table, "Finance costs"))[2] == Decimal("-6480")


def test_per_share_row_is_extracted_without_scaling(tmp_path: Path) -> None:
    # The mapper decides what this means; layout must simply not mangle it.
    table = extract(tmp_path, QUARTERLY_INCOME_STATEMENT)
    assert values(row_by_label(table, "Basic earnings per share")) == [
        Decimal("2.45"),
        Decimal("1.45"),
        Decimal("6.71"),
        Decimal("4.35"),
    ]


def test_styling_does_not_change_the_numbers(tmp_path: Path) -> None:
    """The whole design bet, stated as a test.

    Same figures, different fonts, shaded rows, different column positions,
    wrapped labels, a note reference, and nil dashes. The extracted values must
    be identical.
    """
    plain = extract(tmp_path, QUARTERLY_INCOME_STATEMENT, "plain.pdf")
    ugly = extract(tmp_path, QUARTERLY_INCOME_STATEMENT_UGLY, "ugly.pdf")

    assert ugly is not None and plain is not None
    assert len(ugly.columns) == 4

    for label_plain, label_ugly in [
        ("Revenue", "Turnover"),
        ("Cost of sales", "Cost of goods sold"),
        ("Gross profit", "Gross profit"),
        ("Profit before taxation", "Profit before taxation"),
        ("Income tax expense", "Taxation"),
    ]:
        assert values(row_by_label(plain, label_plain)) == values(
            row_by_label(ugly, label_ugly)
        ), f"{label_plain} != {label_ugly}"


def test_nil_dashes_are_extracted_as_cells_not_dropped(tmp_path: Path) -> None:
    table = extract(tmp_path, QUARTERLY_INCOME_STATEMENT_UGLY, "ugly.pdf")
    impairment = row_by_label(table, "Impairment loss")
    assert [c.text for c in sorted(impairment.cells, key=lambda c: c.col_index)] == [
        "-",
        "-",
        "-",
        "(1,200)",
    ]


def test_two_column_balance_sheet(tmp_path: Path) -> None:
    pdf = build_statement_pdf(tmp_path / "bs.pdf", BALANCE_SHEET)
    with pymupdf.open(pdf) as doc:
        table = extract_page(doc[0], 1, Statement.BALANCE_SHEET)

    assert table is not None
    assert len(table.columns) == 2
    assert values(row_by_label(table, "TOTAL ASSETS"))[0] == Decimal("465600")
    assert values(row_by_label(table, "Total equity"))[0] == Decimal("300675")


def test_balance_sheet_actually_balances_after_extraction(tmp_path: Path) -> None:
    """Cross-foot the extracted figures - the cheapest possible integrity check."""
    pdf = build_statement_pdf(tmp_path / "bs.pdf", BALANCE_SHEET)
    with pymupdf.open(pdf) as doc:
        table = extract_page(doc[0], 1, Statement.BALANCE_SHEET)

    for col in (0, 1):
        assets = values(row_by_label(table, "TOTAL ASSETS"))[col]
        equity = values(row_by_label(table, "Total equity"))[col]
        liabilities = values(row_by_label(table, "Total liabilities"))[col]
        assert assets == equity + liabilities


def test_section_headings_carry_no_cells(tmp_path: Path) -> None:
    pdf = build_statement_pdf(tmp_path / "bs.pdf", BALANCE_SHEET)
    with pymupdf.open(pdf) as doc:
        table = extract_page(doc[0], 1, Statement.BALANCE_SHEET)

    assert row_by_label(table, "ASSETS").cells == []
    assert row_by_label(table, "EQUITY AND LIABILITIES").cells == []


def test_indentation_is_recorded(tmp_path: Path) -> None:
    pdf = build_statement_pdf(tmp_path / "bs.pdf", BALANCE_SHEET)
    with pymupdf.open(pdf) as doc:
        table = extract_page(doc[0], 1, Statement.BALANCE_SHEET)

    assert row_by_label(table, "Inventories").indent_level > 0
    assert row_by_label(table, "TOTAL ASSETS").indent_level == 0


def test_header_text_carries_the_scale_and_period(tmp_path: Path) -> None:
    table = extract(tmp_path, QUARTERLY_INCOME_STATEMENT)
    header = table.header_text
    assert "RM'000" in header.replace("’", "'")
    assert "30.09.2024" in header


@pytest.mark.parametrize("spec", [QUARTERLY_INCOME_STATEMENT, BALANCE_SHEET])
def test_every_data_row_has_a_label(tmp_path: Path, spec: StatementSpec) -> None:
    pdf = build_statement_pdf(tmp_path / "x.pdf", spec)
    with pymupdf.open(pdf) as doc:
        table = extract_page(doc[0], 1, None)

    for row in table.rows:
        if row.cells:
            assert row.label, f"row {row.row_index} has figures but no label"


# --------------------------------------------------------------------------
# Two-block pages: a real space-saving layout (confirmed on a real cash flow
# statement) that places two independent panels side by side on one page,
# each with its own labels and its own numeric columns. A plain Y-only
# row-grouper merges a left-panel row and a right-panel row sitting at the
# same height into one garbled row - see `group_rows_by_block` in layout.py.
# --------------------------------------------------------------------------

TWO_BLOCK_CASH_FLOW = TwoBlockStatementSpec(
    page_title="STATEMENT OF CASH FLOWS FOR THE YEAR ENDED 30 JUNE 2025",
    left=BlockSpec(
        heading="Cash Flows From Operating Activities",
        label_x=40.0,
        column_x=[220.0, 280.0],
        rows=[
            Row("Profit before tax", ["45,230", "38,900"]),
            Row("Depreciation and amortisation", ["12,100", "11,400"]),
            Row("Interest expense", ["(3,400)", "(3,100)"]),
            Row("Working capital changes", ["(8,900)", "(6,200)"]),
            Row("Net cash from operating activities", ["45,030", "41,000"], bold=True),
        ],
    ),
    right=BlockSpec(
        heading="Cash Flows From Investing Activities",
        label_x=320.0,
        column_x=[500.0, 555.0],
        rows=[
            Row("Dividends received from associates", ["6,700", "5,900"]),
            Row("Purchase of property, plant and equipment", ["(22,500)", "(19,800)"]),
            Row("Proceeds from disposal of investments", ["9,800", "2,400"]),
            Row("Interest received", ["1,250", "1,100"]),
            Row("Net cash from investing activities", ["(4,750)", "(10,400)"], bold=True),
        ],
    ),
)


def test_two_block_page_rows_are_not_merged(tmp_path: Path) -> None:
    pdf = build_two_block_pdf(tmp_path / "two_block.pdf", TWO_BLOCK_CASH_FLOW)
    with pymupdf.open(pdf) as doc:
        table = extract_page(doc[0], 1, Statement.CASH_FLOW)

    assert table is not None
    left_row = row_by_label(table, "Profit before tax")
    right_row = row_by_label(table, "Dividends received from associates")

    # Neither row's label absorbed any text from the other block - the exact
    # symptom of the bug ("Profit before tax Dividends received from
    # associates" as one concatenated label).
    assert left_row.label.strip().lower() == "profit before tax"
    assert right_row.label.strip().lower() == "dividends received from associates"
    assert left_row is not right_row

    # Each row's own figures still extract correctly, and only in its own
    # block's column bands - the other block's bands are blank for that row.
    assert values(left_row) == [Decimal("45230"), Decimal("38900")]
    assert values(right_row) == [Decimal("6700"), Decimal("5900")]


def test_two_block_page_every_row_from_both_blocks_survives(tmp_path: Path) -> None:
    pdf = build_two_block_pdf(tmp_path / "two_block.pdf", TWO_BLOCK_CASH_FLOW)
    with pymupdf.open(pdf) as doc:
        table = extract_page(doc[0], 1, Statement.CASH_FLOW)

    for row in [*TWO_BLOCK_CASH_FLOW.left.rows, *TWO_BLOCK_CASH_FLOW.right.rows]:
        extracted = row_by_label(table, row.label)
        assert extracted.label.strip().lower() == row.label.strip().lower()


def test_two_block_page_title_is_not_split_or_duplicated(tmp_path: Path) -> None:
    """The exact IOI symptom this bug produces: a page-wide caption above two
    panels gets read twice and glued together
    ("FOR THE YEAR ENDED... FOR THE YEAR ENDE[D]..."). The caption must
    survive as one clean line, not duplicated or garbled with panel content."""
    pdf = build_two_block_pdf(tmp_path / "two_block.pdf", TWO_BLOCK_CASH_FLOW)
    with pymupdf.open(pdf) as doc:
        table = extract_page(doc[0], 1, Statement.CASH_FLOW)

    header = table.header_text
    occurrences = header.upper().count("STATEMENT OF CASH FLOWS FOR THE YEAR ENDED")
    assert occurrences == 1, f"expected the title exactly once, got {occurrences}: {header!r}"


def test_wide_column_gap_alone_does_not_split_a_real_single_table(tmp_path: Path) -> None:
    """Adversarial regression: a real single table can legitimately have a
    much wider gap than any genuine inter-block gutter - e.g. a "Group"
    figure-group and a "Company" figure-group spaced far apart, sharing one
    label column between them. Splitting on gap width alone would sever every
    row's label from its own numbers, which is strictly worse than the bug
    being fixed (every row ends up label-only or numbers-only, never both)."""
    wide_gap_spec = StatementSpec(
        title="STATEMENT OF FINANCIAL POSITION",
        subtitle="As at 31 December 2025 (RM'000)",
        column_headers=[["Group", "Group", "Company", "Company"], ["2025", "2024", "2025", "2024"]],
        rows=[
            Row("Property, plant and equipment", ["248,330", "241,115", "180,200", "175,600"]),
            Row("Trade receivables", ["88,415", "79,330", "40,220", "38,100"]),
            Row("Cash and cash equivalents", ["42,105", "38,220", "15,900", "14,300"]),
            Row("TOTAL ASSETS", ["378,850", "358,665", "236,320", "228,000"], bold=True),
        ],
        # A 200pt gap between the two figure-groups (280 -> 480) - five times
        # wider than TWO_BLOCK_CASH_FLOW's own genuine ~40pt gutter (280 -> 320).
        column_x=[220.0, 280.0, 480.0, 540.0],
    )
    pdf = build_statement_pdf(tmp_path / "wide_gap.pdf", wide_gap_spec)
    with pymupdf.open(pdf) as doc:
        table = extract_page(doc[0], 1, Statement.BALANCE_SHEET)

    assert table is not None
    assert len(table.columns) == 4
    row = row_by_label(table, "TOTAL ASSETS")
    assert row.label.strip().lower() == "total assets"
    assert values(row) == [
        Decimal("378850"),
        Decimal("358665"),
        Decimal("236320"),
        Decimal("228000"),
    ]


# --------------------------------------------------------------------------
# Header-row misclassification: a running-header/page-furniture row carrying
# exactly one stray small number (a page number, a note-reference digit) must
# never be mistaken for the start of the real data body - confirmed real on
# two otherwise-cleanly-extracted documents this session (Tenaga Nasional's
# income statement: row 0 was "FINANCIAL STATEMENTS" with a page number "311"
# in one column; RHB Bank's: a table-of-contents strip with a note-reference
# "03"), and measured dataset-wide as 52%/54% of all extracted columns
# silently losing their header text/year entirely as a result.
# --------------------------------------------------------------------------


def test_running_header_page_number_does_not_wipe_out_header_rows(tmp_path: Path) -> None:
    spec = StatementSpec(
        title="TENAGA NASIONAL BERHAD",
        subtitle="STATEMENTS OF PROFIT OR LOSS (RM'000)",
        column_headers=[
            ["Group", "Group", "Company", "Company"],
            ["30.09.2024", "30.09.2023", "30.09.2024", "30.09.2023"],
        ],
        rows=[
            Row("Revenue", ["125,430", "110,220", "88,900", "81,200"]),
            Row("Cost of sales", ["(92,318)", "(83,655)", "(60,100)", "(58,400)"]),
            Row("Profit for the period", ["10,352", "6,100", "8,900", "7,400"], bold=True),
        ],
        # Reproduces the real Tenaga Nasional shape exactly: a running section
        # header carrying a single stray number (a page number here) landing
        # in one numeric band, printed *before* the genuine column headers.
        pre_header_rows=[Row("FINANCIAL STATEMENTS", ["", "", "", "311"])],
    )
    pdf = build_statement_pdf(tmp_path / "running_header.pdf", spec)
    with pymupdf.open(pdf) as doc:
        table = extract_page(doc[0], 1, Statement.INCOME_STATEMENT)

    assert table is not None
    assert table.header_rows, "the real column-header band must survive, not be wiped out"
    header = table.header_text.replace("’", "'")
    assert "30.09.2024" in header
    assert "RM'000" in header

    # The real data still starts at Revenue, not at the running-header row -
    # and the running-header row's own figure ("311") never leaks in as data.
    revenue = row_by_label(table, "Revenue")
    assert values(revenue) == [
        Decimal("125430"),
        Decimal("110220"),
        Decimal("88900"),
        Decimal("81200"),
    ]
    assert not any(r.label.strip().upper() == "FINANCIAL STATEMENTS" for r in table.rows)


def test_bare_year_unit_header_row_is_not_mistaken_for_the_data_body(tmp_path: Path) -> None:
    """Reproduces a real 1000x scale bug found on United Plantations'
    quarterly reports: a unit/year header row ("(RM'000)  2022  2021  (%)")
    has a real label *and* two cells that parse as plain numbers ("2022",
    "2021" are each a lone 4-digit integer), so it satisfied the same
    >=2-numeric-hits rule the page-furniture test above exercises - except
    here the row being misclassified as data is the one carrying the scale
    token itself. That truncated `header_rows`/`page_text_head` before
    "(RM'000)" was ever collected, so `detect_scale()` silently defaulted to
    multiplier=1 and every figure on the page was read at 1/1000th its real
    value."""
    spec = StatementSpec(
        title="UNITED PLANTATIONS BERHAD",
        subtitle="Condensed Consolidated Statement of Comprehensive Income for the "
        "Three Months Ended 31 March 2022",
        column_headers=[],
        rows=[
            Row("Revenue", ["642,908", "399,654", "60.9%"]),
            Row("Operating expenses", ["(586,730)", "(311,783)", "88.2%"]),
            Row("Profit for the period", ["61,610", "75,710", "-18.6%"], bold=True),
        ],
        column_x=[400.0, 480.0, 555.0],
        # Exactly the real United Plantations line: a unit label and bare
        # calendar-year column headers on one row, no other text in between.
        pre_header_rows=[Row("(RM'000)", ["2022", "2021", "(%)"])],
    )
    pdf = build_statement_pdf(tmp_path / "bare_year_header.pdf", spec)
    with pymupdf.open(pdf) as doc:
        table = extract_page(doc[0], 1, Statement.INCOME_STATEMENT)

    assert table is not None
    assert "RM'000" in table.header_text.replace("’", "'")

    revenue = row_by_label(table, "Revenue")
    assert values(revenue) == [Decimal("642908"), Decimal("399654"), Decimal("60.9")]
    assert not any(r.label.strip() == "(RM'000)" for r in table.rows)


def test_single_numeric_column_table_still_finds_its_data(tmp_path: Path) -> None:
    """Adversarial regression: a table with only 1 real numeric column must
    fall back to the historical >=1-numeric-cell rule, not the new >=2 -
    otherwise no row would ever qualify as the start of the data body."""
    spec = StatementSpec(
        title="EXAMPLE BERHAD",
        subtitle="STATEMENT OF PROFIT OR LOSS (RM'000)",
        column_headers=[["30.09.2024"]],
        rows=[
            Row("Revenue", ["500,000"]),
            Row("Cost of sales", ["(300,000)"]),
            Row("Profit for the period", ["200,000"], bold=True),
        ],
        column_x=[480.0],
    )
    pdf = build_statement_pdf(tmp_path / "single_col.pdf", spec)
    with pymupdf.open(pdf) as doc:
        table = extract_page(doc[0], 1, Statement.INCOME_STATEMENT)

    assert table is not None
    assert len(table.columns) == 1
    assert values(row_by_label(table, "Revenue")) == [Decimal("500000")]


def test_fallback_ladder_rescues_a_table_where_no_row_hits_two_columns(tmp_path: Path) -> None:
    """Adversarial regression: 2 numeric columns are detected overall, but no
    single row ever populates both (each row fills exactly one, alternating).
    A flat >=2 threshold would find no row at all and silently discard every
    figure - the fallback-to->=1 ladder must rescue this instead."""
    spec = StatementSpec(
        title="EXAMPLE BERHAD",
        subtitle="STATEMENT OF PROFIT OR LOSS (RM'000)",
        column_headers=[["Note", "30.09.2024"]],
        rows=[
            # Each column needs >=_MIN_COLUMN_SUPPORT (3) of its own numeric
            # values for detect_columns to recognize it as a real band at all.
            Row("Revenue", ["500,000", ""]),
            Row("Other income", ["", "12,000"]),
            Row("Expenses", ["(300,000)", ""]),
            Row("Finance costs", ["", "(2,000)"]),
            Row("Tax expense", ["(40,000)", ""]),
            Row("Profit for the period", ["", "160,000"], bold=True),
        ],
        column_x=[300.0, 480.0],
    )
    pdf = build_statement_pdf(tmp_path / "alternating_cols.pdf", spec)
    with pymupdf.open(pdf) as doc:
        table = extract_page(doc[0], 1, Statement.INCOME_STATEMENT)

    assert table is not None
    assert len(table.columns) == 2
    assert table.rows, "the fallback ladder must rescue this table, not empty it out"
    assert values(row_by_label(table, "Revenue"))[0] == Decimal("500000")


# --------------------------------------------------------------------------
# A genuine 2-page spread rendered as one physical PDF page at double width -
# a real, more extreme shape than the 2-block-on-one-normal-page case above
# (confirmed real: one filer's interior pages measure exactly 2.000x its own
# cover page's width, every page). The recurrence-ranked `_find_gutter`
# picks the wrong gap on this shape - a narrow, densely-recurring intra-panel
# column gap outranks the real, wider, less-recurring panel boundary, and a
# "right side" split at the wrong gutter scoops up the entire adjacent,
# genuinely-labelled statement too, passing every validity check on borrowed
# labels rather than its own. Detecting the doubled width up front and
# splitting at the page's own literal geometric midpoint sidesteps that
# contest entirely.
# --------------------------------------------------------------------------

SPREAD_LEFT_BS = BlockSpec(
    heading="STATEMENTS OF FINANCIAL POSITION",
    label_x=40.0,
    # A narrow (~27pt) gap between these two sub-columns recurs on every one
    # of this panel's own 8 rows - deliberately more recurring than the real
    # panel-to-panel boundary below, to reproduce the actual ranking
    # (recurrence, then width) that picks the wrong gutter on the real page.
    column_x=[210.0, 270.0],
    rows=[
        Row("Property, plant and equipment", ["248,330", "241,115"]),
        Row("Investments in subsidiaries", ["19,334", "19,008"]),
        Row("Intangible assets", ["8,220", "7,140"]),
        Row("Trade receivables", ["88,415", "79,330"]),
        Row("Inventories", ["42,105", "38,220"]),
        Row("Deferred tax assets", ["3,100", "2,900"]),
        Row("Cash and bank balances", ["12,500", "11,200"]),
        Row("TOTAL ASSETS", ["421,704", "398,913"], bold=True),
    ],
)
SPREAD_RIGHT_IS = BlockSpec(
    heading="STATEMENTS OF PROFIT OR LOSS",
    label_x=650.0,
    column_x=[900.0, 960.0],
    # Only 5 of these rows ever align in height with the left panel's rows -
    # the real panel-to-panel gutter only registers as a candidate on rows
    # where both panels merge into one naive row, so it recurs less often
    # than the left panel's own internal gap (8 rows).
    rows=[
        Row("Revenue", ["125,430", "110,220"]),
        Row("Cost of sales", ["(92,318)", "(83,655)"]),
        Row("Other income", ["3,200", "2,900"]),
        Row("Administrative expenses", ["(6,100)", "(5,400)"]),
        Row("Profit for the period", ["30,212", "24,065"], bold=True),
    ],
)


def build_calibrated_spread(tmp_path: Path) -> Path:
    return build_spread_pdf(
        tmp_path / "cover.pdf",
        tmp_path / "spread.pdf",
        tmp_path / "combined.pdf",
        SPREAD_LEFT_BS,
        SPREAD_RIGHT_IS,
    )


def test_find_gutter_alone_picks_the_wrong_gap_on_a_spread_page(tmp_path: Path) -> None:
    """Proves the bypass is doing real work, not coincidentally redundant
    with a heuristic that would have worked anyway: called directly on the
    unsplit spread page's words (bypassing `_is_spread_page`'s detection
    entirely), `_find_gutter` picks the left panel's own narrow internal
    column gap, nowhere near the true panel boundary at the page's own
    geometric midpoint."""
    combined_path = build_calibrated_spread(tmp_path)
    with pymupdf.open(combined_path) as doc:
        spread_page = doc[1]
        words = words_from_page(spread_page)
        gutter = _find_gutter(words)
        true_midpoint = spread_page.rect.width / 2

    assert gutter is not None
    gutter_start, _gutter_end = gutter
    assert abs(gutter_start - true_midpoint) > 200, (
        f"expected _find_gutter to pick a gap far from the true midpoint "
        f"({true_midpoint}), got {gutter_start}"
    )


def test_spread_page_resolves_the_correct_half_per_statement(tmp_path: Path) -> None:
    combined_path = build_calibrated_spread(tmp_path)
    with pymupdf.open(combined_path) as doc:
        spread_page = doc[1]
        bs_table = extract_page(spread_page, 2, Statement.BALANCE_SHEET)
        is_table = extract_page(spread_page, 2, Statement.INCOME_STATEMENT)

    assert bs_table is not None and is_table is not None
    assert values(row_by_label(bs_table, "TOTAL ASSETS")) == [
        Decimal("421704"),
        Decimal("398913"),
    ]
    assert values(row_by_label(is_table, "Profit for the period")) == [
        Decimal("30212"),
        Decimal("24065"),
    ]
    # Neither half's own rows leak into the other's result - the exact
    # confirmed real damage this fix addresses (BS figures torn from labels,
    # IS rows appended into the same flat list as BS rows).
    assert not any("revenue" in r.label.lower() for r in bs_table.rows)
    assert not any("total assets" in r.label.lower() for r in is_table.rows)


def test_page_without_columns_yields_nothing(tmp_path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "The Board is pleased to present the annual report.")
    doc.save(tmp_path / "prose.pdf")
    doc.close()

    with pymupdf.open(tmp_path / "prose.pdf") as d:
        assert extract_page(d[0], 1, None) is None
