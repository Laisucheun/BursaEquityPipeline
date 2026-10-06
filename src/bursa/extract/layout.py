"""Stage 3 - recover table structure from a statement page.

Why not ``page.find_tables()``: Bursa financial statements are frequently laid
out with no ruling lines at all, and where lines exist they are decorative. The
one thing that is reliable across every issuer, font and colour scheme is that
**figures are right-aligned into columns**. So:

1. Collect every word on the page with its bounding box.
2. Take the words that parse as numbers and cluster their *right edges*. Each
   cluster is a column.
3. Group words into rows by vertical overlap.
4. In each row, a numeric word lands in the column whose band contains its right
   edge; everything to the left of the first band is the label.

That makes the extractor indifferent to borders, shading, fonts and colours -
which is exactly the variation that defeats template-based parsers.

Nothing in this module interprets meaning. It emits text and coordinates only;
assigning concepts and periods is the mapper's job.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from bursa.db.enums import Statement
from bursa.normalize.numbers import looks_numeric, parse_number

BBox = tuple[float, float, float, float]

# Two words belong to the same row if their vertical spans overlap by at least
# this fraction of the smaller word's height.
_ROW_OVERLAP = 0.45
# Right edges within this many points of each other are the same column. Columns
# in these documents sit tens of points apart, so this is comfortably safe.
_COLUMN_TOLERANCE = 9.0
# A column band must be used by at least this many rows to be real, which drops
# page numbers and stray note references.
_MIN_COLUMN_SUPPORT = 3
# Indentation buckets, in points.
_INDENT_STEP = 9.0
# How many of a table's own numeric columns a row must populate to count as
# the start of the real data body, not a running-header/page-furniture row
# that carries exactly one stray number by coincidence - see `build_rows`.
# Mirrors `page_scoring.MIN_ROW_KEYWORD_HITS`, raised from `>=1` for the same
# class of reason: a single weak signal is not enough to trust.
_MIN_DATA_ROW_NUMERIC_HITS = 2

# A bare 4-digit column header ("2022", "2021") parses as a number just as
# happily as a real figure, which let the unit/year header row itself
# ("(RM'000)  2022  2021  (%)") satisfy the numeric-hits threshold and get
# mistaken for the first data row - confirmed on United Plantations' quarterly
# reports, where it truncated `header_rows` right before the "(RM'000)" token
# ever reached `detect_scale`, silently extracting every quarterly figure at
# 1/1000th its real value. Real figures in these filings are always printed
# with thousands separators or decimals; a plain 4-digit integer in a
# plausible calendar-year range never is, so it never counts as a data hit.
_BARE_YEAR = re.compile(r"^(19[89]\d|20[0-4]\d)$")

# A page laid out as two independent physical blocks side by side (e.g. a
# cash flow statement's "Operating Activities" panel on the left, "Investing
# Activities" on the right, each with its own labels and its own numeric
# columns - a real space-saving convention) defeats plain row-grouping: a
# left-block word and a right-block word at the same height get merged into
# one garbled row. See `group_rows_by_block` below for the fix. These
# constants govern detecting a genuine block boundary ("gutter") without
# wrongly slicing a normal single wide table in half.
#
# Coarse pre-filter only - NOT the discriminator. An ordinary label-to-first-
# figure gap on a real single table is routinely *wider* than a genuine
# inter-block gutter (confirmed against this module's own fixture geometry),
# so gap width alone can never be trusted to mean "this is a block boundary".
_MIN_GUTTER_WIDTH = 20.0
# A block candidate with fewer rows than this is noise, not a real second
# table - same reasoning `_MIN_COLUMN_SUPPORT` already applies to column bands.
_MIN_BLOCK_ROWS = 3
# The actual discriminator: a genuine second block carries its own line-item
# labels on most of its own rows; a numeric continuation of the same table (a
# second "Company"-style column-group, say) does not. Checked on *both* sides
# of a candidate split - checking only one side would let a normal table get
# wrongly sliced at its own label/numbers boundary, which silently destroys
# more data than the bug being fixed (every row would end up either label-only
# or numbers-only, with no row ever carrying both).
_MIN_LABEL_ROW_FRACTION = 0.5
# If more than this fraction of the page's rows have a word straddling the
# candidate gutter, it isn't a real structural gap - a single page-wide
# caption/title row above two real blocks is expected to straddle and must
# not veto the split by itself, but too many straddling rows means this
# candidate is coincidental, not structural.
_MAX_STRADDLE_FRACTION = 0.2
# Bounds recursive splitting to at most 2 blocks - what's actually confirmed
# on real documents (a real page split into two side-by-side panels). A more
# extreme case turned out, on inspection, not to be a "3+ panels on one normal
# page" shape at all - see `_SPREAD_WIDTH_RATIO` below, which handles that
# real shape by a different, more direct mechanism instead of raising this.
_MAX_BLOCK_SPLIT_DEPTH = 1
# Some filers render a genuine 2-page spread (e.g. a facing "Statement of
# Financial Position" / "Statement of Profit or Loss" pair) as one physical
# PDF page at double width, rather than a single normal-width page with an
# internal 2-panel layout. Confirmed real: one filer's interior pages measure
# exactly 2.000x its own cover page's width, every page, throughout. The
# recurrence-ranked gutter search above is the wrong tool for this shape - a
# genuine ~96pt page-spread boundary loses to narrower (~30pt) but far more
# recurring intra-table column gaps native to the page's own multi-column
# layout, and picking the wrong gutter means a "right side" split can scoop
# up an entire adjacent, genuinely-labelled statement, passing every
# validity check on borrowed labels rather than its own. Detecting the
# doubled width up front and splitting at the page's own literal geometric
# midpoint sidesteps that contest entirely, since the true boundary is
# already known structurally - see `_is_spread_page`/`_split_spread_page`
# and their use in `extract_page`. 1.8, not e.g. 1.95: comfortably above an
# ordinary landscape-vs-portrait ratio (~1.4-1.5), comfortably below the
# confirmed real 2.000x case.
_SPREAD_WIDTH_RATIO = 1.8


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def bbox(self) -> BBox:
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def y_mid(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def x_mid(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Cell:
    col_index: int
    text: str
    bbox: BBox

    def to_json(self) -> dict:
        return {"col_index": self.col_index, "text": self.text, "bbox": list(self.bbox)}


@dataclass
class ExtractedRow:
    row_index: int
    label: str
    cells: list[Cell]
    bbox: BBox
    indent_level: int = 0

    @property
    def is_data_row(self) -> bool:
        return bool(self.label) and bool(self.cells)


@dataclass
class ColumnBand:
    index: int
    x_min: float
    x_max: float
    support: int

    def contains(self, x: float) -> bool:
        return self.x_min - _COLUMN_TOLERANCE <= x <= self.x_max + _COLUMN_TOLERANCE


@dataclass
class ExtractedTable:
    page_no: int
    table_index: int
    statement: Statement | None
    columns: list[ColumnBand]
    header_rows: list[ExtractedRow] = field(default_factory=list)
    rows: list[ExtractedRow] = field(default_factory=list)
    page_text_head: str = ""
    # The full text of exactly the words this table was actually built from -
    # for an ordinary page, equivalent to the whole page's own text; for a
    # confirmed spread page, isolated to the one selected half. Numbers are
    # kept in (unlike `page_text_head`, deliberately numeric-free for the
    # mapper) - this exists specifically so page_scoring.py's own-heading
    # check can tell a spread page's two different statements' headings
    # apart, using text dense enough for classify_page_text's numeric-density
    # confidence check to still fire correctly (that check needs real
    # figures present, which a caption-only string can never supply).
    raw_text: str = ""

    @property
    def header_text(self) -> str:
        """Everything above the first data row, for scale/period detection."""
        parts = [self.page_text_head]
        for row in self.header_rows:
            parts.append(" ".join([row.label, *(c.text for c in row.cells)]).strip())
        return "\n".join(p for p in parts if p.strip())


# --------------------------------------------------------------------------


def words_from_page(page) -> list[Word]:  # type: ignore[no-untyped-def]
    """PyMuPDF words, filtered to the ones that carry content."""
    raw = page.get_text("words")  # (x0, y0, x1, y1, text, block, line, word_no)
    return [
        Word(text=w[4].strip(), x0=w[0], y0=w[1], x1=w[2], y1=w[3])
        for w in raw
        if w[4] and w[4].strip()
    ]


def group_rows(words: list[Word]) -> list[list[Word]]:
    """Cluster words into visual rows by vertical overlap.

    Overlap rather than a fixed y tolerance, so a row mixing 8pt and 12pt type
    (common where a label wraps beside a large figure) still groups correctly.
    """
    if not words:
        return []

    ordered = sorted(words, key=lambda w: (w.y0, w.x0))
    rows: list[list[Word]] = [[ordered[0]]]

    for word in ordered[1:]:
        current = rows[-1]
        top = min(w.y0 for w in current)
        bottom = max(w.y1 for w in current)
        overlap = min(bottom, word.y1) - max(top, word.y0)
        smaller = min(bottom - top, word.height) or 1.0

        if overlap / smaller >= _ROW_OVERLAP:
            current.append(word)
        else:
            rows.append([word])

    for row in rows:
        row.sort(key=lambda w: w.x0)
    return rows


_LABEL_LETTER_RUN = re.compile(r"[A-Za-z]{3,}")


def _row_has_own_label(row_words: list[Word]) -> bool:
    """Whether this row carries a real line-item label of its own - a run of
    at least 3 letters after discarding anything number-shaped (figures, nil
    dashes). A bare note mark like "(a)" or a lone digit doesn't count."""
    text = " ".join(w.text for w in row_words if not looks_numeric(w.text))
    return bool(_LABEL_LETTER_RUN.search(text))


def _is_valid_block(block_words: list[Word]) -> bool:
    """Whether this candidate side of a split reads as a genuine, independent
    block - enough rows, and most of them carrying their own label. A
    numeric-only continuation of some other block's table fails this."""
    if not block_words:
        return False
    rows = group_rows(block_words)
    if len(rows) < _MIN_BLOCK_ROWS:
        return False
    labelled = sum(1 for row in rows if _row_has_own_label(row))
    return labelled / len(rows) >= _MIN_LABEL_ROW_FRACTION


def _find_gutter(words: list[Word]) -> tuple[float, float] | None:
    """Look for a recurring, page-wide horizontal gap wide enough and
    consistent enough across rows to be a real block boundary, not an
    ordinary label-to-figures gap on a single table. Returns the gutter's
    ``(start, end)`` x-range, or ``None`` if nothing validates.
    """
    naive_rows = group_rows(words)
    if len(naive_rows) < 2 * _MIN_BLOCK_ROWS:
        return None

    # 1. Every per-row gap wide enough to be a candidate at all.
    candidates: list[tuple[float, float]] = []
    for row in naive_rows:  # already sorted by x0
        for a, b in zip(row, row[1:]):
            if b.x0 - a.x1 >= _MIN_GUTTER_WIDTH:
                candidates.append((a.x1, b.x0))
    if not candidates:
        return None

    # 2. Cluster candidates whose start points agree within _COLUMN_TOLERANCE -
    #    the same clustering idea detect_columns already uses for numeric
    #    right edges, reused here rather than reinvented.
    candidates.sort()
    clusters: list[list[tuple[float, float]]] = [[candidates[0]]]
    for cand in candidates[1:]:
        if cand[0] - clusters[-1][-1][0] <= _COLUMN_TOLERANCE:
            clusters[-1].append(cand)
        else:
            clusters.append([cand])

    # 3. Rank by recurrence (how many rows agree this gap exists), not width.
    clusters = [c for c in clusters if len(c) >= _MIN_BLOCK_ROWS]
    clusters.sort(key=lambda c: (len(c), min(x1 - x0 for x0, x1 in c)), reverse=True)

    for cluster in clusters:
        gutter_start = max(x0 for x0, _ in cluster)
        gutter_end = min(x1 for _, x1 in cluster)
        if gutter_end <= gutter_start:
            continue  # contributing rows disagree on where the gap actually is

        # 4. A page-wide title above two real blocks is expected to straddle
        #    the gutter and must not veto it alone - but too many straddling
        #    rows means this candidate isn't a genuine structural gap.
        straddling = sum(
            1
            for row in naive_rows
            if any(w.x0 < gutter_end and w.x1 > gutter_start for w in row)
        )
        if straddling / len(naive_rows) > _MAX_STRADDLE_FRACTION:
            continue

        # 5. The actual discriminator: both resulting sides must be genuine,
        #    independently-labelled blocks.
        left = [w for w in words if w.x1 <= gutter_start]
        right = [w for w in words if w.x0 >= gutter_end]
        if _is_valid_block(left) and _is_valid_block(right):
            return (gutter_start, gutter_end)

    return None


def _split_into_blocks(words: list[Word], depth: int = 0) -> list[list[Word]]:
    """Recursively split a page's words into independent horizontal blocks,
    bounded to `_MAX_BLOCK_SPLIT_DEPTH`. Falls back to a single block whenever
    no gutter validates, which is the overwhelming majority of real pages."""
    if depth >= _MAX_BLOCK_SPLIT_DEPTH:
        return [words]
    gutter = _find_gutter(words)
    if gutter is None:
        return [words]

    gutter_start, gutter_end = gutter
    gutter_mid = (gutter_start + gutter_end) / 2
    shared: list[Word] = []
    left: list[Word] = []
    right: list[Word] = []
    for row in group_rows(words):
        if _row_has_its_own_gutter_gap(row, gutter_start, gutter_end):
            # This row's *own* word spacing has a gap at least as wide as the
            # established gutter, overlapping it - genuinely two independent
            # texts sitting at the same height (two real block rows, or two
            # panels' own local headings), safe to split at that gap. A word
            # whose own bbox straddles the gutter zone itself (neither
            # cleanly left nor right) still needs a side - assigned to
            # whichever it's nearer, rather than silently dropped (confirmed
            # real: a title word straddling a gutter vanished entirely from
            # the extracted text under the previous version of this split).
            for w in row:
                if w.x1 <= gutter_start:
                    left.append(w)
                elif w.x0 >= gutter_end:
                    right.append(w)
                else:
                    (left if w.x_mid <= gutter_mid else right).append(w)
        else:
            # No comparably-wide gap of its own at the gutter position - this
            # is one continuous line of text (a page-wide title/caption)
            # whose word boundary just happens to fall near where the gutter
            # sits elsewhere on the page. Splitting by raw x-position would
            # scramble its word order across two different groups (confirmed
            # real: a page-wide caption came out as "STATEMENT OF CASH FLOWS
            # THE FOR YEAR") - keep it together, unsplit. Only ever affects
            # header/caption text, never a data row's own figures.
            shared.extend(row)

    return [
        *([shared] if shared else []),
        *_split_into_blocks(left, depth + 1),
        *_split_into_blocks(right, depth + 1),
    ]


def _row_has_its_own_gutter_gap(row: list[Word], gutter_start: float, gutter_end: float) -> bool:
    """Whether this row's own word spacing has a gap at least as wide as the
    established gutter, overlapping the gutter's x-range. This is the actual
    discriminator between "genuinely two independent texts at this height"
    and "one continuous sentence whose ordinary word-to-word spacing happens
    to fall near the gutter" - both look identical if you only ask "does this
    row have words on both sides", which is why that weaker check is not
    used here."""
    for a, b in zip(row, row[1:]):  # group_rows already sorts each row by x0
        gap_start, gap_end = a.x1, b.x0
        if gap_end - gap_start < _MIN_GUTTER_WIDTH:
            continue
        if gap_start < gutter_end and gap_end > gutter_start:
            return True
    return False


def group_rows_by_block(words: list[Word]) -> list[list[Word]]:
    """`group_rows`, but first split the page into independent horizontal
    blocks when a real, recurring, independently-labelled gutter is found - so
    a left-block row and a right-block row at the same height are never
    merged. Falls back to plain `group_rows(words)` whenever no such gutter
    validates - `group_rows` itself is unchanged and still independently
    correct/testable for the ordinary single-block case.
    """
    blocks = _split_into_blocks(words)
    grouped: list[list[Word]] = []
    for block in blocks:
        grouped.extend(group_rows(block))
    return grouped


def detect_columns(rows: list[list[Word]]) -> list[ColumnBand]:
    """Find the numeric columns by clustering right edges.

    Right edges, not centres: financial figures are right-aligned, so the right
    edge is stable across values of wildly different width while the centre is
    not.
    """
    edges: list[float] = []
    for row in rows:
        for word in row:
            if parse_number(word.text) is not None:
                edges.append(word.x1)

    if not edges:
        return []

    edges.sort()
    clusters: list[list[float]] = [[edges[0]]]
    for edge in edges[1:]:
        if edge - clusters[-1][-1] <= _COLUMN_TOLERANCE:
            clusters[-1].append(edge)
        else:
            clusters.append([edge])

    bands = [
        ColumnBand(index=0, x_min=min(c), x_max=max(c), support=len(c))
        for c in clusters
        if len(c) >= _MIN_COLUMN_SUPPORT
    ]
    bands.sort(key=lambda b: b.x_min)
    for index, band in enumerate(bands):
        band.index = index
    return bands


def _indent_level(x0: float, baseline: float) -> int:
    return max(0, round((x0 - baseline) / _INDENT_STEP))


def build_rows(
    row_groups: list[list[Word]], columns: list[ColumnBand]
) -> tuple[list[ExtractedRow], list[ExtractedRow]]:
    """Split each visual row into a label and its column cells.

    Returns ``(header_rows, data_rows)``: everything before the first row that
    actually has figures is header.
    """
    if not columns:
        return [], []

    first_band_x = columns[0].x_min - _COLUMN_TOLERANCE

    built: list[ExtractedRow] = []
    for group in row_groups:
        label_words = [w for w in group if w.x1 <= first_band_x]
        value_words = [w for w in group if w.x1 > first_band_x]

        per_column: dict[int, list[Word]] = {}
        for word in value_words:
            band = next((b for b in columns if b.contains(word.x1)), None)
            if band is None:
                # A word between columns is part of the label that ran long
                # (a wrapped line), not a figure.
                label_words.append(word)
                continue
            per_column.setdefault(band.index, []).append(word)

        cells: list[Cell] = []
        for col_index in sorted(per_column):
            parts = sorted(per_column[col_index], key=lambda w: w.x0)
            text = " ".join(p.text for p in parts).strip()
            cells.append(
                Cell(
                    col_index=col_index,
                    text=text,
                    bbox=(
                        min(p.x0 for p in parts),
                        min(p.y0 for p in parts),
                        max(p.x1 for p in parts),
                        max(p.y1 for p in parts),
                    ),
                )
            )

        label_words.sort(key=lambda w: w.x0)
        label = " ".join(w.text for w in label_words).strip()
        if not label and not cells:
            continue

        built.append(
            ExtractedRow(
                row_index=0,
                label=label,
                cells=cells,
                bbox=(
                    min(w.x0 for w in group),
                    min(w.y0 for w in group),
                    max(w.x1 for w in group),
                    max(w.y1 for w in group),
                ),
            )
        )

    # The body starts at the first row that has a label and enough real
    # figures to be genuine data - not just any one numeric-parseable cell.
    # A running-header/page-furniture row (a section title carrying a page
    # number, a table-of-contents strip carrying a note-reference digit) has
    # a label and exactly one stray number by coincidence; a genuine data row
    # almost always populates several of the table's own columns at once
    # (multi-year and/or Group/Company presentation is the norm here).
    # Requiring only one number let such a row become `first_data`, which
    # truncates `header_rows` to near-nothing and silently discards every
    # real header row that exists just below it on the page - confirmed real
    # on two otherwise-cleanly-extracted documents (Tenaga Nasional's income
    # statement: row 0 was "FINANCIAL STATEMENTS" with a page number "311" in
    # one column; RHB Bank's: a table-of-contents strip with a note-reference
    # "03"), and measured dataset-wide as 52%/54% of all extracted columns
    # losing their header text/year entirely.
    def _numeric_hits(row: ExtractedRow) -> int:
        return sum(
            1
            for c in row.cells
            if parse_number(c.text) is not None and not _BARE_YEAR.match(c.text.strip())
        )

    def _first_data_index(min_hits: int) -> int | None:
        return next(
            (i for i, r in enumerate(built) if r.label and _numeric_hits(r) >= min_hits), None
        )

    # Adaptive, not a flat >=2: a table with only 1 real numeric column would
    # otherwise never satisfy a flat threshold at all.
    threshold = min(_MIN_DATA_ROW_NUMERIC_HITS, len(columns))
    first_data = _first_data_index(threshold)
    if first_data is None and threshold > 1:
        # The adaptive cap still found nothing - a genuine table where no row
        # ever populates 2+ of its own columns at once (e.g. a two-block page
        # where each panel itself has only 1 numeric column, so the columns
        # `detect_columns` reports globally never co-occur on one row).
        # Falling back to the historical >=1 rule recovers the real first
        # data row instead of silently discarding every row's figures.
        first_data = _first_data_index(1)
    if first_data is None:
        first_data = len(built)
    # Above the body: rows with cells are header bands, but rows with only a
    # label are section headings ("ASSETS", "Non-current assets") and belong to
    # the body, where the mapper can use them as context.
    header_rows = [r for r in built[:first_data] if r.cells]
    data_rows = [r for r in built[:first_data] if not r.cells] + built[first_data:]

    label_x0s = [r.bbox[0] for r in data_rows if r.label]
    baseline = min(label_x0s) if label_x0s else 0.0
    for index, row in enumerate(data_rows):
        row.row_index = index
        row.indent_level = _indent_level(row.bbox[0], baseline)
    for index, row in enumerate(header_rows):
        row.row_index = index

    return header_rows, data_rows


def _extract_from_words(
    words: list[Word],
    page_no: int,
    statement: Statement | None,
    table_index: int,
) -> ExtractedTable | None:
    """The actual extraction pipeline, given a page's words - factored out of
    `extract_page` so a confirmed spread page (see `_is_spread_page`) can run
    each geometric half through this same, otherwise-unmodified pipeline
    independently. Depends only on its own parameters, never on the PyMuPDF
    page object itself, so the ordinary single-page path is byte-identical to
    calling this directly on a whole page's words."""
    if not words:
        return None

    row_groups = group_rows_by_block(words)
    columns = detect_columns(row_groups)
    if not columns:
        return None

    header_rows, data_rows = build_rows(row_groups, columns)
    if not data_rows:
        return None

    # Caption text = everything visually above the first figure-bearing row.
    # It carries the scale token ("RM'000") and the period, and must contain no
    # figures: it is sent to the mapper, which is never shown a number.
    numeric_rows = [r for r in data_rows if r.cells]
    head_cutoff = min((r.bbox[1] for r in numeric_rows), default=float("inf"))
    head_text = "\n".join(
        " ".join(w.text for w in group)
        for group in row_groups
        if max(w.y1 for w in group) <= head_cutoff
    )
    raw_text = "\n".join(" ".join(w.text for w in group) for group in row_groups)

    return ExtractedTable(
        page_no=page_no,
        table_index=table_index,
        statement=statement,
        columns=columns,
        header_rows=header_rows,
        rows=data_rows,
        page_text_head=head_text,
        raw_text=raw_text,
    )


def _is_spread_page(page) -> bool:  # type: ignore[no-untyped-def]
    """Whether this page is a genuine 2-page spread rendered at double width,
    not an ordinary single page - see `_SPREAD_WIDTH_RATIO`'s comment."""
    doc = page.parent
    if doc is None or doc.page_count == 0:
        return False
    cover_width = doc[0].rect.width
    return cover_width > 0 and page.rect.width >= _SPREAD_WIDTH_RATIO * cover_width


def _split_spread_page(words: list[Word], page_width: float) -> tuple[list[Word], list[Word]]:
    """Split a confirmed spread page's words at its own literal geometric
    midpoint - never via `_find_gutter`/word-gap heuristics, since the true
    boundary is already known structurally once the page is confirmed doubled."""
    mid = page_width / 2
    left = [w for w in words if w.x_mid <= mid]
    right = [w for w in words if w.x_mid > mid]
    return left, right


def _select_spread_half(
    left: ExtractedTable | None,
    right: ExtractedTable | None,
    statement: Statement | None,
) -> ExtractedTable | None:
    """Pick whichever half's own heading matches the requested statement -
    confirmed real that each half of a genuine spread carries its own
    heading (e.g. "STATEMENTS OF FINANCIAL POSITION" on one side,
    "STATEMENTS OF PROFIT OR LOSS" on the other)."""
    if left is None:
        return right
    if right is None:
        return left
    if statement is not None:
        from bursa.extract.classify import classify_page_text

        for table in (left, right):
            # raw_text, not page_text_head: classify_page_text's own
            # confidence check requires real numeric density to reach
            # 0.5+, which the deliberately-numeric-free page_text_head can
            # never supply regardless of whether the heading itself matches
            # - confirmed real, the same starvation this session's Gap #1
            # fix in page_scoring.py hit and fixed the same way.
            result = classify_page_text(0, table.raw_text)
            if result.statement == statement and result.confidence >= 0.5:
                return table
    # No statement given, or neither half's own heading matched - fall back
    # to whichever half has more real data rows, the least-arbitrary default.
    return left if len(left.rows) >= len(right.rows) else right


def extract_page(
    page,  # type: ignore[no-untyped-def]
    page_no: int,
    statement: Statement | None = None,
    table_index: int = 0,
) -> ExtractedTable | None:
    """Extract one statement page. ``None`` when the page has no column structure."""
    words = words_from_page(page)
    if not words:
        return None

    if _is_spread_page(page):
        left_words, right_words = _split_spread_page(words, page.rect.width)
        left_table = _extract_from_words(left_words, page_no, statement, table_index)
        right_table = _extract_from_words(right_words, page_no, statement, table_index)
        return _select_spread_half(left_table, right_table, statement)

    return _extract_from_words(words, page_no, statement, table_index)


def extract_pages(
    pdf_path: Path, page_numbers: dict[int, Statement | None]
) -> list[ExtractedTable]:
    """Extract the given 1-indexed pages from a PDF."""
    import pymupdf

    tables: list[ExtractedTable] = []
    with pymupdf.open(pdf_path) as doc:
        for page_no in sorted(page_numbers):
            if not 1 <= page_no <= doc.page_count:
                continue
            table = extract_page(doc[page_no - 1], page_no, page_numbers[page_no])
            if table is not None:
                tables.append(table)
    return tables


def median_row_height(rows: list[list[Word]]) -> float:
    heights = [w.height for row in rows for w in row]
    return statistics.median(heights) if heights else 0.0
