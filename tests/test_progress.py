from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bursa.config import get_settings
from bursa.db.enums import DocSource
from bursa.db.models import Base, Company, Document, JobProgress
from bursa.db.session import get_engine, get_sessionmaker, session_scope
from bursa.pipeline.jobs import track_job


def _reset_caches() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'test.sqlite'}")
    _reset_caches()
    Base.metadata.create_all(get_engine())
    yield
    get_engine().dispose()
    _reset_caches()


def test_track_job_records_progress_and_completion(db: None) -> None:
    with track_job("normalize facts", 3) as job:
        job.step(1, "0012 Three-A")
        with session_scope() as s:
            row = s.get(JobProgress, job.job_id)
            assert (row.status, row.done, row.current) == ("RUNNING", 1, "0012 Three-A")

    with session_scope() as s:
        row = s.get(JobProgress, job.job_id)
        assert (row.status, row.done, row.current) == ("SUCCEEDED", 3, None)
        assert row.finished_at is not None


def test_track_job_marks_failure(db: None) -> None:
    with pytest.raises(RuntimeError), track_job("normalize facts", 2) as job:
        raise RuntimeError("boom")

    with session_scope() as s:
        row = s.get(JobProgress, job.job_id)
        assert row.status == "FAILED"
        assert "boom" in row.error


def test_progress_endpoint_and_dashboard(db: None) -> None:
    from bursa.api.app import app

    with session_scope() as s:
        company = Company(stock_code="9999", name="Test <b>Berhad</b>")
        s.add_all([company, Company(stock_code="0001", name="No Docs Bhd")])
        s.flush()
        s.add(Document(company_id=company.id, source=DocSource.UPLOAD, original_filename="ar.pdf",
                       file_sha256="x", file_size=1, storage_path="ar.pdf"))
    with track_job("normalize facts", 1):
        pass

    client = TestClient(app)
    d = client.get("/api/progress").json()

    assert d["totals"]["companies"] == 2
    assert [f["count"] for f in d["funnel"][:2]] == [2, 1]
    assert d["jobs"][0]["status"] == "SUCCEEDED"
    assert {c["stock_code"] for c in d["companies"]} == {"9999", "0001"}
    assert any(s["section"] for s in d["roadmap"])

    page = client.get("/")
    assert page.status_code == 200
    assert "/api/progress" in page.text


def test_progress_endpoint_without_job_table(db: None) -> None:
    from bursa.api.app import app

    JobProgress.__table__.drop(get_engine())
    assert TestClient(app).get("/api/progress").json()["jobs"] == []
