"""Fetch financial figures from yfinance and cache locally."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from bursa.db.models import BenchmarkCache

log = logging.getLogger(__name__)

YFINANCE_FIELDS: dict[str, str] = {
    "Total Revenue": "is.revenue",
    "Net Income": "is.pat_owners",
    "Pretax Income": "is.profit_before_tax",
    "Total Assets": "bs.total_assets",
    "Stockholders Equity": "bs.equity_owners",
    "Net Interest Income": "is.net_interest_income",
}

_IS_FIELDS = {"Total Revenue", "Net Income", "Pretax Income", "Net Interest Income"}
_BS_FIELDS = {"Total Assets", "Stockholders Equity"}


@dataclass(frozen=True)
class YfinanceFigure:
    concept_key: str
    period_end: date
    value: Decimal
    field_name: str


def _to_ticker(stock_code: str) -> str:
    return f"{stock_code}.KL"


def fetch_yfinance(
    session: Session,
    stock_code: str,
    *,
    refresh: bool = False,
    delay: float = 1.0,
) -> list[YfinanceFigure]:
    """Fetch financials for a Bursa stock code, returning cached data when available."""
    ticker = _to_ticker(stock_code)

    if not refresh:
        cached = _load_cache(session, ticker)
        if cached:
            return cached

    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance not installed — run: uv pip install yfinance")
        return []

    log.info("fetching yfinance data for %s", ticker)
    try:
        t = yf.Ticker(ticker)
        is_df = t.financials
        bs_df = t.balance_sheet
    except Exception:
        log.warning("yfinance fetch failed for %s", ticker, exc_info=True)
        return []

    figures: list[YfinanceFigure] = []

    for df, fields in [(is_df, _IS_FIELDS), (bs_df, _BS_FIELDS)]:
        if df is None or df.empty:
            continue
        for field_name in fields:
            concept_key = YFINANCE_FIELDS[field_name]
            if field_name not in df.index:
                continue
            row = df.loc[field_name]
            for col, val in row.items():
                if val is None or (hasattr(val, "__float__") and val != val):
                    continue
                period = col.date() if hasattr(col, "date") else col
                figures.append(
                    YfinanceFigure(
                        concept_key=concept_key,
                        period_end=period,
                        value=Decimal(str(float(val))),
                        field_name=field_name,
                    )
                )

    if figures:
        _save_cache(session, ticker, figures)

    if delay > 0:
        time.sleep(delay)

    return figures


def _load_cache(session: Session, ticker: str) -> list[YfinanceFigure]:
    rows = session.execute(
        select(BenchmarkCache).where(BenchmarkCache.ticker == ticker)
    ).scalars().all()
    if not rows:
        return []
    return [
        YfinanceFigure(
            concept_key=YFINANCE_FIELDS[r.field_name],
            period_end=r.period_end,
            value=r.value,
            field_name=r.field_name,
        )
        for r in rows
        if r.field_name in YFINANCE_FIELDS
    ]


def _save_cache(
    session: Session, ticker: str, figures: list[YfinanceFigure]
) -> None:
    session.execute(delete(BenchmarkCache).where(BenchmarkCache.ticker == ticker))
    session.flush()
    for fig in figures:
        session.add(
            BenchmarkCache(
                ticker=ticker,
                field_name=fig.field_name,
                period_end=fig.period_end,
                value=fig.value,
                fetched_at=datetime.now(),
            )
        )
    session.flush()
