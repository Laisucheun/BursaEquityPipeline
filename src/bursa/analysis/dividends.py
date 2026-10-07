"""Dividend history per company: amount, DPS, payout, cover, streak, yield.

Built only on `load_annual_facts` (one value per FY and concept), so the
same consolidated / FY / latest-filing selection rules apply as everywhere
else. Every derived number records where it came from in ``sources`` and
anything suspicious lands in ``flags`` - suspicious values are kept and
flagged, never silently dropped.

Choice of inputs, per fiscal year:

* **Dividends (RM)** - ``cf.dividends_paid`` (cash actually paid to owners
  during the year: typically last year's final + this year's interim).
  Falls back to ``eq.dividends`` from the statement of changes in equity,
  which is read from the total-equity column and may include dividends paid
  by subsidiaries to non-controlling interests - so it is flagged.
* **Shares** - ``is.weighted_avg_shares`` (the EPS denominator); otherwise
  implied as PATAMI / (EPS / 100), flagged as implied.
* **DPS (sen)** - dividends / shares * 100. For a REIT the reported
  ``is.dpu`` wins when present.
* **Payout** - dividends / PATAMI (REIT with DPU only: DPU / EPS);
  **cover** is its inverse. Payout above ``PAYOUT_FLAG`` is flagged.
* **Streak** - consecutive fiscal years (no gaps) with a dividend, ending at
  the most recent year that has any dividend information.
* **Yield** - DPS / FY-end close, only when prices are requested (offline by
  default; the fetcher is injectable for tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from bursa.analysis.facts import AnnualFacts, load_annual_facts
from bursa.db.models import Company, Fact

# Payout above this is almost always an extraction error (scale, wrong row)
# or a one-off special dividend - either way the user should look.
PAYOUT_FLAG = 3.0
# Weighted shares vs PATAMI/EPS disagreeing by more than this points at a
# scale error in one of the three facts.
SHARES_MISMATCH = 0.2
# DPS growth beyond +-90%/+1000% is usually a special dividend or bad data.
DPS_GROWTH_FLAG = (-0.9, 10.0)
# Share count moving by more than this year on year: bonus issue, split or
# consolidation - DPS growth across it compares different share bases.
SHARE_JUMP = 1.3


@dataclass
class YearDividend:
    fiscal_year: int
    period_end: date
    dividends: float | None = None  # RM, positive
    dividends_basis: str | None = None  # "cash" | "equity" | None
    shares: float | None = None
    patami: float | None = None
    eps_sen: float | None = None
    dps_sen: float | None = None
    payout_ratio: float | None = None
    dividend_cover: float | None = None
    dps_growth: float | None = None
    price: float | None = None
    dividend_yield: float | None = None
    paid: bool | None = None  # None = no dividend information this year
    sources: dict[str, str] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


@dataclass
class DividendHistory:
    stock_code: str
    name: str
    is_reit: bool = False
    years: list[YearDividend] = field(default_factory=list)
    streak: int = 0
    streak_end: int | None = None
    longest_streak: int = 0
    median_payout: float | None = None  # over the current streak, unflagged years
    notes: list[str] = field(default_factory=list)


def _f(value) -> float | None:  # type: ignore[no-untyped-def]
    return None if value is None else float(value)


def is_reit(company: Company, annual: dict[int, AnnualFacts] | None = None) -> bool:
    from bursa.analysis.peers import profile_of

    if profile_of(company.stock_code) == "reit":
        return True
    name = company.name.lower()
    if "real estate investment trust" in name or name.endswith(" reit"):
        return True
    return bool(annual) and any(f.get("is.dpu") is not None for f in annual.values())


def _shares(f: AnnualFacts, yd: YearDividend) -> None:
    reported = _f(f.get("is.weighted_avg_shares"))
    implied = None
    if yd.patami is not None and yd.eps_sen:
        implied = yd.patami / (yd.eps_sen / 100)

    if reported is not None and reported <= 0:
        yd.flags.append(f"weighted shares {reported:,.0f} not positive - ignored")
        reported = None
    if implied is not None and implied <= 0:
        implied = None  # loss year with positive EPS or vice versa: unusable

    if reported is not None:
        yd.shares = reported
        yd.sources["shares"] = "is.weighted_avg_shares"
        if implied is not None and abs(implied - reported) / reported > SHARES_MISMATCH:
            yd.flags.append(
                f"PATAMI/EPS implies {implied:,.0f} shares vs {reported:,.0f} reported "
                f"({implied / reported - 1:+.0%}) - check PATAMI/EPS/shares scale"
            )
    elif implied is not None:
        yd.shares = implied
        yd.sources["shares"] = "implied: is.pat_owners / (is.eps_basic / 100)"
        yd.flags.append("shares implied from PATAMI / EPS")


def year_dividend(f: AnnualFacts, *, reit: bool = False) -> YearDividend:
    yd = YearDividend(fiscal_year=f.fiscal_year, period_end=f.period_end)
    yd.patami = _f(f.get("is.pat_owners"))
    if yd.patami is not None:
        yd.sources["patami"] = "is.pat_owners"
    yd.eps_sen = _f(f.get("is.eps_basic"))

    cash = _f(f.get("cf.dividends_paid"))
    equity = _f(f.get("eq.dividends"))
    if cash is not None:
        yd.dividends, yd.dividends_basis = abs(cash), "cash"
        yd.sources["dividends"] = "cf.dividends_paid (cash paid to owners in the year)"
        if cash > 0:
            yd.flags.append("cash-flow dividends reported as an inflow - sign ignored")
    elif equity is not None:
        yd.dividends, yd.dividends_basis = abs(equity), "equity"
        yd.sources["dividends"] = (
            "eq.dividends (statement of changes in equity, total column - may include NCI)"
        )
        yd.flags.append("dividends from equity statement (no cash-flow figure)")

    _shares(f, yd)

    dpu = _f(f.get("is.dpu")) if reit else None
    if dpu is not None:
        yd.dps_sen = dpu
        yd.sources["dps_sen"] = "is.dpu (reported distribution per unit)"
    elif yd.dividends is not None and yd.shares:
        yd.dps_sen = yd.dividends / yd.shares * 100
        yd.sources["dps_sen"] = f"dividends ({yd.dividends_basis}) / shares * 100"

    if yd.dividends is not None or yd.dps_sen is not None:
        yd.paid = bool(yd.dividends) or bool(yd.dps_sen)

    # Payout / cover.
    if yd.dividends is not None and yd.patami is not None:
        if yd.patami > 0:
            yd.payout_ratio = yd.dividends / yd.patami
            yd.sources["payout_ratio"] = "dividends / is.pat_owners"
        elif yd.dividends > 0:
            yd.flags.append("dividend paid in a loss year - payout not meaningful")
    elif dpu is not None and yd.eps_sen and yd.eps_sen > 0:
        yd.payout_ratio = dpu / yd.eps_sen
        yd.sources["payout_ratio"] = "is.dpu / is.eps_basic"
    if (yd.dps_sen is not None and yd.eps_sen is not None and yd.eps_sen > 0
            and yd.dps_sen > PAYOUT_FLAG * yd.eps_sen):
        yd.flags.append(
            f"DPS {yd.dps_sen:,.2f} sen > {PAYOUT_FLAG:.0f}x EPS {yd.eps_sen:,.2f} sen"
            " - implausible"
        )
    if yd.payout_ratio:
        yd.dividend_cover = 1 / yd.payout_ratio
        yd.sources["dividend_cover"] = "1 / payout_ratio"
    if yd.payout_ratio is not None and yd.payout_ratio > PAYOUT_FLAG:
        yd.flags.append(
            f"payout {yd.payout_ratio:.0%} > {PAYOUT_FLAG:.0%} - special dividend or "
            "a scale error in dividends/PATAMI"
        )
    return yd


def _streaks(years: list[YearDividend]) -> tuple[int, int | None, int]:
    """(current streak, its last FY, longest streak)."""
    known = [y for y in years if y.paid is not None]
    if not known:
        return 0, None, 0
    paid_years = {y.fiscal_year for y in known if y.paid}
    longest = run = 0
    prev = None
    for fy in sorted(paid_years):
        run = run + 1 if prev is not None and fy == prev + 1 else 1
        longest = max(longest, run)
        prev = fy
    end = known[-1].fiscal_year
    current = 0
    fy = end
    while fy in paid_years:
        current += 1
        fy -= 1
    return current, end, longest


def build_history(
    company: Company,
    annual: dict[int, AnnualFacts],
    *,
    closes=None,  # type: ignore[no-untyped-def]  # prices.Closes | None
) -> DividendHistory:
    reit = is_reit(company, annual)
    hist = DividendHistory(stock_code=company.stock_code, name=company.name, is_reit=reit)
    prev: YearDividend | None = None
    for fy, f in sorted(annual.items()):
        yd = year_dividend(f, reit=reit)
        consecutive = prev is not None and prev.fiscal_year == fy - 1
        if consecutive and prev is not None and prev.dps_sen and yd.dps_sen is not None:
            yd.dps_growth = yd.dps_sen / prev.dps_sen - 1
            yd.sources["dps_growth"] = f"dps_sen vs FY{prev.fiscal_year}"
            lo, hi = DPS_GROWTH_FLAG
            if not lo <= yd.dps_growth <= hi:
                yd.flags.append(f"DPS growth {yd.dps_growth:+.0%} - special dividend or bad data")
            if prev.shares and yd.shares:
                ratio = yd.shares / prev.shares
                if not 1 / SHARE_JUMP <= ratio <= SHARE_JUMP:
                    yd.flags.append(
                        f"share count x{ratio:.2f} vs FY{prev.fiscal_year} - bonus issue/split? "
                        "DPS growth is not adjusted"
                    )
        if closes is not None:
            from bursa.analysis.prices import close_on_or_before

            yd.price = close_on_or_before(closes, f.period_end)
            if yd.price and yd.dps_sen is not None:
                yd.dividend_yield = yd.dps_sen / 100 / yd.price
                yd.sources["dividend_yield"] = f"dps_sen / 100 / close on or before {f.period_end}"
        hist.years.append(yd)
        prev = yd

    hist.streak, hist.streak_end, hist.longest_streak = _streaks(hist.years)
    if hist.streak_end is not None:
        streak_years = [
            y for y in hist.years
            if hist.streak_end - hist.streak < y.fiscal_year <= hist.streak_end
        ]
        payouts = [
            y.payout_ratio for y in streak_years
            if y.payout_ratio is not None and y.payout_ratio <= PAYOUT_FLAG
        ]
        hist.median_payout = median(payouts) if payouts else None

    if not hist.years:
        hist.notes.append("no annual facts")
    elif hist.streak_end is None:
        hist.notes.append("no dividend facts (cf.dividends_paid / eq.dividends / is.dpu)")
    else:
        latest = hist.years[-1].fiscal_year
        if hist.streak_end < latest:
            hist.notes.append(
                f"no dividend figure for FY{hist.streak_end + 1}-FY{latest}; streak ends at "
                f"FY{hist.streak_end}"
            )
        fys = [y.fiscal_year for y in hist.years]
        gaps = [fy for fy in range(fys[0], fys[-1] + 1) if fy not in set(fys)]
        if gaps:
            hist.notes.append(f"fiscal years with no facts at all: {gaps} (breaks the streak)")
    if any(y.dividends_basis == "cash" for y in hist.years):
        hist.notes.append(
            "cash-flow dividends are paid in the year (prior final + current interim), "
            "not declared for it - DPS lags the declared DPS by up to one payment"
        )
    return hist


def compute_dividends(
    session: Session,
    company: Company,
    *,
    prices: bool = False,
    fetch=None,  # type: ignore[no-untyped-def]  # prices.PriceFetcher | None
) -> DividendHistory:
    annual = load_annual_facts(session, company)
    closes = None
    if prices and annual:
        from bursa.analysis.prices import MAX_PRICE_STALENESS, fetch_closes, yahoo_ticker

        fetcher = fetch or fetch_closes
        first = min(f.period_end for f in annual.values())
        last = max(f.period_end for f in annual.values())
        closes = fetcher(yahoo_ticker(company.stock_code), first - MAX_PRICE_STALENESS, last)
    hist = build_history(company, annual, closes=closes)
    if prices and not closes:
        hist.notes.append("no prices available - yield not computed")
    return hist


def get_company(session: Session, stock_code: str) -> Company:
    company = session.scalar(select(Company).where(Company.stock_code == stock_code))
    if company is None:
        raise LookupError(f"No company {stock_code}")
    return company


def screen_dividends(
    session: Session,
    *,
    min_years: int = 5,
    min_payout: float | None = None,
    max_payout: float | None = None,
) -> list[DividendHistory]:
    """Companies with a current unbroken record of at least ``min_years``.

    Payout bounds apply to the median payout over the streak (unflagged
    years); a company with no computable payout fails any payout bound.
    Sorted by streak, then median payout, both descending.
    """
    ids = select(Fact.company_id).where(
        Fact.concept_key.in_(("cf.dividends_paid", "eq.dividends", "is.dpu"))
    ).distinct()
    companies = session.scalars(select(Company).where(Company.id.in_(ids))).all()
    hits: list[DividendHistory] = []
    for company in companies:
        hist = compute_dividends(session, company)
        if hist.streak < min_years:
            continue
        mp = hist.median_payout
        if min_payout is not None and (mp is None or mp < min_payout):
            continue
        if max_payout is not None and (mp is None or mp > max_payout):
            continue
        hits.append(hist)
    hits.sort(key=lambda h: (h.streak, h.median_payout or 0.0), reverse=True)
    return hits
