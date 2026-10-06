"""PDF upload endpoint — runs the full pipeline."""

import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from sqlalchemy import select

from bursa.db.enums import DocSource, DocType
from bursa.db.models import Company
from bursa.db.session import session_scope

router = APIRouter()


@router.post("")
async def upload_pdf(
    file: UploadFile = File(...),
    stock_code: str = Form(...),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")

    from bursa.extract.statement_extract import extract_statements
    from bursa.pipeline.derive import derive_facts_for_company
    from bursa.pipeline.ingest import ingest_file
    from bursa.pipeline.normalize import write_facts_for_company
    from bursa.pipeline.validate import validate_company
    from bursa.scrapers.content_filter import has_financial_statements

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        with session_scope() as session:
            company = session.execute(
                select(Company).where(Company.stock_code == stock_code)
            ).scalar_one_or_none()
            if company is None:
                raise HTTPException(404, f"No company with stock code {stock_code}")

            doc, created = ingest_file(
                session, tmp_path,
                source=DocSource.UPLOAD,
                company_id=company.id,
                doc_type=DocType.ANNUAL_REPORT,
            )

            storage = Path(doc.storage_path)
            if not storage.is_file():
                storage = tmp_path

            has_statements = has_financial_statements(storage)

            result = extract_statements(session, doc.id, storage, company_id=company.id)
            found_statements = [s.value for s in result.statements.keys()]

            norm = write_facts_for_company(session, company)
            derive = derive_facts_for_company(session, company)
            val = validate_company(session, company)

            return {
                "document_id": doc.id,
                "created": created,
                "page_count": doc.page_count,
                "has_financial_statements": has_statements,
                "statements_found": found_statements,
                "facts_written": norm.facts_written,
                "facts_updated": norm.facts_updated,
                "periods_created": norm.periods_created,
                "facts_derived": derive.derived,
                "validation": {
                    "rules_run": val.rules_run,
                    "rules_passed": val.rules_passed,
                    "rules_failed": val.rules_failed,
                    "failures": [
                        {"rule": f.rule_key, "detail": f.detail}
                        for f in val.failures
                    ],
                },
                "comparative": {
                    "match": val.comparative.match,
                    "rounding": val.comparative.rounding,
                    "restatement": val.comparative.restatement,
                } if val.comparative else None,
            }
    finally:
        tmp_path.unlink(missing_ok=True)
