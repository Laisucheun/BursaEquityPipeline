from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bursa.db.enums import Basis, DocSource, PeriodType
from bursa.db.models import Company, Document, ExtractionRun, Fact, Period, ValidationResult
from bursa.mapping.synonyms import seed_concepts
from bursa.pipeline.normalize import write_facts_for_company, write_facts_for_document
from tests.fixtures.synthetic import (
    ANNUAL_BALANCE_SHEET,
    ANNUAL_BALANCE_SHEET_TWO_YEARS_EARLIER,
    ANNUAL_BALANCE_SHEET_WITH_NOTE_AND_BASIS_COLUMNS,
    ANNUAL_BALANCE_SHEET_WITH_RESTATED_OPENING_COLUMN,
    ANNUAL_CASH_FLOW_STATEMENT,
    QUARTERLY_INCOME_STATEMENT,
    QUARTERLY_INCOME_STATEMENT_Q1_ONLY,
    build_statement_pdf,
)

# Page selection and row/concept extraction are tested in test_page_scoring.py
# and test_statement_extract.py. What's tested here is what normalize.py does
# with an already-extracted statement: period dates read off the subtitle,
# instant vs duration bounds, scale/sign normalisation, and the upsert.


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


# --------------------------------------------------------------------------
# Balance sheet: instant facts, two "as at" columns a year apart.
# --------------------------------------------------------------------------


def test_writes_instant_facts_for_both_columns_of_an_annual_balance_sheet(
    seeded: Session, tmp_path: Path
) -> None:
    company, document = _make_company_and_document(seeded, tmp_path, ANNUAL_BALANCE_SHEET, "bs.pdf")

    result = write_facts_for_document(seeded, company, document.id, Path(document.storage_path))

    # The fixture is a one-page, balance-sheet-only PDF, so the other two
    # statements are correctly reported as not found - not a normalize bug.
    assert result.facts_written > 0

    rows = seeded.execute(
        select(Fact, Period)
        .join(Period, Fact.period_id == Period.id)
        .where(Fact.concept_key == "bs.total_assets")
        .order_by(Period.period_end)
    ).all()
    assert len(rows) == 2

    prior_fact, prior_period = rows[0]
    current_fact, current_period = rows[1]

    assert prior_period.period_type == PeriodType.INSTANT
    assert prior_period.period_start is None
    assert prior_period.period_end == date(2023, 12, 31)
    assert prior_fact.value == 438_860_000
    assert prior_fact.value_as_printed == "438,860"
    assert prior_fact.basis == Basis.CONSOLIDATED
    assert prior_fact.reported_in_document_id == document.id

    assert current_period.period_end == date(2024, 12, 31)
    assert current_fact.value == 465_600_000

    # Read off the statement's own subtitle, not pre-populated - and left in
    # place on the company since it started unset.
    assert company.fy_end_month == 12


# --------------------------------------------------------------------------
# Cash flow: duration facts, plus the "cash at beginning/end of year"
# boundary rows that are instants within a duration statement.
# --------------------------------------------------------------------------


def test_writes_cash_flow_duration_and_boundary_instant_facts(seeded: Session, tmp_path: Path) -> None:
    company, document = _make_company_and_document(
        seeded, tmp_path, ANNUAL_CASH_FLOW_STATEMENT, "cf.pdf"
    )

    result = write_facts_for_document(seeded, company, document.id, Path(document.storage_path))

    # One-page, cash-flow-only PDF - the other two statements are correctly
    # reported as not found.
    assert result.facts_written > 0

    op_fact, op_period = seeded.execute(
        select(Fact, Period)
        .join(Period, Fact.period_id == Period.id)
        .where(Fact.concept_key == "cf.net_operating", Period.fiscal_year == 2024)
    ).one()
    assert op_period.period_type == PeriodType.FY
    assert op_period.period_start == date(2024, 1, 1)
    assert op_period.period_end == date(2024, 12, 31)
    assert op_fact.value == 52_340_000

    # Cash at end of FY2024 is an instant at the FY's own period end - not a
    # second FY-typed row duplicating the same figure.
    end_fact, end_period = seeded.execute(
        select(Fact, Period)
        .join(Period, Fact.period_id == Period.id)
        .where(Fact.concept_key == "cf.cash_end", Period.fiscal_year == 2024)
    ).one()
    assert end_period.period_type == PeriodType.INSTANT
    assert end_period.period_end == date(2024, 12, 31)
    assert end_fact.value == 59_840_000

    # Cash at beginning of FY2024 is the start of that same fiscal year (1
    # Jan 2024, this schema's own FY-start convention - see
    # `periods.fiscal_year_start`), not a fresh "as at" date of its own.
    beginning_fact, beginning_period = seeded.execute(
        select(Fact, Period)
        .join(Period, Fact.period_id == Period.id)
        .where(Fact.concept_key == "cf.cash_beginning", Period.fiscal_year == 2024)
    ).one()
    assert beginning_period.period_type == PeriodType.INSTANT
    assert beginning_period.period_end == date(2024, 1, 1)
    assert beginning_fact.value == 38_220_000


# --------------------------------------------------------------------------
# Re-running the same document is an upsert, not a duplicate.
# --------------------------------------------------------------------------


def test_two_different_years_documents_both_contribute_facts(
    seeded: Session, tmp_path: Path
) -> None:
    """A multi-year backfill: two documents for the same company, genuinely
    different filing years (not two documents competing for the same year,
    like Public Bank's narrative/financial-statements pair). Both must
    contribute facts - keying the per-document "best scoring" selection by
    statement alone would keep only one year and silently drop the other."""
    company, document = _make_company_and_document(seeded, tmp_path, ANNUAL_BALANCE_SHEET, "bs_2024.pdf")
    pdf2 = build_statement_pdf(tmp_path / "bs_2022.pdf", ANNUAL_BALANCE_SHEET_TWO_YEARS_EARLIER)
    document2 = Document(
        company_id=company.id,
        source=DocSource.UPLOAD,
        original_filename="bs_2022.pdf",
        file_sha256="sha-bs_2022.pdf",
        file_size=pdf2.stat().st_size,
        storage_path=str(pdf2),
        page_count=20,
    )
    seeded.add(document2)
    seeded.flush()
    document.page_count = 20  # both must clear write_facts_for_company's MIN_CANDIDATE_PAGES

    result = write_facts_for_company(seeded, company)

    assert result.documents_used == {document.id, document2.id}

    period_ends = seeded.execute(
        select(Period.period_end).join(Fact, Fact.period_id == Period.id)
        .where(Fact.concept_key == "bs.total_assets")
    ).scalars().all()
    assert set(period_ends) == {date(2024, 12, 31), date(2023, 12, 31), date(2022, 12, 31), date(2021, 12, 31)}


def test_rerunning_the_same_document_updates_instead_of_duplicating(
    seeded: Session, tmp_path: Path
) -> None:
    company, document = _make_company_and_document(seeded, tmp_path, ANNUAL_BALANCE_SHEET, "bs.pdf")

    first = write_facts_for_document(seeded, company, document.id, Path(document.storage_path))
    second = write_facts_for_document(seeded, company, document.id, Path(document.storage_path))

    assert second.facts_written == 0
    assert second.facts_updated == first.facts_written

    total = seeded.execute(select(func.count(Fact.id))).scalar_one()
    assert total == first.facts_written


# --------------------------------------------------------------------------
# Stale facts/runs on re-extraction: a real gap hit this session (a
# quarterly-period classification fix left an old, wrongly-typed `Fact`
# sitting right alongside a new, correctly-typed one for the same real
# figure, since `_upsert_fact` is a pure upsert keyed by the *current*
# extraction's own period/concept - nothing ever deleted a fact whose
# combination the current extraction stops producing at all).
# --------------------------------------------------------------------------


def test_re_extracting_a_document_with_different_results_drops_the_old_facts(
    seeded: Session, tmp_path: Path
) -> None:
    """Simulates exactly the real incident: the same document, re-extracted
    (here, with a deliberately different fixture standing in for "a bugfix
    changed what this document resolves to") - the periods the second run
    doesn't reproduce must be gone, not left sitting alongside the new
    ones."""
    company, document = _make_company_and_document(seeded, tmp_path, ANNUAL_BALANCE_SHEET, "bs.pdf")
    old_pdf_path = Path(document.storage_path)
    new_pdf_path = build_statement_pdf(tmp_path / "bs_v2.pdf", ANNUAL_BALANCE_SHEET_TWO_YEARS_EARLIER)

    first = write_facts_for_document(seeded, company, document.id, old_pdf_path)
    assert first.facts_written > 0

    # A ValidationResult manually attached to the first run - standing in
    # for what `bursa validate facts` would have written against it - to
    # prove the *cascade* cleanup works, not just the Fact deletion.
    first_run_id = seeded.execute(
        select(Fact.run_id).where(Fact.reported_in_document_id == document.id).limit(1)
    ).scalar_one()
    seeded.add(ValidationResult(run_id=first_run_id, rule_key="bs_footing", passed=True))
    seeded.flush()

    second = write_facts_for_document(seeded, company, document.id, new_pdf_path)

    assert second.facts_deleted == first.facts_written
    assert second.stale_runs_deleted == 1

    remaining = seeded.execute(
        select(Fact, Period)
        .join(Period, Fact.period_id == Period.id)
        .where(Fact.reported_in_document_id == document.id)
    ).all()
    assert remaining  # the second run's own facts are still there
    period_ends = {period.period_end for _fact, period in remaining}
    # Only the second extraction's own periods (2022/2021) survive - the
    # first's (2024/2023) are gone entirely, not left stale alongside them.
    assert period_ends == {date(2022, 12, 31), date(2021, 12, 31)}

    runs = seeded.execute(
        select(ExtractionRun).where(ExtractionRun.document_id == document.id)
    ).scalars().all()
    assert len(runs) == 1  # the stale first run was deleted, not left orphaned

    orphaned_results = seeded.execute(
        select(ValidationResult).where(ValidationResult.run_id == first_run_id)
    ).scalars().all()
    assert orphaned_results == []  # cascade-deleted along with the stale run


def test_re_extracting_an_unchanged_document_deletes_no_facts_but_cleans_up_the_old_run(
    seeded: Session, tmp_path: Path
) -> None:
    """The common case - nothing actually changed - must lose zero facts
    (every one gets re-affirmed, touching it rather than orphaning it), but
    a fresh `ExtractionRun` is still created every call (existing,
    documented behaviour - not this fix's concern), so the *previous* run
    is still legitimately cleaned up even though no fact did."""
    company, document = _make_company_and_document(seeded, tmp_path, ANNUAL_BALANCE_SHEET, "bs.pdf")
    pdf_path = Path(document.storage_path)

    first = write_facts_for_document(seeded, company, document.id, pdf_path)
    second = write_facts_for_document(seeded, company, document.id, pdf_path)

    assert second.facts_deleted == 0
    assert second.facts_updated == first.facts_written
    assert second.stale_runs_deleted == 1
    runs = seeded.execute(
        select(ExtractionRun).where(ExtractionRun.document_id == document.id)
    ).scalars().all()
    assert len(runs) == 1


# --------------------------------------------------------------------------
# A quarterly report's "individual quarter" and "cumulative" columns share
# printed years. They are typed by position (Bursa's standard layout:
# current quarter, prior-year quarter, current cumulative, prior cumulative)
# - S P Setia's 3-month column used to be written as its financial year.
# --------------------------------------------------------------------------


def test_quarterly_report_is_skipped_while_fy_end_is_unknown(
    seeded: Session, tmp_path: Path
) -> None:
    company, document = _make_company_and_document(
        seeded, tmp_path, QUARTERLY_INCOME_STATEMENT, "is.pdf"
    )

    result = write_facts_for_document(seeded, company, document.id, Path(document.storage_path))

    assert result.facts_written == 0
    assert company.fy_end_month is None  # a Q3 end month is never taken as the year end


def test_quarterly_report_columns_are_typed_by_position(seeded: Session, tmp_path: Path) -> None:
    company, document = _make_company_and_document(
        seeded, tmp_path, QUARTERLY_INCOME_STATEMENT, "is.pdf"
    )
    company.fy_end_month = 12

    write_facts_for_document(seeded, company, document.id, Path(document.storage_path))

    revenue = {
        (p.period_type, p.period_start, p.period_end): f.value_as_printed
        for f, p in seeded.execute(
            select(Fact, Period).join(Period, Fact.period_id == Period.id)
            .where(Fact.concept_key == "is.revenue")
        ).all()
    }
    assert revenue == {
        (PeriodType.Q3, date(2024, 7, 1), date(2024, 9, 30)): "125,430",
        (PeriodType.Q3, date(2023, 7, 1), date(2023, 9, 30)): "110,220",
        (PeriodType.YTD, date(2024, 1, 1), date(2024, 9, 30)): "362,890",
        (PeriodType.YTD, date(2023, 1, 1), date(2023, 9, 30)): "318,455",
    }


# --------------------------------------------------------------------------
# A real 1000x-adjacent bug, fixed 2026-10-02: a quarterly statement's own
# stated end-date month (e.g. March) is a quarter end, not the company's real
# fiscal year end - using it as one silently wrote a fake 12-month "FY"
# period out of a single quarter's figures (confirmed real on United
# Plantations). Fixed by reading the statement's own "Three Months Ended"
# duration text instead of guessing FY always.
# --------------------------------------------------------------------------


def test_skips_a_quarterly_statement_rather_than_guessing_fy_end_month(
    seeded: Session, tmp_path: Path
) -> None:
    """With no annual statement processed yet and no `Company.fy_end_month`
    set, this company's real fiscal year end is unknowable from a lone Q1
    report alone - must be skipped, never silently written as a fake FY."""
    company, document = _make_company_and_document(
        seeded, tmp_path, QUARTERLY_INCOME_STATEMENT_Q1_ONLY, "q1.pdf"
    )
    assert company.fy_end_month is None

    result = write_facts_for_document(seeded, company, document.id, Path(document.storage_path))

    assert result.facts_written == 0
    assert any("fiscal year end is known" in msg for msg in result.skipped_statements.values())
    assert company.fy_end_month is None


def test_a_quarterly_statement_resolves_to_its_real_quarter_once_fy_end_month_is_known(
    seeded: Session, tmp_path: Path
) -> None:
    company, document = _make_company_and_document(
        seeded, tmp_path, QUARTERLY_INCOME_STATEMENT_Q1_ONLY, "q1.pdf"
    )
    company.fy_end_month = 12  # a real December year end, known in advance

    result = write_facts_for_document(seeded, company, document.id, Path(document.storage_path))

    assert result.facts_written > 0
    revenue_facts = seeded.execute(
        select(Fact, Period)
        .join(Period, Fact.period_id == Period.id)
        .where(Fact.concept_key == "is.revenue")
        .order_by(Period.period_end)
    ).all()
    assert len(revenue_facts) == 2

    prior_fact, prior_period = revenue_facts[0]
    current_fact, current_period = revenue_facts[1]

    # A real quarter, correctly bounded (1 Jan - 31 Mar) - not a fake 12-month
    # "financial year" built out of a 3-month statement.
    assert current_period.period_type == PeriodType.Q1
    assert current_period.period_start == date(2022, 1, 1)
    assert current_period.period_end == date(2022, 3, 31)
    assert current_fact.value == 642_908_000
    assert current_fact.value_as_printed == "642,908"

    assert prior_period.period_type == PeriodType.Q1
    assert prior_period.period_start == date(2021, 1, 1)
    assert prior_period.period_end == date(2021, 3, 31)
    assert prior_fact.value == 399_654_000

    # The statement's own stated month (March) must never overwrite the real,
    # already-known fiscal year end (December).
    assert company.fy_end_month == 12


# --------------------------------------------------------------------------
# A bank-style "Note | Group 2024 | Group 2023 | Company 2024 | Company
# 2023" layout - confirmed real on Alliance Bank's and AMMB's filings, where
# the Note column's own header carries no year and the table-wide fallback
# supplied one anyway, turning footnote reference numbers into bogus facts.
# --------------------------------------------------------------------------


def test_a_note_reference_column_is_skipped_not_written_as_a_fact(seeded: Session, tmp_path: Path) -> None:
    company, document = _make_company_and_document(
        seeded, tmp_path, ANNUAL_BALANCE_SHEET_WITH_NOTE_AND_BASIS_COLUMNS, "bs_note.pdf"
    )

    result = write_facts_for_document(seeded, company, document.id, Path(document.storage_path))

    assert any("Note-reference column" in msg for msg in result.skipped_columns)

    ppe_facts = seeded.execute(select(Fact).where(Fact.concept_key == "bs.ppe")).scalars().all()
    # Four real value columns (Group/Company x 2024/2023) - the Note column's
    # own value ("16", a footnote reference, not a figure) must not have
    # produced a fifth fact.
    assert len(ppe_facts) == 4
    assert all(f.value_as_printed != "16" for f in ppe_facts)


def test_group_and_company_columns_for_the_same_year_are_not_a_collision(
    seeded: Session, tmp_path: Path
) -> None:
    company, document = _make_company_and_document(
        seeded, tmp_path, ANNUAL_BALANCE_SHEET_WITH_NOTE_AND_BASIS_COLUMNS, "bs_basis.pdf"
    )

    result = write_facts_for_document(seeded, company, document.id, Path(document.storage_path))

    assert not any("ambiguous" in msg for msg in result.skipped_columns)

    rows = seeded.execute(
        select(Fact.basis, Fact.value_as_printed)
        .join(Period, Fact.period_id == Period.id)
        .where(Fact.concept_key == "bs.total_assets", Period.period_end == date(2024, 12, 31))
    ).all()
    assert {(Basis.CONSOLIDATED, "465,600"), (Basis.COMPANY, "320,500")} == set(rows)


# --------------------------------------------------------------------------
# A restated MFRS-transition opening-balance column carries its own date,
# different from the statement's own subtitle date - confirmed real on
# Tenaga Nasional's balance sheet. A Note column with stray page-title text
# bled into its header ("STATEMENTS STATEMENT Note") must still be caught.
# --------------------------------------------------------------------------


def test_a_column_s_own_dotted_date_overrides_the_statement_subtitle(
    seeded: Session, tmp_path: Path
) -> None:
    company, document = _make_company_and_document(
        seeded, tmp_path, ANNUAL_BALANCE_SHEET_WITH_RESTATED_OPENING_COLUMN, "bs_restated.pdf"
    )

    result = write_facts_for_document(seeded, company, document.id, Path(document.storage_path))

    assert any("Note-reference column" in msg for msg in result.skipped_columns)

    period_ends = seeded.execute(
        select(Period.period_end)
        .join(Fact, Fact.period_id == Period.id)
        .where(Fact.concept_key == "bs.total_assets")
    ).scalars().all()
    # Current year end, restated comparative year end, and the restated
    # opening-balance date a year further back - three distinct dates, not
    # the restated opening balance mislabelled as the comparative year end.
    assert set(period_ends) == {date(2024, 12, 31), date(2023, 12, 31), date(2023, 1, 1)}
