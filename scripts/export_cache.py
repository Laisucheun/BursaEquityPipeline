"""Dump facts + validation results to a pickle so analysis doesn't require
re-running extract/normalize/validate (each a multi-hour pass over 291 PDFs).

Run after any `bursa normalize facts` / `bursa validate facts` pass:
    uv run python scripts/export_cache.py

Load later with:
    import pickle
    data = pickle.load(open("cache/bursa_snapshot.pkl", "rb"))
    data["facts"], data["validation_failures"], data["companies"]
"""

from __future__ import annotations

import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select

from bursa.db.models import Company, Document, Fact, Period
from bursa.db.session import session_scope
from bursa.pipeline.validate import validate_company

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


def main() -> None:
    CACHE_DIR.mkdir(exist_ok=True)

    with session_scope() as session:
        companies = list(session.scalars(select(Company)))

        company_rows = [
            {
                "id": c.id,
                "stock_code": c.stock_code,
                "name": c.name,
                "fy_end_month": c.fy_end_month,
                "ir_homepage_url": c.ir_homepage_url,
            }
            for c in companies
        ]

        fact_rows = []
        q = (
            select(Fact, Period, Document, Company)
            .join(Period, Fact.period_id == Period.id)
            .join(Document, Fact.reported_in_document_id == Document.id)
            .join(Company, Fact.company_id == Company.id)
        )
        for fact, period, doc, company in session.execute(q):
            fact_rows.append(
                {
                    "stock_code": company.stock_code,
                    "company_name": company.name,
                    "concept_key": fact.concept_key,
                    "period_start": period.period_start,
                    "period_end": period.period_end,
                    "period_type": str(period.period_type),
                    "fiscal_year": period.fiscal_year,
                    "basis": str(fact.basis),
                    "continuity": str(fact.continuity),
                    "value": fact.value,
                    "value_as_printed": fact.value_as_printed,
                    "scale_multiplier": fact.scale_multiplier,
                    "confidence": fact.confidence,
                    "review_status": str(fact.review_status),
                    "document_id": doc.id,
                    "document_filename": doc.original_filename,
                    "reported_in_document_id": fact.reported_in_document_id,
                }
            )

        validation_rows = []
        summary_rows = []
        for company in companies:
            result = validate_company(session, company)
            if result.rules_run == 0:
                continue
            summary_rows.append(
                {
                    "stock_code": company.stock_code,
                    "name": company.name,
                    "rules_run": result.rules_run,
                    "rules_passed": result.rules_passed,
                    "rules_failed": result.rules_failed,
                }
            )
            for outcome in result.failures:
                validation_rows.append(
                    {
                        "stock_code": company.stock_code,
                        "name": company.name,
                        "rule_key": outcome.rule_key,
                        "detail": outcome.detail,
                        "expected": outcome.expected,
                        "actual": outcome.actual,
                        "delta": outcome.delta,
                        "period_end": outcome.period_end,
                    }
                )

    snapshot = {
        "generated_at": datetime.now(timezone.utc),
        "companies": company_rows,
        "facts": fact_rows,
        "validation_summary": summary_rows,
        "validation_failures": validation_rows,
    }

    out_path = CACHE_DIR / "bursa_snapshot.pkl"
    with open(out_path, "wb") as f:
        pickle.dump(snapshot, f, protocol=pickle.HIGHEST_PROTOCOL)

    total_run = sum(r["rules_run"] for r in summary_rows)
    total_passed = sum(r["rules_passed"] for r in summary_rows)
    total_failed = sum(r["rules_failed"] for r in summary_rows)
    print(f"wrote {out_path}")
    print(f"  {len(company_rows)} companies, {len(fact_rows)} facts")
    print(f"  validation: {total_run} rules run, {total_passed} passed, {total_failed} failed")


if __name__ == "__main__":
    main()
