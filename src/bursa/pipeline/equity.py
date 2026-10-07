"""Statement of changes in equity: matrix layout -> structured facts.

`bursa.pipeline.normalize._write_statement_facts` assumes a statement's
columns are *years*. The statement of changes in equity is a matrix instead:
its columns are equity *components* (Share capital | Treasury shares |
reserves | Retained earnings | Total attributable to owners | NCI | Total
equity) and its rows are movements stacked in year-blocks, each bounded by
an opening and a closing balance row whose own label carries the date:

    At 1 January 2023                     ...   8,005,350
    Profit for the year                   ...   1,517,992
    Dividends                             ...    (576,772)
    At 31 December 2023                   ...   8,396,253
    Profit for the year                   ...   ...
    At 31 December 2024                   ...   ...

(the second block's opening is often the previous closing row, or a combined
"At 31 December 2023/1 January 2024" row). This module reads only the
statement's *total equity* column and writes, per complete block:

* ``eq.opening_balance`` - INSTANT at the fiscal year's first day (the same
  convention as ``cf.cash_beginning``);
* ``eq.closing_balance`` - INSTANT at the fiscal year end, comparable with
  ``bs.total_equity`` (see `bursa.validate.rules.eq_closing_matches_bs`);
* one FY duration fact per recognised movement concept (profit, OCI, TCI,
  dividends, share issues/buybacks, share-based payments, transfers, NCI
  ownership changes). Several rows mapping to the same additive concept in
  one block (dividends to owners + dividends to NCI, interim + final) are
  summed; a duplicated profit/OCI/TCI row is ambiguous and skipped.

Everything is written only when it is unambiguous - otherwise a reason goes
into ``FactWriteResult.skipped_columns`` (the project's "report an honest
miss, never guess" rule):

* no column header names the total equity (or more than one does), or the
  total column fails the cross-check "attributable total + NCI = total" on
  a balance row;
* an interim (non-12-month) statement;
* a balance row whose date does not sit on the company's fiscal-year
  boundary, or a block whose opening and closing years disagree, or a block
  with no closing row (a statement cut at the page boundary);
* a block whose fiscal year is not the statement's own year or the one
  before it, or that repeats an earlier block's year, or whose opening is
  not the adjacent year's closing (a Group and a Company matrix stacked on
  one page - only the blocks before the break are kept);
* a page with no "changes in equity" heading (a roll-forward note), no unit
  row, or that names neither the Group nor the Company (no consolidated
  default here - those pages were, in practice, the Company's own).

A page with no parseable subtitle date (a "(Cont'd)" page) takes its year
from its latest dated closing balance, given a known fiscal year end.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from bursa.db.enums import Basis, PeriodType
from bursa.db.models import Company, Concept
from bursa.extract.statement_extract import ColumnInfo, RowInfo, StatementExtraction
from bursa.normalize.numbers import parse_number, to_base_units
from bursa.normalize.periods import (
    fiscal_year_end,
    fiscal_year_of,
    fiscal_year_start,
    parse_stated_period_end,
    parse_statement_duration_months,
    period_bounds,
)

OPENING = "eq.opening_balance"
CLOSING = "eq.closing_balance"

# Concepts whose several rows in one block are genuinely separate line
# items to add up; the rest (profit, OCI, TCI) appear once per block, so a
# second row is a sign of a misread, not something to add.
_ADDITIVE = frozenset(
    {
        "eq.dividends",
        "eq.issuance_of_shares",
        "eq.share_buyback",
        "eq.share_based_payments",
        "eq.transfer_to_reserves",
        "eq.changes_in_nci",
    }
)
_MOVEMENT_CONCEPTS = _ADDITIVE | {
    "eq.profit_for_year",
    "eq.other_comprehensive_income",
    "eq.total_comprehensive_income",
}

# Deterministic label patterns for movement rows the exact-match synonym
# table misses (real labels carry suffixes: "Dividends paid to owners of the
# Company", "Purchase of treasury shares", "Share-based payment
# transactions"). Matched against the cleaned, lower-cased label; first hit
# wins, so the more specific patterns come first.
_MOVEMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (key, re.compile(pattern))
    for key, pattern in (
        ("eq.total_comprehensive_income", r"^total\s+comprehensive\s+(income|loss|\(loss\)|expense)"),
        ("eq.other_comprehensive_income", r"^other\s+comprehensive\s+(income|loss|\(loss\)|expense)"),
        (
            "eq.profit_for_year",
            r"^(net\s+)?(profit|loss|\(loss\))(\s*/\s*\(?(loss|profit)\)?)?\s+(for|after\s+tax)\b",
        ),
        (
            "eq.issuance_of_shares",
            r"^(issu(e|ance)\s+of\s+(new\s+)?(ordinary\s+)?(shares|units)"
            r"|(new\s+)?(ordinary\s+)?shares\s+issued)(?!.*(subsidiar|non[-\s]?controlling))",
        ),
        (
            "eq.share_buyback",
            r"(purchase|acquisition|repurchase|buy[-\s]?back)\s+of\s+(own\s+|treasury\s+)?shares"
            r"|^treasury\s+shares\s+(purchased|acquired)|^share\s+buy[-\s]?backs?\b",
        ),
        (
            "eq.share_based_payments",
            r"share[-\s]based\s+payments?|^share\s+options?\s+granted|^grant\s+of\s+(share\s+options|esos)",
        ),
        ("eq.transfer_to_reserves", r"^transfer(s|red)?\s+(to|from|of|between)\b"),
        (
            "eq.changes_in_nci",
            r"(changes?\s+in|acquisition\s+of|disposal\s+of|dilution\s+of|accretion\s+of)\s+"
            r"(the\s+)?(additional\s+)?(equity\s+|ownership\s+)?interests?\s+in\s+(a\s+|an\s+)?"
            r"(existing\s+)?subsidiar"
            r"|^acquisition\s+of\s+non[-\s]?controlling\s+interests?",
        ),
        (
            "eq.dividends",
            r"^(dividends?|distributions?)\b(?!.*reinvest)"
            r"|^(interim|final|special|first|second|third|single[-\s]tier)\b.*\bdividends?\b(?!.*reinvest)",
        ),
    )
)

_MONTHS: dict[str, int] = {
    **{name.lower(): i for i, name in enumerate(calendar.month_name) if name},
    **{abbr.lower(): i for i, abbr in enumerate(calendar.month_abbr) if abbr},
    "sept": 9,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
# "1 January 2024", "31 Dec 2023", "January 2024" (the day is sometimes lost
# to the layout extractor - confirmed real: "Balance as January 2023"),
# "1.1.2024" / "31/12/2023".
_DATE = re.compile(
    rf"(?:\b(?P<d>\d{{1,2}})\s+)?\b(?P<m>{_MONTH_ALT})\.?,?\s+(?P<y>(19|20)\d\d)\b"
    r"|\b(?P<nd>\d{1,2})[./](?P<nm>\d{1,2})[./](?P<ny>(19|20)\d\d)\b",
    re.IGNORECASE,
)
# How a balance row's label begins - "At 1 January 2024", "Balance at ...",
# "Balance as at ...", "As at ...", or the bare date itself.
_BALANCE_LEAD = re.compile(
    rf"^\s*((balances?|net\s+assets(\s+value)?|total\s+equity)\s+)?(as\s+)?((at|of|on)\s+)?"
    rf"(\d{{1,2}}[./\s]|({_MONTH_ALT})\b)"
    r"|^\s*(balances?|net\s+assets(\s+value)?|total\s+equity)\s+(as\s+)?(at|of)\b",
    re.IGNORECASE,
)
_BEGINNING = re.compile(r"\bbeginning\b|\bopening\b|\bas\s+previously\s+reported\b", re.IGNORECASE)
_ENDING = re.compile(r"\bend\s+of\b|\bclosing\b", re.IGNORECASE)

_TOTAL_EQUITY_HEADER = re.compile(
    r"total\s+(equity|unitholders|unit\s*holders|funds|shareholders)", re.IGNORECASE
)
_NCI_HEADER = re.compile(r"non[-\s]*controlling|minority", re.IGNORECASE)
_TOTAL_WORD = re.compile(r"\btotal\b", re.IGNORECASE)

_CONSOLIDATED_HEADER = re.compile(
    r"\bconsolidated\b(?!\s+(berhad|bhd)\b)|\bgroup\b(?!\s+(berhad|bhd|holdings|plc|limited)\b)",
    re.IGNORECASE,
)
_COMPANY_LINE = re.compile(r"^\s*(the\s+)?company\b(?!\s+(berhad|bhd)\b)", re.IGNORECASE)
_GROUP_LINE = re.compile(r"^\s*(the\s+)?group\b(?!\s+(berhad|bhd|holdings|plc|limited)\b)", re.IGNORECASE)
_OWNER_PHRASE = re.compile(
    r"(owners|equity\s+holders|shareholders|members)\s+of\s+the\s+company", re.IGNORECASE
)
_COMPANY_WORD = re.compile(r"\bcompany\b", re.IGNORECASE)
_EQ_HEADING = re.compile(
    r"changes\s+in\s+(equity|net\s+asset|unitholders|members|shareholders)|perubahan\s+ekuiti",
    re.IGNORECASE,
)
def _has_eq_heading(header: str) -> bool:
    """The heading, tolerant of the text layer's damage: letter-spaced
    ("STATEME NT S O F C HA N GE S I N E QU IT Y", Kim Teck Cheong) or
    interleaved with the subtitle ("STATEMENTS OF CHANGES FOR THE FINANCIAL
    YEAR IN ENDED FINANCIAL EQUITY", "statement of changes ... in ended net
    ... asset value")."""
    if _EQ_HEADING.search(header):
        return True
    compact = re.sub(r"\s+", "", header).lower()
    if any(k in compact for k in ("changesinequity", "changesinnetassetvalue", "perubahanekuiti")):
        return True
    for line_pair in zip(header.splitlines(), header.splitlines()[1:] + [""]):
        text = " ".join(line_pair).lower()
        if re.search(r"\bstatements?\s+of\s+changes\b", text) and re.search(r"\bequity\b|\bnet\b.*\basset", text):
            return True
    return False


_CURRENCY_WORD = re.compile(r"\b(RM|USD|SGD|RMB|HKD)\b|ringgit", re.IGNORECASE)
_NOTE_WORD = re.compile(r"\bnotes?\b", re.IGNORECASE)

# Trailing tokens the layout extractor leaves on a label: stray figures,
# dashes and note references ("Dividends 23", "Profit for the year -",
# "Purchase of shares 11(b)").
_TRAILING_JUNK = re.compile(
    r"(\s+(\(?[\d,.]+\)?(\([a-z]\))?|[-–—]|\d+(\.\d+)?\([a-z]+\)))+\s*$", re.IGNORECASE
)
_LEADING_MARKER = re.compile(r"^\s*([-–—•]|\([a-z0-9]+\)|[a-z0-9]\)|\d+\.)\s+", re.IGNORECASE)


def _clean_label(label: str) -> str:
    s = unicodedata.normalize("NFKC", label).replace("’", "'").replace("‘", "'")
    s = s.strip()
    s = _LEADING_MARKER.sub("", s)
    s = _TRAILING_JUNK.sub("", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def classify_movement(label: str) -> str | None:
    """The eq.* movement concept a printed label names, or None."""
    cleaned = _clean_label(label)
    for key, pattern in _MOVEMENT_PATTERNS:
        if pattern.search(cleaned):
            return key
    return None


# --------------------------------------------------------------------------
# Balance rows and year blocks
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _LabelDate:
    day: int | None
    month: int
    year: int


def _label_dates(label: str) -> list[_LabelDate]:
    out: list[_LabelDate] = []
    for m in _DATE.finditer(unicodedata.normalize("NFKC", label)):
        if m.group("m"):
            month = _MONTHS[m.group("m").lower()]
            day = int(m.group("d")) if m.group("d") else None
            year = int(m.group("y"))
        else:
            day, month, year = int(m.group("nd")), int(m.group("nm")), int(m.group("ny"))
            if not 1 <= month <= 12:
                continue
        if day is not None and not 1 <= day <= calendar.monthrange(year, month)[1]:
            continue
        out.append(_LabelDate(day, month, year))
    return out


@dataclass(frozen=True)
class _Boundary:
    kind: str  # "open" | "close"
    fiscal_year: int
    instant: date  # the balance's own date


def _boundary_of(d: _LabelDate, fy_end_month: int, label: str) -> _Boundary | None:
    """Opening or closing balance of which fiscal year - only when the date
    sits exactly on that company's fiscal-year boundary."""
    start_month = fy_end_month % 12 + 1
    last_day = calendar.monthrange(d.year, d.month)[1]
    if d.month == start_month and (d.day == 1 or (d.day is None and not _ENDING.search(label))):
        when = date(d.year, d.month, 1)
        return _Boundary("open", fiscal_year_of(when, fy_end_month), when)
    if d.month == fy_end_month and (
        (d.day is not None and d.day >= last_day - 3) or (d.day is None and not _BEGINNING.search(label))
    ):
        when = date(d.year, d.month, d.day if d.day is not None else last_day)
        return _Boundary("close", fiscal_year_of(when, fy_end_month), when)
    return None


@dataclass
class _Block:
    fiscal_year: int
    opening: RowInfo
    movements: list[tuple[str, RowInfo]] = field(default_factory=list)
    closing: RowInfo | None = None
    closing_date: date | None = None
    implicit: bool = False  # opened by the previous block's closing row


def _is_balance_row(row: RowInfo) -> bool:
    return row.concept_key in (OPENING, CLOSING) or bool(_BALANCE_LEAD.search(row.label))


def _split_blocks(
    rows: list[RowInfo], fy_end_month: int, stated: date, notes: list[str]
) -> list[_Block]:
    """Group rows into complete opening..closing year-blocks."""
    dated_balance = [(r, _label_dates(r.label)) for r in rows if _is_balance_row(r)]
    any_dated = any(dates for _, dates in dated_balance)

    blocks: list[_Block] = []
    current: _Block | None = None

    def close_current(row: RowInfo, b: _Boundary) -> None:
        nonlocal current
        if current is None:
            # No opening row was readable for this year (e.g. an undated "At
            # 1 January"), so no block - but this closing balance is still
            # the next year's opening.
            current = _Block(b.fiscal_year + 1, row, implicit=True)
            return
        if current.fiscal_year != b.fiscal_year:
            notes.append(
                f"EQ: block opened for FY{current.fiscal_year} but closed by {row.label!r} "
                f"(FY{b.fiscal_year}) - block skipped"
            )
            current = None
            return
        current.closing = row
        current.closing_date = b.instant
        blocks.append(current)
        # The closing balance is also the next year's opening one - most
        # filers print no separate "At 1 January" row for the second block.
        # An explicit opening row for that year, if one follows, replaces it.
        current = _Block(b.fiscal_year + 1, row, implicit=True)

    def open_new(row: RowInfo, b: _Boundary) -> None:
        nonlocal current
        if current is not None and current.fiscal_year == b.fiscal_year:
            # A restated opening ("as previously reported" -> adjustments ->
            # "as restated"): the later row is the real opening balance, and
            # the rows in between are restatement adjustments, not movements.
            current = _Block(b.fiscal_year, row)
            return
        if current is not None and (current.movements or not current.implicit):
            notes.append(f"EQ: FY{current.fiscal_year} block has no closing row - skipped")
        current = _Block(b.fiscal_year, row)

    for row in rows:
        boundaries: list[_Boundary] = []
        if _is_balance_row(row):
            dates = _label_dates(row.label)
            if dates:
                for d in dates:
                    b = _boundary_of(d, fy_end_month, row.label)
                    if b is None:
                        notes.append(
                            f"EQ: balance row {row.label!r} is not dated on the fiscal-year "
                            f"boundary (FYE month {fy_end_month}) - statement skipped"
                        )
                        return []
                    boundaries.append(b)
            elif not any_dated and row.concept_key in (OPENING, CLOSING):
                # Undated "Balance at beginning/end of year" throughout: only
                # one block can be placed - the statement's own year.
                fy = fiscal_year_of(stated, fy_end_month)
                if row.concept_key == OPENING:
                    boundaries.append(_Boundary("open", fy, fiscal_year_start(fy, fy_end_month)))
                else:
                    boundaries.append(_Boundary("close", fy, fiscal_year_end(fy, fy_end_month)))
        if boundaries:
            for b in boundaries:
                (open_new if b.kind == "open" else close_current)(row, b)
            continue

        if current is None:
            continue
        concept = row.concept_key if row.concept_key in _MOVEMENT_CONCEPTS else classify_movement(row.label)
        if concept is not None:
            current.movements.append((concept, row))

    if current is not None and (current.movements or not current.implicit):
        notes.append(f"EQ: FY{current.fiscal_year} block has no closing row - skipped")

    if not any_dated and len(blocks) > 1:
        notes.append("EQ: several undated year-blocks - which year each is cannot be told - skipped")
        return []
    return blocks


# --------------------------------------------------------------------------
# Columns
# --------------------------------------------------------------------------


@dataclass
class _TotalColumns:
    total: ColumnInfo
    attributable: ColumnInfo | None = None
    nci: ColumnInfo | None = None


def _value_columns(extracted: StatementExtraction) -> list[ColumnInfo]:
    return sorted(
        (c for c in extracted.columns if not c.is_note and not _NOTE_WORD.fullmatch(c.header_text.strip())),
        key=lambda c: c.col_index,
    )


def _foots_across(cols: list[ColumnInfo], balance_rows: list[RowInfo]) -> bool:
    """Rightmost column == sum of all the others, on at least two balance
    rows and on every balance row that has a figure in it."""
    if len(cols) < 2:
        return False
    checked = 0
    for row in balance_rows:
        total = _value(row, cols[-1])
        if total is None:
            continue
        parts = [_value(row, c) for c in cols[:-1]]
        if sum(1 for p in parts if p is not None) < 2:
            return False
        if abs(sum((p for p in parts if p is not None), Decimal(0)) - total) > 1:
            return False
        checked += 1
    return checked >= 2


def find_total_columns(
    extracted: StatementExtraction, notes: list[str], balance_rows: list[RowInfo] | None = None
) -> _TotalColumns | None:
    balance_rows = balance_rows or []
    cols = _value_columns(extracted)
    if not cols:
        notes.append("EQ: no value columns")
        return None
    nci = [c for c in cols if _NCI_HEADER.search(c.header_text)]
    if len(nci) > 1:
        notes.append("EQ: more than one non-controlling-interests column - skipped")
        return None
    nci_col = nci[0] if nci else None

    named = [c for c in cols if _TOTAL_EQUITY_HEADER.search(c.header_text)]
    plain_totals = [c for c in cols if _TOTAL_WORD.search(c.header_text) and c not in named and c is not nci_col]

    attributable = None
    if nci_col is not None:
        left = [c for c in plain_totals if c.col_index < nci_col.col_index]
        right = [c for c in plain_totals if c.col_index > nci_col.col_index]
        attributable = left[-1] if len(left) == 1 else None
    else:
        right = plain_totals

    if len(named) == 1:
        total = named[0]
    elif len(named) > 1:
        notes.append(
            "EQ: several columns headed 'total equity' "
            f"({', '.join(str(c.col_index) for c in named)}) - skipped"
        )
        return None
    elif nci_col is not None and len(right) == 1:
        total = right[0]
    elif nci_col is None and len(plain_totals) == 1 and plain_totals[0] is cols[-1]:
        # No NCI column: the rightmost "Total" column is total equity.
        total = plain_totals[0]
    elif _foots_across(cols, balance_rows):
        # Header text lost to the layout extractor ("Total equity" printed
        # on the page, but not inside this column's own header band) - the
        # rightmost column is still provably the total when it equals the
        # sum of every other column on each opening/closing balance row.
        total = cols[-1]
    else:
        notes.append(
            "EQ: no unambiguous total-equity column among headers "
            f"{[c.header_text for c in cols]!r} - skipped"
        )
        return None

    if nci_col is not None and total.col_index < nci_col.col_index:
        notes.append("EQ: the 'total equity' column sits left of the NCI column - skipped")
        return None
    if attributable is not None and attributable.col_index >= total.col_index:
        attributable = None
    return _TotalColumns(total=total, attributable=attributable, nci=nci_col)


def _value(row: RowInfo, col: ColumnInfo) -> Decimal | None:
    printed = row.values.get(col.col_index)
    return None if printed is None else parse_number(printed)


def _cross_check(blocks: list[_Block], cols: _TotalColumns, notes: list[str]) -> bool:
    """On every balance row, attributable total + NCI must equal the chosen
    total column - a cheap proof the column assignment is right."""
    if cols.attributable is None or cols.nci is None:
        return True
    for block in blocks:
        for row in (block.opening, block.closing):
            if row is None:
                continue
            total, attr, nci = (_value(row, c) for c in (cols.total, cols.attributable, cols.nci))
            if total is None or attr is None:
                continue
            if abs(attr + (nci or Decimal(0)) - total) > 1:
                notes.append(
                    f"EQ: total column fails attributable + NCI = total on {row.label!r} "
                    f"({attr} + {nci} != {total}) - skipped"
                )
                return False
    return True


def _basis(extracted: StatementExtraction, cols: _TotalColumns) -> Basis | None:
    """Group or Company - None when the page says neither. Unlike a year
    column, an equity matrix gets no "consolidated" default: a page titled
    only "Statement of Changes in Equity" is, in practice, the Company's own
    (Kim Hin, Rhong Khen - each wrote company equity under the group label
    when defaulted, caught by the closing-vs-balance-sheet check)."""
    header = extracted.header_text
    if cols.nci is not None:
        return Basis.CONSOLIDATED  # a company's own equity has no NCI
    # An entity band line ("The Company", "Company Note RM RM RM", "Group")
    # outranks the title - company names carry "Group"/"Consolidated" too
    # ("TSA Group Berhad", "Ecofirst Consolidated Bhd", "Kim Teck Cheong
    # Consolidated Berhad"), each of whose Company page read as consolidated.
    for line in header.splitlines():
        if _COMPANY_LINE.match(line):
            return Basis.COMPANY
        if _GROUP_LINE.match(line):
            return Basis.CONSOLIDATED
    if _CONSOLIDATED_HEADER.search(header):
        return Basis.CONSOLIDATED
    if _COMPANY_WORD.search(_OWNER_PHRASE.sub(" ", header)):
        return Basis.COMPANY
    return None


def _continuous(blocks: list[_Block], col: ColumnInfo, notes: list[str]) -> list[_Block]:
    """Adjacent year-blocks must chain: one year's closing is the next
    year's opening (in either print order). A break means a second matrix
    stacked under the first (Group then Company - Epicon's page carries the
    Group's FY2024 tail, then the Company's FY2025 and FY2024) - keep only
    the blocks before it."""
    kept = blocks[:1]
    for prev, block in zip(blocks, blocks[1:]):
        if block.fiscal_year == prev.fiscal_year + 1:
            earlier, later = prev, block
        elif block.fiscal_year == prev.fiscal_year - 1:
            earlier, later = block, prev
        else:
            notes.append(f"EQ: FY{block.fiscal_year} block does not follow FY{prev.fiscal_year} - it and later blocks skipped")
            break
        a, b = _value(earlier.closing, col), _value(later.opening, col)  # type: ignore[arg-type]
        if a is None or b is None or a != b:
            notes.append(
                f"EQ: FY{later.fiscal_year} opening ({b}) is not FY{earlier.fiscal_year}'s closing ({a}) "
                "- a second, stacked matrix? FY"
                f"{block.fiscal_year} block and later ones skipped"
            )
            break
        kept.append(block)
    return kept


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def write_equity_facts(
    session: Session,
    company: Company,
    document_id: int,
    run_id: int,
    concepts: dict[str, Concept],
    extracted: StatementExtraction,
    result,  # FactWriteResult - not imported at module level (circular)
    touched_fact_ids: set[int],
) -> None:
    from bursa.pipeline.normalize import _get_or_create_period, _upsert_fact

    notes = result.skipped_columns
    if not _has_eq_heading(extracted.header_text):
        # A roll-forward note that won EQ page selection (DRB-HICOM's
        # "reconciliation of liabilities arising from financing activities",
        # printed under a cash flow heading) has the same opening/closing
        # shape - only the page's own heading tells them apart.
        notes.append("EQ: no 'changes in equity' heading on the selected page - not an equity statement")
        return
    if extracted.scale.token is None and not _CURRENCY_WORD.search(extracted.header_text):
        notes.append("EQ: no unit row (RM / RM'000) in the header - scale unknown, skipped")
        return
    duration = parse_statement_duration_months(extracted.header_text)
    if duration not in (None, 12):
        notes.append(f"EQ: a {duration}-month statement - only annual equity statements are handled")
        return
    stated = parse_stated_period_end(extracted.header_text, instant=False)
    if stated is not None and company.fy_end_month is None:
        company.fy_end_month = stated.month
        result.derived_fy_end_months.append(stated.month)
    if company.fy_end_month is None:
        notes.append("EQ: no parseable period-end date in the header and no known fiscal year end")
        return
    fy_end_month = company.fy_end_month

    block_notes: list[str] = []
    if stated is None:
        # A "(Cont'd)" page with no subtitle, or a subtitle the text layer
        # interleaved beyond parsing ("FOR THE FINANCIAL YEAR IN ENDED
        # FINANCIAL EQUITY 31 MARCH STATEMENTS 2026"): the statement's own
        # year is its latest dated closing balance.
        probe = _split_blocks(extracted.rows, fy_end_month, date(1900, 12, 31), [])
        # (year 1900 = an undated block, placed only by the probe's dummy date)
        closings = [b.closing_date for b in probe if b.closing_date is not None and b.closing_date.year > 1900]
        if not closings:
            notes.append("EQ: no parseable period-end date in the header, and no dated closing balance")
            return
        stated = max(closings)
    blocks = _split_blocks(extracted.rows, fy_end_month, stated, block_notes)
    balance_rows = [r for b in blocks for r in (b.opening, b.closing) if r is not None]
    cols = find_total_columns(extracted, notes, balance_rows)
    if cols is None:
        return
    notes.extend(block_notes)
    if not blocks:
        if not block_notes:
            notes.append("EQ: no complete opening..closing year-block found")
        return

    stated_fy = fiscal_year_of(stated, fy_end_month)
    kept: list[_Block] = []
    seen: set[int] = set()
    for block in blocks:
        if block.fiscal_year in seen:
            notes.append(
                f"EQ: FY{block.fiscal_year} appears in a second block (a stacked Group/Company "
                "matrix?) - that block and every later one skipped"
            )
            break
        seen.add(block.fiscal_year)
        if block.fiscal_year not in (stated_fy, stated_fy - 1):
            notes.append(
                f"EQ: block FY{block.fiscal_year} is neither the statement's own year "
                f"(FY{stated_fy}) nor its comparative - skipped"
            )
            continue
        kept.append(block)

    if not kept or not _cross_check(kept, cols, notes):
        return
    kept = _continuous(kept, cols.total, notes)

    basis = _basis(extracted, cols)
    if basis is None:
        notes.append("EQ: the page names neither the Group nor the Company - basis unknown, skipped")
        return
    multiplier = extracted.scale.multiplier
    col = cols.total

    def write(concept_key: str, bounds, printed: str, value: Decimal) -> None:
        concept = concepts.get(concept_key)
        if concept is None:
            return
        period = _get_or_create_period(session, company.id, bounds, result)
        fact_id = _upsert_fact(
            session,
            company_id=company.id,
            concept_key=concept_key,
            period_id=period.id,
            basis=basis,
            col_index=col.col_index,
            value=to_base_units(value, multiplier, concept.is_per_share),
            scale_multiplier=multiplier,
            currency=extracted.scale.currency,
            printed=printed,
            document_id=document_id,
            run_id=run_id,
            result=result,
        )
        touched_fact_ids.add(fact_id)

    for block in kept:
        fy = block.fiscal_year
        assert block.closing is not None and block.closing_date is not None
        closing_end = block.closing_date
        fy_bounds = period_bounds(closing_end, PeriodType.FY, fy_end_month)
        open_bounds = period_bounds(fiscal_year_start(fy, fy_end_month), PeriodType.INSTANT, fy_end_month)
        close_bounds = period_bounds(closing_end, PeriodType.INSTANT, fy_end_month)

        for concept_key, row, bounds in (
            (OPENING, block.opening, open_bounds),
            (CLOSING, block.closing, close_bounds),
        ):
            value = _value(row, col)
            if value is not None:
                write(concept_key, bounds, row.values[col.col_index], value)

        by_concept: dict[str, list[RowInfo]] = {}
        for concept_key, row in block.movements:
            if _value(row, col) is not None:
                by_concept.setdefault(concept_key, []).append(row)
        for concept_key, rows in by_concept.items():
            if len(rows) > 1 and concept_key not in _ADDITIVE:
                notes.append(
                    f"EQ: FY{fy} has {len(rows)} rows read as {concept_key} - ambiguous, not written"
                )
                continue
            total = sum((_value(r, col) for r in rows), Decimal(0))  # type: ignore[misc]
            printed = " + ".join(r.values[col.col_index] for r in rows)[:64]
            write(concept_key, fy_bounds, printed, total)
