"""Tests for valuation metrics computation."""

from decimal import Decimal

from bursa.valuation.metrics import _compute_year, _effective_tax_rate

D = Decimal


def test_ebit_from_pbt_plus_finance_costs():
    is_facts = {
        "is.profit_before_tax": D("1000000"),
        "is.finance_costs": D("-200000"),
        "is.tax_expense": D("-250000"),
    }
    m = _compute_year(is_facts, {}, {}, None, 2024, "2024-12-31")
    assert m.ebit == D("1200000")
    assert "EBIT=PBT+|FinCosts|" in m.notes


def test_ebit_fallback_operating_profit():
    is_facts = {
        "is.operating_profit": D("800000"),
    }
    m = _compute_year(is_facts, {}, {}, None, 2024, "2024-12-31")
    assert m.ebit == D("800000")
    assert "EBIT=OpProfit" in m.notes


def test_ebitda_with_dep_amort():
    is_facts = {
        "is.profit_before_tax": D("1000000"),
        "is.finance_costs": D("-200000"),
        "is.depreciation_amortisation": D("150000"),
        "is.tax_expense": D("-250000"),
    }
    m = _compute_year(is_facts, {}, {}, None, 2024, "2024-12-31")
    assert m.ebitda == D("1350000")


def test_nopat():
    is_facts = {
        "is.profit_before_tax": D("1000000"),
        "is.finance_costs": D("-200000"),
        "is.tax_expense": D("-250000"),
    }
    m = _compute_year(is_facts, {}, {}, None, 2024, "2024-12-31")
    assert m.nopat is not None
    assert m.effective_tax_rate == D("0.25")
    assert m.nopat == D("900000")


def test_fcff_full():
    is_facts = {
        "is.profit_before_tax": D("1000000"),
        "is.finance_costs": D("-200000"),
        "is.tax_expense": D("-250000"),
        "is.depreciation_amortisation": D("100000"),
    }
    cf_facts = {
        "cf.purchase_of_ppe": D("-300000"),
        "cf.changes_in_receivables": D("-50000"),
        "cf.changes_in_inventories": D("-30000"),
        "cf.changes_in_payables": D("20000"),
    }
    m = _compute_year(is_facts, {}, cf_facts, None, 2024, "2024-12-31")
    # EBIT = 1000000 + 200000 = 1200000
    # NOPAT = 1200000 * 0.75 = 900000
    # D&A = 100000
    # CapEx = 300000
    # WC change = -50000 + -30000 + 20000 = -60000
    # FCFF = 900000 + 100000 - 300000 - (-60000) = 760000
    assert m.fcff == D("760000")


def test_fcfe():
    cf_facts = {
        "cf.net_operating": D("500000"),
        "cf.purchase_of_ppe": D("-200000"),
    }
    m = _compute_year({}, {}, cf_facts, None, 2024, "2024-12-31")
    assert m.fcfe == D("300000")


def test_net_debt():
    bs_facts = {
        "bs.borrowings_total": D("-5000000"),
        "bs.cash_and_equivalents": D("2000000"),
    }
    m = _compute_year({}, bs_facts, {}, None, 2024, "2024-12-31")
    assert m.net_debt == D("3000000")


def test_net_debt_from_lt_st():
    bs_facts = {
        "bs.lt_borrowings": D("-3000000"),
        "bs.st_borrowings": D("-1000000"),
        "bs.cash_and_equivalents": D("500000"),
    }
    m = _compute_year({}, bs_facts, {}, None, 2024, "2024-12-31")
    assert m.net_debt == D("3500000")


def test_effective_tax_rate_rejection():
    assert _effective_tax_rate({"is.profit_before_tax": D("0"), "is.tax_expense": D("-100")}) is None
    assert _effective_tax_rate({"is.profit_before_tax": D("100"), "is.tax_expense": D("-80")}) is None
    assert _effective_tax_rate({"is.profit_before_tax": D("100"), "is.tax_expense": D("10")}) is None
