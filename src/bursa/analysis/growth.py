"""Growth rate computation from multi-year facts.

Computes:
  Revenue CAGR (3-year and 5-year)
  Earnings growth (PAT CAGR)
  Asset growth (total assets CAGR)
  ROE trend (per-year)

A CAGR is only reported when both endpoints exist for exactly that span -
a "5Y" figure quietly computed over 3 years is worse than none.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from bursa.analysis.dupont import profit_and_equity
from bursa.analysis.facts import load_annual_facts
from bursa.db.models import Company


@dataclass
class GrowthMetric:
    label: str
    years: int
    start_year: int
    end_year: int
    start_value: float
    end_value: float
    cagr: float


@dataclass
class YearROE:
    fiscal_year: int
    roe: float


@dataclass
class GrowthAnalysis:
    stock_code: str
    name: str
    revenue_cagr_3y: GrowthMetric | None = None
    revenue_cagr_5y: GrowthMetric | None = None
    earnings_cagr_3y: GrowthMetric | None = None
    earnings_cagr_5y: GrowthMetric | None = None
    asset_cagr_3y: GrowthMetric | None = None
    asset_cagr_5y: GrowthMetric | None = None
    roe_trend: list[YearROE] = field(default_factory=list)


def cagr(start: float, end: float, years: int) -> float | None:
    if start <= 0 or end <= 0 or years <= 0:
        return None
    return (end / start) ** (1.0 / years) - 1.0


def make_metric(label: str, series: dict[int, float], span: int) -> GrowthMetric | None:
    if not series:
        return None
    end_fy = max(series)
    start_fy = end_fy - span
    if start_fy not in series:
        return None
    rate = cagr(series[start_fy], series[end_fy], span)
    if rate is None:
        return None
    return GrowthMetric(
        label=label, years=span, start_year=start_fy, end_year=end_fy,
        start_value=series[start_fy], end_value=series[end_fy], cagr=rate,
    )


def compute_growth(session: Session, company: Company) -> GrowthAnalysis:
    result = GrowthAnalysis(stock_code=company.stock_code, name=company.name)

    revenue: dict[int, float] = {}
    earnings: dict[int, float] = {}
    assets: dict[int, float] = {}

    for fy, f in load_annual_facts(session, company).items():
        rev = f.get("is.revenue")
        if rev is not None:
            revenue[fy] = float(rev)
        pat = f.get("is.pat_owners", "is.profit_for_period")
        if pat is not None:
            earnings[fy] = float(pat)
        ta = f.get("bs.total_assets")
        if ta is not None:
            assets[fy] = float(ta)
        pair = profit_and_equity(f)
        if pair is not None:
            result.roe_trend.append(YearROE(fiscal_year=fy, roe=pair[0] / pair[1]))

    result.revenue_cagr_3y = make_metric("Revenue", revenue, 3)
    result.revenue_cagr_5y = make_metric("Revenue", revenue, 5)
    result.earnings_cagr_3y = make_metric("Earnings", earnings, 3)
    result.earnings_cagr_5y = make_metric("Earnings", earnings, 5)
    result.asset_cagr_3y = make_metric("Total assets", assets, 3)
    result.asset_cagr_5y = make_metric("Total assets", assets, 5)
    return result
