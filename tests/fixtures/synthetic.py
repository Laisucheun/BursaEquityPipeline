"""Synthetic Bursa-style statement PDFs.

Real filings are copyrighted and cannot live in the repo, so the structural
tests build their own. These deliberately exercise the variation the extractor
claims to be immune to: no ruling lines, coloured shading, mixed fonts, wrapped
labels, and accounting negatives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

LEFT = 40.0
TOP = 60.0
LINE_HEIGHT = 16.0


@dataclass
class Row:
    label: str
    values: list[str] = field(default_factory=list)
    indent: int = 0
    bold: bool = False


@dataclass
class StatementSpec:
    title: str
    subtitle: str
    column_headers: list[list[str]]
    rows: list[Row]
    # Right edge of each numeric column, in points.
    column_x: list[float] = field(default_factory=lambda: [320.0, 400.0, 480.0, 555.0])
    shade_alternate_rows: bool = False
    font: str = "helv"
    bold_font: str = "hebo"
    # Rows printed *before* column_headers - a running section header or a
    # table-of-contents strip carrying a stray page number/note-reference
    # digit, real and confirmed on two otherwise-clean documents (see
    # test_layout.py's reproduction of the header-row misclassification bug).
    pre_header_rows: list[Row] = field(default_factory=list)


def _right_align(page, text: str, right_x: float, y: float, font: str, size: float) -> None:
    width = pymupdf.get_text_length(text, fontname=font, fontsize=size)
    page.insert_text((right_x - width, y), text, fontname=font, fontsize=size)


def build_statement_pdf(path: Path, spec: StatementSpec, page_size: str = "a4") -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842) if page_size == "a4" else doc.new_page()

    y = TOP
    page.insert_text((LEFT, y), spec.title, fontname=spec.bold_font, fontsize=11)
    y += LINE_HEIGHT
    page.insert_text((LEFT, y), spec.subtitle, fontname=spec.font, fontsize=8)
    y += LINE_HEIGHT * 1.5

    for row in spec.pre_header_rows:
        page.insert_text((LEFT + row.indent * 10, y), row.label, fontname=spec.font, fontsize=8.5)
        for index, value in enumerate(row.values):
            if value == "":
                continue
            _right_align(page, value, spec.column_x[index], y, spec.font, 8.5)
        y += LINE_HEIGHT

    for header in spec.column_headers:
        for index, cell in enumerate(header):
            if not cell:
                continue
            _right_align(page, cell, spec.column_x[index], y, spec.font, 7.5)
        y += LINE_HEIGHT * 0.8
    y += LINE_HEIGHT * 0.5

    for row_no, row in enumerate(spec.rows):
        if spec.shade_alternate_rows and row_no % 2 == 0:
            page.draw_rect(
                pymupdf.Rect(LEFT - 4, y - 10, 565, y + 4),
                color=None,
                fill=(0.92, 0.94, 0.98),
            )
        font = spec.bold_font if row.bold else spec.font
        page.insert_text(
            (LEFT + row.indent * 10, y), row.label, fontname=font, fontsize=8.5
        )
        for index, value in enumerate(row.values):
            if value == "":
                continue
            _right_align(page, value, spec.column_x[index], y, font, 8.5)
        y += LINE_HEIGHT

    doc.save(path)
    doc.close()
    return path


# --------------------------------------------------------------------------
# A page laid out as two independent physical blocks side by side - a real
# space-saving convention confirmed on a real filing (a cash flow statement
# with "Operating Activities" printed in the left half of the page and
# "Investing Activities" in the right half, each with its own labels and its
# own numeric columns). Reproduces the exact condition that garbles a plain
# Y-only row-grouper: a left-block row and a right-block row sitting at the
# same height.
# --------------------------------------------------------------------------


@dataclass
class BlockSpec:
    heading: str
    rows: list[Row]
    label_x: float
    column_x: list[float]


@dataclass
class TwoBlockStatementSpec:
    page_title: str
    left: BlockSpec
    right: BlockSpec
    font: str = "helv"
    bold_font: str = "hebo"


def build_two_block_pdf(path: Path, spec: TwoBlockStatementSpec, page_size: str = "a4") -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842) if page_size == "a4" else doc.new_page()

    y = TOP
    # A page-wide caption above both panels - exercises the "a title row is
    # expected to straddle the gutter without vetoing it" handling.
    page.insert_text((LEFT, y), spec.page_title, fontname=spec.bold_font, fontsize=11)
    y += LINE_HEIGHT * 1.5

    page.insert_text((spec.left.label_x, y), spec.left.heading, fontname=spec.bold_font, fontsize=9)
    page.insert_text((spec.right.label_x, y), spec.right.heading, fontname=spec.bold_font, fontsize=9)
    y += LINE_HEIGHT * 1.3

    row_count = max(len(spec.left.rows), len(spec.right.rows))
    for i in range(row_count):
        for block in (spec.left, spec.right):
            if i >= len(block.rows):
                continue
            row = block.rows[i]
            font = spec.bold_font if row.bold else spec.font
            page.insert_text(
                (block.label_x + row.indent * 10, y), row.label, fontname=font, fontsize=8.5
            )
            for index, value in enumerate(row.values):
                if value == "":
                    continue
                _right_align(page, value, block.column_x[index], y, font, 8.5)
        y += LINE_HEIGHT

    doc.save(path)
    doc.close()
    return path


# --------------------------------------------------------------------------
# A genuine 2-page spread rendered as one physical PDF page at double width -
# a different, more extreme real shape than the 2-block-on-one-normal-page
# case above (confirmed real: one filer's interior pages measure exactly
# 2.000x its own cover page's width, every page). A normal-width cover page
# is combined with a double-width interior page via `insert_pdf` (each
# source page keeps its own physical size - the same pattern
# `test_page_scoring.py` already uses for multi-page fixtures).
# --------------------------------------------------------------------------


def build_spread_pdf(
    cover_path: Path,
    spread_path: Path,
    combined_path: Path,
    left: BlockSpec,
    right: BlockSpec,
    page_width: float = 1190.0,
    page_height: float = 850.0,
) -> Path:
    cover = pymupdf.open()
    cover.new_page(width=595.0, height=842.0)
    cover.save(cover_path)
    cover.close()

    doc = pymupdf.open()
    page = doc.new_page(width=page_width, height=page_height)

    y = TOP
    page.insert_text((left.label_x, y), left.heading, fontname="hebo", fontsize=9)
    page.insert_text((right.label_x, y), right.heading, fontname="hebo", fontsize=9)
    y += LINE_HEIGHT * 1.3

    row_count = max(len(left.rows), len(right.rows))
    for i in range(row_count):
        for block in (left, right):
            if i >= len(block.rows):
                continue
            row = block.rows[i]
            font = "hebo" if row.bold else "helv"
            page.insert_text((block.label_x + row.indent * 10, y), row.label, fontname=font, fontsize=8.5)
            for index, value in enumerate(row.values):
                if value == "":
                    continue
                _right_align(page, value, block.column_x[index], y, font, 8.5)
        y += LINE_HEIGHT

    doc.save(spread_path)
    doc.close()

    combined = pymupdf.open()
    combined.insert_pdf(pymupdf.open(cover_path))
    combined.insert_pdf(pymupdf.open(spread_path))
    combined.save(combined_path)
    combined.close()
    return combined_path


# --------------------------------------------------------------------------
# A representative Bursa Appendix 9B quarterly income statement.
# --------------------------------------------------------------------------

QUARTERLY_INCOME_STATEMENT = StatementSpec(
    title="CONDENSED CONSOLIDATED STATEMENT OF PROFIT OR LOSS",
    subtitle="For the third quarter ended 30 September 2024 (Unaudited)  (RM'000)",
    column_headers=[
        ["INDIVIDUAL QUARTER", "", "CUMULATIVE QUARTER", ""],
        ["Current", "Preceding Year", "Current", "Preceding Year"],
        ["Quarter", "Corresponding", "Year To Date", "Corresponding"],
        ["30.09.2024", "30.09.2023", "30.09.2024", "30.09.2023"],
    ],
    rows=[
        Row("Revenue", ["125,430", "110,220", "362,890", "318,455"]),
        Row("Cost of sales", ["(92,318)", "(83,655)", "(268,102)", "(240,330)"]),
        Row("Gross profit", ["33,112", "26,565", "94,788", "78,125"], bold=True),
        Row("Other income", ["2,145", "1,980", "6,410", "5,220"]),
        Row("Distribution costs", ["(8,220)", "(7,510)", "(24,115)", "(22,040)"]),
        Row("Administrative expenses", ["(11,455)", "(10,330)", "(33,890)", "(30,115)"]),
        Row("Profit from operations", ["15,582", "10,705", "43,193", "31,190"], bold=True),
        Row("Finance costs", ["(2,110)", "(2,455)", "(6,480)", "(7,220)"]),
        Row("Share of results of associates", ["330", "(120)", "1,015", "455"]),
        Row("Profit before taxation", ["13,802", "8,130", "37,728", "24,425"], bold=True),
        Row("Income tax expense", ["(3,450)", "(2,030)", "(9,432)", "(6,105)"]),
        Row("Profit for the period", ["10,352", "6,100", "28,296", "18,320"], bold=True),
        Row("Attributable to:", []),
        Row("Owners of the parent", ["9,845", "5,820", "26,930", "17,450"], indent=1),
        Row("Non-controlling interests", ["507", "280", "1,366", "870"], indent=1),
        Row("Basic earnings per share (sen)", ["2.45", "1.45", "6.71", "4.35"]),
    ],
)

# A first-quarter report with only one column pair (current quarter vs its
# own prior-year comparative) - real and confirmed on United Plantations:
# when individual-quarter and cumulative-YTD are the same figure (Q1 *is*
# the first quarter's YTD), some issuers print just one set of columns
# instead of a redundant pair, so the ambiguous-collision guard never
# triggers and this statement's own stated duration ("Three Months Ended")
# is what must drive its period type - not the stated end-date's month, the
# bug `resolve_duration_period_type` fixes.
QUARTERLY_INCOME_STATEMENT_Q1_ONLY = StatementSpec(
    title="UNITED PLANTATIONS BERHAD",
    subtitle="Condensed Consolidated Statement of Comprehensive Income for the "
    "Three Months Ended 31 March 2022 (RM'000)",
    column_headers=[
        ["2022", "2021"],
    ],
    rows=[
        Row("Revenue", ["642,908", "399,654"]),
        Row("Operating expenses", ["(586,730)", "(311,783)"]),
        Row("Other operating income", ["20,401", "4,677"]),
        Row("Finance costs", ["(46)", "(6)"]),
        Row("Profit before taxation", ["76,751", "91,912"], bold=True),
        Row("Income tax expense", ["(15,141)", "(16,202)"]),
        Row("Profit for the period", ["61,610", "75,710"], bold=True),
        Row("Attributable to:", []),
        Row("Owners of the parent", ["59,693", "74,825"], indent=1),
        Row("Non-controlling interests", ["1,917", "885"], indent=1),
        Row("Basic earnings per share (sen)", ["14.39", "18.04"]),
    ],
    column_x=[420.0, 540.0],
)

# Same numbers, adversarially styled: shading, a different font, wrapped labels,
# a nil marker, and a trailing note reference.
QUARTERLY_INCOME_STATEMENT_UGLY = StatementSpec(
    title="Condensed Consolidated Income Statement",
    subtitle="3rd quarter ended 30 September 2024 — unaudited — RM '000",
    column_headers=[
        ["Individual", "Individual", "Cumulative", "Cumulative"],
        ["30.09.2024", "30.09.2023", "30.09.2024", "30.09.2023"],
    ],
    rows=[
        Row("Turnover (Note 3)", ["125,430", "110,220", "362,890", "318,455"]),
        Row("Cost of goods sold", ["(92,318)", "(83,655)", "(268,102)", "(240,330)"]),
        Row("Gross profit/(loss)", ["33,112", "26,565", "94,788", "78,125"], bold=True),
        Row("Other operating income", ["2,145", "1,980", "6,410", "5,220"]),
        Row("Selling and distribution expenses", ["(8,220)", "(7,510)", "(24,115)", "(22,040)"]),
        Row("General and administrative expenses", ["(11,455)", "(10,330)", "(33,890)", "(30,115)"]),
        Row("Impairment loss", ["-", "-", "-", "(1,200)"]),
        Row("Profit before taxation", ["13,802", "8,130", "37,728", "24,425"], bold=True),
        Row("Taxation", ["(3,450)", "(2,030)", "(9,432)", "(6,105)"]),
        Row("Net profit for the period", ["10,352", "6,100", "28,296", "18,320"], bold=True),
    ],
    column_x=[300.0, 380.0, 465.0, 550.0],
    shade_alternate_rows=True,
    font="tiro",
    bold_font="tibo",
)

# A representative annual (not quarterly) cash flow statement: two columns,
# current year and prior-year comparative, the shape
# `bursa.pipeline.normalize` targets - including the "cash at
# beginning/end of year" boundary rows that need their own period handling
# (see that module's `_cf_boundary_bounds`).
ANNUAL_CASH_FLOW_STATEMENT = StatementSpec(
    title="STATEMENTS OF CASH FLOWS",
    subtitle="For the financial year ended 31 December 2024 (RM'000)",
    column_headers=[
        ["Group", "Group"],
        ["2024", "2023"],
    ],
    rows=[
        Row("Profit before taxation", ["45,200", "38,650"]),
        Row("Net cash generated from operating activities", ["52,340", "44,120"], bold=True),
        Row("Net cash generated from investing activities", ["(18,220)", "(15,900)"], bold=True),
        Row("Net cash generated from financing activities", ["(12,500)", "(10,300)"], bold=True),
        Row("Net increase in cash and cash equivalents", ["21,620", "17,920"], bold=True),
        Row("Cash and cash equivalents at beginning of the year", ["38,220", "20,300"]),
        Row("Cash and cash equivalents at end of the year", ["59,840", "38,220"], bold=True),
    ],
    column_x=[420.0, 540.0],
)

# An income statement where "Owners of the Company" / "Non-controlling
# interests" appear twice with identical labels - once under "Profit for the
# financial year" (the net profit split), again under "Total comprehensive
# income for the financial year" (a different split, including OCI) -
# confirmed real on Vitrox Corporation's filing. For
# bursa.extract.statement_extract's section-aware remap
# (is.pat_owners/is.pat_nci for the first pair, is.tci_owners/is.tci_nci for
# the second - see that module's _SECTION_REMAP).
INCOME_STATEMENT_WITH_DUPLICATE_OWNERS_SPLIT = StatementSpec(
    title="STATEMENTS OF PROFIT OR LOSS AND OTHER COMPREHENSIVE INCOME",
    subtitle="For the financial year ended 31 December 2024 (RM'000)",
    column_headers=[
        ["Group", "Group"],
        ["2024", "2023"],
    ],
    rows=[
        Row("Revenue", ["125,430", "110,220"]),
        Row("Cost of sales", ["(92,318)", "(83,655)"]),
        Row("Gross profit", ["33,112", "26,565"], bold=True),
        Row("Profit before taxation", ["13,802", "8,130"], bold=True),
        Row("Income tax expense", ["(3,450)", "(2,030)"]),
        Row("Profit for the financial year", ["10,352", "6,100"], bold=True),
        Row("Owners of the Company", ["9,845", "5,820"], indent=1),
        Row("Non-controlling interests", ["507", "280"], indent=1),
        Row("Other comprehensive income", ["420", "150"]),
        Row("Total comprehensive income for the financial year", ["10,772", "6,250"], bold=True),
        Row("Owners of the Company", ["10,220", "5,940"], indent=1),
        Row("Non-controlling interests", ["552", "310"], indent=1),
    ],
    column_x=[420.0, 540.0],
)

# Same duplicate-breakdown problem as above, but with the *other* real
# layout confirmed on Vitrox Corporation's own filing: both subtotals
# ("Profit for the financial year", "Comprehensive income for the financial
# year" - no "Total" prefix on this one, also confirmed real) are stated
# first, with both owners/NCI breakdown blocks grouped together afterwards -
# not interleaved with their own subtotal. Needs occurrence-order pairing,
# not "whichever subtotal was most recently seen" - see
# bursa.extract.statement_extract's module docstring.
INCOME_STATEMENT_WITH_GROUPED_OWNERS_SPLIT = StatementSpec(
    title="STATEMENTS OF PROFIT OR LOSS AND OTHER COMPREHENSIVE INCOME",
    subtitle="For the financial year ended 31 December 2024 (RM'000)",
    column_headers=[
        ["Group", "Group"],
        ["2024", "2023"],
    ],
    rows=[
        Row("Revenue", ["125,430", "110,220"]),
        Row("Profit before taxation", ["13,802", "8,130"], bold=True),
        Row("Income tax expense", ["(3,450)", "(2,030)"]),
        Row("Profit for the financial year", ["10,352", "6,100"], bold=True),
        Row("Other comprehensive income", ["420", "150"]),
        Row("Comprehensive income for the financial year", ["10,772", "6,250"], bold=True),
        Row("Owners of the Company", ["9,845", "5,820"], indent=1),
        Row("Non-controlling interests", ["507", "280"], indent=1),
        Row("Owners of the Company", ["10,220", "5,940"], indent=1),
        Row("Non-controlling interests", ["552", "310"], indent=1),
    ],
    column_x=[420.0, 540.0],
)

# An annual income statement whose comparative-year revenue is a clean 1000x
# of the current year's - the classic RM'000-scale-confusion bug this project
# hit for real multiple times this session. For
# bursa.pipeline.validate's `magnitude_sanity` wiring.
ANNUAL_INCOME_STATEMENT_WITH_SCALE_ERROR = StatementSpec(
    title="STATEMENTS OF PROFIT OR LOSS",
    subtitle="For the financial year ended 31 December 2024 (RM'000)",
    column_headers=[
        ["Group", "Group"],
        ["2024", "2023"],
    ],
    rows=[
        Row("Revenue", ["125,000", "125"]),
        Row("Cost of sales", ["(92,000)", "(92)"]),
        Row("Gross profit", ["33,000", "33"], bold=True),
        Row("Profit before taxation", ["13,800", "13.8"], bold=True),
        Row("Income tax expense", ["(3,450)", "(3.45)"]),
        Row("Profit for the period", ["10,350", "10.35"], bold=True),
    ],
    column_x=[420.0, 540.0],
)

# An annual (not interim/quarterly) balance sheet: both columns are "as at"
# the same calendar day a year apart (31 Dec 2024 / 31 Dec 2023) - the real
# annual-report shape. `BALANCE_SHEET` below is deliberately the *interim*
# shape instead (unaudited current quarter-end vs last audited financial
# year end, different months) for `test_statement_extract.py`'s own
# purposes - reusing it for `bursa.pipeline.normalize` would silently
# misdate the comparative column, which is exactly the case that module
# documents itself as out of scope for, so it gets its own fixture here.
ANNUAL_BALANCE_SHEET = StatementSpec(
    title="STATEMENTS OF FINANCIAL POSITION",
    subtitle="As at 31 December 2024 (RM'000)",
    column_headers=[
        ["Group", "Group"],
        ["2024", "2023"],
    ],
    rows=[
        Row("Property, plant and equipment", ["248,330", "241,115"], indent=1),
        Row("Total non-current assets", ["270,860", "262,400"], bold=True),
        Row("Inventories", ["64,220", "58,910"], indent=1),
        Row("Total current assets", ["194,740", "176,460"], bold=True),
        Row("TOTAL ASSETS", ["465,600", "438,860"], bold=True),
        Row("Share capital", ["180,000", "180,000"], indent=1),
        Row("Total equity", ["300,675", "279,255"], bold=True),
        Row("Total non-current liabilities", ["87,515", "91,100"], bold=True),
        Row("Total current liabilities", ["77,410", "68,505"], bold=True),
        Row("TOTAL EQUITY AND LIABILITIES", ["465,600", "438,860"], bold=True),
    ],
    column_x=[420.0, 540.0],
)

# Same shape as ANNUAL_BALANCE_SHEET, but the current year's TOTAL ASSETS /
# TOTAL EQUITY AND LIABILITIES figure is deliberately wrong (the comparative
# year still balances) - for bursa.pipeline.validate tests: `bs_balances` and
# `bs_footing` must fail for 2024 and pass for 2023, independently per period.
ANNUAL_BALANCE_SHEET_UNBALANCED = StatementSpec(
    title="STATEMENTS OF FINANCIAL POSITION",
    subtitle="As at 31 December 2024 (RM'000)",
    column_headers=[
        ["Group", "Group"],
        ["2024", "2023"],
    ],
    rows=[
        Row("Total non-current assets", ["270,860", "262,400"], bold=True),
        Row("Total current assets", ["194,740", "176,460"], bold=True),
        Row("TOTAL ASSETS", ["999,999", "438,860"], bold=True),
        Row("Total equity", ["300,675", "279,255"], bold=True),
        Row("Total non-current liabilities", ["87,515", "91,100"], bold=True),
        Row("Total current liabilities", ["77,410", "68,505"], bold=True),
        Row("Total liabilities", ["164,925", "159,605"], bold=True),
        Row("TOTAL EQUITY AND LIABILITIES", ["999,999", "438,860"], bold=True),
    ],
    column_x=[420.0, 540.0],
)

# A bank-style balance sheet: a leading "Note" column (carrying footnote
# reference numbers, not figures) followed by Group and Company columns for
# the same two years - confirmed real on Alliance Bank's and AMMB's filings.
# Exercises two bursa.pipeline.normalize fixes: a Note column must never be
# treated as a value column, and a Group/Company pair sharing the same year
# must not be treated as a colliding/ambiguous pair of columns.
ANNUAL_BALANCE_SHEET_WITH_NOTE_AND_BASIS_COLUMNS = StatementSpec(
    title="STATEMENTS OF FINANCIAL POSITION",
    subtitle="As at 31 December 2024 (RM'000)",
    column_headers=[
        ["", "Group", "Group", "Company", "Company"],
        ["Note", "2024", "2023", "2024", "2023"],
    ],
    rows=[
        Row(
            "Property, plant and equipment",
            ["16", "248,330", "241,115", "180,220", "175,400"],
            indent=1,
        ),
        Row("Deferred tax assets", ["17", "4,110", "3,880", "2,900", "2,650"], indent=1),
        Row("Total non-current assets", ["", "270,860", "262,400", "195,000", "188,300"], bold=True),
        Row("Inventories", ["21", "64,220", "58,910", "40,100", "37,500"], indent=1),
        Row("Trade receivables", ["22", "88,415", "79,330", "55,200", "49,800"], indent=1),
        Row("Total current assets", ["", "194,740", "176,460", "125,500", "116,800"], bold=True),
        Row("TOTAL ASSETS", ["", "465,600", "438,860", "320,500", "305,100"], bold=True),
        Row("Share capital", ["25", "180,000", "180,000", "150,000", "150,000"], indent=1),
        Row("Retained earnings", ["26", "112,455", "92,115", "80,200", "65,400"], indent=1),
        Row("Total equity", ["", "300,675", "279,255", "235,000", "220,500"], bold=True),
        Row("Long term borrowings", ["29", "78,400", "82,330", "50,000", "48,000"], indent=1),
        Row("Total non-current liabilities", ["", "87,515", "91,100", "55,000", "52,500"], bold=True),
        Row("Trade payables", ["31", "51,330", "44,225", "20,500", "22,300"], indent=1),
        Row("Total current liabilities", ["", "77,410", "68,505", "30,500", "32,300"], bold=True),
        Row("TOTAL EQUITY AND LIABILITIES", ["", "465,600", "438,860", "320,500", "305,100"], bold=True),
    ],
    column_x=[260.0, 330.0, 400.0, 470.0, 540.0],
)

# A balance sheet with a restated MFRS-transition opening-balance column -
# confirmed real on Tenaga Nasional's: current year, restated comparative,
# AND a restated opening-balance date a year further back ("1.1.2024"
# alongside the ordinary "31.12.2024" comparative) - plus a Note column
# whose own header has stray page-title text bled into it ("STATEMENTS
# STATEMENT Note"), also confirmed real on that filing.
ANNUAL_BALANCE_SHEET_WITH_RESTATED_OPENING_COLUMN = StatementSpec(
    title="STATEMENTS OF FINANCIAL POSITION",
    subtitle="As at 31 December 2024 (RM'000)",
    column_headers=[
        ["STATEMENTS STATEMENT Note", "31.12.2024", "31.12.2023", "1.1.2023"],
        ["", "", "Restated", "Restated"],
    ],
    rows=[
        Row("Property, plant and equipment", ["16", "248,330", "241,115", "235,000"], indent=1),
        Row("Deferred tax assets", ["17", "4,110", "3,880", "3,600"], indent=1),
        Row("Total non-current assets", ["", "270,860", "262,400", "255,000"], bold=True),
        Row("Inventories", ["21", "64,220", "58,910", "55,000"], indent=1),
        Row("Total current assets", ["", "194,740", "176,460", "170,000"], bold=True),
        Row("TOTAL ASSETS", ["", "465,600", "438,860", "425,000"], bold=True),
        Row("Share capital", ["25", "180,000", "180,000", "180,000"], indent=1),
        Row("Total equity", ["", "300,675", "279,255", "270,000"], bold=True),
        Row("Total non-current liabilities", ["", "87,515", "91,100", "88,000"], bold=True),
        Row("Total current liabilities", ["", "77,410", "68,505", "67,000"], bold=True),
        Row("TOTAL EQUITY AND LIABILITIES", ["", "465,600", "438,860", "425,000"], bold=True),
    ],
    column_x=[220.0, 320.0, 420.0, 540.0],
)

# Same shape as ANNUAL_BALANCE_SHEET, two years further back - for a
# multi-year-backfill test: two separate documents (different filing years)
# for the same company must both contribute facts, not have one discarded
# as "not the best-scoring document" (see bursa.pipeline.normalize's
# write_facts_for_company docstring).
ANNUAL_BALANCE_SHEET_TWO_YEARS_EARLIER = StatementSpec(
    title="STATEMENTS OF FINANCIAL POSITION",
    subtitle="As at 31 December 2022 (RM'000)",
    column_headers=[
        ["Group", "Group"],
        ["2022", "2021"],
    ],
    rows=[
        Row("Property, plant and equipment", ["210,000", "205,000"], indent=1),
        Row("Total non-current assets", ["230,000", "222,000"], bold=True),
        Row("Inventories", ["50,000", "47,000"], indent=1),
        Row("Total current assets", ["160,000", "150,000"], bold=True),
        Row("TOTAL ASSETS", ["390,000", "372,000"], bold=True),
        Row("Share capital", ["150,000", "150,000"], indent=1),
        Row("Total equity", ["250,000", "235,000"], bold=True),
        Row("Total non-current liabilities", ["75,000", "78,000"], bold=True),
        Row("Total current liabilities", ["65,000", "59,000"], bold=True),
        Row("TOTAL EQUITY AND LIABILITIES", ["390,000", "372,000"], bold=True),
    ],
    column_x=[420.0, 540.0],
)

BALANCE_SHEET = StatementSpec(
    title="CONDENSED CONSOLIDATED STATEMENT OF FINANCIAL POSITION",
    subtitle="As at 30 September 2024 (RM'000)",
    column_headers=[
        ["Unaudited", "Audited", "", ""],
        ["30.09.2024", "31.12.2023", "", ""],
    ],
    rows=[
        Row("ASSETS", []),
        Row("Non-current assets", [], bold=True),
        Row("Property, plant and equipment", ["248,330", "241,115"], indent=1),
        Row("Investment in associates", ["18,420", "17,405"], indent=1),
        Row("Deferred tax assets", ["4,110", "3,880"], indent=1),
        Row("Total non-current assets", ["270,860", "262,400"], bold=True),
        Row("Current assets", [], bold=True),
        Row("Inventories", ["64,220", "58,910"], indent=1),
        Row("Trade receivables", ["88,415", "79,330"], indent=1),
        Row("Cash and cash equivalents", ["42,105", "38,220"], indent=1),
        Row("Total current assets", ["194,740", "176,460"], bold=True),
        Row("TOTAL ASSETS", ["465,600", "438,860"], bold=True),
        Row("EQUITY AND LIABILITIES", []),
        Row("Share capital", ["180,000", "180,000"], indent=1),
        Row("Retained earnings", ["112,455", "92,115"], indent=1),
        Row("Equity attributable to owners of the parent", ["292,455", "272,115"], bold=True),
        Row("Non-controlling interests", ["8,220", "7,140"], indent=1),
        Row("Total equity", ["300,675", "279,255"], bold=True),
        Row("Long term borrowings", ["78,400", "82,330"], indent=1),
        Row("Deferred tax liabilities", ["9,115", "8,770"], indent=1),
        Row("Total non-current liabilities", ["87,515", "91,100"], bold=True),
        Row("Trade payables", ["51,330", "44,225"], indent=1),
        Row("Short term borrowings", ["21,080", "20,110"], indent=1),
        Row("Tax payable", ["5,000", "4,170"], indent=1),
        Row("Total current liabilities", ["77,410", "68,505"], bold=True),
        Row("Total liabilities", ["164,925", "159,605"], bold=True),
        Row("TOTAL EQUITY AND LIABILITIES", ["465,600", "438,860"], bold=True),
        Row("Net assets per share (RM)", ["0.73", "0.68"]),
    ],
    column_x=[420.0, 540.0],
)
