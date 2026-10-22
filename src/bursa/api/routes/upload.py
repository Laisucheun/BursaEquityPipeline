"""PDF upload endpoints.

* ``POST /api/upload`` - one file, full pipeline, one response.
* ``POST /api/upload/ingest`` + ``POST /api/upload/finalize/{code}`` - bulk:
  ingest every file first, then run normalize/derive/validate once per
  company. ``write_facts_for_company`` re-extracts all of a company's
  documents, so running it after each of five yearly reports would extract
  1+2+3+4+5 documents instead of 5.
"""

import shutil
import tempfile
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from bursa.db.enums import DocSource, DocType
from bursa.db.models import Company, Document
from bursa.db.session import session_scope
from bursa.storage import materialize

router = APIRouter()


async def _save(file: UploadFile) -> Path:
    """Write the upload to a temp dir under its original basename, so the
    stored ``original_filename`` and filename hints are the real ones."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")
    name = PurePosixPath(file.filename.replace("\\", "/")).name
    path = Path(tempfile.mkdtemp(prefix="bursa-upload-")) / name
    path.write_bytes(await file.read())
    return path


def _discard(path: Path) -> None:
    shutil.rmtree(path.parent, ignore_errors=True)


def _company(session: Session, stock_code: str) -> Company:
    company = session.execute(
        select(Company).where(Company.stock_code == stock_code.strip())
    ).scalar_one_or_none()
    if company is None:
        raise HTTPException(404, f"No company with stock code {stock_code}")
    return company


def _ingest(session: Session, company: Company, path: Path) -> tuple[Document, bool, Path]:
    from bursa.pipeline.ingest import ingest_file, parse_filename

    hinted = parse_filename(path.name).doc_type
    doc, created = ingest_file(
        session, path,
        source=DocSource.UPLOAD,
        company_id=company.id,
        doc_type=DocType.QUARTERLY_REPORT if hinted == DocType.QUARTERLY_REPORT
        else DocType.ANNUAL_REPORT,
    )
    storage = materialize(doc)
    return doc, created, storage if storage.is_file() else path


def _company_pipeline(session: Session, company: Company) -> dict:
    from bursa.pipeline.derive import derive_facts_for_company
    from bursa.pipeline.normalize import write_facts_for_company
    from bursa.pipeline.validate import validate_company

    norm = write_facts_for_company(session, company)
    derive = derive_facts_for_company(session, company)
    val = validate_company(session, company)
    return {
        "facts_written": norm.facts_written,
        "facts_updated": norm.facts_updated,
        "facts_deleted": norm.facts_deleted,
        "periods_created": norm.periods_created,
        "facts_derived": derive.derived,
        "validation": {
            "rules_run": val.rules_run,
            "rules_passed": val.rules_passed,
            "rules_failed": val.rules_failed,
            "failures": [{"rule": f.rule_key, "detail": f.detail} for f in val.failures],
        },
        "comparative": {
            "match": val.comparative.match,
            "rounding": val.comparative.rounding,
            "restatement": val.comparative.restatement,
        } if val.comparative else None,
    }


@router.post("")
async def upload_pdf(
    file: UploadFile = File(...),
    stock_code: str = Form(...),
):
    from bursa.extract.statement_extract import extract_statements
    from bursa.scrapers.content_filter import has_financial_statements

    path = await _save(file)
    try:
        with session_scope() as session:
            company = _company(session, stock_code)
            doc, created, storage = _ingest(session, company, path)
            has_statements = has_financial_statements(storage)
            result = extract_statements(session, doc.id, storage, company_id=company.id)
            return {
                "document_id": doc.id,
                "created": created,
                "page_count": doc.page_count,
                "has_financial_statements": has_statements,
                "statements_found": [s.value for s in result.statements],
                **_company_pipeline(session, company),
            }
    finally:
        _discard(path)


@router.post("/ingest")
async def ingest_pdf(
    file: UploadFile = File(...),
    stock_code: str = Form(...),
):
    """Store one PDF against a company without running the pipeline.
    Follow up with ``/finalize/{stock_code}`` once that company's files are in."""
    from bursa.scrapers.content_filter import has_financial_statements

    path = await _save(file)
    try:
        with session_scope() as session:
            company = _company(session, stock_code)
            doc, created, storage = _ingest(session, company, path)
            # A duplicate keeps its original owner - which may not be the
            # company it was uploaded for this time.
            owner = company if created or doc.company_id == company.id else (
                session.get(Company, doc.company_id) if doc.company_id else None
            )
            return {
                "document_id": doc.id,
                "created": created,
                "stock_code": owner.stock_code if owner else None,
                "company": owner.name if owner else None,
                "doc_type": str(doc.doc_type),
                "page_count": doc.page_count,
                "has_financial_statements": has_financial_statements(storage),
            }
    finally:
        _discard(path)


@router.post("/finalize/{stock_code}")
def finalize_company(stock_code: str):
    """Normalize, derive and validate one company across all its documents."""
    with session_scope() as session:
        company = _company(session, stock_code)
        return {"stock_code": company.stock_code, **_company_pipeline(session, company)}
