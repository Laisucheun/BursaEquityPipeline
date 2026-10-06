"""Stage 2 - find the pages that actually carry the primary statements.

An annual report is 200 pages of which maybe 8 matter. Narrowing first keeps
the expensive stages cheap and stops the mapper from being handed the chairman's
statement.

The heading regexes cover English and Malay, and every match must be
corroborated by a page that is genuinely numeric - otherwise the table of
contents and the "index to financial statements" page score as hits.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from bursa.db.enums import DocType, Statement
from bursa.normalize.numbers import parse_number

# Ordered: the first pattern that matches wins, so more specific headings for a
# statement come before the looser ones.
_HEADINGS: tuple[tuple[Statement, re.Pattern[str]], ...] = (
    (
        Statement.CASH_FLOW,
        re.compile(
            r"statements?\s+of\s+cash\s*flows?|cash\s*flows?\s+statements?"
            r"|penyata\s+aliran\s+tunai",
            re.IGNORECASE,
        ),
    ),
    (
        Statement.EQUITY,
        re.compile(
            r"statements?\s+of\s+changes\s+in\s+equity|penyata\s+perubahan\s+ekuiti",
            re.IGNORECASE,
        ),
    ),
    (
        Statement.BALANCE_SHEET,
        re.compile(
            r"statements?\s+of\s+financial\s+position|balance\s*sheets?"
            r"|penyata\s+kedudukan\s+kewangan|kunci\s+kira[\s-]*kira",
            re.IGNORECASE,
        ),
    ),
    (
        Statement.INCOME_STATEMENT,
        re.compile(
            r"statements?\s+of\s+(profit\s+or\s+loss|comprehensive\s+income)"
            r"|income\s+statements?|statements?\s+of\s+income"
            r"|profit\s+and\s+loss\s+accounts?"
            r"|penyata\s+pendapatan",
            re.IGNORECASE,
        ),
    ),
)

# Pages that merely *point* at the statements. "Notes to the financial
# statements" is deliberately unanchored, unlike its siblings here have
# always been - a trailing `\s*$` (matching only when that phrase is the
# very last thing in the whole multi-line head_blob) let it silently never
# fire in practice, since a real notes page always has more text after its
# own running header. That gap is what let a restatement note's own
# sub-heading ("(b) Consolidated Statement of Financial Position as at 31
# December 2024:", reproduced to show what changed) win a direct heading
# match meant only for the genuine primary statement - confirmed on two
# real, separate documents (Tenaga Nasional p444, Public Bank's financial
# report p253), both restatement-reconciliation notes that otherwise passed
# every other check.
_NOT_A_STATEMENT = re.compile(
    r"table\s+of\s+contents|index\s+to\s+the\s+financial\s+statements"
    r"|notes\s+to\s+the\s+financial\s+statements"
    r"|statement\s+by\s+directors|independent\s+auditors?'?\s+report"
    r"|statutory\s+declaration"
    # A restatement reconciliation note's own "(CONTINUED)" page often
    # doesn't repeat "notes to the financial statements" at all - the only
    # reliable marker left is its column structure, which is distinctive and
    # never appears on a genuinely filed statement: "As previously
    # reported/stated", reconciled through "Adjustments" to "As restated".
    # Confirmed on two separate real documents (Tenaga Nasional, Public
    # Bank) whose restatement notes both reproduced the primary statement's
    # own heading text as a sub-heading, passing every other check.
    r"|as\s+previously\s+(reported|stated)",
    re.IGNORECASE,
)

_MIN_NUMERIC_TOKENS = 8
_HEAD_FRACTION = 0.35  # a statement heading sits in the top third of its page


@dataclass
class PageClassification:
    page_no: int
    has_text_layer: bool
    statement: Statement | None
    confidence: float
    heading: str | None
    numeric_tokens: int
    width: float
    height: float


@dataclass
class DocumentClassification:
    doc_type: DocType
    pages: list[PageClassification]
    scanned_page_count: int

    @property
    def statement_pages(self) -> list[PageClassification]:
        return [p for p in self.pages if p.statement is not None]

    @property
    def needs_ocr(self) -> bool:
        """Whether any page lacks a usable text layer.

        Old annual reports are images. This is the flag that routes a document
        to Docling/Textract instead of the cheap PyMuPDF path.
        """
        return self.scanned_page_count > 0


def count_numeric_tokens(text: str) -> int:
    return sum(1 for token in text.split() if parse_number(token) is not None)


def _head_blob(text: str) -> str:
    """The top `_HEAD_FRACTION` of a page's lines, NFKC-normalized - the
    window every heading/exclusion regex here is matched against."""
    text = unicodedata.normalize("NFKC", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    head_lines = lines[: max(3, int(len(lines) * _HEAD_FRACTION))]
    return "\n".join(head_lines)


def is_excluded_page(text: str) -> bool:
    """Whether this page's own text marks it as definitely *not* a primary
    statement - a table of contents, an index, a directors'/auditors'
    section, or a notes/restatement page - regardless of how numerically
    dense or keyword-rich its content otherwise looks.

    Exposed separately from `classify_page_text` so `page_scoring.py` can use
    it as a hard reject on stage-2 candidates, not only as a tie-break signal.
    That distinction matters: a restatement reconciliation note reproduces
    much of the real statement's own vocabulary *and* is often denser
    (every row filled across "previously reported / adjustments / restated"
    columns) than the genuine statement page it's disclosing changes to -
    confirmed real on two separate documents, where such a note kept winning
    on raw keyword/ratio score alone even after it correctly lost the
    heading-match preference. A page classify.py already knows for certain
    is not a statement should never be eligible to win by density.
    """
    return bool(_NOT_A_STATEMENT.search(_head_blob(text)))


def classify_page_text(
    page_no: int,
    text: str,
    width: float = 0.0,
    height: float = 0.0,
) -> PageClassification:
    """Classify one page from its extracted text."""
    numeric = count_numeric_tokens(text)
    has_text = bool(text.strip())

    # Typeset PDFs (most glossy annual reports) render "fi"/"fl" etc. as single
    # ligature glyphs - "Statement of Financial Position" becomes "...Finﬁancial..."
    # or "profit" becomes "proﬁt". NFKC decomposes those back to plain ASCII
    # before any regex runs, otherwise a real heading silently fails to match its
    # own regex - confirmed on a real page (Genting's income statement heading
    # matched fine since it doesn't use the word "profit", but its row labels
    # further down did, which is what the keyword scorer in page_scoring.py
    # reads - the same normalization is applied there for that reason).
    head_blob = _head_blob(text)

    if _NOT_A_STATEMENT.search(head_blob):
        return PageClassification(
            page_no, has_text, None, 0.0, None, numeric, width, height
        )

    for statement, pattern in _HEADINGS:
        match = pattern.search(head_blob)
        if not match:
            continue
        heading = next(
            (ln for ln in head_blob.splitlines() if pattern.search(ln)), match.group(0)
        )
        # A heading with no figures under it is a divider page, not a statement.
        confidence = 0.95 if numeric >= _MIN_NUMERIC_TOKENS else 0.25
        return PageClassification(
            page_no, has_text, statement, confidence, heading[:300], numeric, width, height
        )

    return PageClassification(page_no, has_text, None, 0.0, None, numeric, width, height)


def classify_document(pdf_path: Path, min_confidence: float = 0.5) -> DocumentClassification:
    """Walk a PDF and mark up every page.

    Statement headings often appear once above a table that then runs across
    several pages, so a page that carries no heading but plenty of figures
    inherits the statement of the page before it.
    """
    import pymupdf

    pages: list[PageClassification] = []
    scanned = 0
    full_text: list[str] = []

    with pymupdf.open(pdf_path) as doc:
        for index, page in enumerate(doc, start=1):
            # sort=True: PyMuPDF's default text order follows the PDF's
            # internal content-stream order, not visual position - confirmed
            # real on a bank's income statement page, where the heading and
            # column labels are a separate text block that came *after* the
            # table body in the raw stream, pushing the true heading out of
            # `_HEAD_FRACTION`'s "top of page" window entirely. Sorting by
            # (y, x) is exactly what `layout.py`'s own word extraction
            # already does for the same reason.
            text = page.get_text("text", sort=True)
            rect = page.rect
            result = classify_page_text(index, text, rect.width, rect.height)
            if not result.has_text_layer:
                scanned += 1
            pages.append(result)
            full_text.append(text)

    _carry_forward(pages, min_confidence)

    return DocumentClassification(
        doc_type=_guess_doc_type("\n".join(full_text[:6]), len(pages)),
        pages=pages,
        scanned_page_count=scanned,
    )


def _carry_forward(pages: list[PageClassification], min_confidence: float) -> None:
    """Continue a statement onto its overflow pages."""
    current: Statement | None = None
    for page in pages:
        if page.statement is not None and page.confidence >= min_confidence:
            current = page.statement
            continue
        if (
            current is not None
            and page.statement is None
            and page.numeric_tokens >= _MIN_NUMERIC_TOKENS
        ):
            page.statement = current
            page.confidence = 0.55
        elif page.numeric_tokens < _MIN_NUMERIC_TOKENS:
            # A page of prose ends the run.
            current = None


_QUARTERLY_MARKERS = re.compile(
    r"condensed\s+(consolidated\s+)?(interim\s+)?(income|statements?)"
    r"|interim\s+financial\s+report"
    r"|individual\s+quarter|cumulative\s+quarter"
    r"|quarterly\s+report|unaudited",
    re.IGNORECASE,
)
_ANNUAL_MARKERS = re.compile(
    r"annual\s+report|laporan\s+tahunan|directors'?\s+report"
    r"|independent\s+auditors?'?\s+report",
    re.IGNORECASE,
)


def _guess_doc_type(head_text: str, page_count: int) -> DocType:
    """Quarterly reports are short and say "condensed"; annual reports are long.

    Page count is the tie-breaker because a quarterly report quotes plenty of
    annual-report vocabulary and vice versa.
    """
    quarterly = bool(_QUARTERLY_MARKERS.search(head_text))
    annual = bool(_ANNUAL_MARKERS.search(head_text))

    if quarterly and not annual:
        return DocType.QUARTERLY_REPORT
    if annual and not quarterly:
        return DocType.ANNUAL_REPORT
    if page_count <= 40:
        return DocType.QUARTERLY_REPORT
    if page_count >= 80:
        return DocType.ANNUAL_REPORT
    return DocType.UNKNOWN
