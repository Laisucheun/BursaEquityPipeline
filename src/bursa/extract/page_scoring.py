"""Two-layer page selection for the three primary statements.

`classify.py`'s heading-regex + carry-forward tagging is a single signal,
and measured against 11 real annual reports it failed in two different
ways: it missed a statement outright (CIMB Group: zero candidate pages for
any of the three statements - the heading text simply never matched), and
it carried a genuine hit forward onto pages that merely *mention* numbers
without being the statement at all. Every one of these was confirmed by
reading the actual extracted row labels, not guessed at:

* Tenaga Nasional's "cash flow" page was a restatement footnote
  ("Taxation and zakat", "LIST OF SUBSIDIARIES").
* Axiata's "balance sheet" page was share-price commentary
  ("Positive performance continued in 3Q25...").
* IOI's "balance sheet" was a cash-flow reconciliation note; its "cash
  flow" page was an impairment/fair-value disclosure note.
* PETRONAS Dagangan's "balance sheet" was a tax-loss-carryforward note.
* AMMB's "balance sheet" was pure strategy narrative, barely a number on it.
* Supermax's "balance sheet" was an ESG "value added statement", not the
  financial one.

None of these are fixed by adjusting one heading regex - they're different
pages entirely, each with its own reason a numeric-density carry-forward
would latch onto it. The fix here is two layers:

**Layer 1 (recall)** - score *every* page in the document cheaply, on its
raw text alone: how many of the statement's anchor keywords appear, and its
numeric-token-to-word ratio (a real statement page is overwhelmingly
numeric; a notes/narrative page has long sentences with the occasional
figure). Keep the top `MAX_CANDIDATES` pages. This is deliberately wider
than classify.py's single carry-forward run, specifically so a statement
with no heading match anywhere still gets a chance.

**Layer 2 (precision)** - extract each candidate's actual table and re-score
using its *row labels* specifically, not raw page text (which is polluted by
running headers and table-of-contents strings), plus the fraction of
extracted rows that are genuinely numeric. The winner is the final page.

The keyword set is not a new hand-written list - it's pulled directly from
`bursa.mapping.taxonomy`'s subtotal concepts for each statement ("Total
assets", "Profit before tax", "Net cash from operating activities", ...).
Those are exactly the phrases a genuinely filed primary statement almost
always contains and a notes/narrative page almost never does, and reusing
them keeps one taxonomy as the source of truth instead of a parallel list
that could drift from it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from bursa.db.enums import Statement
from bursa.extract.classify import is_excluded_page
from bursa.extract.layout import Cell, ColumnBand, ExtractedRow, ExtractedTable, extract_page
from bursa.mapping.taxonomy import CONCEPTS_BY_KEY, concepts_for
from bursa.normalize.numbers import parse_number

MAX_CANDIDATES = 10
MIN_PAGE_NUMERIC_TOKENS = 4  # a page with almost no numbers can't be a statement table

# The usual first line item of each statement, reusing existing taxonomy
# concepts rather than a new list. These are deliberately the *usual* line,
# not a universal one - they are one more signal fed into the score, not a
# hard requirement on their own. A bank's income statement and balance sheet
# both start differently from a general corporate's: banks list assets by
# liquidity, not by permanence (cash first, PPE - if present at all - is a
# minor line near the bottom), and lead their income statement with interest
# income rather than a general "Revenue" line. Confirmed real on Public
# Bank's genuine statements: without the bank-specific alternative, neither
# page could ever register a start line, which is what let each lose to a
# wrong page that happened to score higher elsewhere. Each statement maps to
# a tuple of acceptable starts - any one of them found is enough, matching
# the same reasoning already applied to `END_CONCEPT` below.
#
# Cash flow statements specifically have a second real presentation choice
# on top of that: the reconciliation can start from "Profit before tax"
# (the usual case) or from "Profit for the financial year" (net, after
# tax) - both are legitimate MFRS approaches. Confirmed real: SD Guthrie's
# genuine cash flow statement opens with "Profit for the financial year",
# never "Profit before tax" anywhere on its own page, so a single hard-
# coded start concept made it permanently ineligible for the continuation
# search - without `has_start`, the merge that pulls in page 58's real
# financing/cash-position lines never even gets attempted. Reusing
# `is.profit_for_period` (the income statement's own concept, already
# carrying "profit for the year"/"net profit" among its synonyms) is a
# deliberate cross-statement reuse of concept *phrasings*, not of
# ownership - `_matches_any_concept` only checks whether the text is
# present, nothing here asserts the page is an income statement.
START_CONCEPT: dict[Statement, tuple[str, ...]] = {
    Statement.INCOME_STATEMENT: ("is.revenue", "is.finance_income"),
    Statement.BALANCE_SHEET: ("bs.ppe", "bs.cash_and_equivalents"),
    Statement.CASH_FLOW: ("cf.profit_before_tax", "is.profit_for_period"),
    Statement.EQUITY: ("eq.opening_balance",),
}
# A balance sheet's usual *end* line depends on which of two equally common
# presentation formats the filer chose: "total assets = total equity and
# liabilities" ends on that combined subtotal, but the "net assets" format
# (assets less liabilities, arriving at total net assets) ends on "total
# equity" alone instead - it never states "total equity and liabilities"
# anywhere. Confirmed real and not rare: Tenaga Nasional's genuine balance
# sheet uses the net-assets format throughout, so a single hard-coded end
# concept made its real, correctly-merged statement permanently ineligible
# for the start/end completeness bonus, while a restatement note that
# happened to reproduce the same line items on one dense page kept winning
# on score instead. Each statement maps to a tuple of acceptable endings -
# any one of them found is enough.
END_CONCEPT: dict[Statement, tuple[str, ...]] = {
    Statement.INCOME_STATEMENT: ("is.profit_for_period",),
    Statement.BALANCE_SHEET: ("bs.total_equity_and_liabilities", "bs.total_equity"),
    Statement.CASH_FLOW: ("cf.cash_end",),
    Statement.EQUITY: ("eq.closing_balance",),
}


def _matches_any_concept(label_blob: str, concept_keys: tuple[str, ...]) -> bool:
    return any(_matches_concept(label_blob, key) for key in concept_keys)

# A statement whose start line is found but whose end line isn't usually
# continues onto the very next page, rarely a second - real Malaysian
# annual reports checked so far run at most one page long for a primary
# statement, but this stays a small search window rather than an assumption.
MAX_CONTINUATION_PAGES = 5

# A clean row label is short - a line item name, not a paragraph. A page
# where the layout extractor has blended a real table's columns with an
# adjacent narrative/highlights column produces long, run-on labels instead
# (confirmed on two real pages: genuinely balance-sheet vocabulary, entirely
# unusable rows). More than this fraction of long rows on a continuation
# candidate means "corrupted extraction", not "genuine continuation".
MAX_GARBLED_ROW_FRACTION = 0.3

# Finding both the usual start line and the usual end line (on one page, or
# after a continuation merge) is strong independent evidence this candidate
# really is the complete statement, not a fragment - added on top of, not
# instead of, the keyword/heading scoring above.
START_END_BONUS = 20.0

# A single subtotal keyword is not enough to tell a real primary statement
# apart from a page that only happens to touch on one of them. Checked
# against three confirmed false positives - Genting's "Note 9: Profit Before
# Taxation" disclosure, AMMB's 5-year financial highlights table, Axiata's
# Alternative Performance Measures glossary - all three passed a >=1 gate
# through the exact same single keyword ("profit before tax") and nothing
# else. A real primary statement carries several of its own subtotals at
# once (an income statement shows gross profit *and* profit before tax *and*
# profit for the period; a balance sheet shows total assets *and* total
# equity), so requiring at least two distinct hits rejects a note or summary
# page built around one figure without rejecting a genuine statement.
MIN_ROW_KEYWORD_HITS = 2

# Layer 1 weights: a keyword hit is worth far more than ratio alone - a
# page can be numerically dense without being a statement (a notes page
# full of dollar figures), but "Total assets" appearing on it is a strong,
# specific signal a generic ratio can't fake.
STAGE1_KEYWORD_WEIGHT = 10.0
STAGE1_RATIO_WEIGHT = 1.0

# Layer 2: row-label keyword hits count even more heavily, since they're
# matched against actual line items rather than a whole page's prose.
STAGE2_KEYWORD_WEIGHT = 15.0
STAGE2_ROW_RATIO_WEIGHT = 3.0

MIN_FINAL_SCORE = 1.0  # below this, report not-found rather than guess


def _keywords_for(statement: Statement) -> list[list[str]]:
    """The anchor phrasings for one statement's subtotal concepts, grouped
    *by concept* - not a hand-maintained list, so it can't drift from the
    taxonomy those concepts already define, and not a flat list either, so
    that a concept's several phrasings are counted as evidence for one
    concept, never several.

    Every seeded synonym counts, not just the canonical label. That matters
    specifically for cash flow: real filings almost never write the bare
    label "Net cash from operating activities" verbatim - they insert words
    ("Net cash flows *generated from* operating activities", "Cash *from
    operations* before working capital changes"), which defeated an earlier,
    bare-label version of this function against a real filing (confirmed: it
    scored zero keyword hits on the genuine cash flow statement page, the
    same page a human can see is unmistakably the real one). The seed
    synonym table in `bursa.mapping.taxonomy` already curates these
    real-world phrasings for exactly this reason - reusing it here keeps one
    source of truth instead of a second, narrower list that quietly drifts
    from it.

    The grouping is just as important as adding the synonyms: several
    phrasings of "profit before tax" appearing together must still count as
    *one* matched concept, not several - otherwise a single-concept note
    (the exact false positive `MIN_ROW_KEYWORD_HITS` exists to reject) could
    clear that gate just by using more than one of that concept's own
    synonyms in its own text.
    """
    groups: list[list[str]] = []
    for concept in concepts_for(statement):
        if not concept.is_subtotal:
            continue
        phrasings = [concept.label.lower(), *(s.lower() for s in concept.synonyms)]
        groups.append(phrasings)
    return groups


# A real, common cash-flow-statement convention: showing both the
# positive- and negative-case wording for one subtotal in a single label via
# a slash-parenthetical, in either order - "from/(used in)", "(used in)/
# from", "from/(for)", "(for)/from", "generated from/(used in)", and so on.
# Confirmed real on five separate filers (Frontken, Kerjaya Prospek, Gas
# Malaysia, Astro, SD Guthrie), each choosing a different combination of
# {plain / "generated"} x {"from" / "for"} x {parenthetical-before /
# parenthetical-after}. Flat substring matching against one canonical
# phrasing can never match these, and hand-enumerating every combination
# doesn't scale (this concept group has already been extended three times
# this session for exactly this reason and still missed real cases) -
# collapsing to just the non-parenthetical side, whichever side it's on,
# lets the existing canonical synonyms match regardless of which real
# convention a filer picked.
_BIDIRECTIONAL_VARIANT = re.compile(
    r"(?P<lead>from|for|generated\s+from|used\s+in)\s*/\s*\((?:from|for|generated\s+from|used\s+in)\)"
    r"|\((?:from|for|generated\s+from|used\s+in)\)\s*/\s*(?P<trail>from|for|generated\s+from|used\s+in)",
    re.IGNORECASE,
)

# The rarer case with no slash-parenthetical at all - a filer simply writes
# "for" where every other seen filer writes "from" ("Net Cash For Financing
# Activities", confirmed real on Frontken). Scoped tightly to this exact
# context (immediately between "cash" and an activities subtotal) rather
# than replacing "for" generally, which would corrupt unrelated text ("a
# provision for...", "accounted for...").
_BARE_FOR_DIRECTION = re.compile(
    r"\bcash\s+for\s+(operating|investing|financing)\s+activities\b", re.IGNORECASE
)


def _normalize(text: str) -> str:
    """Decompose typeset ligatures ("proﬁt" -> "profit", "ﬁnancial" ->
    "financial") before any keyword or heading match. Most glossy annual
    reports are set with a font that renders "fi"/"fl"/"ffi" as a single
    ligature glyph, which never matches a plain-ASCII synonym string via
    substring search - confirmed on a real page (Genting's income statement)
    that scored zero keyword hits despite being the genuine, cleanly
    extracted statement, solely because its subtotal rows ("Gross proﬁt",
    "Proﬁt before tax") used the ligature form throughout. NFKC handles this
    (and the equivalent cases for "fl", "ffi", "ffl") as a standard
    compatibility decomposition - no hand-maintained replacement table
    needed.

    Also collapses the two cash-flow bidirectional-wording conventions
    described above, for the same reason - a real phrasing variant that
    defeats substring matching, not just a font-rendering quirk."""
    text = unicodedata.normalize("NFKC", text)
    text = _BIDIRECTIONAL_VARIANT.sub(lambda m: m.group("lead") or m.group("trail"), text)
    text = _BARE_FOR_DIRECTION.sub(lambda m: f"cash from {m.group(1)} activities", text)
    # Raw page text (used by the cheap stage-1 scan, before row extraction
    # has a chance to geometrically reconstruct wrapped labels into one
    # line) routinely line-wraps a subtotal phrase mid-synonym - confirmed
    # real on Kerjaya Prospek: "Net cash generated from/(used in)\n
    # operating activities" is two lines in the PDF's own content stream,
    # so a synonym requiring a single space between "from" and "operating"
    # can never match as a plain substring. Collapsing every run of
    # whitespace (newlines included) to one space is safe here - it only
    # affects whitespace, never word content - and fixes this regardless of
    # where a filer's own page layout happens to wrap.
    text = re.sub(r"\s+", " ", text)
    return text


def _count_keyword_hits(text: str, keyword_groups: list[list[str]]) -> int:
    """Count of *distinct concepts* with at least one matching phrasing -
    never a raw count of matching strings, which would let one concept's
    several synonyms masquerade as several different concepts matched.

    This is also the mechanism behind "more keywords matched means higher
    confidence, without requiring every one of them": nothing here demands
    a complete set - `row_hits` simply grows as more distinct concepts are
    found, `STAGE2_KEYWORD_WEIGHT` turns that count directly into score, and
    only the floor (`MIN_ROW_KEYWORD_HITS`) is a hard requirement.
    """
    text_low = _normalize(text).lower()
    return sum(1 for phrasings in keyword_groups if any(p in text_low for p in phrasings))


def _is_row_quality_ok(rows: list) -> bool:
    """False when too many rows read as corrupted - long, run-on labels are
    the symptom of the layout extractor blending a real table's columns with
    an adjacent narrative/highlights column (confirmed on two real pages).
    Applied to *every* candidate's own table, not just a continuation
    candidate's - the primary page can be the garbled one just as easily."""
    if not rows:
        return False
    long_labels = sum(1 for row in rows if row.label and len(row.label) > 60)
    return long_labels <= len(rows) * MAX_GARBLED_ROW_FRACTION


def _matches_concept(label_blob: str, concept_key: str) -> bool:
    """Whether any phrasing of one specific concept (its canonical label or
    any seeded synonym) appears in the text - used for the start/end line
    check, where exactly one named concept matters, not a whole group."""
    spec = CONCEPTS_BY_KEY[concept_key]
    phrasings = [spec.label.lower(), *(s.lower() for s in spec.synonyms)]
    text_low = _normalize(label_blob).lower()
    return any(p in text_low for p in phrasings)


def _stage1_style_score(text: str, keywords: list[list[str]]) -> float:
    """The same keyword+ratio formula `_stage1_scan` scores a whole document
    with, applied to one page's text on demand - used to credit a
    continuation page's own layer-1 evidence (see `_extend_for_continuation`)."""
    words = text.split()
    if not words:
        return 0.0
    numeric = sum(1 for w in words if parse_number(w) is not None)
    ratio = numeric / len(words)
    hits = _count_keyword_hits(text, keywords)
    return hits * STAGE1_KEYWORD_WEIGHT + ratio * STAGE1_RATIO_WEIGHT


def _remap_to_primary_columns(
    rows: list[ExtractedRow], primary_columns: list[ColumnBand]
) -> list[ExtractedRow]:
    """Re-bucket a continuation page's own cells into the *primary* page's
    column bands by x-coordinate, discarding the continuation page's own
    independently-detected column numbering entirely.

    `detect_columns` runs per-page, in isolation - trusting a continuation
    page's own column indices assumes they line up with the primary page's,
    which is not guaranteed even for two pages of the exact same table.
    Confirmed real and silently wrong on Hong Leong Industries' balance
    sheet: a wrapped label fragment ("Deferred tax" wrapping onto
    "liabilities" on the next line) landed just past the continuation
    page's own first-column boundary, creating an extra, spurious band
    there that the primary page never has - shifting every real column
    (the Note-reference column and all four money columns) one index to
    the right for every continuation row. Silently: nothing crashed or
    looked obviously wrong, it just attributed "Total equity and
    liabilities" to the wrong year's column, caught only by the validation
    rules it later failed (`bs_balances`/`bs_footing`), not by anything in
    extraction itself.

    A cell whose own position doesn't fall inside any of the primary page's
    real bands is dropped, never guessed into the nearest one - same
    "skip, don't guess" discipline as everywhere else in this pipeline. In
    practice this is exactly the kind of stray wrapped-label fragment that
    caused the spurious band in the first place; the row's own `label`
    already carries its *first* line correctly from this page's own
    extraction; losing a wrapped-continuation word that leaked into cell
    data is a far smaller loss than misattributing a real money figure.
    """
    remapped: list[ExtractedRow] = []
    for row in rows:
        new_cells: list[Cell] = []
        for cell in row.cells:
            band = next((b for b in primary_columns if b.contains(cell.bbox[2])), None)
            if band is None:
                continue
            new_cells.append(Cell(col_index=band.index, text=cell.text, bbox=cell.bbox))
        row.cells = new_cells
        remapped.append(row)
    return remapped


def _extend_for_continuation(
    doc, statement: Statement, table: ExtractedTable, start_page_no: int
) -> tuple[ExtractedTable, int | None, float]:
    """If `table` has the statement's usual start line but not its usual end
    line, check the next page(s) for the end line and merge rows in - the
    "found the start, not the end, so it must continue" case. Returns the
    (possibly merged) table, the continuation page number (or `None` if no
    end line turns up within the search window), and that continuation
    page's own layer-1-style score (0.0 if there was no continuation).

    That third value matters: a genuine multi-page statement often splits
    its distinctive vocabulary unevenly - confirmed on Tenaga Nasional's
    real balance sheet, where the primary page (assets) scores far lower on
    layer 1 than its continuation (liabilities and equity, which is where
    most of the taxonomy's subtotal keywords actually land). Scoring the
    merged statement using only the *primary* page's layer-1 score starves
    it of evidence its own continuation legitimately carries, and a dense
    single-page restatement note reproducing the same line items can then
    outscore the real, correctly-merged statement on that gap alone -
    confirmed by re-running the real numbers before and after this fix.
    """
    end_concepts = END_CONCEPT.get(statement)
    if end_concepts is None:
        return table, None, 0.0

    label_blob = " ".join(row.label for row in table.rows if row.label)
    if _matches_any_concept(label_blob, end_concepts):
        return table, None, 0.0  # already complete on this page - nothing to extend

    for offset in range(1, MAX_CONTINUATION_PAGES + 1):
        next_page_no = start_page_no + offset
        if next_page_no > doc.page_count:
            break

        next_page = doc[next_page_no - 1]
        next_text = next_page.get_text("text", sort=True)
        # A *different* statement's own heading here means this is a new
        # section, not a continuation - a real continuation page repeats the
        # same heading (often "(CONTINUED)") or carries none of its own.
        if any(_has_own_heading_match(next_text, other) for other in _other_statements(statement)):
            break
        if is_excluded_page(next_text):
            # A notes/restatement page immediately following the primary
            # candidate is not its continuation, however tempting its own
            # vocabulary looks - see the matching check in `_stage2_score`.
            continue

        next_table = extract_page(next_page, next_page_no, statement)
        if next_table is None or not next_table.rows:
            continue

        next_blob = " ".join(row.label for row in next_table.rows if row.label)
        if not _matches_any_concept(next_blob, end_concepts):
            continue

        # Reject a continuation whose rows read as corrupted, independent of
        # whether its terms lean the right way - see _is_row_quality_ok.
        if not _is_row_quality_ok(next_table.rows):
            continue

        # The candidate page's own content must itself lean toward this
        # statement, checked *in isolation* - not blended with the page
        # already accumulated. Blended counts are not enough: a real page
        # with many genuine hits of its own (confirmed on PETRONAS
        # Chemicals' income statement) can absorb a wrongly-merged summary
        # page's contaminating balance-sheet terms without the *combined*
        # ratio ever tipping over, even though the merge itself was wrong.
        # Checking the candidate alone catches what the blended check can't.
        keywords = _keywords_for(statement)
        other_groups = [_keywords_for(s) for s in _other_statements(statement)]
        next_hits = _count_keyword_hits(next_blob, keywords)
        next_other_hits = max((_count_keyword_hits(next_blob, g) for g in other_groups), default=0)
        if next_hits <= next_other_hits:
            # Strict, matching the main dominance check exactly - a tie is
            # still ambiguous. A genuine continuation page has few or zero
            # other-statement hits of its own; letting a tie through is
            # exactly what let the wrongly-merged page back in the first
            # time this was checked against real data.
            continue

        offset_base = max((row.row_index for row in table.rows), default=0) + 1
        remapped_rows = _remap_to_primary_columns(next_table.rows, table.columns)
        for row in remapped_rows:
            row.row_index += offset_base
        table.rows = [*table.rows, *remapped_rows]
        continuation_score = _stage1_style_score(next_page.get_text("text", sort=True), keywords)
        return table, next_page_no, continuation_score

    return table, None, 0.0


@dataclass
class PageCandidate:
    page_no: int
    stage1_score: float
    numeric_tokens: int
    word_count: int


@dataclass
class ScoredStatementPage:
    page_no: int
    final_score: float
    stage1_score: float
    row_keyword_hits: int
    numeric_row_ratio: float
    table: ExtractedTable
    continuation_page_no: int | None = None


def _stage1_scan(pdf_path: Path, statement: Statement) -> list[PageCandidate]:
    """Score every page cheaply from its raw text alone; no table
    extraction yet - that's layer 2, and only for the survivors here."""
    import pymupdf

    keywords = _keywords_for(statement)
    candidates: list[PageCandidate] = []

    with pymupdf.open(pdf_path) as doc:
        for index, page in enumerate(doc, start=1):
            text = page.get_text("text", sort=True)
            words = text.split()
            if not words:
                continue
            numeric = sum(1 for w in words if parse_number(w) is not None)
            if numeric < MIN_PAGE_NUMERIC_TOKENS:
                continue

            score = _stage1_style_score(text, keywords)

            candidates.append(
                PageCandidate(
                    page_no=index, stage1_score=score, numeric_tokens=numeric, word_count=len(words)
                )
            )

    candidates.sort(key=lambda c: c.stage1_score, reverse=True)
    return candidates[:MAX_CANDIDATES]


_ALL_STATEMENTS = (Statement.INCOME_STATEMENT, Statement.BALANCE_SHEET, Statement.CASH_FLOW, Statement.EQUITY)


def _other_statements(statement: Statement) -> list[Statement]:
    return [s for s in _ALL_STATEMENTS if s != statement]


def _has_own_heading_match(text: str, statement: Statement) -> bool:
    """Whether this *specific* page's own text carries a direct heading
    match for the statement - `classify_page_text`'s regex, reused as-is,
    called fresh per-candidate rather than through `classify_document`'s
    whole-document carry-forward. That distinction is exactly what makes
    this trustworthy where carry-forward wasn't: carry-forward is how a
    genuine hit spreads onto a following notes/highlights page in the first
    place, so checking a candidate page in isolation - does *this* page's
    own text say "Statement of Profit or Loss", not "Financial Highlights"
    or "Five-Year Group Financial Summary" - sidesteps that contamination
    entirely. Confirmed on two real pages that a raw keyword/ratio score
    could not tell apart from the genuine statement: both carried a
    different heading of their own the regex correctly does not match.
    """
    from bursa.extract.classify import classify_page_text

    result = classify_page_text(0, text)
    return result.statement == statement and result.confidence >= 0.5


@dataclass
class _Qualifying:
    page_no: int
    table: ExtractedTable
    row_hits: int
    row_ratio: float
    has_heading: bool
    score: float
    continuation_page_no: int | None = None


def _stage2_rank(
    pdf_path: Path, statement: Statement, candidates: list[PageCandidate]
) -> list[ScoredStatementPage]:
    """Extract each stage-1 survivor's table and re-score from its actual
    row labels - the precise pass. Returns every candidate that clears
    `MIN_FINAL_SCORE`, best first (an empty list is an honest miss, not a
    forced guess) - `find_statement_page` takes just the head of this for
    the normal single-statement case; `select_statement_pages` also uses
    the rest as fallback candidates for its cross-statement proximity
    check (see that function).

    Candidates that pass every keyword/ratio check are further split by
    whether the page's *own* heading names the statement directly
    (`_has_own_heading_match`) - a heading-matched candidate is always
    preferred over one without, since that's the single most reliable signal
    available once it exists. Falling back to the keyword/ratio score alone
    when *no* candidate has a heading match is what keeps recall for a
    document where the heading text simply never matches anywhere (CIMB
    Group: zero heading matches for any of the three statements, yet a real
    primary statement is still findable on keyword/ratio grounds alone).
    """
    import pymupdf

    keywords = _keywords_for(statement)
    other_keyword_groups = [_keywords_for(s) for s in _other_statements(statement)]
    qualifying: list[_Qualifying] = []

    with pymupdf.open(pdf_path) as doc:
        for candidate in candidates:
            page = doc[candidate.page_no - 1]
            if is_excluded_page(page.get_text("text", sort=True)):
                # classify.py already knows for certain this page is a table
                # of contents, notes/restatement page, directors'/auditors'
                # section, etc. - never eligible to win regardless of how
                # dense or keyword-rich it reads. Checked before extraction
                # even runs, since a restatement note can legitimately win on
                # raw score once it slips past every other gate (confirmed
                # real: a reconciliation table denser, row for row, than the
                # genuine statement it discloses changes to).
                continue
            table = extract_page(page, candidate.page_no, statement)
            if table is None or not table.rows:
                continue
            if not _is_row_quality_ok(table.rows):
                # The primary candidate itself can be the corrupted page,
                # not only a continuation - confirmed on a real page mixing
                # a balance sheet with an adjacent OCI/narrative column.
                continue

            label_blob = " ".join(row.label for row in table.rows if row.label)

            # Found the usual start line but not the usual end line on this
            # page? It very likely continues onto the next page (rarely a
            # second) - merge that continuation in before scoring, so the
            # keyword/ratio checks below see the *complete* statement, not
            # a truncated first page.
            continuation_page_no = None
            continuation_stage1_score = 0.0
            start_concepts = START_CONCEPT.get(statement)
            has_start = start_concepts is not None and _matches_any_concept(label_blob, start_concepts)
            if has_start:
                table, continuation_page_no, continuation_stage1_score = _extend_for_continuation(
                    doc, statement, table, candidate.page_no
                )
                label_blob = " ".join(row.label for row in table.rows if row.label)

            end_concepts = END_CONCEPT.get(statement)
            has_end = end_concepts is not None and _matches_any_concept(label_blob, end_concepts)

            row_hits = _count_keyword_hits(label_blob, keywords)
            if row_hits < MIN_ROW_KEYWORD_HITS:
                # A hard gate, not just a heavy weight: a numerically dense
                # page (a trading-volume table, a tax-loss note) can still
                # out-score the real statement on ratio alone if this is only
                # a weighted component - confirmed by a real test failure.
                # And one keyword alone isn't enough either - see
                # MIN_ROW_KEYWORD_HITS.
                continue

            # The already-selected candidate's own text, not a fresh
            # whole-page read: for a spread page (two different statements
            # sharing one physical page - see layout.py's `_is_spread_page`),
            # the raw page text contains *both* statements' headings, and
            # classify_page_text's fixed heading-check order would always
            # resolve to the same one regardless of which statement is
            # actually being scored here. `table.raw_text` is already
            # isolated to the correctly-selected half for a spread page (and
            # equivalent to the whole page for an ordinary one) - unlike
            # `page_text_head` (deliberately numeric-free for the mapper),
            # it keeps real figures in, which classify_page_text's own
            # numeric-density confidence check needs to fire at all; using
            # the numeric-free text here was tried and confirmed to silently
            # cap every page's heading-match confidence at 0.25, regressing
            # a real, already-passing false-positive-rejection test. Computed
            # here, ahead of the dominance check below, specifically so a
            # direct heading match can rescue a candidate from it - see that
            # check's own comment for why.
            has_heading = _has_own_heading_match(table.raw_text, statement)

            # A genuine primary statement page is overwhelmingly about its
            # own statement - a "5-Year Financial Highlights" summary or an
            # Alternative Performance Measures glossary instead straddles
            # several, since that's their whole purpose. Requiring more
            # keyword diversity doesn't fix this on its own (both real
            # false positives and real correct pages can tie on raw counts -
            # confirmed) but combined with the heading-match preference below
            # it still helps separate close cases.
            #
            # A direct own-heading match exempts a candidate from this check
            # entirely - confirmed real and structural, not a one-off:
            # a cash flow statement conventionally *starts* by reconciling
            # from "Profit before tax" and often carries "Operating profit
            # before working capital changes" as an interim subtotal, both
            # genuine CF content that also matches the income statement's own
            # keyword groups by MFRS convention, not by accident. Kerjaya
            # Prospek's real, correctly-headed "Statements of Cash Flows"
            # page ties 2-2 against the income statement's keywords on
            # exactly this overlap and was wrongly rejected before this
            # exemption existed. The heading regex (`classify.py`) is narrow
            # and specific ("statement(s) of cash flow(s)", not "financial
            # highlights" or any looser phrasing) - none of this project's
            # confirmed real false positives (Genting's highlights page,
            # AMMB's narrative page, the Vitrox-style summary decoy) have
            # ever carried a genuine statement heading, so this exemption
            # does not reopen any of those.
            other_hits = max(
                (_count_keyword_hits(label_blob, g) for g in other_keyword_groups), default=0
            )
            if row_hits <= other_hits and not has_heading:
                continue

            numeric_rows = sum(
                1
                for row in table.rows
                if any(parse_number(cell.text) is not None for cell in row.cells)
            )
            row_ratio = numeric_rows / len(table.rows) if table.rows else 0.0
            score = (
                row_hits * STAGE2_KEYWORD_WEIGHT
                + row_ratio * STAGE2_ROW_RATIO_WEIGHT
                + candidate.stage1_score  # carry layer 1's signal forward too
                + continuation_stage1_score  # ...and the continuation page's own, if merged
            )
            if has_start and has_end:
                score += START_END_BONUS

            qualifying.append(
                _Qualifying(
                    candidate.page_no, table, row_hits, row_ratio, has_heading, score,
                    continuation_page_no,
                )
            )

    if not qualifying:
        return []

    heading_matched = [q for q in qualifying if q.has_heading]
    pool = heading_matched or qualifying  # prefer a real heading; fall back for recall
    ranked = sorted(pool, key=lambda q: q.score, reverse=True)

    return [
        ScoredStatementPage(
            page_no=q.page_no,
            final_score=q.score,
            stage1_score=q.score,  # informational only past this point
            row_keyword_hits=q.row_hits,
            numeric_row_ratio=q.row_ratio,
            table=q.table,
            continuation_page_no=q.continuation_page_no,
        )
        for q in ranked
        if q.score >= MIN_FINAL_SCORE
    ]


def _ranked_statement_pages(pdf_path: Path, statement: Statement) -> list[ScoredStatementPage]:
    """Every qualifying page for one statement, best first - the full pool
    `find_statement_page` picks its single winner from."""
    candidates = _stage1_scan(pdf_path, statement)
    if not candidates:
        return []
    return _stage2_rank(pdf_path, statement, candidates)


def find_statement_page(pdf_path: Path, statement: Statement) -> ScoredStatementPage | None:
    """The full two-layer selection for one statement. See module docstring."""
    ranked = _ranked_statement_pages(pdf_path, statement)
    return ranked[0] if ranked else None


# Real filings print the three primary statements as one contiguous block
# (balance sheet, income statement, statement of changes in equity, cash
# flow statement, in whichever order the filer chose) far more often than
# not - confirmed against a real 47-company sample already extracted this
# project: of the companies where all three were found, 74% landed within
# 10 pages of each other. But it is not a safe *hard* rule on its own - a
# real filing can legitimately separate one statement far from the rest, so
# distance alone must never disqualify a candidate outright; it only
# justifies a second look, and only when a nearer, *already-qualifying*
# alternative exists for that same statement (a low-ranked runner-up is not
# a wild guess - it already cleared every correctness gate in
# `_stage2_rank`, just scored lower than the outlier that won).
#
# Confirmed real and worth remembering both ways: every real, large-span
# outlier actually re-checked after this was built turned out to be exactly
# the bug this exists to catch, not a legitimate exception. AMMB Holdings'
# "cash flow statement" 183 pages from its balance sheet was itself a false
# positive - a segment note ("54. Operations of Islamic Banking (CONT'D.)")
# reproducing a cash flow table, with the real primary statement sitting
# correctly nearby (pages 27-29, immediately before "Notes to the Financial
# Statements" begins) but scoring lower and losing on raw terms alone.
# Similarly, RHB Bank's balance sheet winner (page 153, scoring highest of
# all candidates) was "Note 55: Financial Risk Management (CONTINUED) -
# Liquidity Risk", not the statement; the real one, correctly recovered by
# this check, is explicitly headed "Statutory Financial Statements -
# Statements of Financial Position". No genuine real-filing counter-example
# (a legitimately distant, correctly-selected statement with a worse-but-
# real alternate nearby) has been found yet - if one turns up, it belongs
# here as a documented, deliberate exception, not silently assumed away.
MAX_CLUSTER_DISTANCE = 20  # pages


def select_statement_pages(pdf_path: Path) -> dict[Statement, ScoredStatementPage]:
    """Find all three primary statements in one document, then apply the
    proximity cross-check described above. Confirmed real and not rare:
    Vitrox Corporation's "income statement" winner (page 22, a 5-Year
    Financial Highlights fragment, 0 of 8 rows genuinely mapped) sat 79-83
    pages from its real, correctly-found balance sheet and cash flow
    statement - exactly the shape this check exists to catch. AMMB Holdings
    and RHB Bank (see the constant's own comment above) are further real,
    independently-confirmed cases of the identical bug, caught the same way.
    """
    ranked = {statement: _ranked_statement_pages(pdf_path, statement) for statement in _ALL_STATEMENTS}
    winners = {
        statement: candidates[0] for statement, candidates in ranked.items() if candidates
    }

    for statement, candidates in ranked.items():
        if statement not in winners or len(candidates) < 2:
            continue  # nothing found, or no runner-up to fall back on
        others = [w.page_no for s, w in winners.items() if s != statement]
        if not others:
            continue  # only one statement found at all - no cluster to compare against

        current = winners[statement]
        if min(abs(current.page_no - p) for p in others) <= MAX_CLUSTER_DISTANCE:
            continue  # already close to where the other statements were found

        nearby = [
            c for c in candidates[1:]
            if min(abs(c.page_no - p) for p in others) <= MAX_CLUSTER_DISTANCE
        ]
        if nearby:
            winners[statement] = nearby[0]  # candidates is already best-scored-first

    return winners
