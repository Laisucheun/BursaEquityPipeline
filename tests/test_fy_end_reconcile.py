from bursa.db.enums import Statement
from bursa.db.models import Company
from bursa.extract.statement_extract import StatementExtraction
from bursa.normalize.scale import ScaleInfo
from bursa.pipeline.normalize import FactWriteResult, _reconcile_fy_end_month


def _stmt(statement: Statement, header: str) -> StatementExtraction:
    return StatementExtraction(
        statement=statement, page_no=1, final_score=1.0, row_keyword_hits=1,
        scale=ScaleInfo(1000, "'000", "MYR"), header_text=header, columns=[], rows=[],
    )


def _best(*items: tuple[Statement, str]) -> dict:
    return {(s, i): (1.0, _stmt(s, h), i) for i, (s, h) in enumerate(items)}


def test_wrong_stored_fy_end_month_is_corrected_by_annual_statements() -> None:
    company = Company(stock_code="5347", name="TNB", fy_end_month=8)
    result = FactWriteResult()
    best = _best(
        (Statement.INCOME_STATEMENT, "For the financial year ended 31 December 2024"),
        (Statement.CASH_FLOW, "For the financial year ended 31 December 2024"),
        (Statement.INCOME_STATEMENT, "For the financial year ended 31 December 2023"),
        (Statement.BALANCE_SHEET, "As at 31 August 2024"),  # never votes
    )

    _reconcile_fy_end_month(company, best, result)

    assert company.fy_end_month == 12
    assert any("corrected 8 -> 12" in s for s in result.skipped_columns)


def test_a_changed_year_end_follows_the_latest_reports() -> None:
    # S P Setia: October year end until 2017, December since.
    company = Company(stock_code="8664", name="S P Setia", fy_end_month=12)
    old = [(Statement.INCOME_STATEMENT, f"For the financial year ended 31 October {y}") for y in range(2008, 2017)]
    new = [(Statement.INCOME_STATEMENT, f"For the financial year ended 31 December {y}") for y in (2023, 2024)]

    _reconcile_fy_end_month(company, _best(*old, *new), FactWriteResult())

    assert company.fy_end_month == 12


def test_interim_and_single_votes_do_not_change_it() -> None:
    company = Company(stock_code="1", name="X", fy_end_month=3)
    best = _best(
        (Statement.INCOME_STATEMENT, "Three months ended 30 June 2024"),
        (Statement.INCOME_STATEMENT, "For the financial year ended 31 December 2024"),
    )

    _reconcile_fy_end_month(company, best, FactWriteResult())

    assert company.fy_end_month == 3
