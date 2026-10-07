from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from bursa.analysis.dividends import compute_dividends, screen_dividends
from bursa.config import get_settings
from bursa.db.enums import DocSource, DocType
from bursa.db.models import Base, Company, Document
from bursa.db.session import get_engine, get_sessionmaker, session_scope
from tests.test_analysis import Builder


@pytest.fixture
def b(session: Session) -> Builder:
    return Builder(session)


def _year(b: Builder, fy: int, **facts: float) -> None:
    keys = {"div_cf": "cf.dividends_paid", "div_eq": "eq.dividends",
            "shares": "is.weighted_avg_shares", "patami": "is.pat_owners",
            "eps": "is.eps_basic", "dpu": "is.dpu"}
    for k, v in facts.items():
        b.fact(keys[k], v, fy)


def test_cash_dividends_and_reported_shares(b: Builder) -> None:
    _year(b, 2023, div_cf=-40_000_000, div_eq=-45_000_000, shares=200_000_000,
          patami=100_000_000, eps=50)

    y = compute_dividends(b.s, b.company).years[0]

    assert y.dividends == 40_000_000 and y.dividends_basis == "cash"
    assert y.dps_sen == pytest.approx(20.0)
    assert y.payout_ratio == pytest.approx(0.4)
    assert y.dividend_cover == pytest.approx(2.5)
    assert y.sources["shares"] == "is.weighted_avg_shares"
    assert y.sources["dividends"].startswith("cf.dividends_paid")
    assert y.flags == []


def test_equity_statement_fallback_and_implied_shares(b: Builder) -> None:
    _year(b, 2023, div_eq=-30_000_000, patami=100_000_000, eps=25)

    y = compute_dividends(b.s, b.company).years[0]

    assert y.dividends_basis == "equity"
    assert y.shares == pytest.approx(400_000_000)  # 100m / 0.25
    assert y.dps_sen == pytest.approx(7.5)
    assert y.sources["shares"].startswith("implied")
    assert any("equity statement" in f for f in y.flags)
    assert any("implied" in f for f in y.flags)


def test_payout_flagged_not_dropped_and_shares_guard(b: Builder) -> None:
    # PATAMI stored at the wrong scale (thousands): payout explodes.
    _year(b, 2023, div_cf=-500_000_000, patami=365_000, eps=25, shares=-5)

    y = compute_dividends(b.s, b.company).years[0]

    assert y.payout_ratio is not None and y.payout_ratio > 3
    assert any(f.startswith("payout") for f in y.flags)
    assert any("not positive" in f for f in y.flags)
    assert y.sources["shares"].startswith("implied")


def test_bonus_issue_and_implausible_dps_flagged(b: Builder) -> None:
    _year(b, 2022, div_cf=-50_000_000, shares=1_000_000_000, eps=20)
    _year(b, 2023, div_cf=-50_000_000, shares=2_000_000_000, eps=0.5)

    y = compute_dividends(b.s, b.company).years[1]

    assert y.dps_growth == pytest.approx(-0.5)
    assert any("bonus issue" in f for f in y.flags)
    assert any("implausible" in f for f in y.flags)  # 2.5 sen DPS > 3x 0.5 sen EPS


def test_loss_year_has_no_payout(b: Builder) -> None:
    _year(b, 2023, div_cf=-10, patami=-100, shares=1000)

    y = compute_dividends(b.s, b.company).years[0]

    assert y.payout_ratio is None and y.paid is True
    assert any("loss year" in f for f in y.flags)


def test_streak_growth_and_gap(b: Builder) -> None:
    for fy, div in ((2017, 10), (2019, 10), (2020, 0), (2021, 10), (2022, 11), (2023, 12.1)):
        _year(b, fy, div_cf=-div * 1_000_000, shares=100_000_000, patami=50_000_000)
    _year(b, 2024, patami=60_000_000)  # latest year: no dividend figure yet

    h = compute_dividends(b.s, b.company)

    assert (h.streak, h.streak_end, h.longest_streak) == (3, 2023, 3)
    by = {y.fiscal_year: y for y in h.years}
    assert by[2020].paid is False
    assert by[2024].paid is None
    assert by[2022].dps_growth == pytest.approx(0.1)
    assert by[2019].dps_growth is None  # 2018 missing - no YoY across a gap
    assert h.median_payout == pytest.approx(0.22)
    assert any("FY2024" in n for n in h.notes)

    assert [x.stock_code for x in screen_dividends(b.s, min_years=3)] == ["9999"]
    assert screen_dividends(b.s, min_years=4) == []
    assert screen_dividends(b.s, min_years=3, min_payout=0.5) == []


def test_reit_uses_reported_dpu(b: Builder) -> None:
    b.company.name = "Test Real Estate Investment Trust"
    _year(b, 2023, div_cf=-200_000_000, shares=3_000_000_000, dpu=7.5, eps=8.0)

    h = compute_dividends(b.s, b.company)
    y = h.years[0]

    assert h.is_reit
    assert y.dps_sen == 7.5
    assert y.sources["dps_sen"].startswith("is.dpu")
    assert y.payout_ratio == pytest.approx(7.5 / 8.0)  # no PATAMI: DPU / EPS
    assert y.paid is True


def test_reit_payout_from_dpu_without_cash(b: Builder) -> None:
    _year(b, 2023, dpu=9.0, eps=10.0)

    y = compute_dividends(b.s, b.company).years[0]

    assert y.payout_ratio == pytest.approx(0.9)
    assert y.sources["payout_ratio"] == "is.dpu / is.eps_basic"


def test_yield_with_injected_prices(b: Builder) -> None:
    _year(b, 2023, div_cf=-20_000_000, shares=100_000_000)
    seen = []

    def fetch(ticker: str, start: date, end: date):
        seen.append(ticker)
        return [(date(2023, 12, 29), 4.0)]

    y = compute_dividends(b.s, b.company, prices=True, fetch=fetch).years[0]

    assert seen == ["9999.KL"]
    assert y.price == 4.0
    assert y.dividend_yield == pytest.approx(0.05)  # 20 sen / RM4.00
    assert compute_dividends(b.s, b.company).years[0].dividend_yield is None


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def _reset_caches() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from bursa.api.routes import dividends, fiveyear

    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'test.sqlite'}")
    _reset_caches()
    Base.metadata.create_all(get_engine())
    app = FastAPI()
    app.include_router(dividends.router, prefix="/api/dividends")
    app.include_router(fiveyear.router, prefix="/api/fiveyear")
    yield TestClient(app)
    get_engine().dispose()
    _reset_caches()


def test_dividends_endpoint(client: TestClient) -> None:
    with session_scope() as s:
        b = Builder(s)
        _year(b, 2022, div_cf=-10_000_000, shares=100_000_000, patami=40_000_000)
        _year(b, 2023, div_cf=-12_000_000, shares=100_000_000, patami=40_000_000)

    d = client.get("/api/dividends/9999").json()

    assert d["stock_code"] == "9999" and d["name"] == "Test Berhad"
    assert d["streak"] == 2
    assert [y["fiscal_year"] for y in d["years"]] == [2022, 2023]
    assert d["years"][1]["dps_sen"] == pytest.approx(12.0)
    assert d["years"][1]["sources"]["dps_sen"].startswith("dividends")
    assert isinstance(d["notes"], list)
    assert client.get("/api/dividends/0000").status_code == 404


def test_fiveyear_endpoint_read_only(client: TestClient, tmp_path: Path) -> None:
    with session_scope() as s:
        c = Company(stock_code="9998", name="Five Bhd")
        s.add(c)
        s.flush()
        s.add(Document(company_id=c.id, source=DocSource.UPLOAD, doc_type=DocType.ANNUAL_REPORT,
                       original_filename="ar.pdf", file_sha256="y", file_size=1,
                       storage_path=str(tmp_path / "missing.pdf"),
                       document_group_key="9998-2024"))

    d = client.get("/api/fiveyear/9998").json()

    assert d["stock_code"] == "9998"
    assert d["documents_scanned"] == 1 and d["documents_with_summary"] == 0
    assert d["documents"][0]["error"] == "file missing"
    assert d["documents"][0]["report_year"] == 2024
    assert d["checks"] == [] and d["agreement"] is None and d["year_shift"] is None
    assert set(d["counts"]) >= {"MATCH", "MISMATCH"}
    assert client.get("/api/fiveyear/0000").status_code == 404
