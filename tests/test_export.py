from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from bursa.db.enums import Continuity, PeriodType
from bursa.db.models import Company
from bursa.export.excel import FIRST_YEAR_COL, HEADER_ROW, company_workbook
from bursa.export.long_csv import COLUMNS, facts_long_csv
from tests.test_analysis import Builder


@pytest.fixture
def b(session: Session) -> Builder:
    b = Builder(session)
    for fy, rev in {2021: 1_000_000, 2022: 1_200_000, 2023: 1_500_000}.items():
        b.fact("is.revenue", rev, fy, doc=f"AR{fy}.pdf")
        b.fact("is.profit_before_tax", rev * 0.2, fy, doc=f"AR{fy}.pdf")
        b.fact("is.pat_owners", rev * 0.15, fy, doc=f"AR{fy}.pdf")
        b.fact("is.eps_basic", 12.5, fy, doc=f"AR{fy}.pdf")
        b.fact("bs.total_assets", rev * 2, fy, doc=f"AR{fy}.pdf")
        b.fact("bs.equity_owners", rev, fy, doc=f"AR{fy}.pdf")
    # Noise the loader must exclude.
    b.fact("is.revenue", 999, 2023, ptype=PeriodType.Q1, end=date(2023, 3, 31), doc="Q1.pdf")
    b.fact("is.revenue", 7, 2023, doc="X.pdf", continuity=Continuity.DISCONTINUED)
    return b


def _row_of(ws, concept_key: str) -> int:
    for row in range(HEADER_ROW + 1, ws.max_row + 1):
        if ws.cell(row=row, column=2).value == concept_key:
            return row
    raise AssertionError(f"{concept_key} not on {ws.title}")


def test_workbook_sheets_years_and_scaling(b: Builder) -> None:
    wb = company_workbook(b.s, b.company)

    assert wb.sheetnames == ["Income Statement", "Balance Sheet", "Cash Flow", "Ratios", "Sources"]
    ws = wb["Income Statement"]
    years = [ws.cell(row=HEADER_ROW, column=FIRST_YEAR_COL + i).value for i in range(3)]
    assert years == [2021, 2022, 2023]
    assert ws.freeze_panes == "D2"

    rev = _row_of(ws, "is.revenue")
    assert ws.cell(row=rev, column=FIRST_YEAR_COL + 2).value == pytest.approx(1500.0)  # RM '000
    assert ws.cell(row=rev, column=3).value == "RM '000"

    eps = _row_of(ws, "is.eps_basic")
    assert ws.cell(row=eps, column=FIRST_YEAR_COL).value == pytest.approx(12.5)  # not scaled
    assert ws.cell(row=eps, column=3).value == "sen"
    assert ws.cell(row=eps, column=1).value.lower().count("(sen)") == 1
    assert eps > rev  # IS sort order: revenue first

    bs = wb["Balance Sheet"]
    ta = _row_of(bs, "bs.total_assets")
    assert bs.cell(row=ta, column=FIRST_YEAR_COL).value == pytest.approx(2000.0)
    assert bs.cell(row=ta, column=1).font.bold  # total assets is a subtotal


def test_workbook_custom_scale_and_ratios_and_sources(b: Builder) -> None:
    wb = company_workbook(b.s, b.company, scale=1)

    ws = wb["Income Statement"]
    assert ws.cell(row=_row_of(ws, "is.revenue"), column=FIRST_YEAR_COL).value == pytest.approx(1_000_000)
    assert ws.cell(row=_row_of(ws, "is.eps_basic"), column=FIRST_YEAR_COL).value == pytest.approx(12.5)

    ratios = wb["Ratios"]
    roe_rows = [r for r in range(1, ratios.max_row + 1) if ratios.cell(row=r, column=1).value == "ROE"]
    assert ratios.cell(row=roe_rows[0], column=FIRST_YEAR_COL).value == pytest.approx(0.15)

    src = wb["Sources"]
    assert [src.cell(row=r, column=1).value for r in (2, 3, 4)] == [2021, 2022, 2023]
    assert src.cell(row=4, column=4).value == "AR2023.pdf"  # Q1/discontinued docs excluded


def test_facts_long_csv(b: Builder, tmp_path: Path) -> None:
    other = Company(stock_code="8888", name="Empty Berhad")
    b.s.add(other)
    b.s.flush()
    out = tmp_path / "facts.csv"

    n = facts_long_csv(b.s, [b.company, other], out)

    with out.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert n == len(rows) == 3 * 6
    assert list(rows[0].keys()) == COLUMNS
    rev23 = [r for r in rows if r["fiscal_year"] == "2023" and r["concept_key"] == "is.revenue"]
    assert len(rev23) == 1
    assert rev23[0]["value"] == "1500000"
    assert rev23[0]["period_end"] == "2023-12-31"
    assert rev23[0]["statement"] == "IS"
