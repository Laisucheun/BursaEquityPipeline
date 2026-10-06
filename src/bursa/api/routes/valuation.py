"""Valuation metric endpoints."""

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from bursa.db.models import Company
from bursa.db.session import session_scope

router = APIRouter()


@router.get("/{stock_code}")
def company_valuation(stock_code: str):
    from bursa.valuation.metrics import compute_valuation

    with session_scope() as session:
        company = session.execute(
            select(Company).where(Company.stock_code == stock_code)
        ).scalar_one_or_none()
        if company is None:
            raise HTTPException(404, f"No company {stock_code}")

        result = compute_valuation(session, company)

        return {
            "stock_code": company.stock_code,
            "name": company.name,
            "years": [
                {
                    "fiscal_year": m.fiscal_year,
                    "ebit": float(m.ebit) if m.ebit is not None else None,
                    "ebitda": float(m.ebitda) if m.ebitda is not None else None,
                    "effective_tax_rate": float(m.effective_tax_rate) if m.effective_tax_rate is not None else None,
                    "nopat": float(m.nopat) if m.nopat is not None else None,
                    "dep_amort": float(m.dep_amort) if m.dep_amort is not None else None,
                    "capex": float(m.capex) if m.capex is not None else None,
                    "fcff": float(m.fcff) if m.fcff is not None else None,
                    "fcfe": float(m.fcfe) if m.fcfe is not None else None,
                    "net_debt": float(m.net_debt) if m.net_debt is not None else None,
                }
                for m in result.years
            ],
        }
