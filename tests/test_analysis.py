from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from bursa.analysis.dupont import compute_dupont
from bursa.analysis.facts import AnnualFacts, load_annual_facts
from bursa.analysis.growth import compute_growth
from bursa.analysis.prices import close_on_or_before, compute_price_ratios, year_ratios
from bursa.db.enums import Basis, Continuity, DocSource, PeriodType
from bursa.db.models import Company, Document, ExtractionRun, Fact, Period
from bursa.mapping.synonyms import seed_concepts


class Builder:
    def __init__(self, session: Session) -> None:
        self.s = session
        seed_concepts(session)
        self.company = Company(stock_code="9999", name="Test Berhad")
        session.add(self.company)
        session.flush()
        self.docs: dict[str, tuple[Document, ExtractionRun]] = {}
        self.periods: dict[tuple, Period] = {}

    def doc(self, name: str, filed: date) -> tuple[Document, ExtractionRun]:
        if name not in self.docs:
            d = Document(company_id=self.company.id, source=DocSource.UPLOAD,
                         original_filename=name, file_sha256=name, file_size=1,
                         storage_path=name, filed_date=filed)
            self.s.add(d)
            self.s.flush()
            run = ExtractionRun(document_id=d.id)
            self.s.add(run)
            self.s.flush()
            self.docs[name] = (d, run)
        return self.docs[name]

    def period(self, fy: int, end: date, ptype: PeriodType, start: date | None) -> Period:
        key = (start, end, ptype)
        if key not in self.periods:
            p = Period(company_id=self.company.id, period_start=start, period_end=end,
                       period_type=ptype, fiscal_year=fy)
            self.s.add(p)
            self.s.flush()
            self.periods[key] = p
        return self.periods[key]

    def fact(self, concept: str, value: float, fy: int, *, doc: str = "AR", filed: date | None = None,
             ptype: PeriodType | None = None, end: date | None = None,
             continuity: Continuity = Continuity.TOTAL, confidence: float = 1.0) -> None:
        end = end or date(fy, 12, 31)
        if ptype is None:
            ptype = PeriodType.INSTANT if concept.startswith("bs.") else PeriodType.FY
        start = None if ptype == PeriodType.INSTANT else date(end.year, 1, 1)
        d, run = self.doc(doc, filed or date(fy + 1, 4, 30))
        self.s.add(Fact(
            company_id=self.company.id, concept_key=concept,
            period_id=self.period(fy, end, ptype, start).id,
            value=Decimal(str(value)), basis=Basis.CONSOLIDATED, continuity=continuity,
            reported_in_document_id=d.id, run_id=run.id, confidence=confidence,
        ))
        self.s.flush()


@pytest.fixture
def b(session: Session) -> Builder:
    return Builder(session)


def test_loader_ignores_quarters_discontinued_and_low_confidence(b: Builder) -> None:
    b.fact("is.revenue", 1000, 2023)
    b.fact("is.revenue", 250, 2023, ptype=PeriodType.Q1, end=date(2023, 3, 31))
    b.fact("is.revenue", 99, 2023, doc="X", continuity=Continuity.DISCONTINUED)
    b.fact("is.profit_before_tax", 1, 2023, doc="Y", confidence=0.5)

    annual = load_annual_facts(b.s, b.company)

    assert annual[2023].values == {"is.revenue": Decimal(1000)}


def test_loader_prefers_year_end_instant_and_latest_filing(b: Builder) -> None:
    b.fact("bs.total_assets", 500, 2023, end=date(2023, 6, 30))
    b.fact("bs.total_assets", 800, 2023)
    b.fact("is.revenue", 1000, 2023, doc="AR2023", filed=date(2024, 4, 30))
    b.fact("is.revenue", 1100, 2023, doc="AR2024", filed=date(2025, 4, 30))

    f = load_annual_facts(b.s, b.company)[2023]

    assert f.values["bs.total_assets"] == 800
    assert f.values["is.revenue"] == 1100  # restated wins
    assert f.period_end == date(2023, 12, 31)


def test_dupont_factors_multiply_back_to_roe(b: Builder) -> None:
    for k, v in {"is.revenue": 1000, "is.pat_owners": 80, "is.profit_before_tax": 100,
                 "is.finance_costs": -20, "bs.total_assets": 2000, "bs.equity_owners": 800,
                 "is.profit_for_period": 90, "bs.total_equity": 900}.items():
        b.fact(k, v, 2023)

    result = compute_dupont(b.s, b.company)

    d3, d5 = result.three_factor[0], result.five_factor[0]
    assert d3.roe == pytest.approx(80 / 800)  # owners' PAT over owners' equity
    assert d3.net_margin * d3.asset_turnover * d3.equity_multiplier == pytest.approx(d3.roe)
    assert d5.interest_burden == pytest.approx(100 / 120)
    assert (d5.tax_burden * d5.interest_burden * d5.operating_margin
            * d5.asset_turnover * d5.equity_multiplier) == pytest.approx(d5.roe)


def test_cagr_requires_exact_span(b: Builder) -> None:
    for fy, rev in {2020: 100, 2021: 110, 2023: 133.1}.items():
        b.fact("is.revenue", rev, fy)

    g = compute_growth(b.s, b.company)

    assert g.revenue_cagr_3y is not None
    assert g.revenue_cagr_3y.start_year == 2020
    assert g.revenue_cagr_3y.cagr == pytest.approx(0.1)
    assert g.revenue_cagr_5y is None


def test_year_ratios_convert_eps_sen_and_imply_shares() -> None:
    f = AnnualFacts(2023, date(2023, 12, 31), {
        "is.eps_basic": Decimal(20),            # 20 sen
        "is.pat_owners": Decimal(100_000_000),  # -> 500m shares
        "bs.equity_owners": Decimal(1_000_000_000),
        "cf.dividends_paid": Decimal(-50_000_000),
    })

    yr = year_ratios(f, price=4.0, ebitda=200_000_000.0, net_debt=100_000_000.0)

    assert yr.eps == pytest.approx(0.20)
    assert yr.shares == pytest.approx(500_000_000)
    assert yr.market_cap == pytest.approx(2_000_000_000)
    assert yr.pe_ratio == pytest.approx(20.0)
    assert yr.pb_ratio == pytest.approx(2.0)
    assert yr.ev_ebitda == pytest.approx(10.5)
    assert yr.dividend_yield == pytest.approx(0.025)


def test_pb_falls_back_to_nta_per_share_without_shares() -> None:
    f = AnnualFacts(2023, date(2023, 12, 31), {"bs.nta_per_share": Decimal("2.50")})
    yr = year_ratios(f, price=5.0, ebitda=None, net_debt=None)
    assert yr.market_cap is None
    assert yr.pb_ratio == pytest.approx(2.0)


def test_close_on_or_before_never_uses_a_later_or_stale_price() -> None:
    closes = [(date(2023, 12, 20), 1.0), (date(2023, 12, 29), 2.0), (date(2024, 1, 2), 3.0)]
    assert close_on_or_before(closes, date(2023, 12, 31)) == 2.0
    assert close_on_or_before(closes, date(2023, 12, 10)) is None


def test_compute_price_ratios_uses_injected_fetcher(b: Builder) -> None:
    b.fact("is.eps_basic", 10, 2023)
    b.fact("is.pat_owners", 1_000_000, 2023)
    calls = []

    def fetch(ticker: str, start: date, end: date):
        calls.append(ticker)
        return [(date(2023, 12, 29), 1.5), (date(2025, 1, 2), 2.0)]

    result = compute_price_ratios(b.s, b.company, fetch=fetch)

    assert calls == ["9999.KL"]
    assert result.current_price == 2.0
    assert result.years[0].price == 1.5
    assert result.years[0].pe_ratio == pytest.approx(15.0)
