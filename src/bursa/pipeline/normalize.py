"""Write `Fact` rows from a document's deterministic statement extraction.

Companion to `bursa.extract.statement_extract`: that module locates and
parses each statement, this one turns the result into `Period`/`Fact` rows -
still with no LLM call and no API credentials, for the shape this project has
actually been working with all session (an annual report, two columns:
current year and prior-year comparative).

What makes this possible without the model: nearly every real filing states
its own exact period end verbatim in the statement's own subtitle - "for the
financial year ended 31 December 2024", "as at 30 September 2024", "Three
Months Ended 31 March 2022" - which `bursa.normalize.periods` reads
directly: `parse_stated_period_end` for the end date,
`parse_statement_duration_months` for how many months it covers. An annual
statement's parsed end-date month doubles as that filing's fy_end_month for
`period_bounds` - and backfills `Company.fy_end_month` when it was unset -
but an *interim* statement's end-date month is a quarter/half-year end, not
the real fiscal year end, so it's never used that way
(`resolve_duration_period_type`; see `_write_statement_facts`). A company
whose only ingested documents so far are interim ones has every one of them
skipped rather than guessed, until either an annual statement is processed
or `Company.fy_end_month` is set directly.

What this deliberately does NOT attempt, consistent with the rest of this
pipeline's "report an honest miss, never guess" rule:

* a statement whose header states a duration this project has no rule for
  (a 1/2/4/5/7/8/10/11-month transition-period filing, or a garbled OCR
  duration) - `resolve_duration_period_type` returns `None`, skipped;
* a quarterly filing's "individual quarter" / "cumulative YTD" column pair
  presented *side by side in one table* under the same stated duration and
  year - `_resolve_columns`'s collision guard still can't tell those apart
  (both resolve to the same `Period`), so the second one is skipped rather
  than silently merged into the first;
* concept mapping beyond the seed synonym table - a row with no `concept_key`
  (`bursa.extract.statement_extract` already tried) is skipped, not sent to
  the LLM - see `bursa.mapping.llm_mapper.resolve_table` for that path, which
  needs an API key this environment doesn't have;
* `RawRow`/`StatementTable` provenance rows - `Fact.source_raw_row_id` is left
  null, since the layout rows this would point at are never persisted by the
  deterministic extraction path. `Fact.reported_in_document_id` and `run_id`
  still carry real provenance.

Every document re-extraction cleans up after its *own* previous run, not
just upserts on top of it - a documented, previously-just-flagged gap, hit
for real this project's own session: a quarterly-period classification fix
left an old, wrongly-typed `Fact` sitting right alongside a new, correctly-
typed one for the same real figure, because nothing had ever deleted a
`Fact` whose `(concept_key, period, basis)` the current extraction stopped
producing. `write_facts_for_document`/`write_facts_for_company` now track
every fact id touched while writing a document's facts this run, then
delete whatever existing fact for that document *wasn't* touched - and,
since no fact can still reference it once that cleanup runs, delete every
other `ExtractionRun` row for that document too, whose `ondelete=CASCADE`
relationships take its own stale `RawRow`/`ValidationResult` rows with it
for free. Closes the other half of the same gap described in
`bursa.pipeline.validate.validate_company`'s own docstring, which can only
ever clean up a `ValidationResult` tied to a run some *current* fact still
references - exactly the run-ids this now stops orphaning in the first
place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from bursa.db.enums import Basis, Continuity, PeriodType, RunStatus, Statement
from bursa.db.models import Company, Concept, Document, ExtractionRun, Fact, Period
from bursa.extract.statement_extract import ColumnInfo, StatementExtraction, extract_statements
from bursa.normalize.numbers import parse_number, to_base_units
from bursa.normalize.periods import (
    PeriodBounds,
    parse_dotted_date,
    parse_stated_period_end,
    parse_statement_duration_months,
    is_interim_header,
    period_bounds,
    quarterly_column_durations,
    resolve_duration_period_type,
    same_month_day,
)
from bursa.storage import materialize

# Below this many pages a document is almost certainly a standalone
# chairman/MD statement, not a full annual report - same floor
# `extract_statements_cmd` uses.
MIN_CANDIDATE_PAGES = 10

_COMPANY_BASIS = re.compile(r"\bcompany\b", re.IGNORECASE)
_NOTE_WORD = re.compile(r"\bnotes?\b", re.IGNORECASE)


@dataclass
class FactWriteResult:
    facts_written: int = 0
    facts_updated: int = 0
    facts_deleted: int = 0
    stale_runs_deleted: int = 0
    periods_created: int = 0
    documents_used: set[int] = field(default_factory=set)
    skipped_statements: dict[Statement, str] = field(default_factory=dict)
    skipped_columns: list[str] = field(default_factory=list)
    derived_fy_end_months: list[int] = field(default_factory=list)


def _old_fact_ids_for_document(session: Session, document_id: int) -> set[int]:
    return set(session.scalars(select(Fact.id).where(Fact.reported_in_document_id == document_id)))


def _clean_up_stale_facts_and_runs(
    session: Session,
    document_id: int,
    keep_run_id: int,
    old_fact_ids: set[int],
    touched_fact_ids: set[int],
    result: FactWriteResult,
) -> None:
    """Delete this document's own facts that existed before this run but
    weren't touched by it - a concept/period the current extraction no
    longer produces - then every other `ExtractionRun` row for this
    document, now safely orphaned of every fact (its `ondelete=CASCADE`
    relationships take any leftover `RawRow`/`ValidationResult` with it).

    Scoped to one document's own fact ids, never a company- or run-wide
    sweep - a company's other documents' facts are untouched, and so is
    anything legitimately re-affirmed by this exact run."""
    stale_fact_ids = old_fact_ids - touched_fact_ids
    if stale_fact_ids:
        session.execute(delete(Fact).where(Fact.id.in_(stale_fact_ids)))
        result.facts_deleted += len(stale_fact_ids)

    deleted_runs = session.execute(
        delete(ExtractionRun).where(
            ExtractionRun.document_id == document_id, ExtractionRun.id != keep_run_id
        )
    )
    result.stale_runs_deleted += deleted_runs.rowcount or 0


def write_facts_for_document(
    session: Session, company: Company, document_id: int, pdf_path: Path
) -> FactWriteResult:
    """Extract and write facts from exactly one PDF. The simple case - for
    the usual multi-document company, prefer `write_facts_for_company`."""
    extraction = extract_statements(session, document_id, pdf_path, company_id=company.id)
    result = FactWriteResult()
    concepts = {c.concept_key: c for c in session.scalars(select(Concept))}

    if extraction.statements:
        old_fact_ids = _old_fact_ids_for_document(session, document_id)
        run_id = _create_run(session, document_id)
        result.documents_used.add(document_id)
        touched_fact_ids: set[int] = set()
        for statement, extracted in extraction.statements.items():
            _write_statement_facts(
                session, company, document_id, run_id, concepts, statement, extracted, result,
                touched_fact_ids,
            )
        _clean_up_stale_facts_and_runs(
            session, document_id, run_id, old_fact_ids, touched_fact_ids, result
        )

    for statement, why in extraction.skipped_pages.items():
        result.skipped_statements[statement] = why

    _backfill_fy_end_month(company, result)
    return result


def write_facts_for_company(session: Session, company: Company) -> FactWriteResult:
    """Mirror `extract_statements_cmd`'s per-document selection: a company's
    statements can come from different documents (Public Bank's audited
    figures live in a separate filing from its 452-page integrated report -
    see that command's docstring), so each statement is extracted from every
    eligible document and kept from whichever scores it highest - per
    *(statement, filing period)*, not per statement alone. That distinction
    matters once a company has more than one year's worth of documents
    ingested (a multi-year backfill): two documents can genuinely compete
    for the *same* year (a narrative volume and its separate financial-
    statements volume), where the higher-scoring one should win: but two
    documents covering *different* years are not competing at all, and both
    must be kept - keying only by `Statement` would silently keep just the
    single best-scoring year's document and discard every other year.
    """
    return write_company_extraction(session, company, extract_company(session, company))


@dataclass
class CompanyExtraction:
    """Phase 1 of `write_facts_for_company`: the best-scoring statement per
    (statement, filing period) across a company's documents. Read-only, and
    picklable, so it can run in a worker process while one process writes."""

    best: dict[tuple[Statement, object], tuple[float, StatementExtraction, int]] = field(
        default_factory=dict
    )
    skipped_statements: dict[Statement, str] = field(default_factory=dict)


def extract_company(session: Session, company: Company) -> CompanyExtraction:
    docs = list(
        session.scalars(
            select(Document)
            .where(Document.company_id == company.id)
            .order_by(Document.page_count.desc())
        )
    )
    docs = [
        d for d in docs if (d.page_count or 0) >= MIN_CANDIDATE_PAGES and materialize(d).is_file()
    ]

    out = CompanyExtraction()
    # Keyed by (statement, this candidate's own parsed period end) - an
    # unparseable subtitle gets a unique key instead, so it never competes
    # with anything (`_write_statement_facts` skips it on its own anyway).
    unparsed = 0
    for doc in docs:
        extraction = extract_statements(session, doc.id, materialize(doc), company_id=company.id)
        for statement, extracted in extraction.statements.items():
            instant = statement == Statement.BALANCE_SHEET
            stated = parse_stated_period_end(extracted.header_text, instant=instant)
            if stated is None:
                unparsed += 1
                key: tuple[Statement, object] = (statement, f"unparsed-{unparsed}")
            else:
                key = (statement, stated)
            current = out.best.get(key)
            if current is None or extracted.final_score > current[0]:
                out.best[key] = (extracted.final_score, extracted, doc.id)
        for statement, why in extraction.skipped_pages.items():
            if not any(k[0] == statement for k in out.best):
                out.skipped_statements.setdefault(statement, why)
    return out


def extract_company_by_id(company_id: int) -> CompanyExtraction:
    """Worker-process entry point: own engine/session, read-only."""
    from bursa.db.session import session_scope

    with session_scope() as session:
        return extract_company(session, session.get(Company, company_id))


def write_company_extraction(
    session: Session, company: Company, extracted_company: CompanyExtraction
) -> FactWriteResult:
    """Phase 2 of `write_facts_for_company`: write the selected statements."""
    result = FactWriteResult()
    result.skipped_statements.update(extracted_company.skipped_statements)
    best = extracted_company.best
    if not best:
        return result

    concepts = {c.concept_key: c for c in session.scalars(select(Concept))}
    _reconcile_fy_end_month(company, best, result)

    # Process annual-looking statements first, regardless of the dict's own
    # insertion order: `_write_statement_facts` needs `company.fy_end_month`
    # known before it can safely type-classify an interim statement, and the
    # only thing that can supply it is an annual one. Sorting by page count
    # alone (as `docs` above does) puts it first *in practice*, not by
    # guarantee - a company with only quarterly reports ingested so far, or
    # whose biggest file happens to be an interim one, would otherwise have
    # every interim statement skip for a missing fy_end_month that a
    # same-run annual statement could have supplied, just processed later.
    def _is_annual_looking(item: tuple[tuple[Statement, object], tuple[float, StatementExtraction, int]]) -> int:
        (statement, _), (_, extracted, _) = item
        if statement == Statement.BALANCE_SHEET:
            return 0
        duration = parse_statement_duration_months(extracted.header_text)
        return 0 if duration is None or duration == 12 else 1

    run_ids: dict[int, int] = {}
    old_fact_ids_by_doc: dict[int, set[int]] = {}
    touched_fact_ids: set[int] = set()
    for (statement, _period_key), (_, extracted, document_id) in sorted(best.items(), key=_is_annual_looking):
        if document_id not in run_ids:
            old_fact_ids_by_doc[document_id] = _old_fact_ids_for_document(session, document_id)
            run_ids[document_id] = _create_run(session, document_id)
            result.documents_used.add(document_id)
        _write_statement_facts(
            session, company, document_id, run_ids[document_id], concepts, statement, extracted, result,
            touched_fact_ids,
        )

    for document_id, old_fact_ids in old_fact_ids_by_doc.items():
        _clean_up_stale_facts_and_runs(
            session, document_id, run_ids[document_id], old_fact_ids, touched_fact_ids, result
        )

    _backfill_fy_end_month(company, result)
    return result


def _reconcile_fy_end_month(
    company: Company,
    best: dict[tuple[Statement, object], tuple[float, StatementExtraction, int]],
    result: FactWriteResult,
) -> None:
    """Re-derive the fiscal year end from this company's explicitly 12-month
    statements on every run. It used to be set once, from whichever statement
    came first, and never revisited - one misread (TNB stored August for a
    December year end) then shifted every fiscal year it labelled."""
    annual: list[date] = []
    for (statement, _), (_, extracted, _) in best.items():
        if statement == Statement.BALANCE_SHEET:
            continue
        if parse_statement_duration_months(extracted.header_text) != 12:
            continue
        stated = parse_stated_period_end(extracted.header_text, instant=False)
        if stated is not None:
            annual.append(stated)
    if not annual:
        return
    # Only the latest ~two years vote: a company that changed its year end
    # (S P Setia, October -> December) would otherwise be outvoted by a
    # decade of older reports and have its current years mislabelled.
    latest = max(annual)
    votes: dict[int, int] = {}
    for stated in annual:
        if (latest - stated).days <= 730:
            votes[stated.month] = votes.get(stated.month, 0) + 1
    month, count = max(votes.items(), key=lambda kv: kv[1])
    if count >= 2 and count * 2 > sum(votes.values()) and company.fy_end_month != month:
        if company.fy_end_month is not None:
            result.skipped_columns.append(
                f"fy_end_month corrected {company.fy_end_month} -> {month} "
                f"({count} of {sum(votes.values())} annual statements)"
            )
        company.fy_end_month = month
        result.derived_fy_end_months.append(month)


def _create_run(session: Session, document_id: int) -> int:
    run = ExtractionRun(document_id=document_id, status=RunStatus.SUCCEEDED)
    session.add(run)
    session.flush()
    return run.id


def _backfill_fy_end_month(company: Company, result: FactWriteResult) -> None:
    if company.fy_end_month is None and result.derived_fy_end_months:
        company.fy_end_month = result.derived_fy_end_months[0]


@dataclass
class _ResolvedColumn:
    column: ColumnInfo
    basis: Basis
    bounds: PeriodBounds  # this column's own statement-level bounds (FY or INSTANT)


def _resolve_columns(
    extracted: StatementExtraction,
    instant_statement: bool,
    stated: date,
    result: FactWriteResult,
    *,
    fy_end_month: int,
    duration_months: int | None,
) -> list[_ResolvedColumn]:
    """Assign each column a basis and period bounds, skipping (not guessing)
    a column whose year is missing, whose own header marks it as something
    other than a value column, whose duration this project has no rule for
    yet, or whose resolved (period, basis) collides with an already-resolved
    column - the last of these is what a quarterly filing's "individual
    quarter" / "cumulative" column pair would otherwise do when both carry
    the same stated duration and year (ambiguous as to which period each one
    really is), which would silently merge two different figures into one
    `Period` if not caught here. Basis is part of that collision key on
    purpose - a real "Group" / "Company" column pair legitimately shares the
    same year and must not be treated as a collision (confirmed on a real
    bank filing: Alliance Bank's balance sheet carries Group-2026 and
    Company-2026 side by side).

    ``fy_end_month``/``duration_months`` are resolved once per statement by
    the caller (every column in one statement shares the same stated
    duration and - once known - the same real company fiscal year end), not
    re-derived per column."""
    resolved: list[_ResolvedColumn] = []
    seen_keys: set[tuple] = set()

    # A quarterly report's standard four value columns - current quarter,
    # prior-year quarter, current cumulative, prior cumulative - are typed by
    # position: their own header cells are too bled-into to trust (S P Setia's
    # prior-year quarter column read as 2025), and without this the 3-month
    # column was written as the financial year.
    value_columns = [
        c for c in extracted.columns if not (c.is_note or _NOTE_WORD.search(c.header_text))
    ]
    quarterly_plan: dict[int, tuple[int, int]] = {}
    if not instant_statement and len(value_columns) == 4:
        durations = quarterly_column_durations(extracted.header_text, stated, fy_end_month)
        if durations is not None:
            quarterly_plan = {
                c.col_index: (durations[i], stated.year - (i % 2))
                for i, c in enumerate(value_columns)
            }

    for column in extracted.columns:
        if column.col_index in quarterly_plan:
            months, year = quarterly_plan[column.col_index]
            period_end = same_month_day(year, stated.month, stated.day)
            period_type = resolve_duration_period_type(months, period_end, fy_end_month)
            if period_type is None:
                result.skipped_columns.append(
                    f"col{column.col_index}: a {months}-month quarterly-report column has no "
                    "period-type rule - not guessed"
                )
                continue
            bounds = period_bounds(period_end, period_type, fy_end_month)
            basis = Basis.COMPANY if _COMPANY_BASIS.search(column.header_text) else Basis.CONSOLIDATED
            key = (bounds.period_start, bounds.period_end, bounds.period_type, basis)
            if key not in seen_keys:
                seen_keys.add(key)
                resolved.append(_ResolvedColumn(column=column, basis=basis, bounds=bounds))
            continue

        if column.is_note or _NOTE_WORD.search(column.header_text):
            # A "Note" column's own header carries no year, so `_column_year`
            # falls back to the table-wide header text and still finds one -
            # confirmed real on several bank/utility filings (Alliance Bank,
            # AMMB, Tenaga Nasional): a column headed "Note" (sometimes with
            # stray page-title text bled in, e.g. "STATEMENTS STATEMENT
            # Note") would otherwise resolve to a real period, writing
            # footnote reference numbers as facts.
            result.skipped_columns.append(f"col{column.col_index}: a Note-reference column, not a value column")
            continue

        if column.year is None:
            result.skipped_columns.append(f"col{column.col_index}: no year found in its own header")
            continue

        # A column's own explicit DD.MM.YYYY date wins over the statement's
        # subtitle month/day - a column can legitimately carry a different
        # date of its own (a restated MFRS-transition opening-balance column,
        # confirmed real on Tenaga Nasional's balance sheet: "1.1.2024"
        # alongside the ordinary "31.12.2024" comparative).
        own_date = parse_dotted_date(column.header_text)
        period_end = own_date if own_date is not None else same_month_day(column.year, stated.month, stated.day)

        if instant_statement:
            period_type = PeriodType.INSTANT
        else:
            resolved_type = resolve_duration_period_type(duration_months, period_end, fy_end_month)
            if resolved_type is None:
                result.skipped_columns.append(
                    f"col{column.col_index}: a {duration_months}-month statement duration has no "
                    "period-type rule yet - not guessed"
                )
                continue
            period_type = resolved_type

        bounds = period_bounds(period_end, period_type, fy_end_month)
        if bounds.period_start is not None and bounds.period_start >= bounds.period_end:
            result.skipped_columns.append(
                f"col{column.col_index}: resolved to an empty period "
                f"{bounds.period_start}..{bounds.period_end} - not written"
            )
            continue
        if column.basis is not None:
            basis = column.basis
        else:
            basis = Basis.COMPANY if _COMPANY_BASIS.search(column.header_text) else Basis.CONSOLIDATED
        key = (bounds.period_start, bounds.period_end, bounds.period_type, basis)
        if key in seen_keys:
            result.skipped_columns.append(
                f"col{column.col_index}: resolves to the same period and basis as an earlier column "
                "(ambiguous - likely a quarterly/cumulative column pair, not yet handled)"
            )
            continue
        seen_keys.add(key)

        resolved.append(_ResolvedColumn(column=column, basis=basis, bounds=bounds))

    return resolved


def _write_statement_facts(
    session: Session,
    company: Company,
    document_id: int,
    run_id: int,
    concepts: dict[str, Concept],
    statement: Statement,
    extracted: StatementExtraction,
    result: FactWriteResult,
    touched_fact_ids: set[int],
) -> None:
    if statement == Statement.EQUITY:  # a component x movement matrix, not year columns
        from bursa.pipeline.equity import write_equity_facts

        write_equity_facts(session, company, document_id, run_id, concepts, extracted, result, touched_fact_ids)
        return
    instant_statement = statement == Statement.BALANCE_SHEET
    stated = parse_stated_period_end(extracted.header_text, instant=instant_statement)
    if stated is None:
        result.skipped_columns.append(
            f"{statement.value}: no parseable period-end date in the statement's own header text"
        )
        return

    # The statement's own stated end-date month is only a safe stand-in for
    # the company's real fiscal year end on a genuine annual statement - for
    # an interim one it's a quarter/half-year end instead (the bug this fixes:
    # United Plantations' Q1 report was written as a fake 12-month "financial
    # year" because nothing told period_bounds apart from a real FYE). Once
    # the company's real fy_end_month is known (from the DB, or derived here
    # from an earlier annual statement this run), always prefer it.
    duration_months = None if instant_statement else parse_statement_duration_months(extracted.header_text)
    if company.fy_end_month is not None:
        fy_end_month = company.fy_end_month
    elif is_interim_header(extracted.header_text):
        result.skipped_statements[statement] = (
            "an interim (quarterly) statement, found before this company's real fiscal year end "
            "is known - skipped rather than guessed"
        )
        return
    elif instant_statement or duration_months is None or duration_months == 12:
        fy_end_month = stated.month
        company.fy_end_month = fy_end_month
        result.derived_fy_end_months.append(fy_end_month)
    else:
        result.skipped_statements[statement] = (
            f"a {duration_months}-month interim statement, found before this company's real "
            "fiscal year end is known - skipped rather than guessed (needs an annual statement "
            "processed first, or Company.fy_end_month set directly)"
        )
        return

    columns = _resolve_columns(
        extracted, instant_statement, stated, result, fy_end_month=fy_end_month, duration_months=duration_months
    )
    if not columns:
        return

    for row in extracted.rows:
        if row.concept_key is None:
            continue
        concept = concepts.get(row.concept_key)
        if concept is None:
            continue

        for resolved in columns:
            printed = row.values.get(resolved.column.col_index)
            if printed is None:
                continue
            parsed = parse_number(printed)
            if parsed is None:
                continue  # explicit nil/dash, or not a figure - nothing to write

            bounds = resolved.bounds
            if concept.is_instant and not instant_statement:
                bounds = _cf_boundary_bounds(row.concept_key, bounds, stated.month)
                if bounds is None:
                    continue  # an unanticipated instant concept on a duration statement

            value = to_base_units(parsed, extracted.scale.multiplier, concept.is_per_share)
            period = _get_or_create_period(session, company.id, bounds, result)
            fact_id = _upsert_fact(
                session,
                company_id=company.id,
                concept_key=row.concept_key,
                period_id=period.id,
                basis=resolved.basis,
                col_index=resolved.column.col_index,
                value=value,
                scale_multiplier=extracted.scale.multiplier,
                currency=extracted.scale.currency,
                printed=printed,
                document_id=document_id,
                run_id=run_id,
                result=result,
            )
            touched_fact_ids.add(fact_id)

    if statement == Statement.INCOME_STATEMENT:
        # Weighted share counts live in the EPS note, not on the face; anchored
        # to the periods the face facts above were just written to.
        from bursa.extract.eps_note import write_weighted_shares_facts

        touched_fact_ids |= write_weighted_shares_facts(
            session, company.id, document_id, run_id, extracted, result
        )


def _cf_boundary_bounds(concept_key: str, fy_bounds: PeriodBounds, fy_end_month: int) -> PeriodBounds | None:
    """Cash at beginning/end of the cash flow statement are instants at the
    boundaries of this column's own fiscal year, not a fresh "as at" date of
    their own - so reuse the FY bounds already resolved for this column
    rather than guessing a date for them independently."""
    if concept_key == "cf.cash_beginning":
        if fy_bounds.period_start is None:
            return None
        return period_bounds(fy_bounds.period_start, PeriodType.INSTANT, fy_end_month)
    if concept_key == "cf.cash_end":
        return period_bounds(fy_bounds.period_end, PeriodType.INSTANT, fy_end_month)
    return None


def _get_or_create_period(session: Session, company_id: int, bounds: PeriodBounds, result: FactWriteResult) -> Period:
    existing = session.execute(
        select(Period).where(
            Period.company_id == company_id,
            Period.period_start == bounds.period_start,
            Period.period_end == bounds.period_end,
            Period.period_type == bounds.period_type,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    period = Period(
        company_id=company_id,
        period_start=bounds.period_start,
        period_end=bounds.period_end,
        period_type=bounds.period_type,
        fiscal_year=bounds.fiscal_year,
    )
    session.add(period)
    session.flush()
    result.periods_created += 1
    return period


def _upsert_fact(
    session: Session,
    *,
    company_id: int,
    concept_key: str,
    period_id: int,
    basis: Basis,
    col_index: int,
    value: Decimal,
    scale_multiplier: int,
    currency: str,
    printed: str,
    document_id: int,
    run_id: int,
    result: FactWriteResult,
) -> int:
    existing = session.execute(
        select(Fact).where(
            Fact.company_id == company_id,
            Fact.concept_key == concept_key,
            Fact.period_id == period_id,
            Fact.basis == basis,
            Fact.continuity == Continuity.TOTAL,
            Fact.reported_in_document_id == document_id,
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.value = value
        existing.currency = currency
        existing.value_as_printed = printed
        existing.scale_multiplier = scale_multiplier
        existing.source_col_index = col_index
        existing.run_id = run_id
        result.facts_updated += 1
        return existing.id

    fact = Fact(
        company_id=company_id,
        concept_key=concept_key,
        period_id=period_id,
        value=value,
        currency=currency,
        value_as_printed=printed,
        scale_multiplier=scale_multiplier,
        basis=basis,
        continuity=Continuity.TOTAL,
        reported_in_document_id=document_id,
        source_col_index=col_index,
        run_id=run_id,
    )
    session.add(fact)
    session.flush()
    result.facts_written += 1
    return fact.id
