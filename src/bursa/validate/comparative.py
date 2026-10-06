"""Comparative match — cross-document consistency check.

When a company's FY2024 annual report restates FY2023 figures alongside
its current-year numbers, we extract both. If we also extracted FY2023
from its own annual report, the two values should match. A mismatch is
either a restatement (the company changed a prior-year figure — useful
signal) or an extraction error (we picked the wrong column/row/page).

Rounding tolerance: the newer report often rounds to RM'000 while the
original has exact figures, so a small absolute difference is expected.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from bursa.db.enums import PeriodType
from bursa.db.models import Company, Document, Fact, Period

log = logging.getLogger(__name__)

ROUNDING_TOLERANCE = Decimal("1000")
RELATIVE_TOLERANCE = Decimal("0.02")


@dataclass
class ComparativeResult:
    concept_key: str
    period_end: str
    original_doc_id: int
    comparative_doc_id: int
    original_value: Decimal
    comparative_value: Decimal
    abs_diff: Decimal
    rel_diff: float | None
    classification: str  # MATCH, ROUNDING, RESTATEMENT, EXTRACTION_ISSUE


@dataclass
class ComparativeSummary:
    stock_code: str
    name: str
    match: int = 0
    rounding: int = 0
    restatement: int = 0
    extraction_issue: int = 0
    results: list[ComparativeResult] | None = None


def _classify(original: Decimal, comparative: Decimal) -> str:
    diff = abs(original - comparative)
    if diff == 0:
        return "MATCH"
    denom = abs(original) if original != 0 else abs(comparative)
    if denom == 0:
        return "MATCH" if diff == 0 else "RESTATEMENT"
    rel = diff / denom
    if diff <= ROUNDING_TOLERANCE and rel <= RELATIVE_TOLERANCE:
        return "ROUNDING"
    return "RESTATEMENT"


def compare_company(
    session: Session,
    company: Company,
) -> ComparativeSummary:
    summary = ComparativeSummary(stock_code=company.stock_code, name=company.name)

    rows = session.execute(
        select(
            Fact.concept_key,
            Fact.value,
            Fact.reported_in_document_id,
            Period.period_end,
            Period.period_type,
            Document.period_end_hint,
            Fact.basis,
        )
        .join(Period, Fact.period_id == Period.id)
        .join(Document, Fact.reported_in_document_id == Document.id)
        .where(
            Fact.company_id == company.id,
            Period.period_type.in_([PeriodType.FY, PeriodType.INSTANT]),
            Fact.confidence >= 0.9,
        )
        .order_by(Period.period_end)
    ).all()

    by_key: dict[tuple[str, str, str], list[tuple[Decimal, int, str | None]]] = defaultdict(list)
    for concept_key, value, doc_id, period_end, ptype, doc_hint, basis in rows:
        pe_str = str(period_end)
        by_key[(concept_key, pe_str, basis)].append((value, doc_id, doc_hint))

    results: list[ComparativeResult] = []
    for (concept_key, pe_str, _basis), entries in by_key.items():
        if len(entries) < 2:
            continue

        unique_docs = {}
        for val, doc_id, doc_hint in entries:
            if doc_id not in unique_docs:
                unique_docs[doc_id] = (val, doc_hint)

        if len(unique_docs) < 2:
            continue

        sorted_docs = sorted(unique_docs.items())
        original_doc_id = sorted_docs[0][0]
        original_val = sorted_docs[0][1][0]

        for doc_id, (comp_val, doc_hint) in sorted_docs[1:]:
            diff = abs(original_val - comp_val)
            denom = abs(original_val) if original_val != 0 else abs(comp_val)
            rel = float(diff / denom) if denom != 0 else None

            classification = _classify(original_val, comp_val)

            results.append(ComparativeResult(
                concept_key=concept_key,
                period_end=pe_str,
                original_doc_id=original_doc_id,
                comparative_doc_id=doc_id,
                original_value=original_val,
                comparative_value=comp_val,
                abs_diff=diff,
                rel_diff=rel,
                classification=classification,
            ))

    for r in results:
        if r.classification == "MATCH":
            summary.match += 1
        elif r.classification == "ROUNDING":
            summary.rounding += 1
        elif r.classification == "RESTATEMENT":
            summary.restatement += 1

    summary.results = results
    return summary
