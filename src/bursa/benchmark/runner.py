"""Orchestrate yfinance benchmark: fetch, compare, store results."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from bursa.benchmark.compare import BenchmarkOutcome, compare_facts
from bursa.benchmark.sectors import get_profile
from bursa.benchmark.yfinance_fetch import fetch_yfinance
from bursa.db.enums import PeriodType
from bursa.db.models import BenchmarkResult, Company, Fact, Period

log = logging.getLogger(__name__)

BENCHMARK_CONCEPTS = frozenset({
    "is.revenue",
    "is.pat_owners",
    "is.profit_before_tax",
    "bs.total_assets",
    "bs.equity_owners",
    "is.net_interest_income",
})

# yfinance returns company-level (not group/consolidated) or outright wrong
# data for these tickers.  We extract group-level — correct for valuation
# (EV/EBITDA, FCFF, FCFE) — so comparing against company-level is noise.
# Each entry: stock_code → reason for exclusion.
YFINANCE_UNRELIABLE: dict[str, str] = {
    # Bad / wrong-entity data from yfinance (50-400x off)
    "0086": "yfinance returns wrong entity data (~50-150x off all concepts)",
    "5265": "yfinance returns wrong entity data (100-400x off)",
    "5169": "yfinance returns wrong entity data (extreme ratios, negative PBT)",
    # yfinance reports company-level, not group/consolidated
    "5100": "yfinance has company-level data (5-28x off group figures)",
    "5101": "yfinance has company-level data (2-3x BS, negative IS vs profitable group)",
    "5152": "yfinance has company-level data (~3-8x off group)",
    "5192": "yfinance has company-level data (~3-5x off group)",
    "4065": "yfinance has group incl Wilmar; we extract company-level (0.14x revenue)",
    "5299": "yfinance has company-level data (2-13x off group)",
    "5307": "yfinance has company-level data (varying ratios)",
    "5292": "yfinance has company-level data (REIT, 10-20x off group)",
    "5323": "yfinance has company-level data (0.2-2.7x inconsistent ratios)",
    "1082": "yfinance has subsidiary-level data (~1.5x off, bank holding group)",
    "3182": "yfinance has subsidiary-level data (1.6x equity, sign-flipped profit)",
    "5273": "yfinance has company-level data (~1.8-2x off group profit)",
    "4677": "yfinance has subsidiary-level data (YTL, 1.8x profit, 0.36x equity)",
    "5250": "yfinance has company-level data (7-Eleven, inconsistent ratios 0.27-4x)",
}


@dataclass
class CompanySummary:
    stock_code: str
    name: str
    sector: str | None = None
    match: int = 0
    close: int = 0
    mismatch: int = 0
    scale_error: int = 0
    missing: int = 0
    skipped: int = 0
    outcomes: list[BenchmarkOutcome] | None = None


def benchmark_company(
    session: Session,
    company: Company,
    *,
    refresh: bool = False,
    match_tolerance: float = 0.05,
    close_tolerance: float = 0.15,
) -> CompanySummary:
    """Run benchmark for a single company."""
    _expected, inapplicable = get_profile(company.stock_code)
    summary = CompanySummary(stock_code=company.stock_code, name=company.name)

    from bursa.benchmark.sectors import get_industry
    summary.sector = get_industry(company.stock_code)

    if company.stock_code in YFINANCE_UNRELIABLE:
        reason = YFINANCE_UNRELIABLE[company.stock_code]
        log.info("%s: skipped — %s", company.stock_code, reason)
        session.execute(
            delete(BenchmarkResult).where(
                BenchmarkResult.company_id == company.id,
                BenchmarkResult.source == "yfinance",
            )
        )
        session.flush()
        return summary

    our_facts = _load_company_facts(session, company.id)
    if not our_facts:
        log.info("%s: no FY facts to benchmark", company.stock_code)
        return summary

    yf_figures = fetch_yfinance(session, company.stock_code, refresh=refresh)
    if not yf_figures:
        log.info("%s: no yfinance data available", company.stock_code)
        return summary

    outcomes = compare_facts(
        our_facts, yf_figures,
        match_tolerance=match_tolerance,
        close_tolerance=close_tolerance,
    )

    filtered: list[BenchmarkOutcome] = []
    skipped = 0
    for o in outcomes:
        if o.concept_key in inapplicable:
            skipped += 1
            continue
        filtered.append(o)

    counts: Counter[str] = Counter()
    for o in filtered:
        counts[o.classification] += 1

    summary.match = counts.get("MATCH", 0)
    summary.close = counts.get("CLOSE", 0)
    summary.mismatch = counts.get("MISMATCH", 0)
    summary.scale_error = counts.get("SCALE_ERROR", 0)
    summary.missing = counts.get("MISSING", 0)
    summary.skipped = skipped
    summary.outcomes = filtered

    _save_results(session, company.id, filtered)

    return summary


def _load_company_facts(
    session: Session, company_id: int
) -> dict[tuple[str, int], Decimal]:
    rows = session.execute(
        select(Fact.concept_key, Fact.value, Period.period_end)
        .join(Period, Fact.period_id == Period.id)
        .where(
            Fact.company_id == company_id,
            Period.period_type.in_([PeriodType.FY, PeriodType.INSTANT]),
            Fact.concept_key.in_(BENCHMARK_CONCEPTS),
        )
    ).all()

    facts: dict[tuple[str, int], Decimal] = {}
    for concept_key, value, period_end in rows:
        fy = period_end.year
        key = (concept_key, fy)
        if key not in facts:
            facts[key] = value
    return facts


def _save_results(
    session: Session, company_id: int, outcomes: list[BenchmarkOutcome]
) -> None:
    session.execute(
        delete(BenchmarkResult).where(
            BenchmarkResult.company_id == company_id,
            BenchmarkResult.source == "yfinance",
        )
    )
    session.flush()
    for o in outcomes:
        session.add(
            BenchmarkResult(
                company_id=company_id,
                concept_key=o.concept_key,
                fiscal_year=o.fiscal_year,
                source="yfinance",
                our_value=o.our_value,
                external_value=o.external_value,
                deviation_pct=o.deviation_pct,
                classification=o.classification,
                detail=o.detail,
            )
        )
    session.flush()
