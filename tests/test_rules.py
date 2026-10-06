from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from bursa.db.enums import PeriodType
from bursa.validate.rules import (
    PeriodFacts,
    PeriodKey,
    comparative_match,
    magnitude_sanity,
    q4_derivation,
    run_single_period_rules,
)

D = Decimal
TOL = D(1)


def facts(values: dict[str, str], period_type: PeriodType = PeriodType.INSTANT) -> PeriodFacts:
    return PeriodFacts(
        key=PeriodKey(date(2024, 9, 30), period_type),
        values={k: D(v) for k, v in values.items()},
    )


# Consistent with tests/fixtures/synthetic.py BALANCE_SHEET.
BALANCED = {
    "bs.total_assets": "465600",
    "bs.total_non_current_assets": "270860",
    "bs.total_current_assets": "194740",
    "bs.total_liabilities": "164925",
    "bs.total_non_current_liabilities": "87515",
    "bs.total_current_liabilities": "77410",
    "bs.total_equity": "300675",
    "bs.equity_owners": "292455",
    "bs.nci": "8220",
    "bs.total_equity_and_liabilities": "465600",
}


def outcomes_by_key(results) -> dict[str, object]:
    return {o.rule_key: o for o in results}


def test_a_balanced_balance_sheet_passes_everything() -> None:
    results = run_single_period_rules(facts(BALANCED), TOL)
    assert results, "no rules fired"
    assert all(o.passed for o in results), [o.detail for o in results if not o.passed]


def test_unbalanced_balance_sheet_is_caught() -> None:
    broken = BALANCED | {"bs.total_assets": "465601000"}
    results = outcomes_by_key(run_single_period_rules(facts(broken), TOL))
    assert results["bs_balances"].passed is False
    assert results["bs_balances"].delta == D("465135400")


def test_rules_do_not_fire_on_partial_data() -> None:
    # A missing input means "not applicable", never "failed".
    results = run_single_period_rules(facts({"bs.total_assets": "100"}), TOL)
    assert [o for o in results if o.rule_key == "bs_balances"] == []


def test_income_statement_ladder_with_bracketed_expenses() -> None:
    results = outcomes_by_key(
        run_single_period_rules(
            facts(
                {
                    "is.revenue": "125430",
                    "is.cost_of_sales": "-92318",
                    "is.gross_profit": "33112",
                    "is.profit_before_tax": "13802",
                    "is.tax_expense": "-3450",
                    "is.profit_for_period": "10352",
                    "is.pat_owners": "9845",
                    "is.pat_nci": "507",
                },
                PeriodType.Q3,
            ),
            TOL,
        )
    )
    assert results["is_gross_profit"].passed
    assert results["is_pbt_to_pat"].passed
    assert results["is_pat_split"].passed


def test_income_statement_ladder_with_unbracketed_expenses() -> None:
    """Some issuers print costs positive. The rule must not cry wolf."""
    results = outcomes_by_key(
        run_single_period_rules(
            facts(
                {
                    "is.revenue": "125430",
                    "is.cost_of_sales": "92318",
                    "is.gross_profit": "33112",
                    "is.profit_before_tax": "13802",
                    "is.tax_expense": "3450",
                    "is.profit_for_period": "10352",
                },
                PeriodType.Q3,
            ),
            TOL,
        )
    )
    assert results["is_gross_profit"].passed
    assert results["is_pbt_to_pat"].passed


def test_a_genuinely_wrong_figure_fits_neither_sign_convention() -> None:
    results = outcomes_by_key(
        run_single_period_rules(
            facts(
                {
                    "is.revenue": "125430",
                    "is.cost_of_sales": "-92318",
                    "is.gross_profit": "51000",  # wrong under either convention
                },
                PeriodType.Q3,
            ),
            TOL,
        )
    )
    assert results["is_gross_profit"].passed is False


def test_pat_split_is_strict_about_signs() -> None:
    broken = {
        "is.profit_for_period": "10352",
        "is.pat_owners": "9845",
        "is.pat_nci": "5070",  # a misread digit
    }
    results = outcomes_by_key(run_single_period_rules(facts(broken, PeriodType.Q3), TOL))
    assert results["is_pat_split"].passed is False


def test_cash_flow_rolls_forward() -> None:
    results = outcomes_by_key(
        run_single_period_rules(
            facts(
                {
                    "cf.net_operating": "31200",
                    "cf.net_investing": "-18400",
                    "cf.net_financing": "-8915",
                    "cf.net_change_in_cash": "3885",
                    "cf.cash_beginning": "38220",
                    "cf.forex_effect": "0",
                    "cf.cash_end": "42105",
                },
                PeriodType.YTD,
            ),
            TOL,
        )
    )
    assert results["cf_net_change"].passed
    assert results["cf_cash_roll"].passed


def test_cash_flow_catches_a_dropped_sign() -> None:
    results = outcomes_by_key(
        run_single_period_rules(
            facts(
                {
                    "cf.net_operating": "31200",
                    "cf.net_investing": "18400",  # bracket lost in OCR
                    "cf.net_financing": "-8915",
                    "cf.net_change_in_cash": "3885",
                },
                PeriodType.YTD,
            ),
            TOL,
        )
    )
    assert results["cf_net_change"].passed is False


def test_eps_consistency_catches_the_1000x_scale_error() -> None:
    good = facts(
        {
            "is.eps_basic": "2.45",
            "is.pat_owners": "9845000",
            "is.weighted_avg_shares": "401836735",
        },
        PeriodType.Q3,
    )
    assert outcomes_by_key(run_single_period_rules(good, TOL))["eps_consistency"].passed

    # EPS wrongly multiplied by the RM'000 factor along with the money columns.
    bad = facts(
        {
            "is.eps_basic": "2450",
            "is.pat_owners": "9845000",
            "is.weighted_avg_shares": "401836735",
        },
        PeriodType.Q3,
    )
    assert outcomes_by_key(run_single_period_rules(bad, TOL))["eps_consistency"].passed is False


def test_q4_derivation() -> None:
    fy = facts({"is.revenue": "480000"}, PeriodType.FY)
    quarters = [
        facts({"is.revenue": "115000"}, PeriodType.Q1),
        facts({"is.revenue": "120000"}, PeriodType.Q2),
        facts({"is.revenue": "127890"}, PeriodType.Q3),
        facts({"is.revenue": "117110"}, PeriodType.Q4),
    ]
    assert q4_derivation(fy, quarters, "is.revenue", TOL).passed

    quarters[3] = facts({"is.revenue": "107110"}, PeriodType.Q4)
    outcome = q4_derivation(fy, quarters, "is.revenue", TOL)
    assert outcome.passed is False
    assert outcome.delta == D("10000")


def test_q4_derivation_needs_all_four_quarters() -> None:
    fy = facts({"is.revenue": "480000"}, PeriodType.FY)
    assert q4_derivation(fy, [facts({"is.revenue": "115000"})], "is.revenue") is None


def test_comparative_match_is_silent_when_nothing_was_restated() -> None:
    current = facts({"is.revenue": "318455", "is.profit_for_period": "18320"}, PeriodType.YTD)
    earlier = facts({"is.revenue": "318455", "is.profit_for_period": "18320"}, PeriodType.YTD)
    assert all(o.passed for o in comparative_match(current, earlier, TOL))


def test_comparative_match_surfaces_a_restatement() -> None:
    current = facts({"is.revenue": "310000"}, PeriodType.YTD)
    earlier = facts({"is.revenue": "318455"}, PeriodType.YTD)
    outcomes = comparative_match(current, earlier, TOL)
    assert len(outcomes) == 1
    assert outcomes[0].passed is False
    assert outcomes[0].delta == D("-8455")


def test_comparative_match_only_compares_shared_concepts() -> None:
    current = facts({"is.revenue": "1", "is.other_income": "5"})
    earlier = facts({"is.revenue": "1"})
    assert [o.rule_key for o in comparative_match(current, earlier, TOL)] == ["comparative_match"]


def test_pbt_to_pat_uses_profit_continuing_when_discontinued_exists() -> None:
    results = outcomes_by_key(
        run_single_period_rules(
            facts(
                {
                    "is.profit_before_tax": "2644403",
                    "is.tax_expense": "-639912",
                    "is.profit_continuing": "2004491",
                    "is.profit_for_period": "2039152",
                },
                PeriodType.FY,
            ),
            TOL,
        )
    )
    assert results["is_pbt_to_pat"].passed


def test_pat_split_with_perpetual_bond() -> None:
    results = outcomes_by_key(
        run_single_period_rules(
            facts(
                {
                    "is.profit_for_period": "100000",
                    "is.pat_owners": "80000",
                    "is.pat_nci": "12000",
                    "is.pat_perpetual_bond": "8000",
                },
                PeriodType.FY,
            ),
            TOL,
        )
    )
    assert results["is_pat_split"].passed


def test_magnitude_sanity_identifies_a_scale_error() -> None:
    current = facts({"is.revenue": "362890000"})
    prior = facts({"is.revenue": "362890"})
    outcomes = magnitude_sanity(current, prior)
    assert len(outcomes) == 1
    assert "1000x scale error" in outcomes[0].detail
    assert outcomes[0].severity == 95


def test_magnitude_sanity_tolerates_normal_growth() -> None:
    assert magnitude_sanity(facts({"is.revenue": "400000"}), facts({"is.revenue": "362890"})) == []


@pytest.mark.parametrize("threshold", [Decimal("0.8")])
def test_magnitude_sanity_flags_a_big_but_unexplained_move(threshold: Decimal) -> None:
    outcomes = magnitude_sanity(
        facts({"is.revenue": "900000"}), facts({"is.revenue": "362890"}), threshold=threshold
    )
    assert len(outcomes) == 1
    # Not a clean multiple, so it is a lower-priority review item, not a scale bug.
    assert outcomes[0].severity == 40
