from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from bursa.config import get_settings
from bursa.db.enums import DocSource
from bursa.db.models import Base, Company, Document, Fact, JobProgress, Period
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


def _build_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / name}")
    _reset_caches()
    Base.metadata.create_all(get_engine())
    with session_scope() as s:
        seed_concepts(s)
        specs = [ANNUAL_BALANCE_SHEET, ANNUAL_CASH_FLOW_STATEMENT, ANNUAL_BALANCE_SHEET]
        for i, spec in enumerate(specs):
            company = Company(stock_code=f"900{i}", name=f"Test {i} Berhad")
            s.add(company)
            s.flush()
            pdf = build_statement_pdf(tmp_path / f"{name}-{i}.pdf", spec)
            s.add(Document(
                company_id=company.id, source=DocSource.UPLOAD, original_filename=pdf.name,
                file_sha256=f"{name}-{i}", file_size=pdf.stat().st_size,
                storage_path=str(pdf), page_count=20,
            ))


def _facts() -> set[tuple]:
    with session_scope() as s:
        return set(s.execute(
            select(Company.stock_code, Fact.concept_key, Period.period_end,
                   Period.period_type, Fact.basis, Fact.value)
            .join(Company, Fact.company_id == Company.id)
            .join(Period, Fact.period_id == Period.id)
        ).all())


def _run(workers: int) -> tuple[set[tuple], JobProgress]:
    from bursa.cli import app

    result = CliRunner().invoke(app, ["normalize", "facts", "--workers", str(workers)])
    assert result.exit_code == 0, result.output
    with session_scope() as s:
        job = s.scalars(select(JobProgress).order_by(JobProgress.id.desc())).first()
        s.expunge(job)
    return _facts(), job


def test_parallel_extraction_writes_the_same_facts_as_sequential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_db(tmp_path, monkeypatch, "seq.sqlite")
    sequential, seq_job = _run(workers=1)
    get_engine().dispose()

    _build_db(tmp_path, monkeypatch, "par.sqlite")
    parallel, par_job = _run(workers=2)
    get_engine().dispose()
    _reset_caches()

    assert sequential
    assert parallel == sequential
    for job in (seq_job, par_job):
        assert (job.status, job.done, job.total, job.error) == ("SUCCEEDED", 3, 3, None)


def test_a_failing_company_is_reported_and_the_rest_still_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_db(tmp_path, monkeypatch, "fail.sqlite")
    (tmp_path / "fail.sqlite-1.pdf").write_bytes(b"not a pdf")

    facts, job = _run(workers=2)
    get_engine().dispose()
    _reset_caches()

    assert {f[0] for f in facts} == {"9000", "9002"}
    assert job.status == "SUCCEEDED"
    assert job.error and job.error.startswith("9001:")
