"""Five-year financial summary pages - a second, company-published source.

Most Malaysian annual reports carry a "Five-Year Financial Highlights" /
"5-Year Financial Summary" / "Group Financial Highlights" page: revenue, PBT,
PAT, PATAMI, total assets, shareholders' equity, EPS, NTA and dividend per
share across five (sometimes six) financial years. It reaches back further
than any external benchmark, so it is the cheapest deep cross-check of our
own statement extraction.

These pages are deliberately *excluded* from primary-statement selection
(`classify.is_excluded_page`), so they get their own finder here. Nothing in
this module writes Facts: a summary is restated-later, rounded, and laid out
by a designer, so it is a lower-trust *check* on the statements, never a
source for them. See `bursa.validate.five_year_check` for the comparison.

Pipeline, per page:

1. Cheap text screen (`is_candidate_text`): a heading line naming a
   financial summary/highlights page, and at least 4 distinct years printed.
2. The ordinary layout engine (`layout.extract_page`) recovers rows/columns.
3. A *year header row* - 4-6 cells that are consecutive financial years
   ("2025", "FY2025", "FYE 31.3.2025", "2025/26", "2021*", "2021 (Restated)")
   - assigns a fiscal year to each column. A later year row (a second
   section with its own header) re-assigns.
4. Each data row's label is mapped (summary-specific synonyms first, then
   the shared synonym table) and its *unit is resolved per row*: summaries
   mix RM'000 / RM million money rows with sen, RM-per-share and % rows on
   one page, so a table-wide scale must never touch a per-share or ratio row.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

from sqlalchemy.orm import Session

from bursa.db.enums import Statement
from bursa.extract.layout import (
    ExtractedRow,
    ExtractedTable,
    Word,
    extract_page,
    words_from_page,
)
from bursa.mapping.synonyms import lookup, normalize_label
from bursa.normalize.numbers import parse_number
from bursa.normalize.scale import detect_scale

# --------------------------------------------------------------------------
# Concepts a summary page is compared on. Anything else a row maps to
# (cost of sales, borrowings, ...) is ignored: summaries are too inconsistent
# about sub-line definitions to be a useful check below these headline items.
# --------------------------------------------------------------------------

DPS_KEY = "x.dividend_per_share"  # no taxonomy concept: reported, never compared

MONEY_CONCEPTS = frozenset({
    "is.revenue",
    "is.profit_before_tax",
    "is.profit_for_period",
    "is.pat_owners",
    "bs.total_assets",
    "bs.total_liabilities",
    "bs.total_equity",
    "bs.equity_owners",
})
# Per-share concepts and the unit each is *stored* in (matches the taxonomy:
# EPS in sen, NTA in RM). DPS follows EPS's convention.
PER_SHARE_CONCEPTS: dict[str, str] = {
    "is.eps_basic": "sen",
    "bs.nta_per_share": "RM",
    DPS_KEY: "sen",
}
SUMMARY_CONCEPTS = MONEY_CONCEPTS | frozenset(PER_SHARE_CONCEPTS)

# Summary-page wording the statement synonym table doesn't (or shouldn't)
# carry. Keys pass through `normalize_label` at import, exactly as lookups do.
_SUMMARY_SYNONYMS_RAW: dict[str, tuple[str, ...]] = {
    "is.revenue": (
        "revenue", "turnover", "group revenue", "total revenue", "operating revenue",
        "gross revenue", "sales", "group turnover",
    ),
    "is.profit_before_tax": (
        "profit before tax", "profit before taxation", "pbt", "profit/(loss) before tax",
        "profit/(loss) before taxation", "(loss)/profit before taxation",
        "profit before zakat and taxation", "profit before taxation and zakat",
        "profit before tax and zakat", "group profit before taxation",
        "pre-tax profit", "profit before income tax",
    ),
    "is.profit_for_period": (
        "profit after tax", "profit after taxation", "net profit after taxation",
        "net profit after tax", "pat", "profit/(loss) after taxation",
        "profit/(loss) after tax", "profit for the year", "profit for the financial year",
        "net profit for the year", "profit after taxation and zakat",
        "profit after tax and zakat",
    ),
    "is.pat_owners": (
        "profit attributable to owners of the company",
        "profit attributable to owners of the parent",
        "profit attributable to owners",
        "profit attributable to equity holders of the company",
        "profit attributable to equity holders",
        "profit attributable to shareholders",
        "profit attributable to shareholders of the company",
        "profit attributable to ordinary equity holders",
        "net profit attributable to shareholders",
        "net profit attributable to owners of the company",
        "net profit attributable to equity holders",
        "profit after tax attributable to owners of the company",
        "profit after taxation attributable to owners of the company",
        "profit after tax and minority interests",
        "profit after taxation and minority interests",
        "profit after tax and non-controlling interests",
        "patami", "net earnings", "net profit attributable to owners",
    ),
    "bs.total_assets": ("total assets",),
    "bs.total_liabilities": ("total liabilities",),
    "bs.total_equity": ("total equity",),
    "bs.equity_owners": (
        "shareholders' funds", "shareholders funds", "shareholders' fund",
        "shareholders' equity", "total shareholders' equity", "total shareholders' funds",
        "equity attributable to owners of the company",
        "equity attributable to owners of the parent",
        "equity attributable to equity holders of the company",
        "equity attributable to shareholders", "owners' equity",
    ),
    "is.eps_basic": (
        "earnings per share", "basic earnings per share", "eps", "basic eps",
        "earnings/(loss) per share", "basic earnings/(loss) per share",
        "net earnings per share", "earnings per share - basic",
    ),
    "bs.nta_per_share": (
        "net assets per share", "net asset per share",
        "net assets per share attributable to owners",
        "net assets per share attributable to owners of the company",
        "net assets per ordinary share",
    ),
    DPS_KEY: (
        "dividend per share", "dividends per share", "net dividend per share",
        "gross dividend per share", "dps", "single tier dividend per share",
        "dividend declared per share", "total dividend per share",
    ),
}

# Unit words stripped from a label before it is matched. Their meaning is
# captured separately by `_row_unit`.
_UNIT_PAREN = re.compile(
    r"\(\s*(?:rm|myr|usd|sen|cents?|%|times|x|rm\s*'?\s*000|rm\s*'?\s*mil(?:lion)?|"
    r"rm\s*'?\s*bil(?:lion)?|'?\s*000|mil(?:lion)?|billion|rm\s*mn|rm\s*m|rm\s*b"
    r"|rm\s+per\s+share|sen\s+per\s+share)\s*\)",
    re.IGNORECASE,
)
_UNIT_TAIL = re.compile(
    r"(?:\s+|^)(?:rm\s*'?\s*000|rm\s*'?\s*mil(?:lion)?|rm\s*'?\s*bil(?:lion)?|rm\s*mn|"
    r"rm\s*m|rm|sen|%|times)\s*$",
    re.IGNORECASE,
)

# -- heading / page screen ---------------------------------------------------

_HEADING = re.compile(
    r"(?:five|5|six|6)[\s-]*years?['’]?[\s-]+(?:group\s+)?(?:financial|summary|highlights|"
    r"record|review|statistics|performance\s+review|key\s+financial)"
    r"|(?<!non-)(?<!non )(?<!non)(?:group\s+|key\s+)?"
    r"\bfinancial\s+(?:highlights|summary|record|statistics)"
    r"|(?:group\s+)?financial\s+performance\s+(?:summary|highlights)"
    r"|summary\s+of\s+(?:group\s+)?financial\s+(?:performance|results|information)",
    re.IGNORECASE,
)
_YEAR_TOKEN = re.compile(r"(?<!\d)(?:19[89]\d|20[0-4]\d)(?!\d)")
# A heading line is short; a sentence of narrative merely mentioning
# "financial highlights" is not a heading.
_MAX_HEADING_LINE = 70

# -- year header cells -------------------------------------------------------

_RESTATED = re.compile(r"\(?\s*restated\s*\)?|\*|†|#", re.IGNORECASE)
_YEAR_CELL = re.compile(
    r"^(?:fye?\s*'?)?\s*"
    r"(?:\d{1,2}\s*[./-]\s*\d{1,2}\s*[./-]\s*|\d{1,2}\s+[a-z]{3,9}\s+)?"  # 31.3.2025 / 31 Mar 2025
    r"(?P<y>(?:19[89]\d|20[0-4]\d))"
    r"(?:\s*/\s*(?P<y2>\d{2}|\d{4}))?$",
    re.IGNORECASE,
)
_MIN_YEARS = 4
_MAX_YEARS = 6

# -- per-row units -----------------------------------------------------------

_PCT_ROW = re.compile(
    r"%|\bratio\b|\bmargin\b|\breturn\s+on\b|\broe\b|\broa\b|\byield\b|\btimes\b|\(x\)"
    r"|\bgearing\b|\bpayout\b|\bgrowth\b",
    re.IGNORECASE,
)
# Operating statistics ("RM/MT", "(Ha)", "(MT)") - not money, not per-share.
_OTHER_UNIT_ROW = re.compile(
    r"rm\s*/\s*[a-z]|/\s*(?:ha|mt|tonne|kg|unit|sq)|\((?:ha|mt|tonnes?|kg|mw|gwh|no\.?)\)"
    r"|\bnumber\s+of\b|\bemployees\b|\bshare\s+price\b|\bmarket\s+capitali[sz]ation\b",
    re.IGNORECASE,
)
_SEN = re.compile(r"\b(?:sen|cents?)\b", re.IGNORECASE)
_RM_WORD = re.compile(r"\b(?:rm|myr)\b", re.IGNORECASE)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return text.replace("’", "'").replace("‘", "'").replace("`", "'")


def _build_synonyms() -> dict[str, str]:
    table: dict[str, str] = {}
    for concept, phrases in _SUMMARY_SYNONYMS_RAW.items():
        for phrase in phrases:
            norm = normalize_label(phrase)
            if norm and norm not in table:
                table[norm] = concept
    return table


_SUMMARY_SYNONYMS = _build_synonyms()


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SummaryValue:
    concept_key: str
    fiscal_year: int
    value: Decimal  # money in RM; EPS/DPS in sen; NTA in RM - as Facts store them
    raw_text: str
    label: str
    multiplier: int  # money scale applied, or the per-share conversion (1 / 100)
    unit: str  # "RM" (money) | "sen" | "RM/share"
    unit_source: str  # "row" | "section" | "table" | "assumed"
    restated: bool
    page_no: int
    # Half of the last printed digit, in stored units - the rounding a
    # summary's "1,068" (RM million) can legitimately differ by.
    rounding: Decimal = Decimal(0)


@dataclass
class SummaryTable:
    page_no: int
    heading: str
    years: dict[int, int]  # column index -> fiscal year (last header seen)
    values: list[SummaryValue] = field(default_factory=list)
    unmapped_labels: list[str] = field(default_factory=list)

    @property
    def fiscal_years(self) -> list[int]:
        return sorted({v.fiscal_year for v in self.values})


# --------------------------------------------------------------------------
# Page screen
# --------------------------------------------------------------------------


def find_heading(text: str) -> str | None:
    """The summary-page heading line, if any line of the page is one."""
    for line in _fold(text).splitlines():
        line = line.strip()
        if not line or len(line) > _MAX_HEADING_LINE:
            continue
        if _HEADING.search(line):
            return line
    return None


def is_candidate_text(text: str) -> bool:
    """Cheap pre-filter: a summary heading and at least 4 distinct years."""
    if find_heading(text) is None:
        return False
    return len(set(_YEAR_TOKEN.findall(text))) >= _MIN_YEARS


def find_summary_pages(doc) -> list[int]:  # type: ignore[no-untyped-def]
    """1-indexed pages of an open PyMuPDF document passing the text screen."""
    pages: list[int] = []
    for index, page in enumerate(doc):
        try:
            text = page.get_text()
        except Exception:
            continue
        if is_candidate_text(text):
            pages.append(index + 1)
    return pages


# --------------------------------------------------------------------------
# Year header
# --------------------------------------------------------------------------


def parse_year_label(text: str) -> tuple[int, bool] | None:
    """``"FY2025"`` -> ``(2025, False)``; ``"2021 (Restated)"`` -> ``(2021, True)``.

    A split year ("2025/26") names its *ending* year, per the project-wide
    convention that a financial year is named for the year it ends in.
    """
    s = _fold(text).strip()
    if not s:
        return None
    restated = bool(_RESTATED.search(s))
    s = _RESTATED.sub("", s).strip()
    m = _YEAR_CELL.match(s)
    if not m:
        return None
    year = int(m.group("y"))
    if m.group("y2"):
        y2 = m.group("y2")
        end = int(y2) if len(y2) == 4 else (year // 100) * 100 + int(y2)
        if end == year + 1:
            year = end
        elif end != year:
            return None
    return year, restated


def _year_header(row: ExtractedRow) -> tuple[dict[int, int], set[int]] | None:
    """Column -> year mapping when this row is a year header, else None."""
    years: dict[int, int] = {}
    restated: set[int] = set()
    for cell in row.cells:
        parsed = parse_year_label(cell.text)
        if parsed is None:
            continue
        years[cell.col_index] = parsed[0]
        if parsed[1]:
            restated.add(cell.col_index)
    distinct = sorted(set(years.values()))
    if not _MIN_YEARS <= len(distinct) <= _MAX_YEARS or len(distinct) != len(years):
        return None
    if any(b - a != 1 for a, b in pairwise(distinct)):
        return None
    # The year cells must be (nearly) the whole row - a data row of figures
    # that happen to look like years would be numbers in other columns too.
    figures = [
        c for c in row.cells
        if c.col_index not in years and parse_number(c.text) is not None
    ]
    if figures:
        return None
    return years, restated


def _restated_marker_row(row: ExtractedRow) -> set[int]:
    """Columns flagged by a "Restated" row printed under the year header."""
    return {
        c.col_index for c in row.cells
        if re.fullmatch(r"\(?\s*restated\s*\)?\*?", c.text.strip(), re.I)
    }


# --------------------------------------------------------------------------
# Label mapping and units
# --------------------------------------------------------------------------


def strip_units(label: str) -> str:
    s = _fold(label)
    s = _UNIT_PAREN.sub(" ", s)
    prev = None
    while prev != s:
        prev = s
        s = _UNIT_TAIL.sub("", s).strip()
    return re.sub(r"\s+", " ", s).strip(" :-")


def map_label(
    label: str,
    *,
    session: Session | None = None,
    company_id: int | None = None,
    section: str = "",
) -> str | None:
    """Resolve a summary row label to one of `SUMMARY_CONCEPTS`, or None."""
    cleaned = strip_units(label)
    candidates = [cleaned, _fold(label)]
    concept: str | None = None
    for text in candidates:
        norm = normalize_label(text)
        if not norm:
            continue
        concept = _SUMMARY_SYNONYMS.get(norm)
        if concept:
            break
        if session is not None:
            balance_first = bool(
                re.search(r"equity|asset|financial\s+position|balance", section, re.I)
            )
            order = (
                (Statement.BALANCE_SHEET, Statement.INCOME_STATEMENT)
                if balance_first
                else (Statement.INCOME_STATEMENT, Statement.BALANCE_SHEET)
            )
            for statement in order:
                found = lookup(session, text, statement, company_id)
                if found in SUMMARY_CONCEPTS:
                    concept = found
                    break
            if concept:
                break
    if concept is None:
        concept = _pattern_concept(cleaned, section)
    if concept is None:
        return None
    # A bare "Owners of the Company" row means whatever its context splits:
    # profit under a profit heading, equity under an equity heading, and
    # nothing at all when the context is unknown or per-share (a wrapped
    # "Net Assets Per Share Attributable To" / "Owners Of The Parent (RM)").
    if concept in ("is.pat_owners", "bs.equity_owners") and not re.search(
        r"profit|earning|income|patami|pat\b|equity|fund", cleaned, re.I
    ):
        if _PER_SHARE_SECTION.search(section):
            return None
        if re.search(r"equity|fund|asset", section, re.I):
            return "bs.equity_owners"
        if re.search(r"profit|earning|income", section, re.I):
            return "is.pat_owners"
        return None
    return concept


# Last-resort shape rules for the wording variety exact synonyms can't cover
# ("Net profit attributable to equity holders of the Bank", "Profit before
# tax expense and zakat"). Each is anchored on the line item's head noun and
# excludes the obvious near-misses.
_NOT_OWNERS = re.compile(r"non[\s-]*controlling|minority|\bnci\b|perpetual", re.I)
_PATTERN_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(?:group\s+)?(?:net\s+)?(?:profit|earnings?|income)\b.*\b(?:attributable|owners|"
                r"equity\s+holders|shareholders)\b", re.I), "is.pat_owners"),
    (re.compile(r"^(?:group\s+)?(?:net\s+)?profit\b(?!.*\bafter\b)"
                r".*\bbefore\s+(?:income\s+)?tax", re.I),
     "is.profit_before_tax"),
    (re.compile(r"^(?:group\s+)?(?:net\s+)?profit\b(?!.*\bbefore\b)"
                r".*\bafter\s+(?:income\s+)?tax", re.I),
     "is.profit_for_period"),
)
# Inside a "Per share (sen)" block the label drops its "per share".
_PER_SHARE_SECTION = re.compile(r"per\s+(?:ordinary\s+)?(?:share|unit)", re.I)
# ... and that block's own heading starts with it ("Per share (sen)", "Per
# Share Data"), unlike a wrapped "Net assets per share attributable to".
_PER_SHARE_HEADING = re.compile(r"^\s*(?:data\s+)?per\s+(?:ordinary\s+)?(?:share|unit)\b", re.I)
_PER_SHARE_SHORT: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(?:basic(?:\s*/\s*diluted)?\s+)?(?:net\s+)?earnings?$", re.I), "is.eps_basic"),
    (re.compile(r"^(?:gross\s+|net\s+|total\s+|single[\s-]*tier\s+)?"
                r"dividends?(?:\s+declared)?$", re.I),
     DPS_KEY),
    (re.compile(r"^net\s+assets?(?:\s+attributable.*)?$", re.I), "bs.nta_per_share"),
)


# Full per-share wordings with any "attributable to owners of ..." tail.
_PER_SHARE_FULL: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(?:group\s+)?net\s+(?:tangible\s+)?assets?\s+(?:value\s+)?"
                r"per\s+(?:ordinary\s+)?share\b", re.I),
     "bs.nta_per_share"),
    (re.compile(r"^(?:basic\s+)?(?:net\s+)?earnings?(?:\s*/\s*\(loss\))?\s+per\s+(?:ordinary\s+)?share\b"
                r"(?!.*\bdiluted\b)", re.I), "is.eps_basic"),
    (re.compile(r"^(?:gross\s+|net\s+|total\s+|single[\s-]*tier\s+)?dividends?\s+per\s+(?:ordinary\s+)?share\b",
                re.I), DPS_KEY),
)


def _pattern_concept(cleaned: str, section: str) -> str | None:
    text = re.sub(r"\s+", " ", cleaned).strip()
    if _PER_SHARE_SECTION.search(section):
        for pattern, concept in _PER_SHARE_SHORT:
            if pattern.match(text):
                return concept
    for pattern, concept in _PER_SHARE_FULL:
        if pattern.match(text):
            return concept
    if _PER_SHARE_SECTION.search(text) or _PCT_ROW.search(text):
        return None
    for pattern, concept in _PATTERN_RULES:
        if pattern.search(text):
            if concept == "is.pat_owners" and _NOT_OWNERS.search(text):
                return None
            return concept
    return None


def _explicit_scale(text: str) -> int | None:
    """Money multiplier when the text carries an explicit scale token."""
    info = detect_scale(_fold(text))
    return info.multiplier if info.token else None


def _per_share_unit(text: str) -> str | None:
    folded = _fold(text)
    if _SEN.search(folded):
        return "sen"
    if _RM_WORD.search(folded):
        return "RM"
    return None


def _decimals(raw: str) -> int:
    m = re.search(r"\.(\d+)", raw)
    return len(m.group(1)) if m else 0


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


_RM_AMOUNT = re.compile(r"\b(?:rm|myr)\s*\d", re.IGNORECASE)


def _caption_scale(head: str) -> int | None:
    """Scale from the caption above the table - nearest line first, and
    never from prose ("revenue of RM237.1 million" would set a 1,000,000x
    scale on an RM'000 table)."""
    for line in reversed(_fold(head).splitlines()):
        line = line.strip()
        if not line or len(line) > _MAX_HEADING_LINE or _RM_AMOUNT.search(line):
            continue
        scale = _explicit_scale(line)
        if scale:
            return scale
    return None


def _row_text(row: ExtractedRow) -> str:
    return " ".join([row.label, *(c.text for c in row.cells)]).strip()


def _full_label(row: ExtractedRow, first_x: float, words: list[Word] | None) -> str:
    if words is None:
        left = [c.text for c in row.cells if c.bbox[2] <= first_x]
        return " ".join([row.label, *left]).strip()
    y0, y1 = row.bbox[1], row.bbox[3]
    picked = [
        w for w in words
        if y0 - 0.5 <= w.y_mid <= y1 + 0.5 and w.x1 <= first_x + 0.5 and w.x0 >= row.bbox[0] - 0.5
    ]
    picked.sort(key=lambda w: w.x0)
    return " ".join(w.text for w in picked).strip() or row.label


def summarise_table(
    table: ExtractedTable,
    *,
    heading: str = "",
    session: Session | None = None,
    company_id: int | None = None,
    words: list[Word] | None = None,
) -> SummaryTable | None:
    """Turn a layout-engine table into fiscal-year-keyed summary values.

    None when no year header row (4-6 consecutive years) is found.

    ``words`` (the page's words) lets a row's label be rebuilt from every
    word left of its first figure. The layout engine files a label word
    that lands inside a numeric column band as a "cell" ("Gross" | "Profit"
    -> label "Profit"), and a unit printed in its own column ("RM'000",
    "(RM'mil)") the same way - both matter here, so both are recovered.
    """
    table_scale = _caption_scale(table.page_text_head)
    all_rows = sorted([*table.header_rows, *table.rows], key=lambda r: (r.bbox[1], r.bbox[0]))

    years: dict[int, int] | None = None
    restated_cols: set[int] = set()
    section = ""
    section_scale: int | None = None
    section_ps_unit: str | None = None
    prev_label_only = ""
    out = SummaryTable(page_no=table.page_no, heading=heading, years={})
    seen: set[tuple[str, int]] = set()

    for row in all_rows:
        header = _year_header(row)
        if header is not None:
            years, restated_cols = header
            out.years = dict(years)
            # A year row's own label ("RM'000", "Financial year ended ...")
            # sets the scale for the block beneath it.
            label_scale = _explicit_scale(_row_text(row))
            if label_scale:
                section_scale = label_scale
            prev_label_only = ""
            continue
        if years is None:
            # Above the first year header: a caption carrying the unit.
            text = _row_text(row)
            if len(text) <= _MAX_HEADING_LINE and not _RM_AMOUNT.search(text):
                scale = _explicit_scale(text)
                if scale:
                    table_scale = scale
            continue

        marker = _restated_marker_row(row)
        if marker:
            restated_cols |= {c for c in marker if c in years}

        figures = [
            c for c in row.cells if c.col_index in years and parse_number(c.text) is not None
        ]
        if not figures:
            # Section heading ("Operating Results (RM Million)", "Per Share
            # (sen)"), a unit row ("RM'000" over every column), or the first
            # line of a wrapped label.
            text = _full_label(row, float("inf"), words) if words is not None else _row_text(row)
            if text:
                scale = _explicit_scale(text)
                unit = _per_share_unit(text) if not scale else None
                letters = re.sub(r"[^A-Za-z]", "", text)
                if scale or unit or _PER_SHARE_HEADING.match(text) or (
                    len(letters) >= 4 and letters.isupper()
                ):
                    section = text
                    if scale:
                        section_scale = scale
                    section_ps_unit = unit
                if not marker:
                    prev_label_only = text
            continue

        first_x = min(c.bbox[0] for c in figures)
        full_label = _full_label(row, first_x, words)

        # A wrapped label ("Net assets per share attributable to" / "owners
        # of the Company" + figures) is tried joined first: the tail alone
        # can map to something else entirely (here, PATAMI).
        concept = None
        label = full_label
        if prev_label_only:
            joined = f"{prev_label_only} {full_label}".strip()
            concept = map_label(joined, session=session, company_id=company_id, section=section)
            if concept is not None:
                label = joined
        if concept is None:
            # The tail alone, with the line above it as context for a bare
            # "Owners of the Company".
            context = f"{section} {prev_label_only}".strip()
            concept = map_label(label, session=session, company_id=company_id, section=context)
        prev_label_only = ""
        if concept is None:
            if full_label:
                out.unmapped_labels.append(full_label)
            continue

        folded = _fold(label)
        if concept in MONEY_CONCEPTS:
            if _PCT_ROW.search(folded) or _OTHER_UNIT_ROW.search(folded):
                continue  # "Revenue growth (%)", "Cost of sales (RM/MT)"
            row_scale = _explicit_scale(label)
            if row_scale:
                multiplier, source = row_scale, "row"
            elif section_scale:
                multiplier, source = section_scale, "section"
            elif table_scale:
                multiplier, source = table_scale, "table"
            else:
                multiplier, source = 1, "assumed"
            unit = "RM"
        else:
            if "%" in folded or _OTHER_UNIT_ROW.search(folded):
                continue  # "Dividend payout (%)"
            stored = PER_SHARE_CONCEPTS[concept]
            row_unit = _per_share_unit(folded)
            if row_unit:
                printed, source = row_unit, "row"
            elif section_ps_unit:
                printed, source = section_ps_unit, "section"
            else:
                printed, source = stored, "assumed"
            unit = "sen" if stored == "sen" else "RM/share"
            multiplier = 1

        for cell in row.cells:
            fy = years.get(cell.col_index)
            if fy is None:
                continue
            value = parse_number(cell.text)
            if value is None:
                continue
            raw_step = Decimal(1).scaleb(-_decimals(cell.text))
            if concept in MONEY_CONCEPTS:
                stored_value = value * multiplier
                rounding = raw_step * multiplier / 2
            else:
                factor = _per_share_factor(printed, PER_SHARE_CONCEPTS[concept])
                stored_value = value * factor
                rounding = raw_step * factor / 2
            key = (concept, fy)
            if key in seen:
                continue
            seen.add(key)
            out.values.append(SummaryValue(
                concept_key=concept,
                fiscal_year=fy,
                value=stored_value,
                raw_text=cell.text,
                label=label,
                multiplier=multiplier,
                unit=unit,
                unit_source=source,
                restated=cell.col_index in restated_cols or bool(_RESTATED.search(cell.text)),
                page_no=table.page_no,
                rounding=rounding,
            ))

    if not out.years:
        return None
    return out


def _per_share_factor(printed: str, stored: str) -> Decimal:
    if printed == stored:
        return Decimal(1)
    if printed == "RM" and stored == "sen":
        return Decimal(100)
    return Decimal("0.01")  # printed sen, stored RM


# Minimum mapped content for a page to count as a real financial summary,
# not an operating-statistics page that happens to share the heading shape.
_MIN_MAPPED_CONCEPTS = 2


def extract_summary_page(
    page,  # type: ignore[no-untyped-def]
    page_no: int,
    *,
    session: Session | None = None,
    company_id: int | None = None,
) -> SummaryTable | None:
    text = page.get_text()
    heading = find_heading(text)
    if heading is None:
        return None
    table = extract_page(page, page_no)
    if table is None:
        return None
    summary = summarise_table(
        table, heading=heading, session=session, company_id=company_id,
        words=words_from_page(page),
    )
    if summary is None:
        return None
    if len({v.concept_key for v in summary.values}) < _MIN_MAPPED_CONCEPTS:
        return None
    return summary


def extract_five_year_summary(
    pdf_path: Path | str,
    *,
    session: Session | None = None,
    company_id: int | None = None,
) -> list[SummaryTable]:
    """Every five-year summary table found in one annual report."""
    import pymupdf

    tables: list[SummaryTable] = []
    with pymupdf.open(pdf_path) as doc:
        for page_no in find_summary_pages(doc):
            summary = extract_summary_page(
                doc[page_no - 1], page_no, session=session, company_id=company_id,
            )
            if summary is not None:
                tables.append(summary)
    return tables


def merge_values(tables: list[SummaryTable]) -> dict[tuple[str, int], SummaryValue]:
    """First value per (concept, fiscal_year) across a document's tables."""
    merged: dict[tuple[str, int], SummaryValue] = {}
    for table in tables:
        for value in table.values:
            merged.setdefault((value.concept_key, value.fiscal_year), value)
    return merged
