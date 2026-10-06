"""Run `bursa.validate.rules` against real `Fact`/`Period` rows.

`validate/rules.py` is a complete, already-tested accounting-identity rule
engine (see `tests/test_rules.py`) that has simply never been run against the
`facts` table - nothing anywhere else in the codebase referenced it before
this module. This is the glue: group a company's facts into the `PeriodFacts`
buckets the rules expect, run the rules, and write `ValidationResult` rows.

Only what the current `facts` table actually supports is run:

* `run_single_period_rules` over every period independently - balance sheet
  footing, income statement ladder checks, EPS consistency, cash flow
  roll-forward.
* `magnitude_sanity` between a company's two already-present FY periods
  (current year vs the comparative column `bursa.pipeline.normalize` already
  writes from the same document) - catches the RM'000 1000x scale-confusion
  class of bug this project hit for real multiple times this session.

Deliberately NOT run, because the data doesn't exist yet: `q4_derivation`
(needs quarterly filings - this project only ingests annual reports) and
`comparative_match` (needs two *separately filed* documents for the same
company - only one annual report per company exists right now).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from bursa.db.enums import Basis, Continuity, PeriodType
from bursa.db.models import Company, Document, Fact, Period, ValidationResult
from bursa.validate.comparative import ComparativeSummary, compare_company as _compare_company
from bursa.validate.rules import PeriodFacts, PeriodKey, RuleOutcome, magnitude_sanity, run_single_period_rules


@dataclass
class ValidateResult:
    rules_run: int = 0
    rules_passed: int = 0
    rules_failed: int = 0
    failures: list[RuleOutcome] = field(default_factory=list)
    comparative: ComparativeSummary | None = None


@dataclass
class _Bucket:
    facts: PeriodFacts
    period_id: int
    run_id_by_concept: dict[str, int] = field(default_factory=dict)
    scale_multiplier: int = 1


def _load_buckets(session: Session, company_id: int) -> tuple[dict[PeriodKey, _Bucket], set[int]]:
    rows = session.execute(
        select(Fact, Period).join(Period, Fact.period_id == Period.id).where(Fact.company_id == company_id)
    ).all()

    buckets: dict[PeriodKey, _Bucket] = {}
    touched_run_ids: set[int] = set()

    for fact, period in rows:
        key = PeriodKey(
            period_end=period.period_end,
            period_type=PeriodType(period.period_type),
            basis=Basis(fact.basis),
            continuity=Continuity(fact.continuity),
        )
        bucket = buckets.get(key)
        if bucket is None:
            bucket = _Bucket(facts=PeriodFacts(key=key), period_id=period.id)
            buckets[key] = bucket
        bucket.facts.values[fact.concept_key] = fact.value
        bucket.run_id_by_concept[fact.concept_key] = fact.run_id
        bucket.scale_multiplier = fact.scale_multiplier or bucket.scale_multiplier
        touched_run_ids.add(fact.run_id)

    return buckets, touched_run_ids


def _primary_run_id(bucket: _Bucket) -> int:
    """The run most of this bucket's facts came from - a bucket's facts
    overwhelmingly come from one document in practice, so this is real
    provenance even though it isn't pinned to the one specific concept a
    given rule's `actual` traces to."""
    return Counter(bucket.run_id_by_concept.values()).most_common(1)[0][0]


def validate_company(session: Session, company: Company) -> ValidateResult:
    """Run every applicable rule over one company's facts and write
    `ValidationResult` rows. Safe to re-run: existing results for every run
    this company's facts touch are deleted first, so a re-run after
    `bursa normalize facts` replaces stale results instead of accumulating
    duplicates (`ValidationResult` carries no uniqueness constraint)."""
    buckets, touched_run_ids = _load_buckets(session, company.id)
    result = ValidateResult()
    if not buckets:
        return result

    if touched_run_ids:
        session.execute(delete(ValidationResult).where(ValidationResult.run_id.in_(touched_run_ids)))

    def record(key: PeriodKey, outcome: RuleOutcome, run_id: int) -> None:
        bucket = buckets[key]
        session.add(
            ValidationResult(
                run_id=run_id,
                rule_key=outcome.rule_key,
                period_id=bucket.period_id,
                passed=outcome.passed,
                expected=outcome.expected,
                actual=outcome.actual,
                delta=outcome.delta,
                detail=outcome.detail,
            )
        )
        result.rules_run += 1
        if outcome.passed:
            result.rules_passed += 1
        else:
            result.rules_failed += 1
            result.failures.append(outcome)

    for key, bucket in buckets.items():
        tolerance = Decimal(max(bucket.scale_multiplier, 1))
        for outcome in run_single_period_rules(bucket.facts, tolerance):
            record(key, outcome, _primary_run_id(bucket))

    # Pair consecutive periods *within* each (period_type, basis, continuity)
    # group for the period-on-period magnitude check - not just FY: an
    # INSTANT (balance sheet) group needs this exactly as much as an FY one,
    # since magnitude_sanity's own default concept list spans both
    # (`bs.total_assets`/`bs.total_equity` alongside `is.revenue`).
    ordered_keys = sorted(buckets, key=lambda k: (k.period_type, k.basis, k.continuity, k.period_end))
    for prev_key, cur_key in zip(ordered_keys, ordered_keys[1:]):
        if (prev_key.period_type, prev_key.basis, prev_key.continuity) != (
            cur_key.period_type,
            cur_key.basis,
            cur_key.continuity,
        ):
            continue
        for outcome in magnitude_sanity(buckets[cur_key].facts, buckets[prev_key].facts):
            record(cur_key, outcome, _primary_run_id(buckets[cur_key]))

    doc_count = session.scalar(
        select(func.count(Document.id)).where(Document.company_id == company.id)
    )
    if doc_count and doc_count >= 2:
        result.comparative = _compare_company(session, company)

    return result
