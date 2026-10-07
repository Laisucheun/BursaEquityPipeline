"""DuPont decomposition of Return on Equity.

3-factor:
  ROE = Net Margin × Asset Turnover × Equity Multiplier
      = (PAT/Revenue) × (Revenue/Assets) × (Assets/Equity)

5-factor:
  ROE = Tax Burden × Interest Burden × Operating Margin × Asset Turnover × Equity Multiplier
      = (PAT/PBT) × (PBT/EBIT) × (EBIT/Revenue) × (Revenue/Assets) × (Assets/Equity)

PAT and equity are taken as a matched pair - owners' PAT over owners' equity,
or total PAT over total equity - so the factors multiply back to ROE exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from bursa.analysis.facts import AnnualFacts, load_annual_facts
from bursa.db.models import Company


@dataclass
class DuPont3:
    fiscal_year: int
    roe: float
    net_margin: float
    asset_turnover: float
    equity_multiplier: float


@dataclass
class DuPont5:
    fiscal_year: int
    roe: float
    tax_burden: float
    interest_burden: float
    operating_margin: float
    asset_turnover: float
    equity_multiplier: float


@dataclass
class DuPontAnalysis:
    stock_code: str
    name: str
    three_factor: list[DuPont3] = field(default_factory=list)
    five_factor: list[DuPont5] = field(default_factory=list)


def profit_and_equity(f: AnnualFacts) -> tuple[float, float] | None:
    for pat_key, eq_key in (
        ("is.pat_owners", "bs.equity_owners"),
        ("is.profit_for_period", "bs.total_equity"),
    ):
        pat, eq = f.get(pat_key), f.get(eq_key)
        if pat is not None and eq is not None and eq > 0:
            return float(pat), float(eq)
    return None


def ebit(f: AnnualFacts) -> float | None:
    pbt = f.get("is.profit_before_tax")
    finance_costs = f.get("is.finance_costs")
    if pbt is not None and finance_costs is not None:
        return float(pbt) + abs(float(finance_costs))
    operating_profit = f.get("is.operating_profit")
    return float(operating_profit) if operating_profit is not None else None


def dupont_for_year(f: AnnualFacts) -> tuple[DuPont3 | None, DuPont5 | None]:
    pair = profit_and_equity(f)
    revenue = f.get("is.revenue")
    total_assets = f.get("bs.total_assets")
    if pair is None or revenue is None or total_assets is None:
        return None, None
    if revenue <= 0 or total_assets <= 0:
        return None, None
    pat, equity = pair
    rev, ta = float(revenue), float(total_assets)

    net_margin = pat / rev
    asset_turnover = rev / ta
    equity_multiplier = ta / equity
    roe = net_margin * asset_turnover * equity_multiplier
    three = DuPont3(f.fiscal_year, roe, net_margin, asset_turnover, equity_multiplier)

    pbt = f.get("is.profit_before_tax")
    e = ebit(f)
    if pbt is None or e is None or pbt == 0 or e == 0:
        return three, None
    five = DuPont5(
        fiscal_year=f.fiscal_year,
        roe=roe,
        tax_burden=pat / float(pbt),
        interest_burden=float(pbt) / e,
        operating_margin=e / rev,
        asset_turnover=asset_turnover,
        equity_multiplier=equity_multiplier,
    )
    return three, five


def compute_dupont(session: Session, company: Company) -> DuPontAnalysis:
    result = DuPontAnalysis(stock_code=company.stock_code, name=company.name)
    for f in load_annual_facts(session, company).values():
        three, five = dupont_for_year(f)
        if three is not None:
            result.three_factor.append(three)
        if five is not None:
            result.five_factor.append(five)
    return result
