from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from bursa.config import get_settings
from bursa.db.enums import DocSource, RunStatus, Statement
from bursa.db.models import Base, Company, ConceptSynonym, Document, ExtractionRun, ReviewItem
from bursa.db.session import get_engine, get_sessionmaker, session_scope
from bursa.extract.statement_extract import ColumnInfo, RowInfo, StatementExtraction
from bursa.mapping.synonyms import lookup, seed_concepts
from bursa.normalize.scale import ScaleInfo
from bursa.pipeline.review import (
    UNMAPPED_LABEL,
    is_reviewable_label,
    record_unmapped_rows,
    severity_for,
    unmapped_candidates,
)


def _extraction(rows: list[RowInfo], statement: Statement = Statement.INCOME_STATEMENT) -> StatementExtraction:
    return StatementExtraction(
        statement=statement,
        page_no=42,
        final_score=1.0,
        row_keyword_hits=5,
        scale=ScaleInfo(multiplier=1000, token="RM'000", currency="MYR"),
        header_text="for the financial year ended 31 December 2024",
        columns=[
            ColumnInfo(col_index=1, header_text="Note", year=None, is_note=True),
            ColumnInfo(col_index=2, header_text="2024", year=2024),
            ColumnInfo(col_index=3, header_text="2023", year=2023),
        ],
        rows=rows,
    )


def _row(i: int, label: str, key: str | None = None, values: dict[int, str] | None = None) -> RowInfo:
    return RowInfo(row_index=i, label=label, concept_key=key, indent_level=0,
                   values=values if values is not None else {2: "1,234", 3: "(987)"})


ROWS = [
    _row(0, "Revenue", "is.revenue"),
    _row(1, "Share of results of jointly controlled entities"),         # genuine gap
    _row(2, "Fair value gain on biological assets", values={1: "7", 2: "55", 3: "12"}),
    _row(3, "2024", values={2: "2024", 3: "2023"}),         # year header read as figures
    _row(4, "Basic (sen)"),                                 # EPS sub-row
    _row(5, "Valueless header", values={}),                 # no figures
    _row(6, "Zero line", values={2: "0", 3: "-"}),          # nothing to lose
    _row(7, "Share of results of jointly controlled entities"),          # duplicate label
    _row(8, "Revenue"),                                     # demoted by context, but known label
]


@pytest.fixture
def seeded(session: Session) -> Session:
    seed_concepts(session)
    session.commit()
    return session


def _company_doc(session: Session, code: str = "9999", sha: str = "a") -> tuple[Company, Document]:
    company = session.scalar(select(Company).where(Company.stock_code == code))
    if company is None:
        company = Company(stock_code=code, name=f"Test {code} Berhad")
        session.add(company)
        session.flush()
    doc = Document(company_id=company.id, source=DocSource.UPLOAD, original_filename=f"ar-{sha}.pdf",
                   file_sha256=sha, file_size=1, storage_path=f"ar-{sha}.pdf")
    session.add(doc)
    session.flush()
    return company, doc


def _run(session: Session, document_id: int) -> int:
    run = ExtractionRun(document_id=document_id, status=RunStatus.SUCCEEDED)
    session.add(run)
    session.flush()
    return run.id


def _items(session: Session) -> list[ReviewItem]:
    return list(session.scalars(select(ReviewItem).order_by(ReviewItem.id)))


# --------------------------------------------------------------------------
# record_unmapped_rows
# --------------------------------------------------------------------------


def test_filter_skips_headers_dates_and_eps() -> None:
    assert is_reviewable_label("Share of results of associates", [1.0])
    assert not is_reviewable_label("31 December 2024", [1.0])
    assert not is_reviewable_label("As at 30.6.2024", [1.0])
    assert not is_reviewable_label("Note 12", [1.0])
    assert not is_reviewable_label("Diluted (sen)", [1.0])
    assert not is_reviewable_label("Group", [1.0])
    assert not is_reviewable_label("capital changes", [1.0])  # wrapped-label tail
    assert not is_reviewable_label("Year header", [2024.0, 2023.0])


def test_candidates_only_figure_bearing_unknown_labels(seeded: Session) -> None:
    cands = unmapped_candidates(seeded, None, Statement.INCOME_STATEMENT, _extraction(ROWS))
    assert [c["label"] for c in cands] == [
        "Share of results of jointly controlled entities",
        "Fair value gain on biological assets",
    ]
    fv = cands[1]
    assert fv["values"] == {"2": "55", "3": "12"}  # note column dropped
    assert fv["page_no"] == 42 and fv["row_index"] == 2
    assert fv["magnitude"] == 55_000


def test_equity_statement_is_not_reviewed(seeded: Session) -> None:
    assert unmapped_candidates(seeded, None, Statement.EQUITY, _extraction(ROWS, Statement.EQUITY)) == []


def test_record_creates_open_items_and_dedupes(seeded: Session) -> None:
    company, doc = _company_doc(seeded)
    run_id = _run(seeded, doc.id)
    assert record_unmapped_rows(seeded, company, doc.id, run_id, Statement.INCOME_STATEMENT, _extraction(ROWS)) == 2
    # Same run again, and a second document of the same company: nothing new.
    assert record_unmapped_rows(seeded, company, doc.id, run_id, Statement.INCOME_STATEMENT, _extraction(ROWS)) == 0
    _, doc2 = _company_doc(seeded, sha="b")
    assert record_unmapped_rows(
        seeded, company, doc2.id, _run(seeded, doc2.id), Statement.INCOME_STATEMENT, _extraction(ROWS)
    ) == 0

    items = _items(seeded)
    assert len(items) == 2
    assert all(i.reason == UNMAPPED_LABEL and i.resolved_at is None for i in items)
    detail = json.loads(items[0].detail)
    assert detail["statement"] == "IS" and detail["label"] == "Share of results of jointly controlled entities"
    # Same label on another statement is a separate question.
    assert record_unmapped_rows(
        seeded, company, doc.id, run_id, Statement.CASH_FLOW, _extraction(ROWS, Statement.CASH_FLOW)
    ) >= 1


def test_rerun_keeps_open_and_resolved_items_and_drops_stale(seeded: Session) -> None:
    from datetime import UTC, datetime

    company, doc = _company_doc(seeded)
    run1 = _run(seeded, doc.id)
    record_unmapped_rows(seeded, company, doc.id, run1, Statement.INCOME_STATEMENT, _extraction(ROWS))
    jv, fv = _items(seeded)
    fv.resolved_at, fv.resolution = datetime.now(UTC), {"action": "ignore"}
    seeded.flush()

    # Re-run: new run, then the old one is deleted exactly as normalize does.
    # This time the JV row is gone from the page (say, now mapped).
    run2 = _run(seeded, doc.id)
    rows = [r for r in ROWS if "jointly controlled" not in r.label]
    assert record_unmapped_rows(seeded, company, doc.id, run2, Statement.INCOME_STATEMENT, _extraction(rows)) == 0
    seeded.execute(delete(ExtractionRun).where(ExtractionRun.document_id == doc.id, ExtractionRun.id != run2))
    seeded.expire_all()

    items = _items(seeded)
    assert [(i.id, i.run_id) for i in items] == [(fv.id, run2)]  # ignore decision survived
    assert items[0].resolution == {"action": "ignore"}

    # A third run with the JV row back raises it again, once.
    run3 = _run(seeded, doc.id)
    assert record_unmapped_rows(seeded, company, doc.id, run3, Statement.INCOME_STATEMENT, _extraction(ROWS)) == 1
    assert len(_items(seeded)) == 2


def test_severity_ranks_magnitude_and_frequency() -> None:
    assert severity_for(5e9, 1) > severity_for(5e5, 1)
    assert severity_for(5e5, 4) > severity_for(5e5, 1)
    assert 0 <= severity_for(0, 1) <= 100 and severity_for(1e15, 99) <= 100


def test_frequency_across_companies_raises_severity(seeded: Session) -> None:
    rows = [_row(0, "Share of results of jointly controlled entities")]
    sev = []
    for code in ("1001", "1002", "1003"):
        company, doc = _company_doc(seeded, code=code, sha=code)
        record_unmapped_rows(seeded, company, doc.id, _run(seeded, doc.id), Statement.INCOME_STATEMENT,
                             _extraction(rows))
        sev.append(_items(seeded)[-1].severity)
    assert sev[0] < sev[1] < sev[2]


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def _reset_caches() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'test.sqlite'}")
    _reset_caches()
    Base.metadata.create_all(get_engine())
    with session_scope() as s:
        seed_concepts(s)
        company, doc = _company_doc(s)
        record_unmapped_rows(s, company, doc.id, _run(s, doc.id), Statement.INCOME_STATEMENT, _extraction(ROWS))
        record_unmapped_rows(s, company, doc.id, _run(s, doc.id), Statement.BALANCE_SHEET,
                             _extraction([_row(0, "Deferred tax assets on revaluation reserve")],
                                         Statement.BALANCE_SHEET))
    from bursa.api.routes import review

    app = FastAPI()
    app.include_router(review.router, prefix="/api")
    yield TestClient(app)
    get_engine().dispose()
    _reset_caches()


def test_list_review_items(client: TestClient) -> None:
    d = client.get("/api/review").json()
    assert d["total"] == 3
    assert d["reasons"] == [{"reason": UNMAPPED_LABEL, "count": 3}]
    item = next(i for i in d["items"] if i["raw_label"] == "Fair value gain on biological assets")
    assert item["stock_code"] == "9999" and item["company_name"] == "Test 9999 Berhad"
    assert item["document_filename"] == "ar-a.pdf"
    assert item["statement"] == "is" and item["page_no"] == 42
    assert item["cells"] == [{"col_index": 2, "text": "55"}, {"col_index": 3, "text": "12"}]
    assert item["created_at"] and item["resolved_at"] is None
    assert 1 <= len(item["suggestions"]) <= 3
    assert all(s["concept_key"].startswith("is.") for s in item["suggestions"])

    bs = next(i for i in d["items"] if i["statement"] == "bs")
    assert bs["suggestions"][0]["concept_key"] == "bs.deferred_tax_assets"

    assert client.get("/api/review", params={"stock_code": "0000"}).json()["total"] == 0
    assert client.get("/api/review", params={"reason": "NOPE"}).json()["total"] == 0
    assert client.get("/api/review", params={"status": "resolved"}).json()["total"] == 0
    page = client.get("/api/review", params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 3 and len(page["items"]) == 1


def test_resolve_map_creates_global_synonym(client: TestClient) -> None:
    items = client.get("/api/review").json()["items"]
    jv = next(i for i in items if i["raw_label"] == "Share of results of jointly controlled entities")
    assert "is.share_of_jv" in [s["concept_key"] for s in jv["suggestions"]]
    key = "is.share_of_jv"
    r = client.post(f"/api/review/{jv['id']}/resolve",
                    json={"action": "map", "concept_key": key, "resolved_by": "alice"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["resolved_by"] == "alice" and out["resolved_at"]
    assert out["resolution"]["action"] == "map" and out["resolution"]["concept_key"] == key
    assert out["resolution"]["synonym"]["company_scoped"] is False

    with session_scope() as s:
        assert lookup(s, "Share of results of jointly controlled entities", Statement.INCOME_STATEMENT) == key

    d = client.get("/api/review").json()
    assert d["total"] == 2
    assert client.get("/api/review", params={"status": "resolved"}).json()["total"] == 1
    assert client.get("/api/review", params={"status": "all"}).json()["total"] == 3


def test_resolve_map_company_scoped_and_validation(client: TestClient) -> None:
    items = client.get("/api/review").json()["items"]
    fv = next(i for i in items if i["raw_label"] == "Fair value gain on biological assets")
    assert client.post(f"/api/review/{fv['id']}/resolve", json={"action": "map"}).status_code == 422
    assert client.post(f"/api/review/{fv['id']}/resolve",
                       json={"action": "map", "concept_key": "bs.total_assets"}).status_code == 422
    assert client.post("/api/review/99999/resolve", json={"action": "ignore"}).status_code == 404

    r = client.post(f"/api/review/{fv['id']}/resolve",
                    json={"action": "map", "concept_key": "is.other_income", "company_scoped": True})
    assert r.status_code == 200, r.text
    with session_scope() as s:
        syn = s.scalar(select(ConceptSynonym).where(ConceptSynonym.normalized == "fair value gain on biological assets"))
        assert syn.company_id is not None and syn.concept_key == "is.other_income"
        assert lookup(s, "Fair value gain on biological assets", Statement.INCOME_STATEMENT) is None


def test_resolve_ignore_and_reject_add_no_synonym(client: TestClient) -> None:
    with session_scope() as s:
        before = len(list(s.scalars(select(ConceptSynonym))))
    ids = [i["id"] for i in client.get("/api/review").json()["items"]]
    a = client.post(f"/api/review/{ids[0]}/resolve", json={"action": "ignore"}).json()
    b = client.post(f"/api/review/{ids[1]}/resolve", json={"action": "reject"}).json()
    assert a["resolution"] == {"action": "ignore"}
    assert b["resolution"]["action"] == "reject"
    with session_scope() as s:
        assert len(list(s.scalars(select(ConceptSynonym)))) == before


def test_concepts_endpoint(client: TestClient) -> None:
    all_ = client.get("/api/concepts").json()
    is_ = client.get("/api/concepts", params={"statement": "is"}).json()
    assert is_ and all(c["statement"] == "is" for c in is_)
    assert {"concept_key", "statement", "label"} == set(is_[0])
    assert len(all_) > len(is_)
    assert any(c["concept_key"] == "is.revenue" for c in is_)
    assert client.get("/api/concepts", params={"statement": "xx"}).status_code == 422
