"""Peer comparison and sector analysis.

One metric snapshot per company for a single fiscal year (its latest, or a
chosen one), then sector statistics (median / quartiles) and each company's
percentile rank per metric.

Two guards keep the comparison honest:

* **Sector applicability** - a bank has no "revenue" in the trading-company
  sense and its borrowings are its raw material, so revenue-based ratios,
  net debt/EBITDA and FCF margin are reported as None (listed under
  ``not_applicable``) rather than as nonsense numbers. Profiles come from
  ``bursa.benchmark.sectors``.
* **Outliers** - upstream extraction errors exist (a scale slip turns a 10%
  margin into 1000%). Values outside a plausibility band are kept on the row
  but flagged, and excluded from the sector statistics and percentile ranks,
  so one bad fact cannot move a median.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from bursa.analysis.dupont import ebit, profit_and_equity
from bursa.analysis.facts import AnnualFacts, load_annual_facts
from bursa.analysis.growth import make_metric
from bursa.benchmark.sectors import (
    _BANK_INDUSTRIES,
    _INSURANCE_INDUSTRIES,
    _REIT_INDUSTRIES,
    get_industry,
    get_sector,
)
from bursa.db.models import Company, Fact
from bursa.valuation.metrics import compute_valuation

# Display order. Ratios are fractions (0.12 = 12%) except where noted.
METRICS: tuple[str, ...] = (
    "revenue",            # RM
    "patami",             # RM
    "roe",
    "roa",
    "net_margin",
    "operating_margin",
    "asset_turnover",     # x
    "equity_multiplier",  # x
    "revenue_cagr_3y",
    "earnings_cagr_3y",
    "net_debt_ebitda",    # x
    "fcf_margin",
)

PERCENT_METRICS = frozenset({
    "roe", "roa", "net_margin", "operating_margin",
    "revenue_cagr_3y", "earnings_cagr_3y", "fcf_margin",
})
MONEY_METRICS = frozenset({"revenue", "patami"})

# Plausibility bands (inclusive). Outside -> flagged, excluded from stats.
OUTLIER_BOUNDS: dict[str, tuple[float, float]] = {
    "roe": (-2.0, 2.0),
    "roa": (-1.0, 1.0),
    "net_margin": (-1.0, 1.0),
    "operating_margin": (-1.0, 1.0),
    "asset_turnover": (0.0, 10.0),
    "equity_multiplier": (0.0, 50.0),
    "revenue_cagr_3y": (-0.9, 3.0),
    "earnings_cagr_3y": (-0.9, 5.0),
    "net_debt_ebitda": (-20.0, 20.0),
    "fcf_margin": (-2.0, 2.0),
}

MIN_FULL_YEAR_DAYS = 330

# PATAMI / profit-for-the-period outside this band (or of opposite sign)
# means one of the two facts is wrong - minorities rarely take > 80% or
# give back > 100% of group profit.
PATAMI_TO_PAT_BAND = (0.2, 2.0)
_PATAMI_DERIVED = ("patami", "roa", "net_margin", "earnings_cagr_3y")

_REVENUE_BASED = frozenset({
    "revenue", "net_margin", "operating_margin", "asset_turnover",
    "revenue_cagr_3y", "fcf_margin",
})
_LEVERAGE_CASHFLOW = frozenset({"net_debt_ebitda", "fcf_margin"})

INAPPLICABLE: dict[str, frozenset[str]] = {
    "general": frozenset(),
    "reit": frozenset(),
    # Interest income is the top line and deposits/borrowings are the business.
    "bank": _REVENUE_BASED | _LEVERAGE_CASHFLOW,
    # Premiums are a top line, but net debt and FCF are not how insurers work.
    "insurance": _LEVERAGE_CASHFLOW,
}


def profile_of(stock_code: str) -> str:
    industry = get_industry(stock_code)
    if industry in _BANK_INDUSTRIES:
        return "bank"
    if industry in _INSURANCE_INDUSTRIES:
        return "insurance"
    if industry in _REIT_INDUSTRIES:
        return "reit"
    return "general"


def sector_of(company: Company) -> str | None:
    return company.sector or get_sector(company.stock_code)


@dataclass
class CompanySnapshot:
    stock_code: str
    name: str
    sector: str | None
    industry: str | None
    profile: str
    fiscal_year: int | None
    period_end: date | None
    metrics: dict[str, float | None] = field(default_factory=dict)
    not_applicable: list[str] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)
    percentiles: dict[str, float | None] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class MetricStats:
    metric: str
    n: int
    n_flagged: int
    median: float | None
    q1: float | None
    q3: float | None
    min: float | None
    max: float | None


@dataclass
class PeerComparison:
    title: str
    sector: str | None
    requested_fy: int | None
    rows: list[CompanySnapshot] = field(default_factory=list)
    stats: dict[str, MetricStats] = field(default_factory=dict)
    fiscal_years: dict[int, int] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- snapshot


def _div(a: float | None, b: float | None, *, positive_denominator: bool = True) -> float | None:
    if a is None or b is None or b == 0:
        return None
    if positive_denominator and b < 0:
        return None
    return a / b


def _short_year(annual: dict[int, AnnualFacts], fy: int) -> int | None:
    """Days since the prior FY's year end, when too short to be a full year.

    Interim YTD figures mislabelled as FY upstream show up as a "year" ending
    3-9 months (or 0 days) after the previous one; comparing them with full
    years would halve revenue and distort every flow ratio.
    """
    prev = annual.get(fy - 1)
    if prev is None:
        return None
    days = (annual[fy].period_end - prev.period_end).days
    return days if days < MIN_FULL_YEAR_DAYS else None


def _pick_fy(
    annual: dict[int, AnnualFacts], fiscal_year: int | None, warnings: list[str],
) -> int | None:
    if fiscal_year is not None:
        return fiscal_year if fiscal_year in annual else None
    # Latest full year with an actual profit figure - a year holding only an
    # interim balance sheet, a stray comparative or a YTD figure is not a
    # "latest FY".
    for fy in sorted(annual, reverse=True):
        if annual[fy].get("is.pat_owners", "is.profit_for_period") is None:
            continue
        days = _short_year(annual, fy)
        if days is not None:
            warnings.append(
                f"skipped FY{fy} (ends {days} days after FY{fy - 1}; likely interim)"
            )
            continue
        return fy
    return max(annual) if annual else None


def _cagr_3y(annual: dict[int, AnnualFacts], fy: int, *keys: str) -> float | None:
    series = {
        y: float(v) for y, f in annual.items()
        if y <= fy and (v := f.get(*keys)) is not None
    }
    if fy not in series:
        return None
    m = make_metric("", series, 3)
    return m.cagr if m is not None else None


def snapshot(
    session: Session, company: Company, fiscal_year: int | None = None,
) -> CompanySnapshot:
    profile = profile_of(company.stock_code)
    snap = CompanySnapshot(
        stock_code=company.stock_code, name=company.name,
        sector=sector_of(company), industry=get_industry(company.stock_code),
        profile=profile, fiscal_year=None, period_end=None,
        metrics={k: None for k in METRICS},
        not_applicable=[k for k in METRICS if k in INAPPLICABLE[profile]],
    )
    annual = load_annual_facts(session, company)
    fy = _pick_fy(annual, fiscal_year, snap.warnings)
    if fy is None:
        return snap
    f = annual[fy]
    snap.fiscal_year, snap.period_end = fy, f.period_end
    if fiscal_year is not None and (days := _short_year(annual, fy)) is not None:
        snap.warnings.append(f"FY{fy} ends {days} days after FY{fy - 1}; may be interim")

    def fget(*keys: str) -> float | None:
        v = f.get(*keys)
        return float(v) if v is not None else None

    revenue = fget("is.revenue")
    patami = fget("is.pat_owners", "is.profit_for_period")
    total_assets = fget("bs.total_assets")
    pair = profit_and_equity(f)
    pos_rev = revenue if revenue is not None and revenue > 0 else None

    m = snap.metrics
    m["revenue"] = revenue
    m["patami"] = patami
    if pair is not None:
        pat, equity = pair
        m["roe"] = pat / equity
        m["equity_multiplier"] = _div(total_assets, equity)
    m["roa"] = _div(patami, total_assets)
    m["net_margin"] = _div(patami, pos_rev)
    m["operating_margin"] = _div(ebit(f), pos_rev)
    m["asset_turnover"] = _div(pos_rev, total_assets)
    m["revenue_cagr_3y"] = _cagr_3y(annual, fy, "is.revenue")
    m["earnings_cagr_3y"] = _cagr_3y(annual, fy, "is.pat_owners", "is.profit_for_period")

    year = next((y for y in compute_valuation(session, company).years if y.fiscal_year == fy), None)
    if year is not None:
        if year.net_debt is not None and year.ebitda is not None and year.ebitda > 0:
            m["net_debt_ebitda"] = float(year.net_debt) / float(year.ebitda)
        if year.fcfe is not None:
            m["fcf_margin"] = _div(float(year.fcfe), pos_rev)

    for key in snap.not_applicable:
        m[key] = None
    for key, (lo, hi) in OUTLIER_BOUNDS.items():
        v = m.get(key)
        if v is not None and not lo <= v <= hi:
            snap.flags[key] = f"outside plausible range [{lo:g}, {hi:g}]"

    owners, group = f.get("is.pat_owners"), f.get("is.profit_for_period")
    if owners is not None and group is not None and group != 0:
        lo, hi = PATAMI_TO_PAT_BAND
        if not lo <= float(owners) / float(group) <= hi:
            # ROE too, when profit_and_equity paired it with owners' equity.
            owners_roe = ("roe",) if f.get("bs.equity_owners") is not None else ()
            for key in (*_PATAMI_DERIVED, *owners_roe):
                if m.get(key) is not None:
                    snap.flags.setdefault(
                        key, f"PATAMI {float(owners):,.0f} inconsistent with "
                             f"profit for the period {float(group):,.0f}",
                    )
    return snap


# ---------------------------------------------------------------- statistics


def _clean_values(rows: list[CompanySnapshot], metric: str) -> list[float]:
    return [
        r.metrics[metric] for r in rows
        if r.metrics.get(metric) is not None and metric not in r.flags
    ]


def metric_stats(rows: list[CompanySnapshot], metric: str) -> MetricStats:
    values = sorted(_clean_values(rows, metric))
    n_flagged = sum(1 for r in rows if metric in r.flags)
    if not values:
        return MetricStats(metric, 0, n_flagged, None, None, None, None, None)
    if len(values) == 1:
        q1 = q3 = values[0]
    else:
        q1, _, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return MetricStats(
        metric=metric, n=len(values), n_flagged=n_flagged,
        median=statistics.median(values), q1=q1, q3=q3,
        min=values[0], max=values[-1],
    )


def percentile_rank(value: float, population: list[float]) -> float | None:
    """0 = lowest, 100 = highest; ties share the midpoint. None if no peers."""
    n = len(population)
    if n < 2:
        return None
    below = sum(1 for v in population if v < value)
    equal = sum(1 for v in population if v == value)
    return 100.0 * (below + 0.5 * (equal - 1)) / (n - 1)


def _build(
    title: str, sector: str | None, rows: list[CompanySnapshot], requested_fy: int | None,
) -> PeerComparison:
    result = PeerComparison(title=title, sector=sector, requested_fy=requested_fy)
    for r in rows:
        if r.fiscal_year is None:
            result.missing.append(r.stock_code)
        else:
            result.rows.append(r)
    rows = result.rows

    for metric in METRICS:
        result.stats[metric] = metric_stats(rows, metric)
        population = _clean_values(rows, metric)
        for r in rows:
            v = r.metrics.get(metric)
            r.percentiles[metric] = (
                percentile_rank(v, population)
                if v is not None and metric not in r.flags else None
            )

    result.fiscal_years = dict(sorted(Counter(r.fiscal_year for r in rows).items()))
    if len(result.fiscal_years) > 1:
        mix = ", ".join(
            f"FY{fy}: {n}" for fy, n in sorted(result.fiscal_years.items(), reverse=True)
        )
        result.notes.append(f"Companies' latest fiscal years differ ({mix}).")
    month_ends = {r.period_end.month for r in rows if r.period_end is not None}
    if len(month_ends) > 1:
        result.notes.append("Financial year-end months differ across companies.")
    if result.missing:
        what = f"FY{requested_fy}" if requested_fy else "annual facts"
        result.notes.append(f"No {what} for: {', '.join(result.missing)}.")
    for r in rows:
        for w in r.warnings:
            result.notes.append(f"{r.stock_code}: {w}.")
    flagged = sum(len(r.flags) for r in rows)
    if flagged:
        result.notes.append(
            f"{flagged} value(s) flagged as implausible and excluded from statistics."
        )
    profiles = Counter(r.profile for r in rows)
    if len(profiles) > 1:
        result.notes.append(
            "Mixed business models (" + ", ".join(f"{p}: {n}" for p, n in sorted(profiles.items()))
            + "); inapplicable metrics are blank."
        )
    return result


# ---------------------------------------------------------------- queries


def companies_with_facts(session: Session) -> list[Company]:
    has_facts = select(Fact.company_id).distinct()
    return list(session.execute(
        select(Company).where(Company.id.in_(has_facts)).order_by(Company.stock_code)
    ).scalars())


def list_sectors(session: Session) -> list[tuple[str, int, int]]:
    """(sector, companies, companies with facts), most-covered first."""
    total: Counter[str] = Counter()
    for company in session.execute(select(Company)).scalars():
        total[sector_of(company) or "(unclassified)"] += 1
    covered: Counter[str] = Counter(
        sector_of(c) or "(unclassified)" for c in companies_with_facts(session)
    )
    return sorted(
        ((s, total[s], covered[s]) for s in total),
        key=lambda t: (-t[2], -t[1], t[0]),
    )


def compare_sector(
    session: Session, sector: str, fiscal_year: int | None = None,
) -> PeerComparison:
    wanted = sector.strip().casefold()
    members = [
        c for c in companies_with_facts(session)
        if (sector_of(c) or "").casefold() == wanted
    ]
    canonical = sector_of(members[0]) if members else sector
    rows = [snapshot(session, c, fiscal_year) for c in members]
    return _build(f"Sector: {canonical}", canonical, rows, fiscal_year)


def compare_peers(
    session: Session,
    stock_code: str,
    peer_codes: list[str] | None = None,
    fiscal_year: int | None = None,
) -> PeerComparison:
    """``stock_code`` against ``peer_codes`` (default: its sector peers with facts)."""
    target = session.execute(
        select(Company).where(Company.stock_code == stock_code)
    ).scalar_one_or_none()
    if target is None:
        raise LookupError(f"No company {stock_code}")
    sector = sector_of(target)

    if peer_codes:
        codes = [c for c in dict.fromkeys(peer_codes) if c != stock_code]
        found = {
            c.stock_code: c for c in session.execute(
                select(Company).where(Company.stock_code.in_(codes))
            ).scalars()
        }
        unknown = [c for c in codes if c not in found]
        peers = [found[c] for c in codes if c in found]
    else:
        unknown = []
        peers = [
            c for c in companies_with_facts(session)
            if c.id != target.id and sector is not None and sector_of(c) == sector
        ]

    rows = [snapshot(session, c, fiscal_year) for c in [target, *peers]]
    result = _build(f"{target.stock_code} {target.name} vs peers", sector, rows, fiscal_year)
    if unknown:
        result.notes.append(f"Unknown stock codes ignored: {', '.join(unknown)}.")
    return result
