"""Results feed: month-grouped results, digests, filing deadlines, and per-company reports."""

import datetime as dt
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query
from pydantic import ValidationError

from bursa.config import get_settings
from bursa.db.session import session_scope

router = APIRouter()


@router.get("")
def results_feed(months: int = Query(12, ge=1, le=60), days: int = Query(45, ge=1, le=180)):
    from bursa.analysis.feed import build_feed

    with session_scope() as session:
        result = asdict(build_feed(session, months=months, days=days))
        session.rollback()  # read-only
    return result


@router.get("/briefs")
def list_briefs():
    """News briefs on file, newest first."""
    from bursa.briefs.schema import brief_dates, brief_path, load_brief

    briefs_dir = get_settings().briefs_dir
    out = []
    for d in brief_dates(briefs_dir):
        try:
            title = load_brief(brief_path(briefs_dir, d)).title
        except (ValidationError, ValueError, OSError):
            title = None
        out.append({"date": d.isoformat(), "title": title, "valid": title is not None})
    return out


@router.get("/brief")
def daily_brief(date: dt.date | None = None):
    """The news brief for a day (latest on file by default, else today) plus its
    filing calendar. ``news`` is null when no valid brief exists for the day."""
    from bursa.analysis.brief import brief_calendar
    from bursa.briefs.schema import brief_dates, brief_path, load_brief, today_myt

    briefs_dir = get_settings().briefs_dir
    dates = brief_dates(briefs_dir)
    day = date or (dates[0] if dates else today_myt())
    news, news_error = None, None
    path = brief_path(briefs_dir, day)
    if path.exists():
        try:
            news = load_brief(path).model_dump(mode="json")
        except (ValidationError, ValueError) as e:
            news_error = f"{path.name} is not a valid brief: {e}"
    older = [d for d in dates if d < day]
    newer = [d for d in dates if d > day]
    with session_scope() as session:
        calendar = asdict(brief_calendar(session, day))
        session.rollback()
    return {
        "date": day.isoformat(),
        "news": news,
        "news_error": news_error,
        "calendar": calendar,
        "previous_date": older[0].isoformat() if older else None,
        "next_date": newer[-1].isoformat() if newer else None,
    }


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
