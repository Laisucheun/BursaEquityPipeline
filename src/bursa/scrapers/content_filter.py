"""Post-download content check: does a scraped PDF actually contain a
primary financial statement, or is it a decoy that merely lives in the same
"annual report" listing on the IR site?

Link text can't reliably answer this - see `ir_fallback`'s module docstring
for why that module now casts a wide net instead of trying to guess. This
is the precision backstop that net needs: a PDF only "has financial
statements" if `bursa.extract.page_scoring`'s own page-selection scorer -
the same one used for real extraction, not a separate, looser heuristic -
can actually find at least one genuine primary statement page inside it.

Deliberately a low bar (any one of the three, not all three): a company's
real financial-statements volume can legitimately be missing one statement
from this scorer's reach for reasons that have nothing to do with whether
the PDF is relevant (the two-column row-blending gap documented in
`page_scoring.py` affects real, relevant pages too). The goal here is only
to separate "contains real statement content" from "pure narrative/notice/
administrative document" - which is the actual failure mode observed on
real sites (CIMB, AMMB) - not to grade completeness.
"""

from __future__ import annotations

import logging
from pathlib import Path

from bursa.db.enums import Statement
from bursa.extract.page_scoring import find_statement_page

log = logging.getLogger(__name__)

_CHECKED_STATEMENTS = (Statement.BALANCE_SHEET, Statement.INCOME_STATEMENT, Statement.CASH_FLOW)


def has_financial_statements(pdf_path: Path) -> bool:
    """Whether at least one primary statement can be found in this PDF.

    Never raises: a PDF that fails to open or parse (corrupt download,
    scanned/image-only pages) is treated as "no statements found" rather
    than aborting the scrape run over one bad file.
    """
    for statement in _CHECKED_STATEMENTS:
        try:
            if find_statement_page(pdf_path, statement) is not None:
                return True
        except Exception:
            log.exception("content check failed on %s for %s", pdf_path, statement)
            continue
    return False
