"""Share price integration via yfinance.

Market-based ratios at each fiscal year-end close:
  P/E            = Price / EPS                      (EPS printed in sen)
  P/B            = Market Cap / Owners' Equity      (fallback: Price / NTA per share)
  EV/EBITDA      = (Market Cap + Net Debt) / EBITDA
  Dividend yield = Dividends Paid / Market Cap

Weighted-average share count is rarely on the face of the income statement,
so shares are implied from PATAMI / EPS when not extracted directly.

Bursa Malaysia tickers use the `.KL` suffix on Yahoo Finance.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy.orm import Session

from bursa.analysis.facts import AnnualFacts, load_annual_facts
from bursa.db.models import Company
from bursa.valuation.metrics import compute_valuation

log = logging.getLogger(__name__)

# A year-end that falls on a weekend/holiday uses the last close before it,
# but never one older than this.
MAX_PRICE_STALENESS = timedelta(days=10)

Closes = list[tuple[date, float]]
PriceFetcher = Callable[[str, date, date], Closes]


@dataclass
class YearRatios:
    fiscal_year: int
    period_end: date
    price: float | None = None
    shares: float | None = None
    market_cap: float | None = None
    eps: float | None = None
    book_value: float | None = None
    ebitda: float | None = None
    net_debt: float | None = None
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    ev_ebitda: float | None = None
    dividend_yield: float | None = None


@dataclass
class PriceAnalysis:
    stock_code: str
    name: str
    ticker: str
    current_price: float | None = None
    years: list[YearRatios] = field(default_factory=list)


def yahoo_ticker(stock_code: str) -> str:
    return f"{stock_code}.KL"


def fetch_closes(ticker: str, start: date, end: date) -> Closes:
    """Daily closes in [start, end]; empty on any yfinance/network failure."""
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed - pip install 'bursa-pipeline[yfinance]'")
        return []
    try:
        hist = yf.Ticker(ticker).history(start=str(start), end=str(end + timedelta(days=1)))
    except Exception as exc:  # yfinance raises a zoo of network/parse errors
        log.warning("price fetch failed for %s: %s", ticker, exc)
        return []
    return [(ts.date(), float(close)) for ts, close in hist["Close"].items()]


def close_on_or_before(closes: Closes, target: date) -> float | None:
    eligible = [(d, c) for d, c in closes if target - MAX_PRICE_STALENESS <= d <= target]
    return max(eligible)[1] if eligible else None


def year_ratios(
    f: AnnualFacts, price: float | None, ebitda: float | None, net_debt: float | None,
) -> YearRatios:
    yr = YearRatios(fiscal_year=f.fiscal_year, period_end=f.period_end, price=price,
                    ebitda=ebitda, net_debt=net_debt)

    eps_sen = f.get("is.eps_basic")
    if eps_sen is not None:
        yr.eps = float(eps_sen) / 100

    shares = f.get("is.weighted_avg_shares")
    patami = f.get("is.pat_owners")
    if shares is not None and shares > 0:
        yr.shares = float(shares)
    elif patami is not None and yr.eps:
        implied = float(patami) / yr.eps
        yr.shares = implied if implied > 0 else None

    equity = f.get("bs.equity_owners")
    if equity is not None:
        yr.book_value = float(equity)

    if price is None:
        return yr

    if yr.shares is not None:
        yr.market_cap = price * yr.shares

    if yr.eps:
        yr.pe_ratio = price / yr.eps

    nta = f.get("bs.nta_per_share")
    if yr.market_cap is not None and yr.book_value is not None and yr.book_value > 0:
        yr.pb_ratio = yr.market_cap / yr.book_value
    elif nta is not None and nta > 0:
        yr.pb_ratio = price / float(nta)

    if yr.market_cap is not None and ebitda is not None and ebitda > 0:
        yr.ev_ebitda = (yr.market_cap + (net_debt or 0.0)) / ebitda

    dividends = f.get("cf.dividends_paid")
    if dividends is not None and yr.market_cap:
        yr.dividend_yield = abs(float(dividends)) / yr.market_cap

    return yr


def compute_price_ratios(
    session: Session, company: Company, fetch: PriceFetcher = fetch_closes,
) -> PriceAnalysis:
    ticker = yahoo_ticker(company.stock_code)
    result = PriceAnalysis(stock_code=company.stock_code, name=company.name, ticker=ticker)

    annual = load_annual_facts(session, company)
    if not annual:
        return result

    today = date.today()
    first_end = min(f.period_end for f in annual.values())
    closes = fetch(ticker, first_end - MAX_PRICE_STALENESS, today)
    if closes:
        result.current_price = max(closes)[1]

    valuation = {m.fiscal_year: m for m in compute_valuation(session, company).years}

    for fy, f in annual.items():
        m = valuation.get(fy)
        ebitda = float(m.ebitda) if m is not None and m.ebitda is not None else None
        net_debt = float(m.net_debt) if m is not None and m.net_debt is not None else None
        price = close_on_or_before(closes, f.period_end)
        result.years.append(year_ratios(f, price, ebitda, net_debt))

    return result
