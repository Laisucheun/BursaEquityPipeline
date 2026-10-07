"""EPS on the income statement face, and the EPS note's weighted share count."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from bursa.db.enums import DocSource, Statement
from bursa.db.models import Company, Document, Fact
from bursa.extract.eps_note import extract_eps_note, write_weighted_shares_facts
from bursa.extract.statement_extract import _eps_row, _EpsBlock, extract_statements
from bursa.mapping.synonyms import seed_concepts
from bursa.pipeline.normalize import write_facts_for_document
from tests.fixtures.synthetic import Row, StatementSpec, build_statement_pdf


@pytest.fixture
def seeded(session: Session) -> Session:
    seed_concepts(session)
    session.commit()
    return session


def _income_statement(tail: list[Row]) -> StatementSpec:
    return StatementSpec(
        title="STATEMENTS OF PROFIT OR LOSS AND OTHER COMPREHENSIVE INCOME",
        subtitle="For the financial year ended 31 December 2024 (RM'000)",
        column_headers=[["Group", "Group"], ["2024", "2023"]],
        rows=[
            Row("Revenue", ["125,430", "110,220"]),
            Row("Cost of sales", ["(92,318)", "(83,655)"]),
            Row("Gross profit", ["33,112", "26,565"], bold=True),
            Row("Profit before taxation", ["13,802", "8,130"], bold=True),
            Row("Income tax expense", ["(3,450)", "(2,030)"]),
            Row("Profit for the financial year", ["10,352", "6,100"], bold=True),
            Row("Profit attributable to:", []),
            Row("Owners of the Company", ["9,845", "5,820"], indent=1),
            Row("Non-controlling interests", ["507", "280"], indent=1),
            *tail,
        ],
        column_x=[420.0, 540.0],
    )


def _is_rows(session: Session, pdf: Path):  # type: ignore[no-untyped-def]
    return extract_statements(session, document_id=1, pdf_path=pdf).statements[Statement.INCOME_STATEMENT].rows


def _by_key(rows, key: str):  # type: ignore[no-untyped-def]
    return [r for r in rows if r.concept_key == key]


# --------------------------------------------------------------------------
# Face EPS: header + basic/diluted sub-rows
# --------------------------------------------------------------------------


def test_header_then_dash_sub_rows(seeded: Session, tmp_path: Path) -> None:
    spec = _income_statement([
        Row("Earnings per share attributable to owners of the Company (sen):", []),
        Row("- Basic", ["2.45", "1.45"], indent=1),
        Row("- Diluted", ["2.40", "1.44"], indent=1),
    ])
    rows = _is_rows(seeded, build_statement_pdf(tmp_path / "is.pdf", spec))

    [basic] = _by_key(rows, "is.eps_basic")
    [diluted] = _by_key(rows, "is.eps_diluted")
    assert list(basic.values.values()) == ["2.45", "1.45"]
    assert list(diluted.values.values()) == ["2.40", "1.44"]


def test_wrapped_header_and_combined_basic_and_diluted_tail(seeded: Session, tmp_path: Path) -> None:
    spec = _income_statement([
        Row("Basic/diluted earnings per", []),
        Row("share (sen)", ["2.45", "1.45"]),
    ])
    rows = _is_rows(seeded, build_statement_pdf(tmp_path / "is.pdf", spec))

    assert [list(r.values.values()) for r in _by_key(rows, "is.eps_basic")] == [["2.45", "1.45"]]
    assert [list(r.values.values()) for r in _by_key(rows, "is.eps_diluted")] == [["2.45", "1.45"]]


def test_wrapped_header_over_two_lines_then_plain_sub_rows(seeded: Session, tmp_path: Path) -> None:
    spec = _income_statement([
        Row("Earnings per share attributable to", []),
        Row("owners of the parent (sen)", []),
        Row("Basic", ["2.45", "1.45"]),
        Row("Diluted", ["2.40", "1.44"]),
    ])
    rows = _is_rows(seeded, build_statement_pdf(tmp_path / "is.pdf", spec))

    assert len(_by_key(rows, "is.eps_basic")) == 1
    assert _by_key(rows, "is.eps_diluted")[0].values[1] == "1.44"
    # The PAT split before it is untouched.
    assert _by_key(rows, "is.pat_owners")[0].values[0] == "9,845"


def test_a_stray_basic_row_without_an_eps_header_is_never_mapped(seeded: Session, tmp_path: Path) -> None:
    spec = _income_statement([
        Row("Other comprehensive income", ["420", "150"]),
        Row("Basic", ["12", "15"]),
        Row("Diluted", ["11", "14"]),
    ])
    rows = _is_rows(seeded, build_statement_pdf(tmp_path / "is.pdf", spec))

    assert not _by_key(rows, "is.eps_basic")
    assert not _by_key(rows, "is.eps_diluted")
    assert any(r.label == "Basic" and r.concept_key is None for r in rows)


def _run(labels: list[tuple[str, dict[int, str]]]) -> list[tuple[list[str], int] | None]:
    block = _EpsBlock()
    return [_eps_row(block, label, values) for label, values in labels]


def test_em_dash_sub_rows_and_reit_per_unit_header() -> None:
    out = _run([
        ("Earnings per unit (sen)", {}),
        ("— Basic", {0: "16.06", 1: "14.40"}),
        ("– Diluted", {0: "16.05", 1: "14.38"}),
        ("Distribution per unit (sen)", {0: "10.70", 1: "10.47"}),
    ])
    assert out[0] == ([], 0)
    assert out[1] == (["is.eps_basic"], 0)
    assert out[2] == (["is.eps_diluted"], 0)
    assert out[3] is None  # closes the block; the ordinary loop maps DPU


def test_unqualified_one_line_eps_is_basic_and_a_note_only_header_is_valueless() -> None:
    assert _run([("Earnings per share (sen)", {0: "2.45", 1: "1.45"})]) == [(["is.eps_basic"], 0)]
    # "Earnings per share  28" - only the Note reference.
    from bursa.extract.statement_extract import _eps_values
    assert _eps_values({0: "28"}) == {}


def test_discontinued_eps_is_not_total_eps() -> None:
    out = _run([
        ("Basic earnings per share (sen)", {}),
        ("- from continuing operations", {0: "2.00"}),
        ("- from discontinued operations", {0: "0.45"}),
    ])
    # Neither row is mapped as EPS (left to the ordinary loop, unmapped).
    assert all(o is None or o[0] == [] for o in out[1:])


def test_a_section_heading_after_an_eps_header_is_not_eps() -> None:
    out = _run([
        ("Earnings per share", {}),
        ("Total comprehensive income attributable to:", {}),
        ("Owners of the Company", {0: "9,845"}),
    ])
    assert out[1] is None
    assert out[2] is None


def test_eps_printed_in_ringgit_is_stored_in_sen() -> None:
    block = _EpsBlock()
    block.start("Earnings per share (RM)")
    assert block.scaled("- Basic", {0: "0.12"}) == {0: "12.00"}


# --------------------------------------------------------------------------
# EPS note
# --------------------------------------------------------------------------


def _note(rows: list[Row], unit_header: str = "RM'000") -> StatementSpec:
    return StatementSpec(
        title="28. EARNINGS PER SHARE",
        subtitle="Basic earnings per share is calculated by dividing profit by the weighted average shares.",
        column_headers=[["Group", "Group"], ["2024", "2023"], [unit_header, unit_header]],
        rows=[*rows, Row("29. DIVIDENDS", []), Row("Final dividend", ["5,000", "4,000"]),
              Row("Interim dividend", ["3,000", "2,000"]), Row("Total", ["8,000", "6,000"])],
        column_x=[420.0, 540.0],
    )


def test_note_shares_in_thousands_as_stated(tmp_path: Path) -> None:
    pdf = build_statement_pdf(tmp_path / "note.pdf", _note([
        Row("Profit attributable to owners of the Company", ["9,845", "5,820"]),
        Row("Weighted average number of ordinary shares in issue ('000)", ["401,837", "401,379"]),
        Row("Basic earnings per share (sen)", ["2.45", "1.45"]),
    ]))
    note = extract_eps_note(pdf)

    assert note is not None
    cur, prior = note.columns
    assert (cur.year, prior.year) == (2024, 2023)
    assert cur.weighted_shares == Decimal(401_837_000)
    assert cur.shares_multiplier == 1_000 and cur.shares_unit_stated
    assert cur.patami == Decimal(9_845_000)
    assert cur.basic_eps_sen == Decimal("2.45")
    assert cur.arithmetic_ok and prior.arithmetic_ok


def test_note_shares_in_units_with_unstated_unit_and_ringgit_profit(tmp_path: Path) -> None:
    pdf = build_statement_pdf(tmp_path / "note.pdf", _note([
        Row("Profit attributable to owners of the Company", ["9,845,000", "5,820,000"]),
        Row("Weighted average number of ordinary", []),
        Row("shares in issue", ["401,837,000", "401,379,000"]),
        Row("Basic earnings per share (sen)", ["2.45", "1.45"]),
    ], unit_header="RM"))
    note = extract_eps_note(pdf)

    cur = note.columns[0]
    assert cur.weighted_shares == Decimal(401_837_000)
    assert cur.shares_multiplier == 1 and not cur.shares_unit_stated
    assert cur.arithmetic_ok


def test_note_shares_in_millions_and_weighted_total_after_components(tmp_path: Path) -> None:
    pdf = build_statement_pdf(tmp_path / "note.pdf", _note([
        Row("Profit attributable to owners of the Company", ["9,845", "5,820"]),
        Row("Number of ordinary shares in issue at 1 January (million)", ["390.0", "390.0"]),
        Row("Effect of shares issued (million)", ["11.8", "11.4"]),
        Row("Weighted average number of ordinary shares (million)", ["401.8", "401.4"]),
        Row("Basic earnings per share (sen)", ["2.45", "1.45"]),
    ]))
    note = extract_eps_note(pdf)

    cur = note.columns[0]
    assert cur.weighted_shares == Decimal(401_800_000)
    assert cur.shares_multiplier == 1_000_000
    assert cur.arithmetic_ok


# --------------------------------------------------------------------------
# Writing is.weighted_avg_shares
# --------------------------------------------------------------------------


def _two_page_pdf(tmp_path: Path, note_eps: tuple[str, str]) -> Path:
    face = build_statement_pdf(tmp_path / "is.pdf", _income_statement([
        Row("Earnings per share (sen)", []),
        Row("- Basic", ["2.45", "1.45"]),
    ]))
    note = build_statement_pdf(tmp_path / "note.pdf", _note([
        Row("Profit attributable to owners of the Company", ["9,845", "5,820"]),
        Row("Weighted average number of ordinary shares in issue ('000)", ["401,837", "401,379"]),
        Row("Basic earnings per share (sen)", list(note_eps)),
    ]))
    out = pymupdf.open()
    for part in (face, note):
        with pymupdf.open(part) as src:
            out.insert_pdf(src)
    combined = tmp_path / "ar.pdf"
    out.save(combined)
    out.close()
    return combined


@pytest.mark.parametrize(("note_eps", "confidence"), [(("2.45", "1.45"), 1.0), (("2.47", "1.45"), 0.5)])
def test_writes_weighted_shares_on_the_face_periods(
    seeded: Session, tmp_path: Path, note_eps: tuple[str, str], confidence: float,
) -> None:
    pdf = _two_page_pdf(tmp_path, note_eps)
    company = Company(stock_code="9999", name="Test Berhad")
    seeded.add(company)
    seeded.flush()
    doc = Document(
        company_id=company.id, source=DocSource.UPLOAD, original_filename="ar.pdf",
        file_sha256="sha", file_size=pdf.stat().st_size, storage_path=str(pdf),
    )
    seeded.add(doc)
    seeded.flush()

    write_facts_for_document(seeded, company, doc.id, pdf)
    extracted = extract_statements(seeded, doc.id, pdf, company_id=company.id).statements[
        Statement.INCOME_STATEMENT
    ]
    assert extracted.eps_note is not None
    eps_fact = seeded.scalars(select(Fact).where(Fact.concept_key == "is.eps_basic")).first()

    touched = write_weighted_shares_facts(seeded, company.id, doc.id, eps_fact.run_id, extracted)

    facts = {
        f.source_col_index: f
        for f in seeded.scalars(select(Fact).where(Fact.concept_key == "is.weighted_avg_shares"))
    }
    assert {f.id for f in facts.values()} == touched
    cur = facts[0]
    assert cur.value == Decimal(401_837_000)
    assert cur.scale_multiplier == 1_000
    assert cur.period_id == eps_fact.period_id
    assert cur.confidence == confidence
    # The prior year matches the face either way.
    assert facts[1].confidence == 1.0


# --------------------------------------------------------------------------
# A later "Profit for the financial year" row must not demote the real PAT
# --------------------------------------------------------------------------


def _pat_rows(rows):  # type: ignore[no-untyped-def]
    return [(r.concept_key, r.values[0]) for r in rows if r.concept_key in ("is.profit_for_period", "is.profit_continuing")]


def test_profit_row_after_tci_is_not_a_second_pat(seeded: Session, tmp_path: Path) -> None:
    # Country View: a per-share "Profit for the financial year" row below TCI.
    spec = _income_statement([
        Row("Total comprehensive income for the financial year", ["10,352", "6,100"], bold=True),
        Row("Profit for the financial year", ["2.45", "1.45"]),
    ])
    rows = _is_rows(seeded, build_statement_pdf(tmp_path / "is.pdf", spec))

    assert _pat_rows(rows) == [("is.profit_for_period", "10,352")]
    assert any(r.label == "Profit for the financial year" and r.values[0] == "2.45" and r.concept_key is None
               for r in rows)


def test_profit_row_inside_an_eps_block_after_tci_is_eps(seeded: Session, tmp_path: Path) -> None:
    spec = _income_statement([
        Row("Total comprehensive income for the financial year", ["10,352", "6,100"], bold=True),
        Row("Earnings per ordinary share attributable to owners of the Company (sen):", []),
        Row("Basic and diluted:", []),
        Row("Profit for the financial year", ["2.45", "1.45"]),
    ])
    rows = _is_rows(seeded, build_statement_pdf(tmp_path / "is.pdf", spec))

    assert _pat_rows(rows) == [("is.profit_for_period", "10,352")]
    assert _by_key(rows, "is.eps_basic")[0].values[0] == "2.45"
    assert _by_key(rows, "is.eps_diluted")[0].values[0] == "2.45"


def test_pat_repeated_with_identical_figures_is_a_duplicate_not_continuing_ops(
    seeded: Session, tmp_path: Path,
) -> None:
    # Two-statement presentation: PAT restated at the top of the OCI statement.
    spec = _income_statement([
        Row("Profit for the financial year", ["10,352", "6,100"], bold=True),
        Row("Other comprehensive income", ["420", "150"]),
    ])
    rows = _is_rows(seeded, build_statement_pdf(tmp_path / "is.pdf", spec))

    assert _pat_rows(rows) == [("is.profit_for_period", "10,352")]


def test_ligature_profit_tail_inside_an_eps_block() -> None:
    out = _run([("Earnings per share (sen):", {}), ("Proﬁt for the ﬁnancial year", {0: "2.45"})])
    assert out[1] == (["is.eps_basic"], 0)
