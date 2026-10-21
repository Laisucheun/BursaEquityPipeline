"""Results feed: month-grouped results, digests, filing deadlines, and per-company reports."""

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query

from bursa.db.session import session_scope

router = APIRouter()


@router.get("")
def results_feed(months: int = Query(12, ge=1, le=60), days: int = Query(45, ge=1, le=180)):
    from bursa.analysis.feed import build_feed

    with session_scope() as session:
        result = asdict(build_feed(session, months=months, days=days))
        session.rollback()  # read-only
    return result


@router.get("/report/{stock_code}")
def results_report(stock_code: str, fy: int | None = None):
    from bursa.analysis.dividends import get_company
    from bursa.analysis.feed import results_report as build

    with session_scope() as session:
        try:
            result = asdict(build(session, get_company(session, stock_code), fy))
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        session.rollback()
    return result
