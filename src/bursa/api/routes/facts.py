"""Fact query endpoints."""

from fastapi import APIRouter, Query
from sqlalchemy import select

from bursa.db.models import Company, Fact, Period
from bursa.db.session import session_scope

router = APIRouter()


@router.get("")
def query_facts(
    stock_code: str | None = Query(None),
    concept: str | None = Query(None, description="Filter by concept_key prefix, e.g. 'is.' or 'bs.total_assets'"),
    fiscal_year: int | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    with session_scope() as session:
        query = (
            select(Fact, Period, Company.stock_code, Company.name)
            .join(Period, Fact.period_id == Period.id)
            .join(Company, Fact.company_id == Company.id)
        )
        if stock_code:
            query = query.where(Company.stock_code == stock_code)
        if concept:
            if concept.endswith("."):
                query = query.where(Fact.concept_key.like(f"{concept}%"))
            else:
                query = query.where(Fact.concept_key == concept)
        if fiscal_year:
            query = query.where(Period.fiscal_year == fiscal_year)

        rows = session.execute(
            query.order_by(Company.stock_code, Period.period_end.desc(), Fact.concept_key)
            .offset(offset).limit(limit)
        ).all()

        return [
            {
                "stock_code": sc,
                "company_name": name,
                "concept_key": f.concept_key,
                "value": float(f.value) if f.value is not None else None,
                "period_end": str(p.period_end),
                "period_type": str(p.period_type),
                "fiscal_year": p.fiscal_year,
                "basis": str(f.basis),
                "confidence": float(f.confidence) if f.confidence is not None else None,
            }
            for f, p, sc, name in rows
        ]


@router.get("/{stock_code}")
def company_facts(
    stock_code: str,
    fiscal_year: int | None = Query(None),
):
    with session_scope() as session:
        company = session.execute(
            select(Company).where(Company.stock_code == stock_code)
        ).scalar_one_or_none()
        if company is None:
            return {"error": f"No company {stock_code}"}

        query = (
            select(Fact, Period)
            .join(Period, Fact.period_id == Period.id)
            .where(Fact.company_id == company.id)
        )
        if fiscal_year:
            query = query.where(Period.fiscal_year == fiscal_year)

        rows = session.execute(
            query.order_by(Period.period_end.desc(), Fact.concept_key)
        ).all()

        by_period: dict[str, list] = {}
        for f, p in rows:
            key = f"{p.fiscal_year} ({p.period_type})"
            by_period.setdefault(key, []).append({
                "concept_key": f.concept_key,
                "value": float(f.value) if f.value is not None else None,
                "basis": str(f.basis),
                "confidence": float(f.confidence) if f.confidence is not None else None,
            })

        return {
            "stock_code": company.stock_code,
            "name": company.name,
            "periods": by_period,
        }
