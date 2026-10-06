"""Benchmark result endpoints."""

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from bursa.db.models import BenchmarkResult, Company
from bursa.db.session import session_scope

router = APIRouter()


@router.get("")
def benchmark_summary(
    stock_code: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    with session_scope() as session:
        query = select(BenchmarkResult, Company.stock_code, Company.name).join(
            Company, BenchmarkResult.company_id == Company.id
        )
        if stock_code:
            query = query.where(Company.stock_code == stock_code)

        rows = session.execute(
            query.order_by(Company.stock_code, BenchmarkResult.fiscal_year.desc())
            .limit(limit)
        ).all()

        return [
            {
                "stock_code": sc,
                "company_name": name,
                "concept_key": br.concept_key,
                "fiscal_year": br.fiscal_year,
                "our_value": float(br.our_value) if br.our_value is not None else None,
                "external_value": float(br.external_value) if br.external_value is not None else None,
                "deviation_pct": float(br.deviation_pct) if br.deviation_pct is not None else None,
                "classification": br.classification,
            }
            for br, sc, name in rows
        ]


@router.get("/{stock_code}")
def company_benchmark(stock_code: str):
    with session_scope() as session:
        company = session.execute(
            select(Company).where(Company.stock_code == stock_code)
        ).scalar_one_or_none()
        if company is None:
            raise HTTPException(404, f"No company {stock_code}")

        rows = list(session.scalars(
            select(BenchmarkResult)
            .where(BenchmarkResult.company_id == company.id)
            .order_by(BenchmarkResult.fiscal_year.desc(), BenchmarkResult.concept_key)
        ))

        return {
            "stock_code": company.stock_code,
            "name": company.name,
            "results": [
                {
                    "concept_key": br.concept_key,
                    "fiscal_year": br.fiscal_year,
                    "our_value": float(br.our_value) if br.our_value is not None else None,
                    "external_value": float(br.external_value) if br.external_value is not None else None,
                    "deviation_pct": float(br.deviation_pct) if br.deviation_pct is not None else None,
                    "classification": br.classification,
                    "detail": br.detail,
                }
                for br in rows
            ],
        }
