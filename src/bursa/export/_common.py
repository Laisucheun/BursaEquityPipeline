"""Shared helpers for the export formats."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from bursa.analysis.facts import MIN_CONFIDENCE
from bursa.db.enums import Basis, Continuity, PeriodType
from bursa.db.models import Company, Concept, Document, Fact, Period

STATEMENT_ORDER = {"IS": 0, "BS": 1, "CF": 2, "EQ": 3}


@dataclass(frozen=True)
class ConceptInfo:
    key: str
    label: str
    statement: str
    sort_order: int
    is_per_share: bool
    is_subtotal: bool


def concept_catalog(session: Session) -> dict[str, ConceptInfo]:
    return {
        c.concept_key: ConceptInfo(
            key=c.concept_key, label=c.label, statement=str(c.statement),
            sort_order=c.sort_order, is_per_share=c.is_per_share, is_subtotal=c.is_subtotal,
        )
        for c in session.scalars(select(Concept))
    }


def concept_info(catalog: dict[str, ConceptInfo], key: str) -> ConceptInfo:
    """Catalog entry, or a best-effort stand-in for a key missing from the taxonomy."""
    if key in catalog:
        return catalog[key]
    prefix = key.split(".", 1)[0].upper()
    return ConceptInfo(key, key, prefix, 10**9, False, False)


def concept_sort_key(info: ConceptInfo) -> tuple:
    return (STATEMENT_ORDER.get(info.statement, 9), info.sort_order, info.key)


def annual_sources(session: Session, company: Company) -> dict[int, list[str]]:
    """Fiscal year -> filenames of the documents whose facts won selection.

    Provenance only: mirrors the ranking in ``load_annual_facts`` (which does not
    expose document ids) so the Sources sheet names the filings that actually
    supplied each year's numbers. Values themselves always come from
    ``load_annual_facts``.
    """
    rows = session.execute(
        select(
            Fact.concept_key, Period.fiscal_year, Period.period_end,
            Document.filed_date, Document.period_end_hint, Document.id,
            Document.original_filename,
        )
        .join(Period, Fact.period_id == Period.id)
        .join(Document, Fact.reported_in_document_id == Document.id)
        .where(
            Fact.company_id == company.id,
            Fact.basis == Basis.CONSOLIDATED,
            Fact.continuity == Continuity.TOTAL,
            Fact.confidence >= MIN_CONFIDENCE,
            Period.period_type.in_((PeriodType.FY, PeriodType.INSTANT)),
        )
    ).all()

    best: dict[tuple[int, str], tuple[tuple, str]] = {}
    for concept_key, fy, period_end, filed, end_hint, doc_id, filename in rows:
        rank = (period_end, filed or date.min, end_hint or date.min, doc_id)
        key = (fy, concept_key)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, filename)

    counts: dict[int, dict[str, int]] = {}
    for (fy, _), (_, filename) in best.items():
        per_year = counts.setdefault(fy, {})
        per_year[filename] = per_year.get(filename, 0) + 1
    # Main supplier first, so the first name is "the" source for that year.
    return {
        fy: sorted(names, key=lambda n: (-names[n], n))
        for fy, names in sorted(counts.items())
    }
