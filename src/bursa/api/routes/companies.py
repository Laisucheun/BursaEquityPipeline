"""Company endpoints."""

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from bursa.db.models import Company, Document
from bursa.db.session import session_scope

router = APIRouter()


@router.get("")
def list_companies(
    search: str | None = Query(None, description="Filter by name or stock code"),
    sector: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    with session_scope() as session:
        query = (
            select(Company, func.count(Document.id).label("doc_count"))
            .outerjoin(Document, Document.company_id == Company.id)
            .group_by(Company.id)
        )
        if search:
            query = query.where(
                Company.name.ilike(f"%{search}%") | Company.stock_code.ilike(f"%{search}%")
            )
        if sector:
            query = query.where(Company.sector == sector)

        total = session.scalar(
            select(func.count()).select_from(
                query.with_only_columns(Company.id).subquery()
            )
        )

        rows = session.execute(
            query.order_by(Company.stock_code).offset(offset).limit(limit)
        ).all()

        return {
            "total": total,
            "items": [
                {
                    "id": c.id,
                    "stock_code": c.stock_code,
                    "name": c.name,
                    "market": str(c.market),
                    "sector": c.sector,
                    "fy_end_month": c.fy_end_month,
                    "ir_homepage_url": c.ir_homepage_url,
                    "doc_count": doc_count,
                }
                for c, doc_count in rows
            ],
        }


@router.get("/sectors")
def list_sectors():
    with session_scope() as session:
        rows = session.execute(
            select(Company.sector, func.count(Company.id))
            .where(Company.sector.isnot(None))
            .group_by(Company.sector)
            .order_by(func.count(Company.id).desc())
        ).all()
        return [{"sector": s, "count": c} for s, c in rows]


@router.get("/{stock_code}")
def get_company(stock_code: str):
    with session_scope() as session:
        company = session.execute(
            select(Company).where(Company.stock_code == stock_code)
        ).scalar_one_or_none()
        if company is None:
            raise HTTPException(404, f"No company with stock code {stock_code}")

        docs = list(session.scalars(
            select(Document)
            .where(Document.company_id == company.id)
            .order_by(Document.id.desc())
        ))

        return {
            "id": company.id,
            "stock_code": company.stock_code,
            "name": company.name,
            "market": str(company.market),
            "sector": company.sector,
            "fy_end_month": company.fy_end_month,
            "ir_homepage_url": company.ir_homepage_url,
            "documents": [
                {
                    "id": d.id,
                    "doc_type": str(d.doc_type),
                    "source": str(d.source),
                    "original_filename": d.original_filename,
                    "page_count": d.page_count,
                    "status": str(d.status),
                    "source_url": d.source_url,
                }
                for d in docs
            ],
        }
