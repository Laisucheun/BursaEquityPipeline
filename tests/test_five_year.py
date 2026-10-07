"""Five-year summary page extraction and cross-validation (layer 5)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest
from sqlalchemy.orm import Session

from bursa.analysis.facts import AnnualFacts
from bursa.extract.five_year import (
    DPS_KEY,
    SummaryValue,
    extract_five_year_summary,
    find_heading,
    is_candidate_text,
    map_label,
    merge_values,
    parse_year_label,
    strip_units,
)
from bursa.mapping.synonyms import seed_concepts
from bursa.validate.five_year_check import (
    classify_pair,
    compare_summary,
    detect_year_shift,
)
from tests.fixtures.synthetic import Row, StatementSpec, build_statement_pdf

FIVE_COLS = [300.0, 360.0, 420.0, 480.0, 540.0]

SUMMARY = StatementSpec(
    title="FIVE-YEAR FINANCIAL SUMMARY",
    subtitle="Financial year ended 31 March",
    column_headers=[
        ["FY2021", "FY2022", "FY2023", "FY2024", "FY2025"],
        ["Restated", "", "", "", ""],
    ],
    rows=[
        Row("OPERATING RESULTS (RM MILLION)", [], bold=True),
        Row("Turnover", ["1,010.5", "1,120.0", "1,234.5", "1,300.2", "1,456.7"]),
        Row("Profit before taxation", ["101.2", "110.4", "120.9", "131.0", "140.3"]),
        Row("Profit attributable to owners of the", []),
        Row("Company", ["70.1", "75.2", "80.3", "85.4", "90.5"]),
        Row("Revenue growth (%)", ["2.1", "10.8", "10.2", "5.3", "12.0"]),
        Row("FINANCIAL POSITION (RM'000)", [], bold=True),
        Row("Total assets", ["2,500,100", "2,610,200", "2,720,300", "2,830,400", "2,940,500"]),
        Row("Shareholders' funds", ["900,000", "950,000", "1,000,000", "1,050,000", "1,100,000"]),
        Row("SHARE INFORMATION", [], bold=True),
        Row("Earnings per share (sen)", ["17.5", "18.8", "20.1", "21.4", "22.6"]),
        Row("Net assets per share (sen)", ["225", "238", "250", "263", "275"]),
        Row("Dividend per share (sen)", ["8.0", "8.5", "9.0", "9.5", "10.0"]),
        Row("Return on equity (%)", ["7.8", "7.9", "8.0", "8.1", "8.2"]),
        Row("Share price as at 31 March (RM)", ["3.10", "3.20", "3.30", "3.40", "3.50"]),
    ],
    column_x=FIVE_COLS,
)

# The same page, years written as financial-year-end dates and
# descending, with a "*" restated marker on one header cell and a per-share
# block whose unit lives only in its section heading.
SUMMARY_FYE = StatementSpec(
    title="Group Five-Year Financial Highlights",
    subtitle="(RM'000 unless otherwise stated)",
    column_headers=[
        ["FYE 31.3.2025", "FYE 31.3.2024", "FYE 31.3.2023", "FYE 31.3.2022*", "FYE 31.3.2021"],
    ],
    rows=[
        Row("Revenue", ["1,456,700", "1,300,200", "1,234,500", "1,120,000", "1,010,500"]),
        Row("Profit after taxation", ["95,000", "90,000", "85,000", "80,000", "75,000"]),
        Row("Total equity", ["1,150,000", "1,100,000", "1,050,000", "1,000,000", "950,000"]),
        Row("Per share (RM)", [], bold=True),
        Row("Basic earnings", ["0.226", "0.214", "0.201", "0.188", "0.175"]),
        Row("Net assets", ["2.75", "2.63", "2.50", "2.38", "2.25"]),
    ],
    column_x=FIVE_COLS,
)

# Decoys that must never be picked: a summary-headed page with only two
# years (a financial review), and a five-year table with no summary heading
# (sustainability data that happens to have "Total assets"-free rows but
# plenty of numbers and years).
DECOY_TWO_YEARS = StatementSpec(
    title="FINANCIAL HIGHLIGHTS",
    subtitle="RM'000",
    column_headers=[["2025", "2024"]],
    rows=[
        Row("Revenue", ["999,999", "888,888"]),
        Row("Profit before taxation", ["77,777", "66,666"]),
        Row("Total assets", ["5,555,555", "4,444,444"]),
    ],
    column_x=[420.0, 540.0],
)
DECOY_NO_HEADING = StatementSpec(
    title="SUSTAINABILITY PERFORMANCE DATA",
    subtitle="Environmental indicators",
    column_headers=[["2021", "2022", "2023", "2024", "2025"]],
    rows=[
        Row("Revenue", ["1", "2", "3", "4", "5"]),
        Row("Profit before taxation", ["11", "12", "13", "14", "15"]),
        Row("Scope 1 emissions (tCO2e)", ["1,000", "1,100", "1,200", "1,300", "1,400"]),
    ],
    column_x=FIVE_COLS,
)
DECOY_OPERATING = StatementSpec(
    title="5-YEAR PLANTATION PERFORMANCE",
    subtitle="Crop statement",
    column_headers=[["2021", "2022", "2023", "2024", "2025"]],
    rows=[
        Row("FFB production (MT)",
            ["2,839,583", "2,803,965", "2,686,356", "2,726,516", "2,917,621"]),
        Row("Cost of sales (RM/MT)", ["2,623", "2,585", "2,770", "2,401", "1,863"]),
        Row("Revenue (RM/MT)", ["4,332", "3,856", "4,118", "4,688", "3,076"]),
    ],
    column_x=FIVE_COLS,
)


def _combine(tmp_path: Path, specs: list[StatementSpec]) -> Path:
    combined = pymupdf.open()
    for i, spec in enumerate(specs):
        part = build_statement_pdf(tmp_path / f"part{i}.pdf", spec)
        with pymupdf.open(part) as src:
            combined.insert_pdf(src)
    out = tmp_path / "report.pdf"
    combined.save(out)
    combined.close()
    return out


def _values(pdf: Path, **kw):  # type: ignore[no-untyped-def]
    tables = extract_five_year_summary(pdf, **kw)
    return tables, merge_values(tables)


# --------------------------------------------------------------------------
# Unit-level
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2025", (2025, False)),
        ("FY2025", (2025, False)),
        ("FY 2025", (2025, False)),
        ("FYE 31.3.2025", (2025, False)),
        ("31.12.2020", (2020, False)),
        ("2021*", (2021, True)),
        ("2021 (Restated)", (2021, True)),
        ("2024/25", (2025, False)),
        ("2024/2025", (2025, False)),
        ("1,234", None),
        ("RM'000", None),
        ("2025 Target", None),
    ],
)
def test_parse_year_label(text: str, expected: tuple[int, bool] | None) -> None:
    assert parse_year_label(text) == expected


def test_heading_screen() -> None:
    assert find_heading("x\nFIVE-YEAR FINANCIAL SUMMARY\n2025 2024") is not None
    assert find_heading("5 Year Group Financial Highlights") is not None
    assert find_heading("Six-Year Group Financial Summary") is not None
    assert find_heading("Group Financial Highlights") is not None
    assert find_heading("Non-Financial Highlights") is None
    assert find_heading("5-YEAR PLANTATION PERFORMANCE") is None
    # Narrative merely mentioning the phrase is not a heading line.
    assert find_heading(
        "The table on the following page sets out the financial highlights of the Group "
        "over the last five years in more detail."
    ) is None
    assert not is_candidate_text("FINANCIAL HIGHLIGHTS\n2025\n2024")
    assert is_candidate_text("FINANCIAL HIGHLIGHTS\n2025 2024 2023 2022 2021")


def test_strip_units() -> None:
    assert strip_units("Earnings per share (sen)") == "Earnings per share"
    assert strip_units("Revenue RM'000") == "Revenue"
    assert strip_units("Market Capitalisation (RM’Million)") == "Market Capitalisation"


def test_map_label_without_session() -> None:
    assert map_label("Turnover") == "is.revenue"
    assert map_label("Shareholders’ funds") == "bs.equity_owners"
    assert map_label("Net profit attributable to equity holders of the Bank") == "is.pat_owners"
    assert map_label("Profit before tax expense and zakat") == "is.profit_before_tax"
    assert map_label("Profit attributable to non-controlling interests") is None
    assert map_label("Net assets per share (RM)") == "bs.nta_per_share"
    assert map_label("Basic earnings", section="Per share (sen)") == "is.eps_basic"
    assert map_label("Gross dividend", section="Per share (sen)") == DPS_KEY
    assert map_label("Basic earnings") is None
    assert map_label("Return on equity (%)") is None


def test_map_label_uses_shared_synonyms_and_section(session: Session) -> None:
    seed_concepts(session)
    # A bare split row resolves through the shared synonym table, and means
    # whatever its heading splits ...
    assert (
        map_label("Owners of the Company", session=session, section="Profit attributable to:")
        == "is.pat_owners"
    )
    assert (
        map_label("Owners of the Company", session=session, section="EQUITY ATTRIBUTABLE TO:")
        == "bs.equity_owners"
    )
    # ... and nothing without context, or under a per-share wrap (real:
    # "Net Assets Per Share Attributable To" / "Owners Of The Parent (RM)").
    assert map_label("Owners of the Company", session=session) is None
    assert (
        map_label("Owners Of The Parent (RM)", session=session,
                  section="Net Assets Per Share Attributable To")
        is None
    )
    assert map_label("Net Assets Per Share Attributable To Owners Of The Parent (RM)") == (
        "bs.nta_per_share"
    )


# --------------------------------------------------------------------------
# Page-level, synthetic PDFs
# --------------------------------------------------------------------------


def test_mixed_units_per_section_and_row(tmp_path: Path) -> None:
    pdf = build_statement_pdf(tmp_path / "s.pdf", SUMMARY)
    tables, v = _values(pdf)

    assert len(tables) == 1
    assert sorted(set(tables[0].years.values())) == [2021, 2022, 2023, 2024, 2025]
    # RM million section
    assert v[("is.revenue", 2023)].value == Decimal("1234500000.0")
    assert v[("is.profit_before_tax", 2025)].value == Decimal("140300000.0")
    # wrapped label joined, not mis-mapped from its tail
    assert v[("is.pat_owners", 2022)].value == Decimal("75200000.0")
    assert v[("is.pat_owners", 2022)].label.endswith("owners of the Company")
    # RM'000 section overrides the earlier RM million one
    assert v[("bs.total_assets", 2024)].value == Decimal(2_830_400_000)
    assert v[("bs.equity_owners", 2021)].value == Decimal(900_000_000)
    # per-share rows never take the money scale; NTA printed in sen -> RM
    assert v[("is.eps_basic", 2025)].value == Decimal("22.6")
    assert v[("bs.nta_per_share", 2023)].value == Decimal("2.50")
    assert v[(DPS_KEY, 2021)].value == Decimal("8.0")
    # % and price rows are never mapped
    concepts = {k[0] for k in v}
    assert concepts == {
        "is.revenue", "is.profit_before_tax", "is.pat_owners", "bs.total_assets",
        "bs.equity_owners", "is.eps_basic", "bs.nta_per_share", DPS_KEY,
    }
    # rounding allowance = half the last printed digit at the row's scale
    assert v[("is.revenue", 2023)].rounding == Decimal("50000.0")


def test_restated_marker_row(tmp_path: Path) -> None:
    pdf = build_statement_pdf(tmp_path / "s.pdf", SUMMARY)
    _, v = _values(pdf)
    assert v[("is.revenue", 2021)].restated
    assert not v[("is.revenue", 2022)].restated


def test_fye_date_labels_descending_and_section_unit(tmp_path: Path) -> None:
    pdf = build_statement_pdf(tmp_path / "s.pdf", SUMMARY_FYE)
    tables, v = _values(pdf)

    assert len(tables) == 1
    assert v[("is.revenue", 2025)].value == Decimal(1_456_700_000)
    assert v[("is.revenue", 2021)].value == Decimal(1_010_500_000)
    assert v[("is.revenue", 2025)].unit_source == "table"
    assert v[("is.profit_for_period", 2023)].value == Decimal(85_000_000)
    assert v[("bs.total_equity", 2022)].value == Decimal(1_000_000_000)
    assert v[("bs.total_equity", 2022)].restated  # "FYE 31.3.2022*"
    # "Per share (RM)" block: EPS printed in RM -> stored in sen
    assert v[("is.eps_basic", 2024)].value == Decimal("21.400")
    assert v[("bs.nta_per_share", 2025)].value == Decimal("2.75")


def test_decoys_are_not_picked(tmp_path: Path) -> None:
    pdf = _combine(
        tmp_path, [DECOY_TWO_YEARS, DECOY_NO_HEADING, DECOY_OPERATING, SUMMARY],
    )
    tables, v = _values(pdf)
    assert [t.page_no for t in tables] == [4]
    assert v[("is.revenue", 2023)].value == Decimal("1234500000.0")


def test_only_decoys_yield_nothing(tmp_path: Path) -> None:
    pdf = _combine(tmp_path, [DECOY_TWO_YEARS, DECOY_NO_HEADING, DECOY_OPERATING])
    assert extract_five_year_summary(pdf) == []


# --------------------------------------------------------------------------
# Cross-validation
# --------------------------------------------------------------------------


def _sv(
    concept: str, fy: int, value: str, rounding: str = "0", restated: bool = False,
) -> SummaryValue:
    return SummaryValue(
        concept_key=concept, fiscal_year=fy, value=Decimal(value), raw_text=value,
        label=concept, multiplier=1, unit="RM", unit_source="row", restated=restated,
        page_no=1, rounding=Decimal(rounding),
    )


def _annual(data: dict[int, dict[str, str]]) -> dict[int, AnnualFacts]:
    return {
        fy: AnnualFacts(fiscal_year=fy, period_end=date(fy, 12, 31),
                        values={k: Decimal(v) for k, v in vals.items()})
        for fy, vals in data.items()
    }


def test_classify_pair() -> None:
    # RM million summary "1,068" vs exact RM'000 fact 1,067,612 thousand
    rm_million = _sv("is.revenue", 2025, "1068000000", "500000")
    assert classify_pair(rm_million, Decimal(1_067_612_000))[0] == "MATCH"
    assert classify_pair(_sv("is.revenue", 2025, "1000"), Decimal(1030))[0] == "CLOSE"
    assert classify_pair(_sv("is.revenue", 2025, "1000"), Decimal(1200))[0] == "MISMATCH"
    assert classify_pair(_sv("is.revenue", 2025, "1000"), Decimal(1_000_000))[0] == "SCALE_ERROR"
    assert classify_pair(_sv("is.eps_basic", 2025, "22.6"), Decimal("0.226"))[0] == "SCALE_ERROR"
    cls, _, detail = classify_pair(_sv("is.pat_owners", 2025, "100"), Decimal(-100))
    assert (cls, detail) == ("MISMATCH", "sign flipped")
    assert classify_pair(_sv("is.revenue", 2025, "1000"), None)[0] == "ONLY_IN_SUMMARY"
    _, _, detail = classify_pair(_sv("is.revenue", 2025, "1000", restated=True), Decimal(1200))
    assert "restated" in detail


def test_compare_summary_skips_dps_and_covers_old_years() -> None:
    values = [
        _sv("is.revenue", 2018, "500"),
        _sv("is.revenue", 2025, "900"),
        _sv(DPS_KEY, 2025, "10"),
    ]
    checks = compare_summary(values, _annual({2018: {"is.revenue": "500"}}))
    assert [(c.fiscal_year, c.classification) for c in checks] == [
        (2018, "MATCH"), (2025, "ONLY_IN_SUMMARY"),
    ]


def test_infer_assumed_units_per_page() -> None:
    from dataclasses import replace

    from bursa.validate.five_year_check import infer_assumed_units

    raw = [
        replace(_sv("is.revenue", fy, str(v), "0.5"), unit_source="assumed")
        for fy, v in ((2023, 3763), (2024, 4454), (2025, 4218))
    ]
    annual = _annual({
        2023: {"is.revenue": "3762748000"},
        2024: {"is.revenue": "4454447000"},
        2025: {"is.revenue": "9999000000"},  # a real disagreement survives
    })
    fixed = infer_assumed_units(raw, annual)
    assert [v.unit_source for v in fixed] == ["inferred"] * 3
    checks = compare_summary(fixed, annual)
    assert [c.classification for c in checks] == ["MATCH", "MATCH", "MISMATCH"]
    # A page with an explicit unit is never re-scaled.
    explicit = [_sv("is.revenue", 2023, "3763", "0.5")]
    assert infer_assumed_units(explicit, annual)[0].value == Decimal(3763)


def test_detect_year_shift() -> None:
    summary = [_sv("is.revenue", fy, str(2 ** (fy - 2010))) for fy in range(2020, 2025)]
    # Our facts carry each figure one fiscal year late (wrong fy_end_month).
    shifted = _annual({fy + 1: {"is.revenue": str(2 ** (fy - 2010))} for fy in range(2020, 2025)})
    assert detect_year_shift(summary, shifted) == 1
    aligned = _annual({fy: {"is.revenue": str(2 ** (fy - 2010))} for fy in range(2020, 2025)})
    assert detect_year_shift(summary, aligned) is None
