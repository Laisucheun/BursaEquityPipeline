from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from bursa.db.enums import Basis, DocSource, PeriodType
from bursa.db.models import Company, Document, Fact, Period
from bursa.mapping.synonyms import seed_concepts
from bursa.extract.statement_extract import RowInfo
from bursa.pipeline.equity import _split_blocks, classify_movement
from bursa.pipeline.normalize import write_facts_for_document
from bursa.validate.rules import PeriodFacts, PeriodKey, eq_closing_matches_bs, eq_roll_forward
from tests.fixtures.synthetic import Row, StatementSpec, build_statement_pdf

SIX_COLUMNS = [262.0, 314.0, 366.0, 418.0, 492.0, 560.0]

NCI_HEADERS = [
    ["Share", "Other", "Retained", "", "Non-", "Total"],
    ["capital", "reserves", "earnings", "Total", "controlling", "equity"],
    ["RM'000", "RM'000", "RM'000", "RM'000", "interests", "RM'000"],
]


def _rows_two_blocks() -> list[Row]:
    return [
        Row("At 1 January 2023", ["100,000", "5,000", "200,000", "305,000", "20,000", "325,000"]),
        Row("Profit for the year", ["-", "-", "40,000", "40,000", "4,000", "44,000"]),
        Row("Other comprehensive income", ["-", "1,000", "-", "1,000", "-", "1,000"]),
        Row("Total comprehensive income", ["-", "1,000", "40,000", "41,000", "4,000", "45,000"]),
        Row("Dividends paid to owners of the Company", ["-", "-", "(15,000)", "(15,000)", "-", "(15,000)"]),
        Row("Dividends paid to non-controlling interests", ["-", "-", "-", "-", "(2,000)", "(2,000)"]),
        Row("At 31.12.2023", ["100,000", "6,000", "225,000", "331,000", "22,000", "353,000"]),
        Row("Profit for the year", ["-", "-", "50,000", "50,000", "5,000", "55,000"]),
        Row("Other comprehensive income", ["-", "(500)", "-", "(500)", "-", "(500)"]),
        Row("Total comprehensive income", ["-", "(500)", "50,000", "49,500", "5,000", "54,500"]),
        Row("Issuance of ordinary shares", ["10,000", "-", "-", "10,000", "-", "10,000"]),
        Row("Purchase of treasury shares", ["(3,000)", "-", "-", "(3,000)", "-", "(3,000)"]),
        Row("Dividends paid to owners of the Company", ["-", "-", "(20,000)", "(20,000)", "-", "(20,000)"]),
        Row("At 31.12.2024", ["107,000", "5,500", "255,000", "367,500", "27,000", "394,500"]),
    ]


TWO_BLOCK_EQUITY = StatementSpec(
    title="CONSOLIDATED STATEMENT OF CHANGES IN EQUITY",
    subtitle="For the financial year ended 31 December 2024",
    column_headers=NCI_HEADERS,
    rows=_rows_two_blocks(),
    column_x=SIX_COLUMNS,
)

# The same matrix with no column naming its total - nothing marks which
# figure is total equity, so it must be skipped rather than guessed.
AMBIGUOUS_EQUITY = StatementSpec(
    title="CONSOLIDATED STATEMENT OF CHANGES IN EQUITY",
    subtitle="For the financial year ended 31 December 2024",
    column_headers=[
        ["Share", "Other", "Retained", "Owners'", "Non-", "Group"],
        ["capital", "reserves", "earnings", "funds", "controlling", "funds"],
        ["RM'000", "RM'000", "RM'000", "RM'000", "interests", "RM'000"],
    ],
    rows=_rows_two_blocks(),
    column_x=SIX_COLUMNS,
)

# No NCI column, a combined "31 December 2023/1 January 2024" row, and a
# restated opening balance.
COMPANY_EQUITY_COMBINED_ROW = StatementSpec(
    title="STATEMENT OF CHANGES IN EQUITY",
    subtitle="For the financial year ended 30 June 2024",
    column_headers=[
        ["Company", "Share", "Retained", ""],
        ["", "capital", "earnings", "Total"],
        ["", "RM'000", "RM'000", "RM'000"],
    ],
    rows=[
        Row("At 1.7.2022, as previously reported", ["", "50,000", "70,000", "120,000"]),
        Row("Effect of change in accounting policy", ["", "-", "(2,000)", "(2,000)"]),
        Row("At 1.7.2022, as restated", ["", "50,000", "68,000", "118,000"]),
        Row("Profit for the financial year", ["", "-", "12,000", "12,000"]),
        Row("Total comprehensive income", ["", "-", "12,000", "12,000"]),
        Row("Dividends", ["", "-", "(6,000)", "(6,000)"]),
        Row("At 30.6.2023/1.7.2023", ["", "50,000", "74,000", "124,000"]),
        Row("Profit for the financial year", ["", "-", "9,000", "9,000"]),
        Row("Interim dividend", ["", "-", "(3,000)", "(3,000)"]),
        Row("Final dividend", ["", "-", "(4,000)", "(4,000)"]),
        Row("At 30.6.2024", ["", "50,000", "76,000", "126,000"]),
    ],
    column_x=[300.0, 380.0, 460.0, 555.0],
)


@pytest.fixture
def seeded(session: Session) -> Session:
    seed_concepts(session)
    session.commit()
    return session


def _write(session: Session, tmp_path: Path, spec: StatementSpec, fy_end_month: int | None = None):
    company = Company(stock_code="9999", name="Test Berhad", fy_end_month=fy_end_month)
    session.add(company)
    session.flush()
    pdf = build_statement_pdf(tmp_path / "eq.pdf", spec)
    document = Document(
        company_id=company.id,
        source=DocSource.UPLOAD,
        original_filename="eq.pdf",
        file_sha256="sha-eq",
        file_size=pdf.stat().st_size,
        storage_path=str(pdf),
    )
    session.add(document)
    session.flush()
    result = write_facts_for_document(session, company, document.id, pdf)
    facts = {
        (f.concept_key, p.period_type, p.period_start, p.period_end): f
        for f, p in session.execute(
            select(Fact, Period).join(Period, Fact.period_id == Period.id).where(Fact.concept_key.like("eq.%"))
        )
    }
    return result, facts


def _instant(facts, key: str, d: date) -> Decimal:
    return facts[(key, PeriodType.INSTANT, None, d)].value


def _fy(facts, key: str, start: date, end: date) -> Decimal:
    return facts[(key, PeriodType.FY, start, end)].value


def test_two_year_blocks_with_nci_write_total_equity_facts(seeded: Session, tmp_path: Path) -> None:
    result, facts = _write(seeded, tmp_path, TWO_BLOCK_EQUITY)

    assert _instant(facts, "eq.opening_balance", date(2023, 1, 1)) == 325_000_000
    assert _instant(facts, "eq.closing_balance", date(2023, 12, 31)) == 353_000_000
    # The second block has no "At 1 January 2024" row: the 2023 closing row is its opening.
    assert _instant(facts, "eq.opening_balance", date(2024, 1, 1)) == 353_000_000
    assert _instant(facts, "eq.closing_balance", date(2024, 12, 31)) == 394_500_000

    fy23 = (date(2023, 1, 1), date(2023, 12, 31))
    fy24 = (date(2024, 1, 1), date(2024, 12, 31))
    assert _fy(facts, "eq.profit_for_year", *fy23) == 44_000_000  # incl. NCI share
    assert _fy(facts, "eq.total_comprehensive_income", *fy23) == 45_000_000
    # Dividends to owners and to NCI are both line items of total equity.
    assert _fy(facts, "eq.dividends", *fy23) == -17_000_000
    assert _fy(facts, "eq.dividends", *fy24) == -20_000_000
    assert _fy(facts, "eq.issuance_of_shares", *fy24) == 10_000_000
    assert _fy(facts, "eq.share_buyback", *fy24) == -3_000_000
    assert _fy(facts, "eq.other_comprehensive_income", *fy24) == -500_000

    fact = facts[("eq.closing_balance", PeriodType.INSTANT, None, date(2024, 12, 31))]
    assert fact.basis == Basis.CONSOLIDATED
    assert fact.value_as_printed == "394,500"
    assert not any("EQ:" in n for n in result.skipped_columns), result.skipped_columns


def test_no_named_total_column_is_skipped_not_guessed(seeded: Session, tmp_path: Path) -> None:
    result, facts = _write(seeded, tmp_path, AMBIGUOUS_EQUITY)

    assert facts == {}
    assert any("no unambiguous total-equity column" in n for n in result.skipped_columns)


def test_combined_closing_opening_row_restated_opening_and_june_year_end(
    seeded: Session, tmp_path: Path
) -> None:
    result, facts = _write(seeded, tmp_path, COMPANY_EQUITY_COMBINED_ROW, fy_end_month=6)

    # The restated opening wins over "as previously reported".
    assert _instant(facts, "eq.opening_balance", date(2022, 7, 1)) == 118_000_000
    assert _instant(facts, "eq.closing_balance", date(2023, 6, 30)) == 124_000_000
    assert _instant(facts, "eq.opening_balance", date(2023, 7, 1)) == 124_000_000
    assert _instant(facts, "eq.closing_balance", date(2024, 6, 30)) == 126_000_000
    assert _fy(facts, "eq.dividends", date(2022, 7, 1), date(2023, 6, 30)) == -6_000_000
    # Interim + final dividend rows add up.
    assert _fy(facts, "eq.dividends", date(2023, 7, 1), date(2024, 6, 30)) == -7_000_000
    # The restatement adjustment is not a movement of the year.
    assert ("eq.profit_for_year", PeriodType.FY, date(2022, 7, 1), date(2023, 6, 30)) in facts
    fact = facts[("eq.closing_balance", PeriodType.INSTANT, None, date(2024, 6, 30))]
    assert fact.basis == Basis.COMPANY


def test_a_block_cut_off_before_its_closing_row_is_skipped(seeded: Session, tmp_path: Path) -> None:
    rows = _rows_two_blocks()[:-1]  # FY2024 never reaches its closing row
    spec = StatementSpec(
        title=TWO_BLOCK_EQUITY.title,
        subtitle=TWO_BLOCK_EQUITY.subtitle,
        column_headers=NCI_HEADERS,
        rows=rows,
        column_x=SIX_COLUMNS,
    )
    result, facts = _write(seeded, tmp_path, spec)

    assert ("eq.closing_balance", PeriodType.INSTANT, None, date(2023, 12, 31)) in facts
    assert not any(k[3] == date(2024, 12, 31) for k in facts)
    assert any("FY2024 block has no closing row" in n for n in result.skipped_columns)


def test_balance_dated_off_the_fiscal_year_boundary_skips_the_statement(
    seeded: Session, tmp_path: Path
) -> None:
    # Company FYE is June, but the matrix is dated in December - e.g. a
    # change of financial year end. Which year each block is cannot be told.
    result, facts = _write(seeded, tmp_path, TWO_BLOCK_EQUITY, fy_end_month=6)

    assert facts == {}
    assert any("not dated on the fiscal-year boundary" in n for n in result.skipped_columns)


@pytest.mark.parametrize(
    ("label", "concept"),
    [
        ("Dividends 23", "eq.dividends"),
        ("Final single-tier dividend for FY2023", "eq.dividends"),
        ("Dividends paid to non-controlling interests", "eq.dividends"),
        ("Issuance of shares pursuant to dividend reinvestment plan", "eq.issuance_of_shares"),
        ("Purchase of treasury shares 11(b)", "eq.share_buyback"),
        ("Share-based payment transactions", "eq.share_based_payments"),
        ("Profit for the financial year -", "eq.profit_for_year"),
        ("Total comprehensive income for the year", "eq.total_comprehensive_income"),
        ("Other comprehensive income, net of tax", "eq.other_comprehensive_income"),
        ("Acquisition of non-controlling interests", "eq.changes_in_nci"),
        ("Changes in ownership interests in subsidiaries", "eq.changes_in_nci"),
        ("Transfer to statutory reserve", "eq.transfer_to_reserves"),
        ("Total transactions with owners", None),
        ("Issuance of shares to non-controlling interests of a subsidiary", None),
        ("Dividend reinvestment plan", None),
    ],
)
def test_classify_movement(label: str, concept: str | None) -> None:
    assert classify_movement(label) == concept


def _period(values: dict[str, str]) -> PeriodFacts:
    return PeriodFacts(
        key=PeriodKey(date(2024, 12, 31), PeriodType.INSTANT),
        values={k: Decimal(v) for k, v in values.items()},
    )


def test_eq_closing_matches_bs() -> None:
    ok = eq_closing_matches_bs(_period({"eq.closing_balance": "394500", "bs.total_equity": "394500"}), Decimal(1))
    bad = eq_closing_matches_bs(_period({"eq.closing_balance": "367500", "bs.total_equity": "394500"}), Decimal(1))
    assert ok is not None and ok.passed
    assert bad is not None and not bad.passed and bad.delta == -27000
    assert eq_closing_matches_bs(_period({"bs.total_equity": "1"}), Decimal(1)) is None


def test_eq_roll_forward() -> None:
    assert eq_roll_forward(Decimal(325), Decimal(353), [Decimal(45), Decimal(-17)]).passed
    assert not eq_roll_forward(Decimal(325), Decimal(353), [Decimal(45)]).passed


def _row(i: int, label: str, total: str, concept: str | None = None) -> RowInfo:
    return RowInfo(row_index=i, label=label, concept_key=concept, indent_level=0, values={5: total})


def test_day_less_labels_from_the_layout_extractor_still_split_into_blocks() -> None:
    # Real shape (Hong Leong Industries): the layout extractor drops "at 1"
    # from "Balance as at 1 January 2023", and prints the second block's
    # opening as a combined "31 December 2023/1 January 2024" row.
    rows = [
        _row(1, "Balance as January 2023", "100"),
        _row(2, "Profit for the financial year", "30"),
        _row(3, "Dividends 23", "(10)"),
        _row(4, "Balance as December 2023/1 January 2024", "120"),
        _row(5, "Profit for the financial year", "40"),
        _row(6, "Balance as December 2024", "160"),
        # A Company matrix stacked under the Group one repeats FY2023.
        _row(7, "Balance as January 2023", "50"),
        _row(8, "Balance as December 2023", "55"),
    ]
    notes: list[str] = []
    blocks = _split_blocks(rows, 12, date(2024, 12, 31), notes)
    assert [(b.fiscal_year, b.opening.label, b.closing.label) for b in blocks][:2] == [
        (2023, "Balance as January 2023", "Balance as December 2023/1 January 2024"),
        (2024, "Balance as December 2023/1 January 2024", "Balance as December 2024"),
    ]
    assert [m[0] for m in blocks[0].movements] == ["eq.profit_for_year", "eq.dividends"]
    assert blocks[0].closing_date == date(2023, 12, 31)
    assert len(blocks) == 3 and blocks[2].fiscal_year == 2023  # dropped later as a repeat


def test_total_column_proved_by_arithmetic_when_its_header_was_lost() -> None:
    # Real shape (company 667): "Total equity" is printed on the page, but the
    # layout extractor left only "RM'000" in that column's own header band.
    from types import SimpleNamespace

    from bursa.extract.statement_extract import ColumnInfo
    from bursa.pipeline.equity import find_total_columns

    cols = [
        ColumnInfo(0, "Equity capital RM'000", 2025),
        ColumnInfo(1, "Treasury", None),
        ColumnInfo(2, "Distributable earnings", None),
        ColumnInfo(3, "RM'000", 2025),
    ]
    extracted = SimpleNamespace(columns=cols, header_text="Statements of Changes in Equity")

    def bal(i: int, vals: list[str]) -> RowInfo:
        return RowInfo(i, "Balance at 1 January 2024", None, 0, dict(enumerate(vals)))

    good = [bal(1, ["86,407", "(541)", "15,073", "100,939"]), bal(2, ["86,407", "(541)", "13,763", "99,629"])]
    notes: list[str] = []
    found = find_total_columns(extracted, notes, good)
    assert found is not None and found.total.col_index == 3

    bad = [good[0], bal(2, ["86,407", "(541)", "13,763", "99,000"])]
    assert find_total_columns(extracted, notes, bad) is None
    assert find_total_columns(extracted, notes, good[:1]) is None  # one row proves nothing


# --------------------------------------------------------------------------
# Page detection: dated balance rows with written-out dates. The day numbers
# ("1", "31") used to form a column band of their own and cut "At 1" off the
# label, so neither the page nor its opening/closing rows were recognised.
# --------------------------------------------------------------------------


def _written_dates(rows: list[Row], mapping: dict[str, str]) -> list[Row]:
    return [Row(mapping.get(r.label, r.label), r.values) for r in rows]


def test_written_out_balance_dates_are_found_and_kept_whole(seeded: Session, tmp_path: Path) -> None:
    spec = StatementSpec(
        title=TWO_BLOCK_EQUITY.title,
        subtitle=TWO_BLOCK_EQUITY.subtitle,
        column_headers=NCI_HEADERS,
        rows=_written_dates(
            _rows_two_blocks(),
            {"At 31.12.2023": "At 31 December 2023", "At 31.12.2024": "At 31 December 2024"},
        ),
        column_x=SIX_COLUMNS,
    )
    result, facts = _write(seeded, tmp_path, spec)

    assert _instant(facts, "eq.opening_balance", date(2023, 1, 1)) == 325_000_000
    assert _instant(facts, "eq.closing_balance", date(2024, 12, 31)) == 394_500_000
    assert _fy(facts, "eq.profit_for_year", date(2024, 1, 1), date(2024, 12, 31)) == 55_000_000


def test_a_june_year_end_equity_statement_is_found(seeded: Session, tmp_path: Path) -> None:
    # No "at 1 january"/"at 31 december" synonym can match a July-June year.
    rows = _written_dates(
        _rows_two_blocks(),
        {
            "At 1 January 2023": "At 1 July 2022",
            "At 31.12.2023": "At 30 June 2023",
            "At 31.12.2024": "At 30 June 2024",
        },
    )
    spec = StatementSpec(
        title=TWO_BLOCK_EQUITY.title,
        subtitle="For the financial year ended 30 June 2024",
        column_headers=NCI_HEADERS,
        rows=rows,
        column_x=SIX_COLUMNS,
    )
    result, facts = _write(seeded, tmp_path, spec)

    assert _instant(facts, "eq.opening_balance", date(2022, 7, 1)) == 325_000_000
    assert _instant(facts, "eq.closing_balance", date(2023, 6, 30)) == 353_000_000
    assert _instant(facts, "eq.closing_balance", date(2024, 6, 30)) == 394_500_000


def test_eq_boundary_phrases() -> None:
    from bursa.extract.page_scoring import _eq_boundary_phrases

    both = _eq_boundary_phrases(["At 1 July 2023", "Profit", "At 30 June 2024"])
    assert "beginning" in both and "end of year" in both
    # Day lost to the extractor: adjacent months still pair up.
    assert "end of year" in _eq_boundary_phrases(["Balance as January 2023", "Balance as December 2023"])
    assert _eq_boundary_phrases(["Balance as January 2023", "Balance as March 2023"]) == ""
    assert "beginning" in _eq_boundary_phrases(["At 31.12.2023/1.1.2024"])
    assert _eq_boundary_phrases(["Dividends for the year ended 31 December 2023"]) == ""


def test_a_continued_page_without_subtitle_takes_its_year_from_the_closing_row(
    seeded: Session, tmp_path: Path
) -> None:
    spec = StatementSpec(
        title="STATEMENT OF CHANGES IN EQUITY (CONT'D)",
        subtitle="The Company",
        column_headers=COMPANY_EQUITY_COMBINED_ROW.column_headers,
        rows=COMPANY_EQUITY_COMBINED_ROW.rows,
        column_x=COMPANY_EQUITY_COMBINED_ROW.column_x,
    )
    result, facts = _write(seeded, tmp_path, spec, fy_end_month=6)
    assert _instant(facts, "eq.closing_balance", date(2024, 6, 30)) == 126_000_000


def test_neither_group_nor_company_named_is_skipped(seeded: Session, tmp_path: Path) -> None:
    spec = StatementSpec(
        title="STATEMENT OF CHANGES IN EQUITY",
        subtitle="For the financial year ended 30 June 2024",
        column_headers=[["", "Share", "Retained", ""], ["", "capital", "earnings", "Total"], ["", "RM'000", "RM'000", "RM'000"]],
        rows=COMPANY_EQUITY_COMBINED_ROW.rows,
        column_x=COMPANY_EQUITY_COMBINED_ROW.column_x,
    )
    result, facts = _write(seeded, tmp_path, spec, fy_end_month=6)
    assert facts == {}
    assert any("basis unknown" in n for n in result.skipped_columns)


def test_a_stacked_second_matrix_that_does_not_chain_is_dropped() -> None:
    from bursa.extract.statement_extract import ColumnInfo
    from bursa.pipeline.equity import _continuous

    rows = [
        _row(1, "At 1 January 2024", "66,085"),
        _row(2, "At 31 December 2024", "75,436"),
        _row(3, "At 1 January 2025", "35,207"),  # the Company's matrix starts here
        _row(4, "At 31 December 2025", "42,029"),
    ]
    blocks = _split_blocks(rows, 12, date(2025, 12, 31), [])
    notes: list[str] = []
    kept = _continuous(blocks, ColumnInfo(5, "Total equity", None), notes)
    assert [b.fiscal_year for b in kept] == [2024]
    assert any("stacked matrix" in n for n in notes)
