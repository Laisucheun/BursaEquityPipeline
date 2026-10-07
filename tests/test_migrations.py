from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from bursa.db.migrate import (
    BASELINE_REVISION,
    current_revision,
    head_revision,
    schema_diff,
    stamp_existing_db,
    upgrade_to_head,
)
from bursa.db.models import (
    Base,
    Company,
    Concept,
    Document,
    ExtractionRun,
    Fact,
    JobProgress,
    Period,
)


def _url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path.as_posix()}"


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return _url(tmp_path / "migrate.sqlite")


def test_upgrade_empty_db_matches_models(db_url: str) -> None:
    upgrade_to_head(db_url)

    assert current_revision(db_url) == head_revision()
    assert schema_diff(db_url) == []

    engine = create_engine(db_url)
    try:
        insp = inspect(engine)
        assert JobProgress.__tablename__ in insp.get_table_names()
        assert "facts_current" in insp.get_view_names()
    finally:
        engine.dispose()


def test_downgrade_to_base_and_back(db_url: str) -> None:
    from alembic import command

    from bursa.db.migrate import make_config

    upgrade_to_head(db_url)
    command.downgrade(make_config(db_url), "base")
    engine = create_engine(db_url)
    try:
        insp = inspect(engine)
        assert set(insp.get_table_names()) <= {"alembic_version"}
        assert insp.get_view_names() == []
    finally:
        engine.dispose()
    upgrade_to_head(db_url)
    assert schema_diff(db_url) == []


def test_stamp_adopts_create_all_db(db_url: str) -> None:
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    assert current_revision(db_url) is None

    stamp_existing_db(db_url)

    assert current_revision(db_url) == head_revision()
    assert schema_diff(db_url) == []
    engine = create_engine(db_url)
    try:
        assert "facts_current" in inspect(engine).get_view_names()
    finally:
        engine.dispose()


def test_stamp_creates_missing_tables(db_url: str) -> None:
    # The live DB predates job_progress: purely additive drift is filled in.
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE job_progress"))
    engine.dispose()

    msg = stamp_existing_db(db_url)

    assert "job_progress" in msg
    assert current_revision(db_url) == head_revision()
    assert schema_diff(db_url) == []


def test_stamp_refuses_drifted_db(db_url: str) -> None:
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_facts_run"))
    engine.dispose()

    with pytest.raises(RuntimeError, match="ix_facts_run"):
        stamp_existing_db(db_url)
    assert current_revision(db_url) is None


def test_baseline_revision_is_root() -> None:
    from alembic.script import ScriptDirectory

    from bursa.db.migrate import make_config

    script = ScriptDirectory.from_config(make_config("sqlite://"))
    assert script.get_base() == BASELINE_REVISION


def test_facts_current_picks_latest_filed(db_url: str) -> None:
    upgrade_to_head(db_url)
    engine = create_engine(db_url)
    try:
        with Session(engine) as s:
            co = Company(stock_code="1155", name="Maybank")
            s.add_all([co, Concept(concept_key="is.revenue", statement="IS", label="Revenue")])
            s.flush()
            period = Period(
                company_id=co.id,
                period_start=date(2023, 1, 1),
                period_end=date(2023, 12, 31),
                period_type="FY",
                fiscal_year=2023,
            )
            s.add(period)

            def doc(sha: str, filed: date | None, hint: date | None) -> Document:
                d = Document(
                    company_id=co.id,
                    source="UPLOAD",
                    original_filename=f"{sha}.pdf",
                    file_sha256=sha * 64,
                    file_size=1,
                    storage_path=f"/x/{sha}",
                    filed_date=filed,
                    period_end_hint=hint,
                )
                s.add(d)
                return d

            # Inserted out of order so the winner is not simply the last id.
            restated = doc("b", date(2025, 4, 20), date(2024, 12, 31))
            original = doc("a", date(2024, 4, 25), date(2023, 12, 31))
            # NULL filed_date ranks lowest even with the newest period_end_hint.
            undated = doc("c", None, date(2025, 12, 31))
            s.flush()

            def fact(d: Document, value: str, basis: str = "CONSOLIDATED") -> None:
                run = ExtractionRun(document_id=d.id)
                s.add(run)
                s.flush()
                s.add(
                    Fact(
                        company_id=co.id,
                        concept_key="is.revenue",
                        period_id=period.id,
                        value=Decimal(value),
                        basis=basis,
                        reported_in_document_id=d.id,
                        run_id=run.id,
                    )
                )

            fact(original, "100")
            fact(restated, "105")
            fact(undated, "999")

            # A second identity where neither document has a filed_date:
            # period_end_hint breaks the tie.
            hint_old = doc("d", None, date(2023, 12, 31))
            hint_new = doc("e", None, date(2024, 12, 31))
            s.flush()
            fact(hint_new, "7", basis="COMPANY")
            fact(hint_old, "6", basis="COMPANY")
            s.commit()
            hint_new_id, restated_id = hint_new.id, restated.id

            rows = s.execute(
                text(
                    "SELECT basis, value, reported_in_document_id, filed_date "
                    "FROM facts_current ORDER BY basis"
                )
            ).all()

        assert [(r.basis, Decimal(str(r.value))) for r in rows] == [
            ("COMPANY", Decimal("7")),
            ("CONSOLIDATED", Decimal("105")),
        ]
        assert rows[0].reported_in_document_id == hint_new_id
        assert rows[1].reported_in_document_id == restated_id
    finally:
        engine.dispose()
