"""Dividend history endpoint (facts only; yield with ?prices=true)."""

from dataclasses import asdict

from fastapi import APIRouter, HTTPException

from bursa.db.session import session_scope

router = APIRouter()


@router.get("/{stock_code}")
def dividend_history(stock_code: str, prices: bool = False):
    from bursa.analysis.dividends import compute_dividends, get_company

    with session_scope() as session:
        try:
            company = get_company(session, stock_code)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        result = asdict(compute_dividends(session, company, prices=prices))
        session.rollback()  # read-only
    return result
