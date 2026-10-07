"""Validation result endpoints."""

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import Integer, cast, func, select

from bursa.db.models import Company, Period, ValidationResult
from bursa.db.session import session_scope

router = APIRouter()


@router.get("")
def validation_summary(
    stock_code: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    with session_scope() as session:
        query = (
            select(
                Company.stock_code,
                Company.name,
                func.count(ValidationResult.id).label("total"),
                func.sum(cast(ValidationResult.passed, Integer)).label("passed"),
            )
            .join(ValidationResult, ValidationResult.run_id.isnot(None))
            .join(Period, ValidationResult.period_id == Period.id)
            .where(Period.company_id == Company.id)
            .group_by(Company.id)
            .order_by(Company.stock_code)
            .limit(limit)
        )
        if stock_code:
            query = query.where(Company.stock_code == stock_code)

        rows = session.execute(query).all()
        return [
            {
                "stock_code": sc,
                "name": name,
                "rules_run": total,
                "rules_passed": passed or 0,
                "rules_failed": total - (passed or 0),
            }
            for sc, name, total, passed in rows
        ]


@router.get("/{stock_code}")
def company_validation(stock_code: str):
    with session_scope() as session:
        company = session.execute(
            select(Company).where(Company.stock_code == stock_code)
        ).scalar_one_or_none()
        if company is None:
            raise HTTPException(404, f"No company {stock_code}")

        rows = session.execute(
            select(ValidationResult, Period)
            .join(Period, ValidationResult.period_id == Period.id)
            .where(Period.company_id == company.id)
            .order_by(Period.period_end.desc(), ValidationResult.rule_key)
        ).all()

        return {
            "stock_code": company.stock_code,
            "name": company.name,
            "results": [
                {
                    "rule_key": vr.rule_key,
                    "passed": vr.passed,
                    "expected": float(vr.expected) if vr.expected is not None else None,
                    "actual": float(vr.actual) if vr.actual is not None else None,
                    "delta": float(vr.delta) if vr.delta is not None else None,
                    "detail": vr.detail,
                    "period_end": str(p.period_end),
                    "fiscal_year": p.fiscal_year,
                }
                for vr, p in rows
            ],
        }
