from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from bursa.config import get_settings
from bursa.db.models import Base, Company, Document
from bursa.db.session import get_engine, get_sessionmaker, session_scope
from bursa.mapping.synonyms import seed_concepts
from tests.fixtures.synthetic import (
    ANNUAL_BALANCE_SHEET,
    ANNUAL_CASH_FLOW_STATEMENT,
    build_statement_pdf,
)


def _reset_caches() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'test.sqlite'}")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path / "storage"))
    _reset_caches()
    Base.metadata.create_all(get_engine())
    with session_scope() as s:
        seed_concepts(s)
        s.add(Company(stock_code="5101", name="Test Plantations", fy_end_month=12))
    from bursa.api.routes import upload

    app = FastAPI()
    app.include_router(upload.router, prefix="/api/upload")
    yield TestClient(app)
    get_engine().dispose()
    _reset_caches()


@pytest.fixture
def pdfs(tmp_path: Path) -> tuple[bytes, bytes]:
    a = build_statement_pdf(tmp_path / "a.pdf", ANNUAL_BALANCE_SHEET).read_bytes()
    b = build_statement_pdf(tmp_path / "b.pdf", ANNUAL_CASH_FLOW_STATEMENT).read_bytes()
    return a, b


def _post(client: TestClient, name: str, data: bytes, code: str = "5101"):
    return client.post("/api/upload/ingest", data={"stock_code": code},
                       files={"file": (name, data, "application/pdf")})


def test_ingest_keeps_filename_type_and_dedupes(
    client: TestClient, pdfs: tuple[bytes, bytes],
) -> None:
    annual, quarterly = pdfs

    r = _post(client, "5101/5101_AR_2025.pdf", annual)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True and body["company"] == "Test Plantations"
    assert body["doc_type"] == "ANNUAL_REPORT"

    assert _post(client, "renamed copy.pdf", annual).json()["created"] is False
    with session_scope() as s:
        s.add(Company(stock_code="1295", name="Other Co"))
    dup = _post(client, "1295_AR_2025.pdf", annual, code="1295").json()
    assert (dup["created"], dup["stock_code"]) == (False, "5101")
    assert _post(client, "5101_Q3_2025.pdf", quarterly).json()["doc_type"] == "QUARTERLY_REPORT"

    with session_scope() as s:
        names = sorted(s.scalars(select(Document.original_filename)))
    assert names == ["5101_AR_2025.pdf", "5101_Q3_2025.pdf"]


def test_ingest_rejects_bad_input(client: TestClient, pdfs: tuple[bytes, bytes]) -> None:
    assert _post(client, "x.pdf", pdfs[0], code="9999").status_code == 404
    assert _post(client, "notes.txt", b"hello").status_code == 400


def test_finalize_runs_company_pipeline(client: TestClient, pdfs: tuple[bytes, bytes]) -> None:
    _post(client, "5101_AR_2025.pdf", pdfs[0])
    r = client.post("/api/upload/finalize/5101")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stock_code"] == "5101"
    assert {"facts_written", "facts_derived", "validation"} <= body.keys()
    assert client.post("/api/upload/finalize/9999").status_code == 404
