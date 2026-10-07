"""Five-year summary cross-check for one company (read-only).

Wraps `bursa.validate.five_year_check.check_company`: extracts the summary
page(s) from the newest annual report(s) on the fly and classifies each
(fiscal year, concept) against our facts. Nothing is written.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from bursa.db.models import Company
from bursa.db.session import session_scope

router = APIRouter()


def _num(value) -> float | None:  # type: ignore[no-untyped-def]
    return None if value is None else float(value)


@router.get("/{stock_code}")
def five_year_check(
    stock_code: str,
    years: Annotated[int, Query(ge=0, description="Newest N report years (0 = all).")] = 1,
    dps: Annotated[bool, Query(description="Include dividend-per-share rows.")] = False,
):
    from bursa.validate.five_year_check import CLASSES, check_company

    with session_scope() as session:
        company = session.scalar(select(Company).where(Company.stock_code == stock_code))
        if company is None:
            raise HTTPException(404, f"No company {stock_code}")
        res = check_company(session, company, max_years=years or None, include_dps=dps)
        session.rollback()  # read-only: never commit anything

    counts = res.counts
    return {
        "stock_code": res.stock_code,
        "name": res.name,
        "documents_scanned": res.documents_scanned,
        "documents_with_summary": res.documents_with_summary,
        "documents": [
            {
                "document_id": d.document_id,
                "report_year": d.report_year,
                "summary_pages": sorted({t.page_no for t in d.tables}),
                "error": d.error,
            }
            for d in res.documents
        ],
        "summary_pages": sorted({t.page_no for d in res.documents for t in d.tables}),
        "counts": {k: counts.get(k, 0) for k in CLASSES},
        "agreement": res.agreement,
        "year_shift": res.year_shift,
        "fact_years": res.fact_years,
        "checks": [
            {
                "fiscal_year": c.fiscal_year,
                "concept_key": c.concept_key,
                "summary_value": _num(c.summary_value),
                "our_value": _num(c.our_value),
                "deviation_pct": c.deviation_pct,
                "classification": c.classification,
                "detail": c.detail,
                "page_no": c.page_no,
                "document_id": c.document_id,
                "restated": c.restated,
            }
            for c in res.checks
        ],
    }
