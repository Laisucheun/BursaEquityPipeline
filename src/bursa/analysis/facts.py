"""One annual fact set per fiscal year, shared by every analysis module.

A raw fact query returns quarterly/half-year periods alongside FY ones, and
the same FY figure once per filing that printed it (original + the next
year's comparative). Picking "whichever row came first" silently mixes a Q1
revenue into an annual ratio, so the selection rules live here once:

* consolidated, total-continuity, confidence >= 0.9
* FY durations and INSTANTs only
* within a fiscal year, the latest period_end wins (the year-end balance sheet,
  not an interim one)
* between filings reporting the same period, the most recently filed wins
  (restated beats original - same rule as the ``facts_current`` view)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from bursa.db.enums import Basis, Continuity, PeriodType
from bursa.db.models import Company, Document, Fact, Period

MIN_CONFIDENCE = 0.9


@dataclass
class AnnualFacts:
    fiscal_year: int
    period_end: date
    values: dict[str, Decimal] = field(default_factory=dict)

    def get(self, *keys: str) -> Decimal | None:
        """First present value among ``keys`` (a zero counts as present)."""
        for key in keys:
            value = self.values.get(key)
            if value is not None:
                return value
        return None


def load_annual_facts(session: Session, company: Company) -> dict[int, AnnualFacts]:
    rows = session.execute(
        select(
            Fact.concept_key, Fact.value,
            Period.fiscal_year, Period.period_end,
            Document.filed_date, Document.period_end_hint, Document.id,
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

    best: dict[tuple[int, str], tuple[tuple, Decimal, date]] = {}
    for concept_key, value, fy, period_end, filed, end_hint, doc_id in rows:
        rank = (period_end, filed or date.min, end_hint or date.min, doc_id)
        key = (fy, concept_key)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, value, period_end)

    result: dict[int, AnnualFacts] = {}
    for (fy, concept_key), (_, value, period_end) in best.items():
        year = result.setdefault(fy, AnnualFacts(fiscal_year=fy, period_end=period_end))
        year.period_end = max(year.period_end, period_end)
        year.values[concept_key] = value
    return dict(sorted(result.items()))
