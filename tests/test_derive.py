"""Tests for the derived-facts pipeline."""

from decimal import Decimal


from bursa.pipeline.derive import RULES


class TestDerivationRules:
    def test_total_assets_from_tel(self):
        rule = next(r for r in RULES if r.target == "bs.total_assets")
        facts = {"bs.total_equity_and_liabilities": Decimal("100000")}
        assert rule.compute(facts) == Decimal("100000")

    def test_total_equity_from_assets_minus_liabilities(self):
        rules = [r for r in RULES if r.target == "bs.total_equity"]
        rule = rules[0]  # total_assets - total_liabilities
        facts = {
            "bs.total_assets": Decimal("500000"),
            "bs.total_liabilities": Decimal("300000"),
        }
        assert rule.compute(facts) == Decimal("200000")

    def test_total_equity_from_tel_minus_liabilities(self):
        rules = [r for r in RULES if r.target == "bs.total_equity"]
        rule = rules[1]  # total_equity_and_liabilities - total_liabilities
        facts = {
            "bs.total_equity_and_liabilities": Decimal("500000"),
            "bs.total_liabilities": Decimal("300000"),
        }
        assert rule.compute(facts) == Decimal("200000")

    def test_pbt_from_pat_minus_tax(self):
        rule = next(r for r in RULES if r.target == "is.profit_before_tax")
        facts = {
            "is.profit_for_period": Decimal("80000"),
            "is.tax_expense": Decimal("-20000"),  # stored negative
        }
        # PBT = PAT - tax_expense = 80000 - (-20000) = 100000
        assert rule.compute(facts) == Decimal("100000")

    def test_pat_from_pbt_plus_tax(self):
        rule = next(r for r in RULES if r.target == "is.profit_for_period")
        facts = {
            "is.profit_before_tax": Decimal("100000"),
            "is.tax_expense": Decimal("-20000"),  # stored negative
        }
        # PAT = PBT + tax_expense = 100000 + (-20000) = 80000
        assert rule.compute(facts) == Decimal("80000")

    def test_returns_none_when_operand_missing(self):
        rule = next(r for r in RULES if r.target == "bs.total_assets")
        facts = {}
        assert rule.compute(facts) is None

    def test_returns_none_when_partial_operands(self):
        rules = [r for r in RULES if r.target == "bs.total_equity"]
        rule = rules[0]
        facts = {"bs.total_assets": Decimal("500000")}  # missing liabilities
        assert rule.compute(facts) is None

    def test_pat_owners_from_profit_minus_nci(self):
        rule = next(r for r in RULES if r.target == "is.pat_owners")
        facts = {
            "is.profit_for_period": Decimal("100000"),
            "is.pat_nci": Decimal("15000"),
        }
        assert rule.compute(facts) == Decimal("85000")

    def test_equity_owners_from_total_minus_nci(self):
        rule = next(r for r in RULES if r.target == "bs.equity_owners")
        facts = {
            "bs.total_equity": Decimal("500000"),
            "bs.nci": Decimal("80000"),
        }
        assert rule.compute(facts) == Decimal("420000")
