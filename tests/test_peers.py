from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from bursa.analysis.peers import compare_peers, compare_sector, list_sectors, percentile_rank
from bursa.db.enums import Basis, Continuity, DocSource, PeriodType
from bursa.db.models import Company, Document, ExtractionRun, Fact, Period
from bursa.mapping.synonyms import seed_concepts


class Multi:
    """Inserts companies with one annual report's worth of facts each."""

    def __init__(self, session: Session) -> None:
        self.s = session
        seed_concepts(session)

    def company(self, code: str, sector: str | None, facts: dict[int, dict[str, float]]) -> Company:
        c = Company(stock_code=code, name=f"Co {code}", sector=sector)
        self.s.add(c)
        self.s.flush()
        for fy, values in facts.items():
            d = Document(company_id=c.id, source=DocSource.UPLOAD, original_filename=f"{code}-{fy}",
                         file_sha256=f"{code}-{fy}", file_size=1, storage_path=f"{code}-{fy}",
                         filed_date=date(fy + 1, 4, 30))
            self.s.add(d)
            self.s.flush()
            run = ExtractionRun(document_id=d.id)
            self.s.add(run)
            self.s.flush()
            end = date(fy, 12, 31)
            fy_p = Period(company_id=c.id, period_start=date(fy, 1, 1), period_end=end,
                          period_type=PeriodType.FY, fiscal_year=fy)
            inst = Period(company_id=c.id, period_start=None, period_end=end,
                          period_type=PeriodType.INSTANT, fiscal_year=fy)
            self.s.add_all([fy_p, inst])
            self.s.flush()
            for key, v in values.items():
                self.s.add(Fact(
                    company_id=c.id, concept_key=key,
                    period_id=(inst if key.startswith("bs.") else fy_p).id,
                    value=Decimal(str(v)), basis=Basis.CONSOLIDATED, continuity=Continuity.TOTAL,
                    reported_in_document_id=d.id, run_id=run.id, confidence=1.0,
                ))
        self.s.flush()
        return c


def general(
    revenue: float, pat: float, assets: float = 2000, equity: float = 1000,
) -> dict[str, float]:
    return {"is.revenue": revenue, "is.pat_owners": pat,
            "bs.total_assets": assets, "bs.equity_owners": equity}


@pytest.fixture
def m(session: Session) -> Multi:
    return Multi(session)


def test_sector_median_quartiles_and_percentiles(m: Multi) -> None:
    # Net margins 5%, 10%, 15%, 20% (codes are general Industrials companies).
    for code, pat in (("0001", 50), ("0011", 100), ("0024", 150), ("0025", 200)):
        m.company(code, "Industrials", {2023: general(1000, pat)})
    m.company("0012", "Consumer Staples", {2023: general(1000, 999)})  # other sector

    r = compare_sector(m.s, "industrials")

    assert [row.stock_code for row in r.rows] == ["0001", "0011", "0024", "0025"]
    s = r.stats["net_margin"]
    assert s.n == 4
    assert s.median == pytest.approx(0.125)
    assert s.q1 == pytest.approx(0.0875)
    assert s.q3 == pytest.approx(0.1625)
    pct = {row.stock_code: row.percentiles["net_margin"] for row in r.rows}
    assert pct["0001"] == pytest.approx(0.0)
    assert pct["0025"] == pytest.approx(100.0)
    assert pct["0011"] == pytest.approx(100 / 3)
    assert r.rows[0].metrics["roe"] == pytest.approx(0.05)
    assert r.rows[0].metrics["equity_multiplier"] == pytest.approx(2.0)


def test_bank_gets_no_revenue_based_metrics(m: Multi) -> None:
    # 1155 is a money-centre bank; a stray "revenue" fact must not produce margins.
    m.company("1155", "Financials", {2023: {
        **general(5000, 1000, assets=100_000, equity=10_000),
        "is.profit_before_tax": 1300, "is.finance_costs": -2000,
    }})

    row = compare_sector(m.s, "Financials").rows[0]

    assert row.profile == "bank"
    for key in ("revenue", "net_margin", "operating_margin", "asset_turnover",
                "net_debt_ebitda", "fcf_margin"):
        assert row.metrics[key] is None
        assert key in row.not_applicable
    assert row.metrics["roe"] == pytest.approx(0.10)
    assert row.metrics["roa"] == pytest.approx(0.01)
    assert row.metrics["equity_multiplier"] == pytest.approx(10.0)


def test_outlier_flagged_kept_and_excluded_from_median(m: Multi) -> None:
    for code, pat in (("0001", 100), ("0011", 120), ("0024", 140)):
        m.company(code, "Industrials", {2023: general(1000, pat)})
    m.company("0025", "Industrials", {2023: general(1000, 50_000)})  # 5000% margin: scale slip

    r = compare_sector(m.s, "Industrials")

    bad = next(row for row in r.rows if row.stock_code == "0025")
    assert bad.metrics["net_margin"] == pytest.approx(50.0)  # kept on the row
    assert "net_margin" in bad.flags and "roe" in bad.flags
    assert bad.percentiles["net_margin"] is None
    assert r.stats["net_margin"].median == pytest.approx(0.12)
    assert r.stats["net_margin"].n_flagged == 1
    assert any("flagged" in n for n in r.notes)


def test_fy_mismatch_noted_and_cagr_uses_snapshot_year(m: Multi) -> None:
    m.company("0001", "Industrials", {fy: general(rev, 100) for fy, rev in
                                      {2020: 100, 2021: 110, 2022: 121, 2023: 133.1}.items()})
    m.company("0011", "Industrials", {2022: general(1000, 100)})

    r = compare_sector(m.s, "Industrials")

    assert r.fiscal_years == {2022: 1, 2023: 1}
    assert any("differ" in n for n in r.notes)
    assert r.rows[0].metrics["revenue_cagr_3y"] == pytest.approx(0.10)
    assert r.rows[0].metrics["earnings_cagr_3y"] == pytest.approx(0.0)

    fixed = compare_sector(m.s, "Industrials", fiscal_year=2023)
    assert [row.stock_code for row in fixed.rows] == ["0001"]
    assert fixed.missing == ["0011"]


def test_compare_peers_explicit_and_sector_default(m: Multi) -> None:
    m.company("0001", "Industrials", {2023: general(1000, 100)})
    m.company("0011", "Industrials", {2023: general(1000, 200)})
    m.company("0012", None, {2023: general(1000, 300)})  # NULL sector -> falls back to map

    default = compare_peers(m.s, "0001")
    assert [r.stock_code for r in default.rows] == ["0001", "0011"]

    explicit = compare_peers(m.s, "0001", ["0012", "9998"])
    assert [r.stock_code for r in explicit.rows] == ["0001", "0012"]
    assert explicit.rows[1].sector == "Consumer Staples"
    assert any("9998" in n for n in explicit.notes)

    with pytest.raises(LookupError):
        compare_peers(m.s, "0000")

    assert ("Industrials", 2, 2) in list_sectors(m.s)


def test_latest_fy_skips_interim_mislabelled_as_fy(m: Multi) -> None:
    c = m.company("0001", "Industrials", {2024: general(1000, 100)})
    m.company("0011", "Industrials", {2024: general(1000, 100)})
    # A half-year YTD stored as "FY2025" ending 2025-06-30.
    d = m.s.query(Document).filter_by(company_id=c.id).one()
    run = m.s.query(ExtractionRun).filter_by(document_id=d.id).one()
    p = Period(company_id=c.id, period_start=date(2025, 1, 1), period_end=date(2025, 6, 30),
               period_type=PeriodType.FY, fiscal_year=2025)
    m.s.add(p)
    m.s.flush()
    m.s.add(Fact(company_id=c.id, concept_key="is.pat_owners", period_id=p.id, value=Decimal(40),
                 basis=Basis.CONSOLIDATED, continuity=Continuity.TOTAL,
                 reported_in_document_id=d.id, run_id=run.id, confidence=1.0))
    m.s.flush()

    r = compare_sector(m.s, "Industrials")

    assert r.rows[0].fiscal_year == 2024
    assert any("skipped FY2025" in n for n in r.notes)


def test_patami_inconsistent_with_group_profit_is_flagged(m: Multi) -> None:
    m.company("0001", "Industrials", {2023: {**general(1000, 0.2), "is.profit_for_period": 100}})
    m.company("0011", "Industrials", {2023: general(1000, 100)})

    r = compare_sector(m.s, "Industrials")

    bad = r.rows[0]
    assert {"patami", "net_margin", "roa"} <= bad.flags.keys()
    assert r.stats["net_margin"].n == 1


def test_percentile_rank_ties_and_singletons() -> None:
    assert percentile_rank(1.0, [1.0]) is None
    assert percentile_rank(2.0, [1.0, 2.0, 2.0, 3.0]) == pytest.approx(50.0)
