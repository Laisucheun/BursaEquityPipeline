"""Derive missing facts from accounting identities.

Runs after normalization. For each company+period, checks whether key
subtotals are missing but derivable from their components:

  bs.total_assets     = bs.total_equity_and_liabilities
  bs.total_equity     = bs.total_assets - bs.total_liabilities
  bs.total_equity     = bs.total_equity_and_liabilities - bs.total_liabilities
  is.profit_before_tax = is.profit_for_period + is.tax_expense  (tax is stored negative)
  is.profit_for_period = is.profit_before_tax - is.tax_expense

Derived facts are marked with confidence < 1.0 to distinguish them from
directly extracted ones.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from bursa.db.models import Company, Concept, Fact, Period

log = logging.getLogger(__name__)

DERIVED_CONFIDENCE = 0.9


@dataclass
class DeriveResult:
    derived: int = 0
    skipped_existing: int = 0


class _DerivationRule:
    """One accounting identity that can fill a missing fact."""

    def __init__(self, target: str, formula: str, operands: list[tuple[str, int]]):
        self.target = target
        self.formula = formula
        self.operands = operands  # (concept_key, sign_multiplier)

    def compute(self, facts: dict[str, Decimal]) -> Decimal | None:
        result = Decimal(0)
        for key, sign in self.operands:
            val = facts.get(key)
            if val is None:
                return None
            result += val * sign
        return result


RULES: list[_DerivationRule] = [
    _DerivationRule(
        "bs.total_assets",
        "total_equity_and_liabilities",
        [("bs.total_equity_and_liabilities", 1)],
    ),
    _DerivationRule(
        "bs.total_equity",
        "total_assets - total_liabilities",
        [("bs.total_assets", 1), ("bs.total_liabilities", -1)],
    ),
    _DerivationRule(
        "bs.total_equity",
        "total_equity_and_liabilities - total_liabilities",
        [("bs.total_equity_and_liabilities", 1), ("bs.total_liabilities", -1)],
    ),
    _DerivationRule(
        "is.profit_before_tax",
        "profit_for_period - tax_expense",
        [("is.profit_for_period", 1), ("is.tax_expense", -1)],
    ),
    _DerivationRule(
        "is.profit_for_period",
        "profit_before_tax + tax_expense",
        [("is.profit_before_tax", 1), ("is.tax_expense", 1)],
    ),
    _DerivationRule(
        "is.pat_owners",
        "profit_for_period - pat_nci",
        [("is.profit_for_period", 1), ("is.pat_nci", -1)],
    ),
    _DerivationRule(
        "bs.equity_owners",
        "total_equity - nci",
        [("bs.total_equity", 1), ("bs.nci", -1)],
    ),
]


def derive_facts_for_company(
    session: Session,
    company: Company,
) -> DeriveResult:
    """Compute derivable facts for all periods of one company."""
    result = DeriveResult()

    all_facts = session.execute(
        select(Fact.concept_key, Fact.value, Fact.period_id, Fact.basis,
               Fact.continuity, Fact.reported_in_document_id, Fact.run_id,
               Period.period_type)
        .join(Period, Fact.period_id == Period.id)
        .where(Fact.company_id == company.id)
    ).all()

    by_period: dict[tuple[int, str, str], dict[str, Decimal]] = {}
    period_meta: dict[tuple[int, str, str], tuple[int, int]] = {}

    for ck, val, period_id, basis, continuity, doc_id, run_id, ptype in all_facts:
        key = (period_id, basis, continuity)
        if key not in by_period:
            by_period[key] = {}
            period_meta[key] = (doc_id, run_id)
        by_period[key][ck] = val

    valid_concepts = set(
        session.scalars(select(Concept.concept_key))
    )

    for period_key, facts in by_period.items():
        period_id, basis, continuity = period_key
        doc_id, run_id = period_meta[period_key]

        for rule in RULES:
            if rule.target not in valid_concepts:
                continue
            if rule.target in facts:
                result.skipped_existing += 1
                continue

            derived_value = rule.compute(facts)
            if derived_value is None:
                continue

            existing = session.execute(
                select(Fact.id).where(
                    Fact.company_id == company.id,
                    Fact.concept_key == rule.target,
                    Fact.period_id == period_id,
                    Fact.basis == basis,
                    Fact.continuity == continuity,
                )
            ).scalar_one_or_none()

            if existing is not None:
                result.skipped_existing += 1
                facts[rule.target] = derived_value
                continue

            session.add(Fact(
                company_id=company.id,
                concept_key=rule.target,
                period_id=period_id,
                value=derived_value,
                currency="MYR",
                scale_multiplier=1,
                basis=basis,
                continuity=continuity,
                reported_in_document_id=doc_id,
                run_id=run_id,
                confidence=DERIVED_CONFIDENCE,
            ))
            facts[rule.target] = derived_value
            result.derived += 1
            log.info(
                "%s: derived %s for period %d via %s = %s",
                company.stock_code, rule.target, period_id,
                rule.formula, derived_value,
            )

    if result.derived > 0:
        session.flush()

    return result
