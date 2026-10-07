"""Regression tests grounded in real failures, not hypothetical ones.

Every decoy page's text below is adapted directly from what was actually
found on real filings while diagnosing why the deterministic extractor was
picking notes and narrative pages instead of the real statement - see
`bursa.extract.page_scoring`'s module docstring for the full list.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from bursa.db.enums import Statement
from bursa.extract.layout import Cell, ColumnBand, ExtractedRow
from bursa.extract.page_scoring import (
    MAX_CANDIDATES,
    START_CONCEPT,
    _count_keyword_hits,
    _keywords_for,
    _matches_any_concept,
    _matches_concept,
    _remap_to_primary_columns,
    find_statement_page,
    select_statement_pages,
)
from tests.fixtures.synthetic import (
    BALANCE_SHEET,
    QUARTERLY_INCOME_STATEMENT,
    Row,
    StatementSpec,
    build_statement_pdf,
)

# Adapted from a real, confirmed false positive: Genting's Note 9
# ("PROFIT BEFORE TAXATION") disclosure - a genuine table with real figures,
# but one that only ever touches ONE income-statement subtotal ("Profit
# before taxation" itself, as the note's own subject) while breaking down
# unrelated cost components. AMMB's 5-year highlights and Axiata's
# Alternative Performance Measures glossary passed the old >=1 gate through
# this exact same single keyword and nothing else.
SINGLE_KEYWORD_NOTE_DECOY = StatementSpec(
    title="NOTES TO THE FINANCIAL STATEMENTS",
    subtitle="9. The following items have been charged/(credited) in arriving at (RM'000)",
    column_headers=[["2024", "2023"]],
    rows=[
        # The note's own row, mirroring what the real Genting page actually
        # extracted as a table row (not just a page title) - the single
        # keyword AMMB, Axiata, and Genting all slipped through the old gate
        # on.
        Row("PROFIT BEFORE TAXATION", ["45,230", "42,110"], bold=True),
        Row("Depreciation of property, plant and equipment", ["12,340", "11,110"], indent=1),
        Row("Staff costs", ["128,440", "119,220"], indent=1),
        Row("Auditors' remuneration - statutory audit", ["1,850", "1,720"], indent=1),
        Row("Rental of premises", ["8,340", "7,990"], indent=1),
        Row("Net foreign exchange loss/(gain)", ["(2,110)", "3,440"], indent=1),
        Row("Directors' remuneration", ["6,220", "5,880"], indent=1),
    ],
)

# Adapted from the real impairment/tax-loss note that got picked as IOI's
# and PETRONAS Dagangan's "balance sheet".
NOTE_DECOY = (
    "Note 14: Impairment assessment and deferred tax\n"
    "As at 31 December 2024, the total derivative financial instruments of the Group\n"
    "and of the Company that were carried at fair value amounted to RM39.2 million.\n"
    "An impairment loss of RM12.5 million was recognised in respect of goodwill\n"
    "allocated to the plantation cash-generating unit during the financial year.\n"
    "Unutilised tax losses of the Group amounting to RM5,872,000 will expire in 2030,\n"
    "RM11,004,000 in 2031 and RM5,460,000 in 2032, subject to agreement by the tax authorities."
)

# Adapted from the real share-price commentary that got picked as Axiata's
# "balance sheet".
NARRATIVE_DECOY = (
    "Share Price Performance\n"
    "Positive performance continued in 3Q24, underpinned by strong 1H24 results and\n"
    "positive price momentum. Share price increased 12.4% over the quarter, reaching\n"
    "a high of RM4.56 in June before easing to RM4.21 by the end of the period amid\n"
    "broader market volatility in 2Q24 and 4Q24."
)

# A page that is *purely* numeric with no statement vocabulary at all - the
# case that would fool a ratio-only scorer with no keyword signal.
PURE_NUMBER_DECOY = (
    "Daily Trading Volume\n"
    "12,450 13,200 11,980 14,650 12,100 13,880 12,300 14,010 13,555 12,900\n"
    "4.21 4.23 4.19 4.30 4.25 4.28 4.22 4.31 4.27 4.24\n"
    "0.4 0.6 0.3 0.8 0.5 0.7 0.4 0.9 0.6 0.5"
)


def text_page(doc, text: str) -> None:  # type: ignore[no-untyped-def]
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=9)


def build_multi_page_pdf(path: Path, real_statement_pdf: Path, decoys: list[str]) -> Path:
    """A real single-page statement merged with plain-text decoy pages -
    split before/after so the real page never lands conveniently first
    or last, matching how a real annual report actually interleaves them."""
    combined = pymupdf.open()
    half = len(decoys) // 2
    for text in decoys[:half]:
        text_page(combined, text)

    real_doc = pymupdf.open(real_statement_pdf)
    combined.insert_pdf(real_doc)
    real_doc.close()

    for text in decoys[half:]:
        text_page(combined, text)

    combined.save(path)
    combined.close()
    return path


# --------------------------------------------------------------------------
# Keyword vocabulary
# --------------------------------------------------------------------------


def test_keywords_are_drawn_from_the_taxonomys_subtotal_concepts() -> None:
    bs_keywords = [p for group in _keywords_for(Statement.BALANCE_SHEET) for p in group]
    assert "total assets" in bs_keywords
    assert "total equity" in bs_keywords

    is_keywords = [p for group in _keywords_for(Statement.INCOME_STATEMENT) for p in group]
    assert "profit before tax" in is_keywords

    cf_keywords = [p for group in _keywords_for(Statement.CASH_FLOW) for p in group]
    assert "net cash from operating activities" in cf_keywords


def test_cash_flow_keywords_include_real_world_phrasing_variants() -> None:
    """The regression this module's synonym-based keywords exist for: real
    filings insert words ("generated from", "flows") the bare canonical
    label never has - confirmed against a real cash flow statement page
    that scored zero hits before synonyms were included."""
    cf_keywords = [p for group in _keywords_for(Statement.CASH_FLOW) for p in group]
    assert any("generated from operating activities" in p for p in cf_keywords)


def test_keyword_hits_are_case_insensitive() -> None:
    assert _count_keyword_hits("TOTAL ASSETS: 465,600", [["total assets"]]) == 1
    assert _count_keyword_hits("nothing relevant here", [["total assets"]]) == 0


def test_bidirectional_cash_flow_phrasing_collapses_to_the_canonical_form() -> None:
    """Real regression, confirmed on five separate filers: a cash-flow
    subtotal line showing both the positive- and negative-case wording in
    one label via a slash-parenthetical, in either order and with or
    without "generated" - none of which match the taxonomy's canonical
    phrasing via plain substring search without this normalization."""
    cf_keywords = _keywords_for(Statement.CASH_FLOW)
    # Frontken: "from/(for)", in the net_investing group.
    assert _count_keyword_hits(
        "Net Cash (For)/From Investing Activities", cf_keywords
    ) == 1
    # Kerjaya Prospek, Gas Malaysia, Astro: "generated from/(used in)".
    assert _count_keyword_hits(
        "Net cash generated from/(used in) operating activities", cf_keywords
    ) == 1
    # SD Guthrie: the reversed order, "(used in)/generated from".
    assert _count_keyword_hits(
        "Net cash (used in)/generated from financing activities", cf_keywords
    ) == 1
    # Frontken: no slash-parenthetical at all, bare "For".
    assert _count_keyword_hits(
        "Net Cash For Financing Activities", cf_keywords
    ) == 1


def test_a_line_wrapped_subtotal_still_matches_across_the_break() -> None:
    """Real regression, confirmed on Kerjaya Prospek: a cash-flow subtotal
    is wrapped across two physical lines in the PDF's own content stream -
    "Net cash generated from/(used in)\\n     operating activities" - which
    a plain substring match (expecting one space, not a newline and extra
    indentation) can never bridge on its own. This is what `_normalize`'s
    whitespace collapsing exists to fix, independent of the bidirectional-
    phrasing collapsing tested above - confirmed by using a label that
    needs *only* the whitespace fix (no slash-parenthetical at all)."""
    cf_keywords = _keywords_for(Statement.CASH_FLOW)
    wrapped = "Net cash generated from\n     operating activities"
    assert _count_keyword_hits(wrapped, cf_keywords) == 1


def test_cash_flow_accepts_profit_for_the_year_as_its_start_line_too() -> None:
    """Real regression, confirmed on SD Guthrie: a cash flow statement can
    legitimately reconcile from "Profit for the financial year" (net,
    after tax) instead of "Profit before tax" - both are real MFRS
    presentation choices. Without this, `has_start` never fires for such a
    filer, so the continuation search that would pull in its real
    financing/cash-position lines never even runs."""
    start_concepts = START_CONCEPT[Statement.CASH_FLOW]
    assert _matches_any_concept("Profit for the financial year", start_concepts)
    assert _matches_any_concept("Profit before tax", start_concepts)  # the usual case, unaffected


def test_cash_flow_accepts_the_zakat_variant_of_profit_before_tax() -> None:
    """Real regression, confirmed on Gas Malaysia (a utility with Islamic
    financing arrangements): "Profit before zakat and taxation", not plain
    "Profit before tax" - a real, not-rare Shariah-finance phrasing."""
    start_concepts = START_CONCEPT[Statement.CASH_FLOW]
    assert _matches_any_concept("Profit before zakat and taxation", start_concepts)


# Adapted from the real bug this fixes: Kerjaya Prospek's genuine, correctly
# headed cash flow statement carries "Profit before taxation" (its own
# conventional reconciliation start line) and "Net cash generated from/
# (used in) operating activities" - the latter two are CF's own real hits,
# but "Profit before taxation" also matches the income statement's own
# keyword group, by MFRS convention, not by accident. This decoy
# deliberately reproduces that exact shape in miniature: two genuine CF
# hits that tie against two IS hits on the same page.
_CF_WITH_SHARED_IS_VOCABULARY = StatementSpec(
    title="Statements of Cash Flows",
    subtitle="for the financial year ended 31 December 2025 (RM'000)",
    column_headers=[["2025", "2024"]],
    rows=[
        Row("CASH FLOWS FROM OPERATING ACTIVITIES", []),
        Row("Profit before taxation", ["308,909", "216,154"], bold=True),
        Row("Adjustments for:", []),
        Row("Depreciation of property, plant and equipment", ["13,662", "14,232"], indent=1),
        Row("Operating profit before working capital changes", ["340,112", "245,900"], bold=True),
        Row("Net cash generated from/(used in) operating activities", ["269,501", "323,920"], bold=True),
    ],
    column_x=[420.0, 540.0],
)


def test_a_clean_heading_rescues_a_cash_flow_page_from_an_is_vocabulary_tie(
    tmp_path: Path,
) -> None:
    """The Kerjaya Prospek regression, end to end: a genuine, correctly-
    headed cash flow page whose own two legitimate CF hits tie 2-2 against
    the income statement's keyword groups (both "Profit before taxation"
    and "Operating profit before working capital changes" are conventional
    CF reconciliation lines that also happen to match IS's own "profit
    before tax"/"operating profit" groups) must still be found - the plain
    dominance check alone would reject it, but its own unambiguous
    "Statements of Cash Flows" heading is strong enough evidence to
    override a tie specifically (never an outright loss)."""
    cf_path = build_statement_pdf(tmp_path / "cf.pdf", _CF_WITH_SHARED_IS_VOCABULARY)

    result = find_statement_page(cf_path, Statement.CASH_FLOW)

    assert result is not None
    assert any("net cash generated" in row.label.lower() for row in result.table.rows)


def test_a_concepts_several_synonyms_count_as_one_match_not_several() -> None:
    """The fix that keeps the >=2 gate meaningful once synonyms are added:
    two phrasings of the SAME concept appearing together must still count
    as one matched concept - otherwise a single-concept note could clear
    the gate just by using more than one of that concept's own synonyms."""
    text = "profit before tax and profit before taxation are the same figure"
    hits = _count_keyword_hits(text, [["profit before tax", "profit before taxation"]])
    assert hits == 1


# --------------------------------------------------------------------------
# The regression cases - real decoy text, checked against the real scorer
# --------------------------------------------------------------------------


def test_scorer_picks_the_real_statement_over_a_notes_page_and_a_narrative_page(
    tmp_path: Path,
) -> None:
    bs_only = build_statement_pdf(tmp_path / "bs_only.pdf", BALANCE_SHEET)
    combined = build_multi_page_pdf(
        tmp_path / "combined.pdf", bs_only, [NOTE_DECOY, NARRATIVE_DECOY]
    )

    result = find_statement_page(combined, Statement.BALANCE_SHEET)

    assert result is not None
    assert any(row.label.upper() == "TOTAL ASSETS" for row in result.table.rows)


def test_scorer_rejects_pure_numeric_density_without_statement_vocabulary(
    tmp_path: Path,
) -> None:
    """A trading-volume table is numerically denser than nothing, but has
    zero balance-sheet vocabulary - it must lose to the real, sparser-looking
    statement page, not win on raw numeric ratio alone."""
    bs_only = build_statement_pdf(tmp_path / "bs_only.pdf", BALANCE_SHEET)
    combined = build_multi_page_pdf(
        tmp_path / "combined.pdf", bs_only, [PURE_NUMBER_DECOY, NOTE_DECOY]
    )

    result = find_statement_page(combined, Statement.BALANCE_SHEET)

    assert result is not None
    assert any(row.label.upper() == "TOTAL ASSETS" for row in result.table.rows)


def test_scorer_rejects_a_highlights_page_that_mixes_statement_types(tmp_path: Path) -> None:
    """Real regression: Tenaga Nasional's "income statement" candidate was a
    5-Year Financial Highlights summary listing "Revenue" and "Operating
    profit" right alongside "Total assets" and "Share capital" - genuine
    income-statement terms sitting next to genuine balance-sheet terms on
    one compact page. Raising MIN_ROW_KEYWORD_HITS doesn't fix this (a
    highlights page trivially clears any diversity bar, by design) - what
    catches it is requiring the target statement's own hits to strictly
    dominate every other statement's hits on the same page."""
    highlights_decoy = StatementSpec(
        title="5-YEAR GROUP FINANCIAL HIGHLIGHTS",
        subtitle="(RM million)",
        column_headers=[["2025", "2024"]],
        rows=[
            Row("Revenue", ["12,450", "11,220"]),
            Row("Operating profit", ["2,340", "2,010"]),
            Row("Total assets", ["98,400", "94,220"]),
            Row("Total liabilities", ["61,200", "58,900"]),
            Row("Share capital", ["18,000", "18,000"]),
        ],
    )
    is_path = build_statement_pdf(tmp_path / "is_only.pdf", QUARTERLY_INCOME_STATEMENT)
    highlights_path = build_statement_pdf(tmp_path / "highlights.pdf", highlights_decoy)

    combined = pymupdf.open()
    combined.insert_pdf(pymupdf.open(highlights_path))
    combined.insert_pdf(pymupdf.open(is_path))
    combined_path = tmp_path / "combined.pdf"
    combined.save(combined_path)
    combined.close()

    result = find_statement_page(combined_path, Statement.INCOME_STATEMENT)

    assert result is not None
    assert result.page_no == 2
    assert any("revenue" in row.label.lower() for row in result.table.rows)


def test_scorer_rejects_a_note_matching_only_one_subtotal_keyword(tmp_path: Path) -> None:
    """The Genting/AMMB/Axiata case: a page that only ever touches ONE of the
    statement's subtotals, in a note breaking down its components, rather
    than genuinely being the primary statement. All three real false
    positives passed an older >=1 gate through this exact single keyword
    ("profit before tax") - so this must be checked against the income
    statement, the type that keyword actually belongs to."""
    is_path = build_statement_pdf(tmp_path / "is_only.pdf", QUARTERLY_INCOME_STATEMENT)
    note_path = build_statement_pdf(tmp_path / "note.pdf", SINGLE_KEYWORD_NOTE_DECOY)
    real_is = pymupdf.open(is_path)
    note_decoy = pymupdf.open(note_path)

    combined = pymupdf.open()
    combined.insert_pdf(note_decoy)  # the decoy first, so a naive "first match" would pick it
    combined.insert_pdf(real_is)
    combined_path = tmp_path / "combined.pdf"
    combined.save(combined_path)
    combined.close()
    note_decoy.close()
    real_is.close()

    result = find_statement_page(combined_path, Statement.INCOME_STATEMENT)

    assert result is not None
    # The decoy is page 1, the real statement page 2 - winning on page_no
    # confirms the real, multi-subtotal page was picked over the note.
    assert result.page_no == 2
    assert any("revenue" in row.label.lower() for row in result.table.rows)


def test_a_single_keyword_note_alone_is_not_mistaken_for_the_statement(tmp_path: Path) -> None:
    """Isolated (no real statement anywhere), the note alone must not pass -
    it should score as an honest miss, matching the CIMB pattern."""
    note_only = build_statement_pdf(tmp_path / "note.pdf", SINGLE_KEYWORD_NOTE_DECOY)
    assert find_statement_page(note_only, Statement.INCOME_STATEMENT) is None


# --------------------------------------------------------------------------
# Start/end line span detection - "found Revenue but not Profit for the
# period on this page, so it must continue onto the next"
# --------------------------------------------------------------------------

# The real fixture's rows split across a page break: everything up to
# "Profit from operations" on page 1, the rest - crucially including
# "Profit for the period" and EPS, the usual end lines - on page 2.
_IS_PAGE_1 = StatementSpec(
    title="CONDENSED CONSOLIDATED STATEMENT OF PROFIT OR LOSS",
    subtitle="For the third quarter ended 30 September 2024 (Unaudited) (RM'000)",
    column_headers=[["30.09.2024", "30.09.2023"]],
    rows=[
        Row("Revenue", ["125,430", "110,220"]),
        Row("Cost of sales", ["(92,318)", "(83,655)"]),
        Row("Gross profit", ["33,112", "26,565"], bold=True),
        Row("Other income", ["2,145", "1,980"]),
        Row("Distribution costs", ["(8,220)", "(7,510)"]),
        Row("Administrative expenses", ["(11,455)", "(10,330)"]),
        Row("Profit from operations", ["15,582", "10,705"], bold=True),
    ],
    column_x=[420.0, 540.0],
)
_IS_PAGE_2_CONTINUED = StatementSpec(
    title="(CONTINUED)",
    subtitle="",
    column_headers=[["30.09.2024", "30.09.2023"]],
    rows=[
        Row("Finance costs", ["(2,110)", "(2,455)"]),
        Row("Share of results of associates", ["330", "(120)"]),
        Row("Profit before taxation", ["13,802", "8,130"], bold=True),
        Row("Income tax expense", ["(3,450)", "(2,030)"]),
        Row("Profit for the period", ["10,352", "6,100"], bold=True),
        Row("Basic earnings per share (sen)", ["2.45", "1.45"]),
    ],
    column_x=[420.0, 540.0],
)


def merge_two_pages(page1_path: Path, page2_path: Path, out_path: Path) -> Path:
    combined = pymupdf.open()
    combined.insert_pdf(pymupdf.open(page1_path))
    combined.insert_pdf(pymupdf.open(page2_path))
    combined.save(out_path)
    combined.close()
    return out_path


def test_scorer_merges_a_statement_that_spans_two_pages(tmp_path: Path) -> None:
    """Real motivating case: a page showing "Revenue" through "Profit from
    operations" but stopping short of "Profit for the period" or EPS is
    genuinely incomplete, not a bad candidate - the fix is to look at the
    next page for the missing end line and merge it in, not reject the page."""
    page1 = build_statement_pdf(tmp_path / "p1.pdf", _IS_PAGE_1)
    page2 = build_statement_pdf(tmp_path / "p2.pdf", _IS_PAGE_2_CONTINUED)
    combined_path = merge_two_pages(page1, page2, tmp_path / "combined.pdf")

    result = find_statement_page(combined_path, Statement.INCOME_STATEMENT)

    assert result is not None
    assert result.page_no == 1
    assert result.continuation_page_no == 2
    labels = [row.label.lower() for row in result.table.rows]
    assert any("revenue" in lbl for lbl in labels)
    assert any("profit for the period" in lbl for lbl in labels)
    assert any("earnings per share" in lbl for lbl in labels)


def test_a_complete_single_page_statement_is_not_extended_unnecessarily(
    tmp_path: Path,
) -> None:
    """The usual case (no span needed) must stay untouched: a page with both
    the start and end line already present should report no continuation,
    even when a later page exists."""
    is_path = build_statement_pdf(tmp_path / "is.pdf", QUARTERLY_INCOME_STATEMENT)
    decoy_path = build_statement_pdf(tmp_path / "decoy.pdf", SINGLE_KEYWORD_NOTE_DECOY)
    combined_path = merge_two_pages(is_path, decoy_path, tmp_path / "combined.pdf")

    result = find_statement_page(combined_path, Statement.INCOME_STATEMENT)

    assert result is not None
    assert result.page_no == 1
    assert result.continuation_page_no is None


def test_span_merge_does_not_cross_into_a_different_statements_heading(
    tmp_path: Path,
) -> None:
    """If the very next page is genuinely a *different* statement (its own
    heading matches), that page is a new section, not a continuation - the
    merge must not happen even though the first page is itself incomplete."""
    page1 = build_statement_pdf(tmp_path / "p1.pdf", _IS_PAGE_1)  # no end line on this page
    real_bs = build_statement_pdf(tmp_path / "bs.pdf", BALANCE_SHEET)
    combined_path = merge_two_pages(page1, real_bs, tmp_path / "combined.pdf")

    result = find_statement_page(combined_path, Statement.INCOME_STATEMENT)

    # No end line ever turns up for the income statement in this document -
    # the truncated page 1 alone doesn't clear the keyword bar on its own
    # merits either (it never reaches "Profit before tax"/"Profit for the
    # period"), so this must be an honest miss, not a wrong merge into the
    # balance sheet.
    if result is not None:
        assert result.continuation_page_no is None
        assert not any("total assets" in row.label.lower() for row in result.table.rows)


def test_matches_concept_checks_the_named_concepts_own_phrasings() -> None:
    assert _matches_concept("Revenue for the period", "is.revenue")
    assert _matches_concept("Turnover increased", "is.revenue")  # a seeded synonym
    assert not _matches_concept("Total assets", "is.revenue")


def test_scorer_is_honest_when_no_page_is_the_statement(tmp_path: Path) -> None:
    """The CIMB case: not one page in the document is the real statement -
    the scorer must report nothing found, never force a wrong guess."""
    doc = pymupdf.open()
    for text in (NOTE_DECOY, NARRATIVE_DECOY, PURE_NUMBER_DECOY):
        text_page(doc, text)
    path = tmp_path / "no_statement_anywhere.pdf"
    doc.save(path)
    doc.close()

    assert find_statement_page(path, Statement.BALANCE_SHEET) is None


def test_scorer_works_for_the_income_statement_too(tmp_path: Path) -> None:
    is_only = build_statement_pdf(tmp_path / "is_only.pdf", QUARTERLY_INCOME_STATEMENT)
    combined = build_multi_page_pdf(
        tmp_path / "combined.pdf", is_only, [NOTE_DECOY, NARRATIVE_DECOY]
    )

    result = find_statement_page(combined, Statement.INCOME_STATEMENT)

    assert result is not None
    assert any("revenue" in row.label.lower() for row in result.table.rows)


# --------------------------------------------------------------------------
# Cross-statement proximity check - real filings print the three primary
# statements as one contiguous block far more often than not (confirmed
# against a real 47-company sample already extracted this project: 74% of
# companies where all three were found had them within 10 pages of each
# other). `select_statement_pages` uses that as a soft, second-look signal,
# never a hard rule - see its own docstring for why a hard distance cutoff
# would be wrong.
# --------------------------------------------------------------------------

# Adapted from the real bug: Vitrox Corporation's "income statement" winner
# was a 5-Year Financial Summary fragment - genuine income-statement-only
# subtotals (no balance-sheet or cash-flow vocabulary mixed in, so it passes
# every existing dominance/quality gate on its own merits) sitting 79-83
# pages from where its real balance sheet and cash flow statement were
# correctly found, and out-scoring the real, sparser income statement page
# on raw keyword/ratio terms alone.
_ISOLATED_HIGHLIGHTS_DECOY = StatementSpec(
    # A summary page *not* caught by is_excluded_page's heading list (a
    # "5-year financial summary" now is), so the proximity cross-check
    # below is still what has to reject it.
    title="GROUP PERFORMANCE AT A GLANCE",
    subtitle="(RM'000)",
    column_headers=[["2024", "2023", "2022", "2021"]],
    rows=[
        Row("Revenue", ["125,430", "110,220", "98,400", "91,200"]),
        Row("Gross profit", ["33,112", "26,565", "24,100", "21,800"]),
        Row("Profit before taxation", ["13,802", "8,130", "7,200", "6,500"]),
        Row("Profit for the period", ["10,352", "6,100", "5,400", "4,900"]),
    ],
)

# A real but sparse income statement - deliberately not titled in a way that
# matches the heading regex (mirroring a real, if less common, case: a
# genuine primary statement whose own heading text doesn't clear
# `_has_own_heading_match`, the same "no heading anywhere" shape CIMB Group
# hits elsewhere in this project) - competing on raw keyword/ratio score
# alone against the decoy above, and legitimately scoring lower.
_SPARSE_REAL_IS = StatementSpec(
    title="FINANCIAL RESULTS",
    subtitle="For the financial year ended 30 September 2024 (RM'000)",
    column_headers=[["2024", "2023"]],
    rows=[
        Row("Revenue", ["125,430", "110,220"]),
        Row("Staff and other operating costs", ["(115,078)", "(104,120)"]),
        Row("Profit before taxation", ["13,802", "8,130"], bold=True),
        Row("Profit for the period", ["10,352", "6,100"], bold=True),
    ],
    column_x=[420.0, 540.0],
)

_CASH_FLOW = StatementSpec(
    title="STATEMENT OF CASH FLOWS",
    subtitle="For the financial year ended 30 September 2024 (RM'000)",
    column_headers=[["2024", "2023"]],
    rows=[
        Row("Profit before tax", ["13,802", "8,130"], bold=True),
        Row("Depreciation of property, plant and equipment", ["4,220", "3,905"], indent=1),
        Row("Net cash from operating activities", ["18,455", "14,220"], bold=True),
        Row("Purchase of property, plant and equipment", ["(6,110)", "(5,440)"], indent=1),
        Row("Net cash used in investing activities", ["(6,110)", "(5,440)"], bold=True),
        Row("Dividends paid", ["(4,000)", "(3,500)"], indent=1),
        Row("Net cash used in financing activities", ["(4,000)", "(3,500)"], bold=True),
        Row("Cash and cash equivalents at beginning of year", ["38,220", "32,940"]),
        Row("Cash and cash equivalents at end of year", ["46,565", "38,220"], bold=True),
    ],
    column_x=[420.0, 540.0],
)


def test_select_statement_pages_swaps_a_distant_false_positive_for_a_nearby_real_page(
    tmp_path: Path,
) -> None:
    """The Vitrox regression: a plain, single-statement `find_statement_page`
    call picks the distant highlights decoy for the income statement here
    too (it legitimately out-scores the real page on its own terms) -
    `select_statement_pages`'s cross-check is what corrects it, falling
    back to the real income statement page that already qualified nearby
    but lost on raw score."""
    decoy_path = build_statement_pdf(tmp_path / "decoy.pdf", _ISOLATED_HIGHLIGHTS_DECOY)
    is_path = build_statement_pdf(tmp_path / "is.pdf", _SPARSE_REAL_IS)
    bs_path = build_statement_pdf(tmp_path / "bs.pdf", BALANCE_SHEET)
    cf_path = build_statement_pdf(tmp_path / "cf.pdf", _CASH_FLOW)

    combined = pymupdf.open()
    combined.insert_pdf(pymupdf.open(decoy_path))  # page 1 - the false positive
    for i in range(28):  # push the real cluster well past MAX_CLUSTER_DISTANCE
        text_page(combined, f"{NOTE_DECOY}\nFiller page {i}")
    combined.insert_pdf(pymupdf.open(is_path))  # page 30 - the real, sparser IS
    combined.insert_pdf(pymupdf.open(bs_path))  # page 31
    combined.insert_pdf(pymupdf.open(cf_path))  # page 32
    combined_path = tmp_path / "combined.pdf"
    combined.save(combined_path)
    combined.close()

    # Confirm the bug actually reproduces without the cross-check first -
    # a plain single-statement call really does pick the distant decoy.
    plain = find_statement_page(combined_path, Statement.INCOME_STATEMENT)
    assert plain is not None
    assert plain.page_no == 1

    winners = select_statement_pages(combined_path)
    assert winners[Statement.BALANCE_SHEET].page_no == 31
    assert winners[Statement.CASH_FLOW].page_no == 32
    assert winners[Statement.INCOME_STATEMENT].page_no == 30
    assert any(
        "revenue" in row.label.lower() for row in winners[Statement.INCOME_STATEMENT].table.rows
    )


def test_select_statement_pages_does_not_force_a_swap_with_no_alternative(
    tmp_path: Path,
) -> None:
    """Distance alone must never disqualify a candidate outright - only a
    nearer, already-qualifying alternative justifies reconsidering it. Every
    real large-span case actually re-checked after this was built turned
    out to be the Vitrox-style bug, not a legitimate exception (see
    `MAX_CLUSTER_DISTANCE`'s own comment in page_scoring.py) - but the
    mechanism still must not invent a swap out of nothing: here the cash
    flow statement is the only qualifying candidate anywhere in the
    document, so there is nothing to swap to, and the distant page must
    stand rather than being dropped or blocked."""
    is_path = build_statement_pdf(tmp_path / "is.pdf", QUARTERLY_INCOME_STATEMENT)
    bs_path = build_statement_pdf(tmp_path / "bs.pdf", BALANCE_SHEET)
    cf_path = build_statement_pdf(tmp_path / "cf.pdf", _CASH_FLOW)

    combined = pymupdf.open()
    combined.insert_pdf(pymupdf.open(is_path))  # page 1
    combined.insert_pdf(pymupdf.open(bs_path))  # page 2
    for i in range(28):
        text_page(combined, f"{NOTE_DECOY}\nFiller page {i}")
    combined.insert_pdf(pymupdf.open(cf_path))  # page 31 - the only CF candidate anywhere
    combined_path = tmp_path / "combined.pdf"
    combined.save(combined_path)
    combined.close()

    winners = select_statement_pages(combined_path)
    assert winners[Statement.INCOME_STATEMENT].page_no == 1
    assert winners[Statement.BALANCE_SHEET].page_no == 2
    assert winners[Statement.CASH_FLOW].page_no == 31


def test_select_statement_pages_leaves_a_tightly_clustered_result_untouched(
    tmp_path: Path,
) -> None:
    """The ordinary case (no cross-check needed) must produce exactly what
    three independent `find_statement_page` calls would have - confirming
    the cross-check is a no-op whenever nothing is actually an outlier."""
    is_path = build_statement_pdf(tmp_path / "is.pdf", QUARTERLY_INCOME_STATEMENT)
    bs_path = build_statement_pdf(tmp_path / "bs.pdf", BALANCE_SHEET)
    cf_path = build_statement_pdf(tmp_path / "cf.pdf", _CASH_FLOW)

    combined = pymupdf.open()
    combined.insert_pdf(pymupdf.open(is_path))  # page 1
    combined.insert_pdf(pymupdf.open(bs_path))  # page 2
    combined.insert_pdf(pymupdf.open(cf_path))  # page 3
    combined_path = tmp_path / "combined.pdf"
    combined.save(combined_path)
    combined.close()

    winners = select_statement_pages(combined_path)
    assert winners[Statement.INCOME_STATEMENT].page_no == 1
    assert winners[Statement.BALANCE_SHEET].page_no == 2
    assert winners[Statement.CASH_FLOW].page_no == 3


def test_stage1_never_returns_more_than_the_candidate_cap(tmp_path: Path) -> None:
    doc = pymupdf.open()
    for i in range(30):
        text_page(doc, f"{NOTE_DECOY}\nPage marker {i}")
    path = tmp_path / "many_decoys.pdf"
    doc.save(path)
    doc.close()

    from bursa.extract.page_scoring import _stage1_scan

    candidates = _stage1_scan(path, Statement.BALANCE_SHEET)
    assert len(candidates) <= MAX_CANDIDATES


# --------------------------------------------------------------------------
# Continuation-page column remapping: real bug found on Hong Leong
# Industries' balance sheet. Its continuation page (liabilities + the
# statement's own end line) detected its own column bands independently of
# the primary page (assets) - a wrapped label fragment ("Deferred tax"
# wrapping onto "liabilities" on the next line) landed just past the
# continuation page's own first-column boundary, creating an extra,
# spurious band there the primary page never had. Every real column on the
# continuation page - the Note-reference column and all four money columns
# - came out shifted one index to the right: "Total equity and liabilities"
# silently attributed to the wrong year's column, caught only downstream by
# `bs_balances`/`bs_footing` failing, nothing in extraction itself.
# --------------------------------------------------------------------------


def _band(index: int, x: float) -> ColumnBand:
    return ColumnBand(index=index, x_min=x - 2.0, x_max=x + 2.0, support=5)


def test_remap_corrects_a_continuation_pages_own_off_by_one_column_shift() -> None:
    """The exact Hong Leong Industries shape: the primary page's 4 real
    columns sit at bands 0-3; the continuation page's own (independent)
    column detection put them at 1-4 instead, band 0 there being a spurious
    one created by a stray wrapped-label fragment. Remapping by x-position
    against the *primary* page's bands must recover the original 0-3
    numbering and drop the stray fragment - never guess it into a real
    column."""
    primary_columns = [_band(0, 100.0), _band(1, 200.0), _band(2, 300.0), _band(3, 400.0)]

    continuation_row = ExtractedRow(
        row_index=0,
        label="Deferred tax",
        bbox=(10.0, 0.0, 400.0, 10.0),
        cells=[
            Cell(col_index=0, text="liabilities", bbox=(60.0, 0.0, 85.0, 10.0)),  # stray wrapped fragment
            Cell(col_index=1, text="2,922", bbox=(95.0, 0.0, 100.0, 10.0)),
            Cell(col_index=2, text="5,357", bbox=(195.0, 0.0, 200.0, 10.0)),
            Cell(col_index=3, text="-", bbox=(295.0, 0.0, 300.0, 10.0)),
            Cell(col_index=4, text="-", bbox=(395.0, 0.0, 400.0, 10.0)),
        ],
    )

    [remapped] = _remap_to_primary_columns([continuation_row], primary_columns)

    assert [(c.col_index, c.text) for c in remapped.cells] == [
        (0, "2,922"),
        (1, "5,357"),
        (2, "-"),
        (3, "-"),
    ]
    # The stray fragment at x=85 falls outside every primary band - dropped,
    # not guessed into band 0 just because it's the closest one.
    assert "liabilities" not in [c.text for c in remapped.cells]


def test_remap_leaves_an_already_aligned_continuation_page_unchanged() -> None:
    """The common case - no shift at all - must be a no-op, not just
    "happens to produce the same answer": confirms this fix doesn't
    regress the ordinary one-statement-one-page shape any multi-page
    statement whose continuation genuinely does line up still relies on."""
    primary_columns = [_band(0, 100.0), _band(1, 200.0)]
    row = ExtractedRow(
        row_index=0,
        label="Total equity and liabilities",
        bbox=(10.0, 0.0, 200.0, 10.0),
        cells=[
            Cell(col_index=0, text="2,807,309", bbox=(95.0, 0.0, 100.0, 10.0)),
            Cell(col_index=1, text="2,702,789", bbox=(195.0, 0.0, 200.0, 10.0)),
        ],
    )

    [remapped] = _remap_to_primary_columns([row], primary_columns)

    assert [(c.col_index, c.text) for c in remapped.cells] == [
        (0, "2,807,309"),
        (1, "2,702,789"),
    ]


def test_md_and_a_and_highlights_pages_are_excluded() -> None:
    from bursa.extract.classify import is_excluded_page

    assert is_excluded_page("Management Discussion and Analysis\nRevenue 614,372 RM'000")
    assert is_excluded_page("MANAGEMENT'S DISCUSSION & ANALYSIS\n...")
    assert is_excluded_page("MD&A (CONT'D)\nFinancial highlights")
    assert is_excluded_page("5-Year Financial Highlights\n2025 2024 2023")
    assert not is_excluded_page("CONSOLIDATED STATEMENT OF PROFIT OR LOSS\nFor the year ended")


def test_consolidated_page_beats_a_higher_scoring_company_only_page(tmp_path: Path) -> None:
    """Vitrox splits Group and Company statements onto separate pages; the
    company page ("STATEMENT OF ...", no "Consolidated") won on score and
    its RM 67m revenue was stored as the group's RM 750m."""
    import dataclasses

    from tests.fixtures.synthetic import INCOME_STATEMENT_WITH_DUPLICATE_OWNERS_SPLIT as base

    company = dataclasses.replace(
        base,
        title="STATEMENT OF PROFIT OR LOSS AND OTHER COMPREHENSIVE INCOME",
        column_headers=[["2024", "2023"]],
        rows=[*base.rows, Row("Gross profit", ["1,000", "900"]), Row("Finance costs", ["(10)", "(9)"])],
    )
    group = dataclasses.replace(
        base, title="CONSOLIDATED STATEMENT OF PROFIT OR LOSS AND OTHER COMPREHENSIVE INCOME"
    )

    combined = pymupdf.open()
    for name, spec in (("company", company), ("group", group)):
        part = pymupdf.open(build_statement_pdf(tmp_path / f"{name}.pdf", spec))
        combined.insert_pdf(part)
        part.close()
    path = tmp_path / "combined.pdf"
    combined.save(path)
    combined.close()

    winner = find_statement_page(path, Statement.INCOME_STATEMENT)

    assert winner is not None
    assert winner.page_no == 2


def test_consolidated_note_page_does_not_displace_a_group_column_statement(tmp_path: Path) -> None:
    """AMMB: the real statement is "Statements of profit or loss" with Group |
    Bank columns; Note 54 (Islamic banking operations) carries a
    "Consolidated statement of profit or loss" heading and must not win just
    for saying "consolidated"."""
    import dataclasses

    from tests.fixtures.synthetic import INCOME_STATEMENT_WITH_DUPLICATE_OWNERS_SPLIT as base

    real = base
    # The note's own table is a subset of the full statement.
    note = dataclasses.replace(
        base,
        title="54. OPERATIONS OF ISLAMIC BANKING - CONSOLIDATED STATEMENT OF PROFIT OR LOSS",
        column_headers=[["2024", "2023"]],
        rows=base.rows[: len(base.rows) // 2],
    )

    combined = pymupdf.open()
    for name, spec in (("real", real), ("note", note)):
        part = pymupdf.open(build_statement_pdf(tmp_path / f"{name}.pdf", spec))
        combined.insert_pdf(part)
        part.close()
    path = tmp_path / "combined.pdf"
    combined.save(path)
    combined.close()

    winner = find_statement_page(path, Statement.INCOME_STATEMENT)

    assert winner is not None
    assert winner.page_no == 1
