"""Compute valuation metrics from extracted facts.

Metrics computed per company per fiscal year:

  EBITDA   = Operating Profit + D&A
             (fallback: PBT + Finance Costs + D&A)
  EBIT     = PBT + Finance Costs
             (fallback: Operating Profit)
  NOPAT    = EBIT × (1 - effective tax rate)
  FCFF     = NOPAT + D&A - CapEx - ΔWC
             (fallback from CF: net_operating - purchase_of_ppe + interest_paid×(1-t))
  FCFE     = net_operating - purchase_of_ppe
             (owner cash flow after reinvestment)
  Net Debt = Total Borrowings - Cash

All values in RM (base units, not thousands).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from bursa.analysis.facts import load_annual_facts
from bursa.db.models import Company

log = logging.getLogger(__name__)

ZERO = Decimal(0)


@dataclass
class YearMetrics:
    fiscal_year: int
    period_end: str

    ebit: Decimal | None = None
    ebitda: Decimal | None = None
    nopat: Decimal | None = None
    fcff: Decimal | None = None
    fcfe: Decimal | None = None
    net_debt: Decimal | None = None

    effective_tax_rate: Decimal | None = None
    capex: Decimal | None = None
    dep_amort: Decimal | None = None
    wc_change: Decimal | None = None

    notes: list[str] = field(default_factory=list)


@dataclass
class ValuationResult:
    stock_code: str
    name: str
    years: list[YearMetrics] = field(default_factory=list)


def _get(facts: dict[str, Decimal], *keys: str) -> Decimal | None:
    for k in keys:
        v = facts.get(k)
        if v is not None:
            return v
    return None


def _effective_tax_rate(facts: dict[str, Decimal]) -> Decimal | None:
    pbt = facts.get("is.profit_before_tax")
    tax = facts.get("is.tax_expense")
    if pbt is None or tax is None or pbt == 0:
        return None
    rate = -tax / pbt
    if rate < 0 or rate > Decimal("0.60"):
        return None
    return rate


def _compute_year(
    is_facts: dict[str, Decimal],
    bs_facts: dict[str, Decimal],
    cf_facts: dict[str, Decimal],
    prev_bs: dict[str, Decimal] | None,
    fiscal_year: int,
    period_end: str,
) -> YearMetrics:
    m = YearMetrics(fiscal_year=fiscal_year, period_end=period_end)

    pbt = is_facts.get("is.profit_before_tax")
    finance_costs = is_facts.get("is.finance_costs")
    operating_profit = is_facts.get("is.operating_profit")
    tax_rate = _effective_tax_rate(is_facts)
    m.effective_tax_rate = tax_rate

    # EBIT
    if pbt is not None and finance_costs is not None:
        m.ebit = pbt + abs(finance_costs)
        m.notes.append("EBIT=PBT+|FinCosts|")
    elif operating_profit is not None:
        m.ebit = operating_profit
        m.notes.append("EBIT=OpProfit")

    # D&A — prefer CF statement adjustment, fall back to IS line
    dep = _get(cf_facts, "cf.operating_before_wc")
    cf_pbt = cf_facts.get("cf.profit_before_tax")
    if dep is not None and cf_pbt is not None and pbt is not None:
        m.dep_amort = dep - pbt
        if m.dep_amort < 0:
            m.dep_amort = None
    if m.dep_amort is None:
        m.dep_amort = _get(is_facts, "is.depreciation_amortisation")
    if m.dep_amort is not None and m.dep_amort < 0:
        m.dep_amort = abs(m.dep_amort)

    # EBITDA
    if m.ebit is not None and m.dep_amort is not None:
        m.ebitda = m.ebit + m.dep_amort
    elif operating_profit is not None and m.dep_amort is not None:
        m.ebitda = operating_profit + m.dep_amort

    # NOPAT
    if m.ebit is not None and tax_rate is not None:
        m.nopat = m.ebit * (1 - tax_rate)

    # CapEx
    capex_raw = cf_facts.get("cf.purchase_of_ppe")
    if capex_raw is not None:
        m.capex = abs(capex_raw)

    # Working capital change from CF
    wc_items = ["cf.changes_in_receivables", "cf.changes_in_inventories", "cf.changes_in_payables"]
    wc_vals = [cf_facts.get(k) for k in wc_items]
    if all(v is not None for v in wc_vals):
        m.wc_change = sum(wc_vals, ZERO)
    elif cf_facts.get("cf.working_capital_changes") is not None:
        m.wc_change = cf_facts["cf.working_capital_changes"]

    # FCFF (method 1: NOPAT + D&A - CapEx - ΔWC)
    if m.nopat is not None and m.dep_amort is not None and m.capex is not None:
        wc = m.wc_change if m.wc_change is not None else ZERO
        m.fcff = m.nopat + m.dep_amort - m.capex - wc
        if m.wc_change is None:
            m.notes.append("FCFF:noWC")

    # FCFE (simple: net_operating - capex)
    net_op = cf_facts.get("cf.net_operating")
    if net_op is not None and m.capex is not None:
        m.fcfe = net_op - m.capex

    # Net Debt
    borrowings = _get(bs_facts, "bs.borrowings_total")
    if borrowings is None:
        lt = bs_facts.get("bs.lt_borrowings")
        st = bs_facts.get("bs.st_borrowings")
        if lt is not None or st is not None:
            borrowings = (lt or ZERO) + (st or ZERO)
    cash = bs_facts.get("bs.cash_and_equivalents")
    if borrowings is not None and cash is not None:
        m.net_debt = abs(borrowings) - cash

    return m


def compute_valuation(
    session: Session,
    company: Company,
) -> ValuationResult:
    result = ValuationResult(stock_code=company.stock_code, name=company.name)

    fy_data: dict[int, dict[str, dict[str, Decimal]]] = {}
    fy_pe: dict[int, str] = {}
    for fy, annual in load_annual_facts(session, company).items():
        fy_data[fy] = {"is": {}, "bs": {}, "cf": {}}
        fy_pe[fy] = str(annual.period_end)
        for concept_key, value in annual.values.items():
            prefix = concept_key.split(".")[0]
            if prefix in fy_data[fy]:
                fy_data[fy][prefix][concept_key] = value

    sorted_fys = sorted(fy_data.keys())
    for i, fy in enumerate(sorted_fys):
        prev_bs = fy_data[sorted_fys[i - 1]]["bs"] if i > 0 else None
        m = _compute_year(
            fy_data[fy]["is"],
            fy_data[fy]["bs"],
            fy_data[fy]["cf"],
            prev_bs,
            fy,
            fy_pe[fy],
        )
        result.years.append(m)

    return result
