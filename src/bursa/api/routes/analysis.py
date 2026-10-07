"""DuPont, growth, and market-price ratio endpoints."""

from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from bursa.db.models import Company
from bursa.db.session import session_scope

router = APIRouter()


def _company(session: Session, stock_code: str) -> Company:
    company = session.execute(
        select(Company).where(Company.stock_code == stock_code)
    ).scalar_one_or_none()
    if company is None:
        raise HTTPException(404, f"No company {stock_code}")
    return company


@router.get("/{stock_code}/dupont")
def company_dupont(stock_code: str):
    from bursa.analysis.dupont import compute_dupont

    with session_scope() as session:
        return asdict(compute_dupont(session, _company(session, stock_code)))


@router.get("/{stock_code}/growth")
def company_growth(stock_code: str):
    from bursa.analysis.growth import compute_growth

    with session_scope() as session:
        return asdict(compute_growth(session, _company(session, stock_code)))


@router.get("/{stock_code}/prices")
def company_prices(stock_code: str):
    from bursa.analysis.prices import compute_price_ratios

    with session_scope() as session:
        return asdict(compute_price_ratios(session, _company(session, stock_code)))
