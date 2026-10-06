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
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from bursa.db.enums import Statement
from bursa.extract.classify import DocumentClassification, classify_document
from bursa.extract.layout import ExtractedTable
from bursa.extract.page_scoring import select_statement_pages
from bursa.mapping.synonyms import lookup
from bursa.normalize.numbers import parse_number
from bursa.normalize.scale import ScaleInfo, detect_scale

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
_SECTION_REMAP = {"is.pat_owners": "is.tci_owners", "is.pat_nci": "is.tci_nci"}
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


@dataclass
class ColumnInfo:
    col_index: int
    header_text: str
    year: int | None


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


def extract_statements(
    session: Session, document_id: int, pdf_path: Path,
    company_id: int | None = None,
) -> DocumentExtraction:
    """Locate and extract the three primary statements from one PDF."""
    # Still useful for doc_type / needs_ocr metadata, even though page
    # selection below no longer relies on its per-page statement tagging.
    classification = classify_document(pdf_path)
    result = DocumentExtraction(document_id=document_id, classification=classification)

    winners = select_statement_pages(pdf_path)
    for statement in (Statement.INCOME_STATEMENT, Statement.BALANCE_SHEET, Statement.CASH_FLOW, Statement.EQUITY):
        scored = winners.get(statement)
        if scored is None:
            result.skipped_pages[statement] = (
                "no page scored highly enough on keywords/numeric density"
            )
            continue

        table = scored.table
        scale = detect_scale(table.header_text)
        columns = [
            ColumnInfo(
                col_index=col.index,
                header_text=_column_header_text(table, col.index),
                year=_column_year(_column_header_text(table, col.index), table.header_text),
            )
            for col in table.columns
        ]

        rows = []
        # Anchors (is.profit_for_period / is.total_comprehensive_income) seen
        # so far, in encounter order, and how many owners/NCI breakdown
        # blocks have been consumed against that queue - see _SECTION_REMAP
        # above for why this has to be occurrence-order, not "most recent".
        section_queue: list[str] = []
        breakdown_index = 0
        pending_remap: str | None = None  # set on an owners row, applied to its nci row
        seen_pat = False
        first_pat_row_index: int | None = None
        for row in table.rows:
            if not row.label or not row.cells:
                continue
            concept_key = lookup(session, row.label, statement, company_id=company_id)
            if seen_pat and concept_key in _PRE_PAT_ONLY:
                concept_key = None
            if seen_pat and concept_key is None and _TCI_TAIL_PATTERN.search(row.label):
                concept_key = "is.total_comprehensive_income"
            if concept_key in _SECTION_ANCHORS:
                if concept_key == "is.profit_for_period":
                    if seen_pat and first_pat_row_index is not None:
                        # Second is.profit_for_period: the first was actually
                        # continuing-operations profit (before discontinued
                        # ops), not the all-in PAT. Remap it.
                        rows[first_pat_row_index] = RowInfo(
                            row_index=rows[first_pat_row_index].row_index,
                            label=rows[first_pat_row_index].label,
                            concept_key="is.profit_continuing",
                            indent_level=rows[first_pat_row_index].indent_level,
                            values=rows[first_pat_row_index].values,
                        )
                    else:
                        seen_pat = True
                section_queue.append(concept_key)
            elif concept_key == "is.pat_owners":
                target = section_queue[breakdown_index] if breakdown_index < len(section_queue) else None
                breakdown_index += 1
                if target == "is.total_comprehensive_income":
                    concept_key = _SECTION_REMAP[concept_key]
                pending_remap = concept_key
            elif concept_key == "is.pat_nci":
                if pending_remap == "is.tci_owners":
                    concept_key = _SECTION_REMAP[concept_key]
                pending_remap = None
            values = {
                cell.col_index: cell.text
                for cell in row.cells
                if parse_number(cell.text) is not None or cell.text.strip() in ("-", "–", "—")
            }
            if not values:
                continue
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

        result.statements[statement] = StatementExtraction(
            statement=statement,
            page_no=scored.page_no,
            final_score=scored.final_score,
            row_keyword_hits=scored.row_keyword_hits,
            scale=scale,
            header_text=table.header_text,
            columns=columns,
            rows=rows,
            continuation_page_no=scored.continuation_page_no,
        )

    return result
