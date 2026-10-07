"""Stage 6 - accounting identities that catch extraction errors.

These are what make it possible to trust a backfill nobody has eyeballed. Each
rule is a pure function over a period's facts, so they are cheap to test and
cheap to run.

Sign handling: values are stored exactly as printed, and issuers are not
consistent about whether expenses carry brackets. Rules that combine a subtotal
with an expense therefore accept either convention and fail only when the
figures fit *neither* - which is what an extraction error actually looks like.
The balance sheet identity has no such ambiguity and stays strict, which is
why it is the most valuable rule here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from bursa.db.enums import Basis, Continuity, PeriodType

ZERO = Decimal(0)


@dataclass(frozen=True)
class PeriodKey:
    period_end: date
    period_type: PeriodType
    basis: Basis = Basis.CONSOLIDATED
    continuity: Continuity = Continuity.TOTAL


@dataclass
class PeriodFacts:
    """Every fact for one period, keyed by concept."""

    key: PeriodKey
    values: dict[str, Decimal] = field(default_factory=dict)

    def get(self, concept_key: str) -> Decimal | None:
        return self.values.get(concept_key)

    def all_of(self, *concept_keys: str) -> list[Decimal] | None:
        """Every named value, or ``None`` if any is missing.

        Rules must not fire on partial data - a missing input means the rule is
        not applicable, not that the document is wrong.
        """
        out = []
        for key in concept_keys:
            value = self.values.get(key)
            if value is None:
                return None
            out.append(value)
        return out


@dataclass
class RuleOutcome:
    rule_key: str
    passed: bool
    detail: str
    expected: Decimal | None = None
    actual: Decimal | None = None
    delta: Decimal | None = None
    severity: int = 50
    period_end: date | None = None


Rule = Callable[[PeriodFacts, Decimal], RuleOutcome | None]


def _close(a: Decimal, b: Decimal, tolerance: Decimal) -> bool:
    return abs(a - b) <= tolerance


def _outcome(
    rule_key: str,
    expected: Decimal,
    actual: Decimal,
    tolerance: Decimal,
    detail: str,
    facts: PeriodFacts,
    severity: int = 50,
) -> RuleOutcome:
    delta = actual - expected
    return RuleOutcome(
        rule_key=rule_key,
        passed=abs(delta) <= tolerance,
        detail=detail,
        expected=expected,
        actual=actual,
        delta=delta,
        severity=severity,
        period_end=facts.key.period_end,
    )


# --------------------------------------------------------------------------
# Balance sheet - unambiguous signs, so these are strict.
# --------------------------------------------------------------------------


def bs_balances(facts: PeriodFacts, tolerance: Decimal) -> RuleOutcome | None:
    """Assets = liabilities + equity. The single most valuable check."""
    parts = facts.all_of("bs.total_assets", "bs.total_liabilities", "bs.total_equity")
    if parts is None:
        return None
    assets, liabilities, equity = parts
    return _outcome(
        "bs_balances",
        expected=liabilities + equity,
        actual=assets,
        tolerance=tolerance,
        detail="total assets vs total liabilities + total equity",
        facts=facts,
        severity=90,
    )


def bs_assets_split(facts: PeriodFacts, tolerance: Decimal) -> RuleOutcome | None:
    parts = facts.all_of(
        "bs.total_assets", "bs.total_non_current_assets", "bs.total_current_assets"
    )
    if parts is None:
        return None
    total, non_current, current = parts
    return _outcome(
        "bs_assets_split",
        expected=non_current + current,
        actual=total,
        tolerance=tolerance,
        detail="total assets vs non-current + current",
        facts=facts,
        severity=70,
    )


def bs_liabilities_split(facts: PeriodFacts, tolerance: Decimal) -> RuleOutcome | None:
    parts = facts.all_of(
        "bs.total_liabilities",
        "bs.total_non_current_liabilities",
        "bs.total_current_liabilities",
    )
    if parts is None:
        return None
    total, non_current, current = parts
    return _outcome(
        "bs_liabilities_split",
        expected=non_current + current,
        actual=total,
        tolerance=tolerance,
        detail="total liabilities vs non-current + current",
        facts=facts,
        severity=70,
    )


def bs_equity_split(facts: PeriodFacts, tolerance: Decimal) -> RuleOutcome | None:
    parts = facts.all_of("bs.total_equity", "bs.equity_owners", "bs.nci")
    if parts is None:
        return None
    total, owners, nci = parts
    return _outcome(
        "bs_equity_split",
        expected=owners + nci,
        actual=total,
        tolerance=tolerance,
        detail="total equity vs owners + non-controlling interests",
        facts=facts,
        severity=60,
    )


def bs_footing(facts: PeriodFacts, tolerance: Decimal) -> RuleOutcome | None:
    parts = facts.all_of("bs.total_equity_and_liabilities", "bs.total_assets")
    if parts is None:
        return None
    total_eq_liab, assets = parts
    return _outcome(
        "bs_footing",
        expected=assets,
        actual=total_eq_liab,
        tolerance=tolerance,
        detail="the two sides of the balance sheet must foot to the same number",
        facts=facts,
        severity=90,
    )


# --------------------------------------------------------------------------
# Income statement - sign conventions vary, so these accept either.
# --------------------------------------------------------------------------


def _either_sign(
    rule_key: str,
    subtotal: Decimal,
    base: Decimal,
    component: Decimal,
    tolerance: Decimal,
    detail: str,
    facts: PeriodFacts,
    severity: int = 50,
) -> RuleOutcome:
    """Check ``subtotal == base ± component`` and pass if either fits.

    An extraction error fits neither, so the rule still catches what it is for.
    """
    added = base + component
    subtracted = base - component
    best = added if abs(subtotal - added) <= abs(subtotal - subtracted) else subtracted
    delta = subtotal - best
    return RuleOutcome(
        rule_key=rule_key,
        passed=abs(delta) <= tolerance,
        detail=detail,
        expected=best,
        actual=subtotal,
        delta=delta,
        severity=severity,
        period_end=facts.key.period_end,
    )


def is_gross_profit(facts: PeriodFacts, tolerance: Decimal) -> RuleOutcome | None:
    parts = facts.all_of("is.gross_profit", "is.revenue", "is.cost_of_sales")
    if parts is None:
        return None
    gross, revenue, cost = parts
    return _either_sign(
        "is_gross_profit",
        gross,
        revenue,
        cost,
        tolerance,
        "gross profit vs revenue less cost of sales",
        facts,
        severity=70,
    )


def is_pbt_to_pat(facts: PeriodFacts, tolerance: Decimal) -> RuleOutcome | None:
    pbt = facts.get("is.profit_before_tax")
    tax = facts.get("is.tax_expense")
    if pbt is None or tax is None:
        return None
    # PBT and tax are from continuing operations only; when a discontinued-
    # operations split exists, compare against profit_continuing, not the
    # all-in profit_for_period (which includes discontinued).
    pat = facts.get("is.profit_continuing") or facts.get("is.profit_for_period")
    if pat is None:
        return None
    return _either_sign(
        "is_pbt_to_pat",
        pat,
        pbt,
        tax,
        tolerance,
        "profit for the period vs profit before tax less tax",
        facts,
        severity=80,
    )


def is_pat_split(facts: PeriodFacts, tolerance: Decimal) -> RuleOutcome | None:
    """Profit splits between the parent's owners and NCI. Signs unambiguous."""
    owners = facts.get("is.pat_owners")
    nci = facts.get("is.pat_nci")
    if owners is None or nci is None:
        return None
    perp = facts.get("is.pat_perpetual_bond") or ZERO
    split = owners + nci + perp
    pat_cont = facts.get("is.profit_continuing")
    pat_total = facts.get("is.profit_for_period")
    if pat_cont is not None and abs(split - pat_cont) <= tolerance:
        total = pat_cont
    elif pat_total is not None:
        total = pat_total
    elif pat_cont is not None:
        total = pat_cont
    else:
        return None
    return _outcome(
        "is_pat_split",
        expected=split,
        actual=total,
        tolerance=tolerance,
        detail="profit for the period vs owners + non-controlling interests",
        facts=facts,
        severity=80,
    )


def eps_consistency(facts: PeriodFacts, tolerance: Decimal) -> RuleOutcome | None:
    """EPS (sen) x weighted shares ≈ profit attributable to owners (RM).

    Catches the classic 1000x scale error, where EPS was multiplied by the
    RM'000 factor along with everything else.
    """
    parts = facts.all_of("is.eps_basic", "is.pat_owners", "is.weighted_avg_shares")
    if parts is None:
        return None
    eps_sen, patami, shares = parts
    if shares == ZERO:
        return None

    implied = eps_sen / Decimal(100) * shares
    # EPS is rounded to 2 decimals before printing, so the tolerance has to
    # absorb half a sen across the whole share count.
    eps_tolerance = max(tolerance, shares / Decimal(100) / Decimal(2))
    delta = patami - implied
    return RuleOutcome(
        rule_key="eps_consistency",
        passed=abs(delta) <= eps_tolerance,
        detail="EPS x weighted average shares vs profit attributable to owners",
        expected=implied,
        actual=patami,
        delta=delta,
        severity=75,
        period_end=facts.key.period_end,
    )


# --------------------------------------------------------------------------
# Cash flow
# --------------------------------------------------------------------------


def cf_net_change(facts: PeriodFacts, tolerance: Decimal) -> RuleOutcome | None:
    parts = facts.all_of(
        "cf.net_change_in_cash", "cf.net_operating", "cf.net_investing", "cf.net_financing"
    )
    if parts is None:
        return None
    net_change, operating, investing, financing = parts
    return _outcome(
        "cf_net_change",
        expected=operating + investing + financing,
        actual=net_change,
        tolerance=tolerance,
        detail="net change in cash vs operating + investing + financing",
        facts=facts,
        severity=80,
    )


def cf_cash_roll(facts: PeriodFacts, tolerance: Decimal) -> RuleOutcome | None:
    """Opening cash + net change (+ FX) = closing cash."""
    parts = facts.all_of("cf.cash_end", "cf.cash_beginning", "cf.net_change_in_cash")
    if parts is None:
        return None
    cash_end, cash_begin, net_change = parts
    forex = facts.get("cf.forex_effect") or ZERO
    return _outcome(
        "cf_cash_roll",
        expected=cash_begin + net_change + forex,
        actual=cash_end,
        tolerance=tolerance,
        detail="closing cash vs opening + net change + FX effect",
        facts=facts,
        severity=80,
    )


# --------------------------------------------------------------------------
# Statement of changes in equity
# --------------------------------------------------------------------------


def eq_closing_matches_bs(facts: PeriodFacts, tolerance: Decimal) -> RuleOutcome | None:
    """The equity statement's closing total equity is the balance sheet's
    total equity at the same year end - two statements, one figure."""
    parts = facts.all_of("eq.closing_balance", "bs.total_equity")
    if parts is None:
        return None
    closing, total_equity = parts
    return _outcome(
        "eq_closing_matches_bs",
        expected=total_equity,
        actual=closing,
        tolerance=tolerance,
        detail="equity statement closing balance vs balance sheet total equity",
        facts=facts,
        severity=80,
    )


def eq_roll_forward(
    opening: Decimal,
    closing: Decimal,
    movements: Iterable[Decimal],
    tolerance: Decimal = Decimal(1),
    period_end: date | None = None,
) -> RuleOutcome:
    """Closing = opening + the year's movements, for one equity-statement
    year-block. Spans an INSTANT pair and an FY period, so it is not a
    single-period rule: the caller supplies the block's own figures (every
    movement row, not just the mapped ones - an unmapped row would otherwise
    read as a false failure)."""
    expected = opening + sum(movements, ZERO)
    delta = closing - expected
    return RuleOutcome(
        rule_key="eq_roll_forward",
        passed=abs(delta) <= tolerance,
        detail="equity closing balance vs opening + movements",
        expected=expected,
        actual=closing,
        delta=delta,
        severity=70,
        period_end=period_end,
    )


SINGLE_PERIOD_RULES: tuple[Rule, ...] = (
    bs_balances,
    bs_footing,
    bs_assets_split,
    bs_liabilities_split,
    bs_equity_split,
    is_gross_profit,
    is_pbt_to_pat,
    is_pat_split,
    eps_consistency,
    cf_net_change,
    cf_cash_roll,
    eq_closing_matches_bs,
)


def run_single_period_rules(
    facts: PeriodFacts, tolerance: Decimal = Decimal(1)
) -> list[RuleOutcome]:
    """Run every applicable rule over one period."""
    outcomes = []
    for rule in SINGLE_PERIOD_RULES:
        outcome = rule(facts, tolerance)
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


# --------------------------------------------------------------------------
# Cross-period and cross-document rules
# --------------------------------------------------------------------------


def q4_derivation(
    fy: PeriodFacts,
    quarters: list[PeriodFacts],
    concept_key: str,
    tolerance: Decimal = Decimal(1),
) -> RuleOutcome | None:
    """FY - (Q1+Q2+Q3) must equal Q4.

    Many Bursa issuers never publish a standalone Q4; it is derived from the
    annual report less the first three quarters. This rule proves the
    derivation is sound before anything depends on it.
    """
    fy_value = fy.get(concept_key)
    if fy_value is None or len(quarters) != 4:
        return None

    parts = [q.get(concept_key) for q in quarters]
    if any(p is None for p in parts):
        return None

    total = sum(parts, ZERO)  # type: ignore[arg-type]
    delta = fy_value - total
    return RuleOutcome(
        rule_key="q4_derivation",
        passed=abs(delta) <= tolerance,
        detail=f"full year vs sum of four quarters for {concept_key}",
        expected=total,
        actual=fy_value,
        delta=delta,
        severity=70,
        period_end=fy.key.period_end,
    )


def comparative_match(
    current_doc_comparative: PeriodFacts,
    earlier_doc_as_reported: PeriodFacts,
    tolerance: Decimal = Decimal(1),
) -> list[RuleOutcome]:
    """The prior-year column must agree with what was originally filed.

    This is the highest-value rule in the system and costs nothing: it audits
    one document against another the pipeline already holds. A mismatch is
    either a genuine restatement or an extraction error - both of which you want
    surfaced, and neither of which any single-document check can find.
    """
    outcomes: list[RuleOutcome] = []
    shared = set(current_doc_comparative.values) & set(earlier_doc_as_reported.values)

    for concept_key in sorted(shared):
        restated = current_doc_comparative.values[concept_key]
        original = earlier_doc_as_reported.values[concept_key]
        delta = restated - original
        outcomes.append(
            RuleOutcome(
                rule_key="comparative_match",
                passed=abs(delta) <= tolerance,
                detail=(
                    f"{concept_key}: comparative column vs the figure as originally "
                    "reported (a mismatch is a restatement or an extraction error)"
                ),
                expected=original,
                actual=restated,
                delta=delta,
                severity=85,
                period_end=current_doc_comparative.key.period_end,
            )
        )
    return outcomes


def magnitude_sanity(
    current: PeriodFacts,
    prior: PeriodFacts,
    concept_keys: Iterable[str] = ("is.revenue", "bs.total_assets", "bs.total_equity"),
    threshold: Decimal = Decimal("0.8"),
) -> list[RuleOutcome]:
    """Flag implausible period-on-period swings.

    Almost always a scale error (RM'000 read as RM) rather than a real event,
    and the pattern is unmistakable: a clean multiple of 1000.
    """
    outcomes: list[RuleOutcome] = []
    for concept_key in concept_keys:
        now = current.get(concept_key)
        before = prior.get(concept_key)
        if now is None or before is None or before == ZERO:
            continue

        change = abs(now - before) / abs(before)
        if change <= threshold:
            continue

        ratio = now / before
        hint = ""
        for factor in (1000, -1000, 100, -100):
            if abs(ratio - Decimal(factor)) < Decimal("0.05"):
                hint = f" (looks like a {factor}x scale error)"
                break

        outcomes.append(
            RuleOutcome(
                rule_key="magnitude_sanity",
                passed=False,
                detail=f"{concept_key} moved {change:.0%} period on period{hint}",
                expected=before,
                actual=now,
                delta=now - before,
                severity=95 if hint else 40,
                period_end=current.key.period_end,
            )
        )
    return outcomes
