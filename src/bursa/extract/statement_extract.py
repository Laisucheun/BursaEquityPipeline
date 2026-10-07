"""Pull the three primary statements out of a document, using only what's
deterministic - no LLM call, no API credentials required.

This is deliberately *not* the full pipeline (`ingest -> classify -> extract
-> map -> normalize -> validate -> publish`). Quarterly-filing column
semantics (current quarter vs cumulative YTD) and the fallback concept
mapping for labels the seed synonym table doesn't recognise both genuinely
need the model - see `bursa.mapping.llm_mapper`. What's here covers only
what's knowable without it:

* which page holds each of the three primary statements
  (`bursa.extract.page_scoring` - a two-layer keyword/density scorer, not
  `classify.py`'s single heading-regex pass, which measurably picked notes
  and narrative pages instead of the real statement on a majority of the
  real filings this was checked against - see that module's docstring);
* their row/column structure (`extract_page`, already deterministic);
* a row's canonical concept, wherever the seeded/reviewer synonym table
  already recognises its label (`bursa.mapping.synonyms.lookup` - a plain DB
  lookup, no API call);
* which year each column's own header cells mention, where one is written
  there in a recognisable form.

Everything else is reported as raw, not guessed at: an unmatched row keeps
its printed label instead of a fabricated concept key, and a column with no
year found in its own header keeps a null year instead of a guessed one.
Nothing here is written to the `facts` table - this module only locates and
parses. `bursa.pipeline.normalize` is what turns this into `Fact` rows, for
the common annual-report shape (current year + comparative, period end read
directly off the statement's own subtitle) this deterministic pass can
resolve without a guess; it falls back to leaving a column unwritten,
the same discipline as this module, when it can't.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from bursa.db.enums import Basis, Statement
from bursa.extract.classify import DocumentClassification, classify_document
from bursa.extract.layout import ExtractedTable
from bursa.extract.page_scoring import select_statement_pages
from bursa.mapping.synonyms import lookup
from bursa.normalize.numbers import parse_number
from bursa.normalize.scale import ScaleInfo, detect_scale

if TYPE_CHECKING:
    from bursa.extract.eps_note import EpsNote

_YEAR = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")

# An income statement's "Attributable to: Owners of the Company /
# Non-controlling interests" breakdown is printed *twice* under two
# different subtotals - once for "Profit for the financial year", again for
# "Total comprehensive income for the financial year" - with identical row
# labels both times. The synonym table alone cannot tell them apart (same
# label, two meanings), so a plain per-row lookup always resolves both
# occurrences to the same is.pat_owners/is.pat_nci concept keys - silently
# overwriting the profit split with the comprehensive-income split in
# `facts`, caught by `bursa.validate.rules.is_pat_split` failing on several
# real companies.
#
# Two real layouts confirmed, both on the same document (Vitrox
# Corporation): the two subtotal rows can be interleaved with their own
# breakdown immediately underneath each ("profit, its breakdown,
# comprehensive income, its breakdown"), or both subtotals can be stated
# first with both breakdowns grouped together afterwards ("profit,
# comprehensive income, profit's breakdown, comprehensive income's
# breakdown" - this is Vitrox's actual order). "Whichever subtotal was most
# recently seen" only gets the first layout right; the second needs the
# anchors matched to breakdown blocks *by occurrence order* instead - the
# Nth breakdown block (each one an "Owners of the Company" row immediately
# followed by a "Non-controlling interests" row) belongs to the Nth anchor
# encountered so far, which both layouts satisfy by construction (a subtotal
# is always stated before its own breakdown, whether or not another
# subtotal's breakdown is interleaved in between).
_SECTION_ANCHORS = frozenset({"is.profit_for_period", "is.total_comprehensive_income"})
_ATTRIBUTABLE = frozenset({"is.pat_owners", "is.pat_nci", "is.pat_perpetual_bond"})
# Under a total-comprehensive-income anchor. No TCI concept exists for the
# perpetual-holder share, so it is dropped rather than overwriting the PAT one.
_SECTION_REMAP: dict[str, str | None] = {
    "is.pat_owners": "is.tci_owners",
    "is.pat_nci": "is.tci_nci",
    "is.pat_perpetual_bond": None,
}
_PROFIT_SECTION = _SECTION_ANCHORS | _ATTRIBUTABLE | {"is.tci_owners", "is.tci_nci"}
_CONT_DISC_SUBROW = re.compile(
    r"^[\s\-–—]*(from\s+)?(continuing|discontinu(ed|ing))\s+operations?\b", re.IGNORECASE
)


class _AttributionSections:
    """Assign "attributable to" rows to their subtotal.

    A breakdown block is a run of owners / NCI / perpetual-holder rows. A new
    block starts when a holder already seen in the current block appears
    again - so the block's internal order doesn't matter. S P Setia prints
    "perpetual, NCI, owners"; the old rule (a block starts at an owners row)
    let the TCI block's NCI row overwrite the profit NCI. Block N belongs to
    the Nth anchor in encounter order (see the comment above
    `_SECTION_ANCHORS` for why occurrence order, not "most recent").
    """

    def __init__(self) -> None:
        self.anchors: list[str] = []
        self._block = 0
        self._seen: set[str] = set()
        self._sum_concept: str | None = None
        self._sum_row = None
        self._sums: dict[int, Decimal] = {}

    def assign(self, concept_key: str) -> str | None:
        if concept_key in self._seen:
            self._block += 1
            self._seen = set()
        self._seen.add(concept_key)
        anchor = self.anchors[self._block] if self._block < len(self.anchors) else None
        if anchor == "is.total_comprehensive_income":
            return _SECTION_REMAP[concept_key]
        return concept_key

    def start_sum(self, concept_key: str | None, row) -> None:  # type: ignore[no-untyped-def]
        self._sum_concept, self._sum_row, self._sums = concept_key, row, {}

    def absorb_subrow(self, label: str, values: dict[int, str]) -> bool:
        if self._sum_row is None or not _CONT_DISC_SUBROW.match(label):
            return False
        for col, text in values.items():
            number = parse_number(text)
            if number is not None:
                self._sums[col] = self._sums.get(col, Decimal(0)) + number
        return True

    def flush(self) -> list[RowInfo]:
        row, concept_key, sums = self._sum_row, self._sum_concept, self._sums
        self._sum_row, self._sum_concept, self._sums = None, None, {}
        if row is None or concept_key is None or not sums:
            return []
        return [RowInfo(
            row_index=row.row_index, label=row.label, concept_key=concept_key,
            indent_level=row.indent_level, values={c: str(v) for c, v in sorted(sums.items())},
        )]
# Concepts that belong strictly before PAT in an income statement. When one
# of these maps after is.profit_for_period has already been seen, the label
# is an OCI line item whose text happens to collide with an IS synonym (e.g.
# "Taxation" in the OCI section is the tax effect on revaluation, not income
# tax) — suppress the mapping to avoid overwriting the real pre-PAT value.
_PRE_PAT_ONLY = frozenset({
    "is.tax_expense", "is.profit_before_tax", "is.operating_profit",
    "is.gross_profit", "is.revenue", "is.cost_of_sales",
})
# Truncated TCI labels: the layout extractor sometimes puts "Total
# comprehensive income" on a separate line from "for the financial year",
# and the value row only carries the tail fragment. normalize_label strips
# these to empty, so no synonym can match. Detect them by raw text instead.
_TCI_TAIL_PATTERN = re.compile(
    r"^(total\s+)?comprehensive\s+income\s+for\s+the\s|"
    r"^for\s+the\s+(financial\s+)?(year|period)",
    re.IGNORECASE,
)

# Earnings per share. The common face layout is a valueless header -
# "Earnings per share attributable to owners of the Company (sen):", often
# wrapped over two or three lines - followed by "- Basic" / "- Diluted" value
# rows. The sub-row labels alone ("basic", "basic (sen)") are meaningless, so
# they are mapped only inside a block opened by an EPS header after PAT, never
# by a global synonym. A wrapped header can also leave its tail fragment as
# the value row ("Basic/diluted earnings per" / "share (sen)  2.78  3.75").
_EPS_KEYS = ("is.eps_basic", "is.eps_diluted")
_EPS_HEADER = re.compile(
    r"\b(earnings|loss(es)?|profits?)\s*(/\s*\(?\s*(loss|earnings|profit)\s*\)?\s*)?"
    r"per\s+(ordinary\s+|stapled\s+)?(share|unit|security)s?\b"
    r"|\b(earnings|loss)\s*(/\s*\(?\s*(loss|earnings)\s*\)?\s*)?per\s*$"
    r"|\beps\b",
    re.IGNORECASE,
)
_EPS_SUBROW = re.compile(r"^[\s\-–—•·*:]*(\(?[a-z]{1,2}\)\s*)?(basic|diluted)\b", re.IGNORECASE)
# What a wrapped header's tail fragment looks like ("share (sen)", "the
# company (sen):-", "to owners of the parent (sen)").
_EPS_TAIL = re.compile(
    r"\b(sen|cents?|shares?|units?|company|parent|bank|group|owners?|holders?|"
    r"unitholders|shareholders|attributable|rm)\b",
    re.IGNORECASE,
)
_EPS_NOT_TOTAL = re.compile(
    r"realis|distribut|dividend|net\s+assets|before|adjusted|normali[sz]ed|core", re.IGNORECASE
)
_EPS_CONTINUING = re.compile(r"\bcontinuing\b", re.IGNORECASE)
_EPS_DISCONTINUED = re.compile(r"discontinu", re.IGNORECASE)
_EPS_RM_UNIT = re.compile(r"\(\s*rm\s*\)", re.IGNORECASE)
_EPS_SEN_UNIT = re.compile(r"\b(sen|cents?)\b", re.IGNORECASE)
_NOTE_ONLY_VALUE = re.compile(r"^\d{1,2}(\.\d{1,2})?[a-z]?$", re.IGNORECASE)


def _eps_values(values: dict[int, str]) -> dict[int, str]:
    """A header line carrying only its Note reference ("Earnings per share
    28") has no EPS figure of its own - treat it as valueless."""
    if len(values) == 1 and _NOTE_ONLY_VALUE.match(next(iter(values.values())).strip()):
        return {}
    return values


class _EpsBlock:
    """An EPS header and the basic/diluted rows printed under it."""

    def __init__(self) -> None:
        self.text: str | None = None
        self.consumed = False  # a value row has been taken from this block
        self.fragments = 0

    @property
    def active(self) -> bool:
        return self.text is not None

    def start(self, label: str, *, consumed: bool = False) -> None:
        self.text, self.consumed, self.fragments = label, consumed, 1

    def extend(self, label: str) -> None:
        self.text = f"{self.text} {label}"
        self.fragments += 1

    def end(self) -> None:
        self.text, self.consumed, self.fragments = None, False, 0

    def kinds(self, label: str) -> tuple[list[str], int]:
        """Concept keys for a value row, and its priority (0 = total EPS,
        1 = continuing operations only - kept only if nothing better)."""
        header = self.text or ""
        context = f"{header} {label}"
        if _EPS_DISCONTINUED.search(label) or _EPS_NOT_TOTAL.search(context):
            return [], 0
        if _EPS_DISCONTINUED.search(header) and not _EPS_CONTINUING.search(header):
            return [], 0
        if _EPS_CONTINUING.search(label) and not _EPS_SUBROW.match(label):
            return [], 0  # "- from continuing operations" under a "Basic" header
        own = label.lower()
        source = own if ("basic" in own or "dilut" in own) else header.lower()
        keys = []
        if "basic" in source or "dilut" not in source:
            keys.append("is.eps_basic")
        if "dilut" in source:
            keys.append("is.eps_diluted")
        return keys, (1 if _EPS_CONTINUING.search(context) else 0)

    def scaled(self, label: str, values: dict[int, str]) -> dict[int, str]:
        """EPS is stored in sen; a block printed "(RM)" is converted."""
        context = f"{self.text or ''} {label}"
        if not _EPS_RM_UNIT.search(context) or _EPS_SEN_UNIT.search(context):
            return values
        out = {}
        for col, text in values.items():
            number = parse_number(text)
            out[col] = str(number * 100) if number is not None else text
        return out


# A line that starts a different section, never an EPS header's continuation.
_EPS_FOREIGN = re.compile(
    r"\b(total|comprehensive|income|revenue|tax(ation)?|expenses?|weighted|number\s+of|dividends?)\b",
    re.IGNORECASE,
)
_MAX_EPS_HEADER_FRAGMENTS = 4
_EPS_PROFIT_TAIL = re.compile(
    r"^[\s\-–—]*\(?(profit|loss)\)?(\s*/\s*\(?(profit|loss)\)?)?\s+for\s+the\s+"
    r"(financial\s+)?(year|period)\s*:?$",
    re.IGNORECASE,
)
# A valueless profit line ("Profit for the financial year,", "Profit
# attributable to:") - EPS may follow from here on.
_VALUELESS_PROFIT = re.compile(
    r"^[\s\-–—]*(net\s+)?\(?(profit|loss)\)?(\s*/\s*\(?(profit|loss)\)?)?\s+"
    r"(for\s+the\s+(financial\s+)?(year|period)|attributable\s+to|after\s+tax)",
    re.IGNORECASE,
)


def _eps_row(eps: _EpsBlock, label: str, values: dict[int, str]) -> tuple[list[str], int] | None:
    """Advance the EPS block over one row. ``None`` = not part of an EPS
    block, the ordinary loop handles it; otherwise the row's EPS concept keys
    (empty for a header line, which carries no figures)."""
    label = unicodedata.normalize("NFKC", label)  # "Proﬁt" ligatures
    is_header = bool(_EPS_HEADER.search(label))
    if not values:
        if (eps.active and not eps.consumed and eps.fragments < _MAX_EPS_HEADER_FRAGMENTS
                and not _EPS_FOREIGN.search(label)):
            eps.extend(label)  # a wrapped header's next line
            return [], 0
        if is_header:
            eps.start(label)
            return [], 0
        eps.end()
        return None

    if eps.active:
        if _EPS_SUBROW.match(label):
            eps.consumed = True
            return eps.kinds(label)
        if not eps.consumed and not is_header and (
            (_EPS_TAIL.search(label) and not _EPS_FOREIGN.search(label))
            # "Earnings per share (sen) based on:" / "Profit for the
            # financial year  82.30  25.55" (Country View).
            or _EPS_PROFIT_TAIL.match(label)
        ):
            eps.consumed = True
            return eps.kinds(label)
    if is_header:
        eps.start(label, consumed=True)
        return eps.kinds(label)
    eps.end()
    return None


def _same_figures(a: dict[int, str], b: dict[int, str]) -> bool:
    parsed_a = {c: parse_number(t) for c, t in a.items()}
    parsed_b = {c: parse_number(t) for c, t in b.items()}
    return bool(parsed_a) and parsed_a == parsed_b


def _keep_first_eps(rows: list[RowInfo], priority: dict[int, int]) -> None:
    """One EPS row per concept: the first total-EPS row wins over later
    repeats and over a continuing-operations-only row."""
    for key in _EPS_KEYS:
        indexes = [i for i, r in enumerate(rows) if r.concept_key == key]
        if len(indexes) < 2:
            continue
        best = min(indexes, key=lambda i: (priority.get(i, 0), i))
        for i in indexes:
            if i != best:
                rows[i].concept_key = None


@dataclass
class ColumnInfo:
    col_index: int
    header_text: str
    year: int | None
    # A Note-reference column recognised by its content, not its header.
    is_note: bool = False
    # Set only when a "Group ... Company" header band assigns it positionally;
    # otherwise normalize falls back to this column's own header text.
    basis: Basis | None = None


@dataclass
class RowInfo:
    row_index: int
    label: str
    concept_key: str | None  # None = the seed synonym table doesn't recognise this label
    indent_level: int
    values: dict[int, str]  # col_index -> value exactly as printed


@dataclass
class StatementExtraction:
    statement: Statement
    page_no: int
    final_score: float
    row_keyword_hits: int
    scale: ScaleInfo
    # Everything above the first data row - the statement's own title/subtitle
    # and column header bands. `bursa.pipeline.normalize` reads the period end
    # date directly out of this (e.g. "for the financial year ended 31
    # December 2024"), which is why it is kept verbatim rather than discarded
    # once the scale has been read from it.
    header_text: str
    columns: list[ColumnInfo]
    rows: list[RowInfo]
    # Set when the statement's usual start line was found but not its usual
    # end line, and a continuation page supplied the end line instead - see
    # bursa.extract.page_scoring._extend_for_continuation.
    continuation_page_no: int | None = None
    # Income statement only: the EPS note's weighted share count etc. - see
    # bursa.extract.eps_note (written by its write_weighted_shares_facts).
    eps_note: EpsNote | None = None

    @property
    def mapped_row_count(self) -> int:
        return sum(1 for r in self.rows if r.concept_key is not None)


@dataclass
class DocumentExtraction:
    document_id: int
    classification: DocumentClassification
    statements: dict[Statement, StatementExtraction] = field(default_factory=dict)
    skipped_pages: dict[Statement, str] = field(default_factory=dict)


def _column_header_text(table: ExtractedTable, col_index: int) -> str:
    parts = [
        cell.text
        for row in table.header_rows
        for cell in row.cells
        if cell.col_index == col_index and cell.text.strip()
    ]
    return " ".join(parts)


def _column_year(header_text: str, fallback_text: str) -> int | None:
    """The year mentioned in this column's own header - never a different
    column's or the whole table's header, which would misattribute it."""
    stripped = header_text.strip()
    if stripped and not any(ch.isdigit() for ch in stripped):
        # A non-empty column header with no digits at all is a label, not a
        # date - "Note", or stray page-title/company-name text bled into the
        # column band ("BERHAD OR COMPREHENSIVE"), both confirmed real. Never
        # fall back to the table-wide year for a column like this: that's
        # exactly what previously turned a Note-reference column into a
        # dated one, writing footnote reference numbers as facts downstream.
        return None
    for text in (header_text, fallback_text):
        years = [int(m) for m in _YEAR.findall(text)]
        if years:
            return max(years)  # a full date like 30.09.2024 may match twice; harmless
    return None


_NOTE_REF = re.compile(r"^\d{1,2}(\.\d{1,2})?$")
_NOTE_TOKEN = re.compile(r"^notes?$", re.IGNORECASE)
_NOTE_WORD = re.compile(r"\bnotes?\b", re.IGNORECASE)
_GROUP_COMPANY_LINE = re.compile(r"^\s*(the\s+)?group\b.*\bcompany\s*$", re.IGNORECASE)


def _is_note_column(col_index: int, rows: list[RowInfo], leftmost: int) -> bool:
    """The leftmost column holding nothing but small reference numbers ("4",
    "13.1") is a Note column even when its header is blank - confirmed real
    on Ajinomoto and PPB, where a headerless Note column inherited the page's
    year and wrote "4" as a year's revenue."""
    if col_index != leftmost:
        return False
    values = [r.values[col_index] for r in rows if col_index in r.values]
    return len(values) >= 2 and all(_NOTE_REF.match(v.strip()) for v in values)


def _header_year_line(header_text: str, count: int) -> list[int] | None:
    """A header line that is only years (optionally led by "Note") and
    names exactly ``count`` of them - read left to right, it is the column
    order. Per-column header cells are unreliable here: page titles and
    running page numbers bleed into them ("REPORT 2021 INCOME 2021 2020",
    "105"), giving a comparative column the current year."""
    for line in header_text.splitlines():
        tokens = [t for t in line.split() if not _NOTE_TOKEN.match(t)]
        if len(tokens) == count and count >= 2 and all(_YEAR.fullmatch(t) for t in tokens):
            return [int(t) for t in tokens]
    return None


def _has_group_company_band(header_text: str) -> bool:
    return any(_GROUP_COMPANY_LINE.match(line) for line in header_text.splitlines())


def _resolve_columns(table: ExtractedTable, rows: list[RowInfo]) -> list[ColumnInfo]:
    columns = [
        ColumnInfo(
            col_index=col.index,
            header_text=_column_header_text(table, col.index),
            year=_column_year(_column_header_text(table, col.index), table.header_text),
        )
        for col in table.columns
    ]
    if not columns:
        return columns

    leftmost = min(c.col_index for c in columns)
    for c in columns:
        if _NOTE_WORD.search(c.header_text) or _is_note_column(c.col_index, rows, leftmost):
            c.is_note = True
            c.year = None

    value_columns = [c for c in columns if not c.is_note]
    years = _header_year_line(table.header_text, len(value_columns))
    if years is not None:
        for c, year in zip(value_columns, years):
            c.year = year

    if len(value_columns) >= 2 and len(value_columns) % 2 == 0 and _has_group_company_band(table.header_text):
        half = len(value_columns) // 2
        for i, c in enumerate(value_columns):
            c.basis = Basis.CONSOLIDATED if i < half else Basis.COMPANY

    return columns


def extract_statements(
    session: Session, document_id: int, pdf_path: Path,
    company_id: int | None = None, *, ocr: bool = False,
) -> DocumentExtraction:
    """Locate and extract the three primary statements from one PDF."""
    # Still useful for doc_type / needs_ocr metadata, even though page
    # selection below no longer relies on its per-page statement tagging.
    classification = classify_document(pdf_path)
    result = DocumentExtraction(document_id=document_id, classification=classification)

    winners = select_statement_pages(pdf_path, ocr=ocr)
    for statement in (Statement.INCOME_STATEMENT, Statement.BALANCE_SHEET, Statement.CASH_FLOW, Statement.EQUITY):
        scored = winners.get(statement)
        if scored is None:
            result.skipped_pages[statement] = (
                "no page scored highly enough on keywords/numeric density"
            )
            continue

        table = scored.table
        scale = detect_scale(table.header_text)

        rows: list[RowInfo] = []
        sections = _AttributionSections()
        seen_pat = False
        first_pat_row_index: int | None = None
        eps = _EpsBlock()
        eps_priority: dict[int, int] = {}
        # EPS is read only below the profit line. A wrapped "Profit for the
        # year, representing total comprehensive income ..." can map to TCI
        # (or only its owners split) without ever setting seen_pat.
        after_profit = False
        for row in table.rows:
            if not row.label:
                continue
            values = {
                cell.col_index: cell.text
                for cell in row.cells
                if parse_number(cell.text) is not None or cell.text.strip() in ("-", "–", "—")
            }
            if statement == Statement.INCOME_STATEMENT and (seen_pat or after_profit):
                eps_keys = _eps_row(eps, row.label, _eps_values(values))
                if eps_keys is not None:
                    rows.extend(sections.flush())
                    keys, priority = eps_keys
                    if not keys and _eps_values(values):
                        keys = [None]  # inside the block but not total EPS: kept unmapped
                    for key in keys:
                        eps_priority[len(rows)] = priority
                        rows.append(RowInfo(
                            row_index=row.row_index, label=row.label, concept_key=key,
                            indent_level=row.indent_level, values=eps.scaled(row.label, values),
                        ))
                    continue
            if sections.absorb_subrow(row.label, values):
                continue
            if not values:
                if statement == Statement.INCOME_STATEMENT and not after_profit and _VALUELESS_PROFIT.search(
                    unicodedata.normalize("NFKC", row.label)
                ):
                    after_profit = True
                # A valueless "Owners of the Company" line whose figure is
                # printed on "- from continuing / discontinued operations"
                # sub-rows below it (S P Setia) - summed by absorb_subrow.
                if seen_pat:
                    concept_key = lookup(session, row.label, statement, company_id=company_id)
                    if concept_key in _ATTRIBUTABLE:
                        rows.extend(sections.flush())
                        sections.start_sum(sections.assign(concept_key), row)
                continue
            rows.extend(sections.flush())

            concept_key = lookup(session, row.label, statement, company_id=company_id)
            if seen_pat and concept_key in _PRE_PAT_ONLY:
                concept_key = None
            if seen_pat and concept_key is None and _TCI_TAIL_PATTERN.search(row.label):
                concept_key = "is.total_comprehensive_income"
            if concept_key in _SECTION_ANCHORS:
                if concept_key == "is.profit_for_period":
                    if seen_pat and first_pat_row_index is not None and (
                        "is.total_comprehensive_income" in sections.anchors
                        or _same_figures(rows[first_pat_row_index].values, values)
                    ):
                        # Not a second PAT: either a row inside the EPS
                        # section after TCI (Country View prints "Profit for
                        # the financial year" with per-share figures there),
                        # or PAT repeated at the top of a separate OCI
                        # statement. Neither may demote the real PAT.
                        concept_key = None
                    elif seen_pat and first_pat_row_index is not None:
                        # Second is.profit_for_period: the first was actually
                        # continuing-operations profit (before discontinued
                        # ops), not the all-in PAT. Remap it - and don't queue
                        # a second PAT anchor, which would shift every later
                        # breakdown block onto the wrong subtotal.
                        rows[first_pat_row_index] = RowInfo(
                            row_index=rows[first_pat_row_index].row_index,
                            label=rows[first_pat_row_index].label,
                            concept_key="is.profit_continuing",
                            indent_level=rows[first_pat_row_index].indent_level,
                            values=rows[first_pat_row_index].values,
                        )
                    else:
                        seen_pat = True
                        sections.anchors.append(concept_key)
                else:
                    sections.anchors.append(concept_key)
            elif concept_key in _ATTRIBUTABLE:
                concept_key = sections.assign(concept_key)
            if concept_key in _PROFIT_SECTION:
                after_profit = True
            if concept_key == "is.profit_for_period" and first_pat_row_index is None:
                first_pat_row_index = len(rows)
            rows.append(
                RowInfo(
                    row_index=row.row_index,
                    label=row.label,
                    concept_key=concept_key,
                    indent_level=row.indent_level,
                    values=values,
                )
            )
        rows.extend(sections.flush())
        _keep_first_eps(rows, eps_priority)

        result.statements[statement] = StatementExtraction(
            statement=statement,
            page_no=scored.page_no,
            final_score=scored.final_score,
            row_keyword_hits=scored.row_keyword_hits,
            scale=scale,
            header_text=table.header_text,
            columns=_resolve_columns(table, rows),
            rows=rows,
            continuation_page_no=scored.continuation_page_no,
        )
        if statement == Statement.INCOME_STATEMENT:
            extracted = result.statements[statement]
            extracted.eps_note = _read_eps_note(pdf_path, extracted)

    return result


def _read_eps_note(pdf_path: Path, extracted: StatementExtraction) -> EpsNote | None:
    from bursa.extract.eps_note import extract_eps_note, face_patami_by_year

    try:
        return extract_eps_note(
            pdf_path, after_page=extracted.page_no,
            fallback_multiplier=extracted.scale.multiplier,
            face_patami=face_patami_by_year(extracted),
        )
    except Exception:  # noqa: BLE001 - a note-parsing bug must never cost the statements
        return None
