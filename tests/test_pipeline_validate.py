from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bursa.db.enums import DocSource
from bursa.db.models import Company, Document, ValidationResult
from bursa.mapping.synonyms import seed_concepts
from bursa.pipeline.normalize import write_facts_for_document
from bursa.pipeline.validate import validate_company
from tests.fixtures.synthetic import (
    ANNUAL_BALANCE_SHEET,
    ANNUAL_BALANCE_SHEET_UNBALANCED,
    ANNUAL_INCOME_STATEMENT_WITH_SCALE_ERROR,
    build_statement_pdf,
)

# bursa.validate.rules's own logic (sign tolerance, missing-data handling,
# tolerance arithmetic) is already fully unit-tested in tests/test_rules.py,
# against synthetic PeriodFacts with no DB involved. What's tested here is
# the glue this session added: grouping real Fact/Period rows into those
# PeriodFacts buckets, period-on-period pairing, run_id provenance, and
# idempotent re-writing of ValidationResult.


@pytest.fixture
def seeded(session: Session) -> Session:
    seed_concepts(session)
    session.commit()
    return session


def _make_company_and_document(session: Session, tmp_path: Path, spec, filename: str) -> tuple[Company, Document]:
    company = Company(stock_code="9999", name="Test Berhad")
    session.add(company)
    session.flush()

    pdf_path = build_statement_pdf(tmp_path / filename, spec)
    document = Document(
        company_id=company.id,
        source=DocSource.UPLOAD,
        original_filename=filename,
        file_sha256=f"sha-{filename}",
        file_size=pdf_path.stat().st_size,
        storage_path=str(pdf_path),
    )
    session.add(document)
    session.flush()
    return company, document


def test_a_balanced_balance_sheet_produces_no_failures(seeded: Session, tmp_path: Path) -> None:
    company, document = _make_company_and_document(seeded, tmp_path, ANNUAL_BALANCE_SHEET, "bs.pdf")
    write_facts_for_document(seeded, company, document.id, Path(document.storage_path))

    result = validate_company(seeded, company)

    assert result.rules_run > 0
    assert result.rules_failed == 0
    assert result.failures == []


def test_an_unbalanced_balance_sheet_is_caught_for_the_wrong_year_only(
    seeded: Session, tmp_path: Path
) -> None:
    company, document = _make_company_and_document(
        seeded, tmp_path, ANNUAL_BALANCE_SHEET_UNBALANCED, "bs.pdf"
    )
    write_facts_for_document(seeded, company, document.id, Path(document.storage_path))

    result = validate_company(seeded, company)

    failed_keys = {f.rule_key for f in result.failures}
    assert "bs_balances" in failed_keys
    bs_balances = next(f for f in result.failures if f.rule_key == "bs_balances")
    assert bs_balances.expected == 465_600_000
    assert bs_balances.actual == 999_999_000

    # The prior year (2023) still balances - this is a per-period check, not
    # a company-wide pass/fail, so a passed bs_balances result must also
    # exist alongside the failed one (for the other period).
    bs_balances_outcomes = seeded.execute(
        select(ValidationResult).where(ValidationResult.rule_key == "bs_balances")
    ).scalars().all()
    assert len(bs_balances_outcomes) == 2
    assert {r.passed for r in bs_balances_outcomes} == {True, False}


def test_bs_footing_passes_when_both_sides_are_equally_wrong(seeded: Session, tmp_path: Path) -> None:
    # TOTAL ASSETS and TOTAL EQUITY AND LIABILITIES were both set to the same
    # (wrong) number in the fixture - bs_footing only checks the two sides
    # agree with each other, so it correctly passes here; bs_balances is the
    # rule that catches the underlying error.
    company, document = _make_company_and_document(
        seeded, tmp_path, ANNUAL_BALANCE_SHEET_UNBALANCED, "bs.pdf"
    )
    write_facts_for_document(seeded, company, document.id, Path(document.storage_path))

    result = validate_company(seeded, company)
    assert "bs_footing" not in {f.rule_key for f in result.failures}


def test_magnitude_sanity_catches_a_1000x_scale_error_between_the_two_years(
    seeded: Session, tmp_path: Path
) -> None:
    company, document = _make_company_and_document(
        seeded, tmp_path, ANNUAL_INCOME_STATEMENT_WITH_SCALE_ERROR, "is.pdf"
    )
    write_facts_for_document(seeded, company, document.id, Path(document.storage_path))

    result = validate_company(seeded, company)

    revenue_failure = next(f for f in result.failures if f.rule_key == "magnitude_sanity" and "is.revenue" in f.detail)
    assert "1000x scale error" in revenue_failure.detail
    assert revenue_failure.severity == 95


def test_validation_results_carry_a_real_run_id(seeded: Session, tmp_path: Path) -> None:
    company, document = _make_company_and_document(seeded, tmp_path, ANNUAL_BALANCE_SHEET, "bs.pdf")
    write_facts_for_document(seeded, company, document.id, Path(document.storage_path))

    validate_company(seeded, company)

    rows = seeded.execute(select(ValidationResult)).scalars().all()
    assert rows
    assert all(r.run_id is not None for r in rows)


def test_rerunning_validate_replaces_results_instead_of_duplicating(
    seeded: Session, tmp_path: Path
) -> None:
    company, document = _make_company_and_document(
        seeded, tmp_path, ANNUAL_BALANCE_SHEET_UNBALANCED, "bs.pdf"
    )
    write_facts_for_document(seeded, company, document.id, Path(document.storage_path))

    first = validate_company(seeded, company)
    second = validate_company(seeded, company)

    assert second.rules_run == first.rules_run
    total = seeded.scalar(select(func.count(ValidationResult.id)))
    assert total == first.rules_run


def test_no_facts_means_no_rules_run(seeded: Session) -> None:
    company = Company(stock_code="0001", name="No Documents Berhad")
    seeded.add(company)
    seeded.flush()

    result = validate_company(seeded, company)
    assert result.rules_run == 0
    assert result.failures == []
