"""The canonical chart of accounts.

This is the vocabulary the entire system agrees on. Issuers phrase line items
in many ways ("Revenue" / "Turnover" / "Hasil"); every one of those phrasings
resolves to exactly one ``concept_key`` here.

Seed synonyms are deliberately conservative - they only list phrasings that are
unambiguous on their own. Anything borderline is left for the LLM mapper to
judge in context, and the reviewer's correction is what promotes it to a stored
synonym.

Sign convention: values are always stored *exactly as the document reports
them*, including parentheses as negatives. ``typical_sign`` is a QA hint used
by sanity checks, never a transformation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bursa.db.enums import Statement, TypicalSign

POS = TypicalSign.POSITIVE
NEG = TypicalSign.NEGATIVE
ANY = TypicalSign.ANY


@dataclass(frozen=True)
class ConceptSpec:
    key: str
    statement: Statement
    label: str
    parent: str | None = None
    is_subtotal: bool = False
    typical_sign: TypicalSign = ANY
    is_instant: bool = False
    is_per_share: bool = False
    description: str | None = None
    synonyms: tuple[str, ...] = field(default_factory=tuple)
    identity: str | None = None


# --------------------------------------------------------------------------
# Income statement (and other comprehensive income)
# --------------------------------------------------------------------------

INCOME_STATEMENT: list[ConceptSpec] = [
    ConceptSpec(
        "is.revenue", Statement.INCOME_STATEMENT, "Revenue", typical_sign=POS,
        identity="revenue",
        description="Top-line income. For general corporates: revenue/turnover/sales. "
        "For REITs: rental income/lease revenue. Banks use net_interest_income instead.",
        synonyms=("revenue", "turnover", "hasil", "revenue from contracts with customers",
                  "total revenue", "group revenue", "sales",
                  "rental income", "lease revenue", "gross revenue", "gross rental income"),
    ),
    ConceptSpec(
        "is.cost_of_sales", Statement.INCOME_STATEMENT, "Cost of sales",
        parent="is.gross_profit", typical_sign=NEG, identity="cost_of_sales",
        description="Direct costs of producing goods/services sold.",
        synonyms=("cost of sales", "cost of goods sold", "cost of revenue",
                  "kos jualan", "cost of services"),
    ),
    ConceptSpec(
        "is.gross_profit", Statement.INCOME_STATEMENT, "Gross profit",
        is_subtotal=True, typical_sign=ANY,
        identity="gross_profit",
        description="Revenue minus cost of sales.",
        synonyms=("gross profit", "gross profit/(loss)", "untung kasar", "gross margin"),
    ),
    ConceptSpec(
        "is.other_income", Statement.INCOME_STATEMENT, "Other income", typical_sign=POS,
        identity="other_income",
        description="Non-core income (interest, rental, disposal gains etc).",
        synonyms=("other income", "other operating income", "pendapatan lain"),
    ),
    ConceptSpec(
        "is.selling_and_distribution", Statement.INCOME_STATEMENT,
        "Selling and distribution costs", typical_sign=NEG,
        identity="selling_distribution_costs",
        description="Marketing, distribution and selling expenses.",
        synonyms=("selling and distribution costs", "selling and distribution expenses",
                  "distribution costs", "distribution expenses", "selling expenses",
                  "marketing expenses"),
    ),
    ConceptSpec(
        "is.administrative_expenses", Statement.INCOME_STATEMENT,
        "Administrative expenses", typical_sign=NEG,
        identity="admin_expenses",
        description="General and administrative overhead.",
        synonyms=("administrative expenses", "administration expenses",
                  "general and administrative expenses", "belanja pentadbiran"),
    ),
    ConceptSpec(
        "is.other_expenses", Statement.INCOME_STATEMENT, "Other expenses", typical_sign=NEG,
        identity="other_expenses",
        description="Non-core expenses not classified elsewhere.",
        synonyms=("other expenses", "other operating expenses", "belanja lain"),
    ),
    ConceptSpec(
        "is.net_impairment", Statement.INCOME_STATEMENT,
        "Net impairment losses", typical_sign=NEG,
        identity="impairment",
        description="ECL or asset write-downs for general corporates. Banks use allowance_credit_losses.",
        synonyms=("impairment loss", "impairment losses", "net impairment losses",
                  "allowance for impairment", "impairment of receivables"),
    ),
    ConceptSpec(
        "is.operating_profit", Statement.INCOME_STATEMENT, "Operating profit",
        is_subtotal=True,
        identity="operating_profit",
        description="Profit from core operations before finance items.",
        synonyms=("operating profit", "profit from operations", "results from operating activities",
                  "operating profit/(loss)", "untung operasi"),
    ),
    ConceptSpec(
        "is.finance_income", Statement.INCOME_STATEMENT, "Finance income", typical_sign=POS,
        identity="finance_income",
        description="Interest/investment income from financial assets.",
        synonyms=("finance income", "interest income", "finance income/(costs)",
                  "investment income"),
    ),
    ConceptSpec(
        "is.finance_costs", Statement.INCOME_STATEMENT, "Finance costs", typical_sign=NEG,
        identity="finance_costs",
        description="Interest expense on borrowings/leases.",
        synonyms=("finance costs", "finance cost", "interest expense", "kos kewangan",
                  "borrowing costs"),
    ),
    ConceptSpec(
        "is.share_of_associates", Statement.INCOME_STATEMENT,
        "Share of results of associates",
        identity="share_of_associates",
        description="Equity-method pick-up from associated companies (20-50% ownership).",
        synonyms=("share of results of associates", "share of profit of associates",
                  "share of profit/(loss) of associates", "share of results of an associate"),
    ),
    ConceptSpec(
        "is.share_of_jv", Statement.INCOME_STATEMENT, "Share of results of joint ventures",
        # "...of joint venture", no leading "a" - confirmed real on RHB Bank,
        # a plain singular/plural variant the existing phrasings didn't cover.
        identity="share_of_jv",
        description="Equity-method pick-up from joint ventures.",
        synonyms=("share of results of joint ventures", "share of profit of joint ventures",
                  "share of results of a joint venture", "share of results of joint venture"),
    ),
    ConceptSpec(
        "is.profit_before_tax", Statement.INCOME_STATEMENT, "Profit before tax",
        is_subtotal=True,
        # "...and zakat" is the same subtotal role, just an Islamic bank's own
        # phrasing (zakat is an additional religious levy, not a rename of
        # tax) - confirmed real on RHB Bank. Kept as a synonym of the same
        # concept rather than a separate one so it still rolls up with every
        # other issuer's PBT for cross-company comparison.
        identity="profit_before_tax",
        description="Pre-tax profit. Banks may include zakat.",
        synonyms=("profit before tax", "profit before taxation", "profit/(loss) before tax",
                  "profit/(loss) before taxation", "loss before taxation",
                  "untung sebelum cukai", "pbt", "profit before income tax",
                  "profit before taxation and zakat", "profit before zakat and taxation"),
    ),
    ConceptSpec(
        "is.tax_expense", Statement.INCOME_STATEMENT, "Income tax expense", typical_sign=NEG,
        identity="tax_expense",
        description="Corporate income tax. Stored negative. Banks may include zakat.",
        synonyms=("income tax expense", "taxation", "tax expense", "income tax",
                  "cukai", "tax (expense)/credit", "taxation and zakat",
                  "income tax (expense)/credit"),
    ),
    ConceptSpec(
        "is.profit_continuing", Statement.INCOME_STATEMENT,
        "Profit from continuing operations", is_subtotal=True,
        identity="profit_continuing",
        description="Post-tax profit from continuing operations only.",
        synonyms=("profit from continuing operations",
                  "profit/(loss) from continuing operations",
                  "profit for the period from continuing operations"),
    ),
    ConceptSpec(
        "is.profit_discontinued", Statement.INCOME_STATEMENT,
        "Profit from discontinued operations",
        identity="profit_discontinued",
        description="Post-tax profit from discontinued operations.",
        synonyms=("profit from discontinued operations",
                  "profit/(loss) from discontinued operations",
                  "discontinued operations"),
    ),
    ConceptSpec(
        "is.profit_for_period", Statement.INCOME_STATEMENT, "Profit for the period",
        is_subtotal=True,
        identity="net_income",
        description="Bottom-line profit. yfinance: Net Income.",
        synonyms=("profit for the period", "profit for the year",
                  "profit for the financial year", "profit for the financial period",
                  "profit/(loss) for the period", "profit/(loss) for the year",
                  "profit/(loss) for the financial year",
                  "net profit", "net profit for the period", "loss for the period",
                  "untung bersih", "profit after tax", "profit after taxation"),
    ),
    ConceptSpec(
        "is.pat_owners", Statement.INCOME_STATEMENT,
        "Profit attributable to owners of the parent", parent="is.profit_for_period",
        description="PATAMI. The figure EPS is computed from.",
        identity="profit_to_owners",
        synonyms=("owners of the parent", "owners of the company",
                  "equity holders of the parent", "equity holders of the company",
                  "attributable to owners of the parent", "patami",
                  "profit attributable to owners of the parent"),
    ),
    ConceptSpec(
        "is.pat_nci", Statement.INCOME_STATEMENT,
        "Profit attributable to non-controlling interests", parent="is.profit_for_period",
        identity="profit_to_nci",
        description="Profit share for minority shareholders.",
        synonyms=("non-controlling interests", "non-controlling interest",
                  "minority interests", "minority interest",
                  "attributable to non-controlling interests"),
    ),
    ConceptSpec(
        "is.pat_perpetual_bond", Statement.INCOME_STATEMENT,
        "Profit attributable to holders of perpetual bond", parent="is.profit_for_period",
        identity="profit_to_perpetual",
        description="Profit allocated to perpetual bond/sukuk holders.",
        synonyms=("holders of perpetual bond", "holders of perpetual bonds",
                  "perpetual bond holders", "perpetual sukuk holders",
                  "holders of perpetual sukuk"),
    ),
    ConceptSpec(
        "is.other_comprehensive_income", Statement.INCOME_STATEMENT,
        "Other comprehensive income",
        identity="oci",
        description="Unrealised gains/losses bypassing P&L.",
        synonyms=("other comprehensive income", "other comprehensive income/(loss)",
                  "other comprehensive income for the period, net of tax"),
    ),
    ConceptSpec(
        "is.total_comprehensive_income", Statement.INCOME_STATEMENT,
        "Total comprehensive income", is_subtotal=True,
        # Some issuers drop "Total" entirely ("Comprehensive income for the
        # financial year") - confirmed real on Vitrox Corporation's filing.
        # "comprehensive income" alone catches every "for the year/period"
        # variant too, via normalize_label's own stop-phrase stripping.
        identity="total_comprehensive_income",
        description="Net income plus OCI.",
        synonyms=("total comprehensive income", "total comprehensive income for the period",
                  "total comprehensive income/(loss)", "total comprehensive income for the year",
                  "comprehensive income"),
    ),
    ConceptSpec(
        "is.tci_owners", Statement.INCOME_STATEMENT,
        "Total comprehensive income attributable to owners",
        parent="is.total_comprehensive_income",
        # A REIT has unitholders, not shareholders - confirmed real on IGB
        # REIT.
        identity="tci_to_owners",
        description="TCI attributable to parent shareholders.",
        synonyms=("total comprehensive income attributable to unitholders",),
    ),
    ConceptSpec(
        "is.tci_nci", Statement.INCOME_STATEMENT,
        "Total comprehensive income attributable to NCI",
        parent="is.total_comprehensive_income",
        identity="tci_to_nci",
        description="TCI attributable to non-controlling interests.",
    ),
    ConceptSpec(
        "is.eps_basic", Statement.INCOME_STATEMENT, "Basic earnings per share (sen)",
        is_per_share=True,
        # A REIT's equivalent metric is phrased "per unit", not "per share" -
        # confirmed real on IGB REIT - but it is the same economic measure
        # (profit divided by the number of units/shares in issue), so this
        # stays one concept rather than forking into a parallel REIT-only key.
        identity="eps_basic",
        description="Basic EPS in sen. PATAMI / weighted avg shares.",
        synonyms=("basic earnings per share", "basic earnings per share (sen)",
                  "basic eps", "earnings per share - basic",
                  "basic earnings/(loss) per share",
                  "basic earnings per unit", "basic earnings per unit (sen)"),
    ),
    ConceptSpec(
        "is.eps_diluted", Statement.INCOME_STATEMENT, "Diluted earnings per share (sen)",
        is_per_share=True,
        identity="eps_diluted",
        description="Diluted EPS in sen.",
        synonyms=("diluted earnings per share", "diluted eps",
                  "earnings per share - diluted", "diluted earnings/(loss) per share",
                  "diluted earnings per unit", "diluted earnings per unit (sen)"),
    ),
    ConceptSpec(
        "is.weighted_avg_shares", Statement.INCOME_STATEMENT,
        "Weighted average number of ordinary shares", typical_sign=POS,
        identity="weighted_avg_shares",
        description="EPS denominator.",
        synonyms=("weighted average number of ordinary shares",
                  "weighted average number of shares in issue"),
    ),
    ConceptSpec(
        "is.depreciation_amortisation", Statement.INCOME_STATEMENT,
        "Depreciation and amortisation", typical_sign=NEG,
        identity="depreciation",
        description="Non-cash charge for asset usage.",
        synonyms=("depreciation and amortisation", "depreciation and amortization",
                  "depreciation", "amortisation"),
    ),
    # ----------------------------------------------------------------------
    # Bank-specific. Found by reading real unmapped rows from RHB Bank,
    # Alliance Bank, AFFIN Bank, and Kenanga Investment Bank's own income
    # statements - a conventional bank's income statement is built around a
    # different vocabulary from the start ("Net interest income", not
    # "Revenue"), not a few missing synonyms of the general concepts above.
    # ----------------------------------------------------------------------
    ConceptSpec(
        "is.net_interest_income", Statement.INCOME_STATEMENT, "Net interest income",
        is_subtotal=True, typical_sign=POS,
        description="A bank's own top-line measure, playing Revenue's role.",
        identity="net_interest_income",
        synonyms=("net interest income",),
    ),
    ConceptSpec(
        "is.fee_commission_income", Statement.INCOME_STATEMENT,
        "Fee and commission income", typical_sign=POS,
        identity="fee_income",
        description="Bank fee/commission income.",
        synonyms=("fee and commission income", "fee income", "commission income"),
    ),
    ConceptSpec(
        "is.fee_commission_expense", Statement.INCOME_STATEMENT,
        "Fee and commission expense", typical_sign=NEG,
        identity="fee_expense",
        description="Bank fee/commission expenses.",
        synonyms=("fee and commission expense", "fee expense", "commission expense"),
    ),
    ConceptSpec(
        "is.islamic_banking_income", Statement.INCOME_STATEMENT,
        "Income from Islamic banking business", typical_sign=POS,
        identity="islamic_banking_income",
        description="Income from Shariah-compliant banking.",
        synonyms=("income from islamic banking business",
                  "net income from islamic banking business"),
    ),
    ConceptSpec(
        "is.operating_profit_before_allowances", Statement.INCOME_STATEMENT,
        "Operating profit before allowances", is_subtotal=True,
        description="A bank's subtotal before credit-loss allowances - plays "
        "Operating profit's role, distinctly named because a bank's next "
        "line down (allowances) has no equivalent on a general corporate's "
        "income statement.",
        identity="operating_profit_pre_allowances",
        synonyms=("operating profit before allowances",),
    ),
    ConceptSpec(
        "is.operating_profit_after_allowances", Statement.INCOME_STATEMENT,
        "Operating profit after allowances", is_subtotal=True,
        identity="operating_profit_post_allowances",
        description="Bank subtotal after ECL charges.",
        synonyms=("operating profit after allowances",),
    ),
    ConceptSpec(
        "is.allowance_credit_losses", Statement.INCOME_STATEMENT,
        "Allowance for credit losses on financial assets", typical_sign=NEG,
        description="A bank's MFRS 9 ECL charge - always its own named line, "
        "distinct enough from a general corporate's net_impairment to keep "
        "separate rather than conflate the two.",
        identity="credit_loss_allowance",
        synonyms=("allowance for credit losses on financial assets",
                  "allowance for impairment losses on loans, advances and financing"),
    ),
    # ----------------------------------------------------------------------
    # REIT-specific. Found by reading real unmapped rows from IGB REIT and
    # Pavilion REIT's own income statements, once "Revenue" itself resolved
    # via the rental-income synonyms above - a REIT's statement still has
    # several lines with no general-corporate equivalent at all.
    # ----------------------------------------------------------------------
    ConceptSpec(
        "is.property_operating_expenses", Statement.INCOME_STATEMENT,
        "Property operating expenses", typical_sign=NEG,
        description="The REIT-specific cost line directly under rental "
        "income - utilities, maintenance, quit rent, etc. are deliberately "
        "not broken out as their own concepts, too granular to be worth "
        "cross-company comparison.",
        identity="property_opex",
        synonyms=("property operating expenses", "property expenses"),
    ),
    ConceptSpec(
        "is.net_property_income", Statement.INCOME_STATEMENT, "Net property income",
        is_subtotal=True, typical_sign=POS,
        description="Rental income less property operating expenses - a "
        "REIT's own subtotal, playing Gross profit's role.",
        identity="net_property_income",
        synonyms=("net property income",),
    ),
    ConceptSpec(
        "is.manager_fees", Statement.INCOME_STATEMENT, "Manager's fees", typical_sign=NEG,
        identity="manager_fees",
        description="REIT management fee.",
        synonyms=("manager's fees", "manager's management fees", "managers management fees"),
    ),
    ConceptSpec(
        "is.trustee_fees", Statement.INCOME_STATEMENT, "Trustee's fees", typical_sign=NEG,
        identity="trustee_fees",
        description="REIT trustee fee.",
        synonyms=("trustee's fees", "trustees' fees", "trustee fees"),
    ),
    ConceptSpec(
        "is.distributable_income", Statement.INCOME_STATEMENT, "Distributable income",
        is_subtotal=True,
        description="The REIT-specific figure a distribution per unit is "
        "actually computed from - not the same number as profit for the "
        "period (non-cash items like fair value gains are excluded).",
        identity="distributable_income",
        synonyms=("distributable income", "income available for distribution"),
    ),
    ConceptSpec(
        "is.dpu", Statement.INCOME_STATEMENT, "Distribution per unit (sen)",
        is_per_share=True,
        identity="dpu",
        description="Distribution per unit in sen. REIT EPS equivalent.",
        synonyms=("distribution per unit", "distribution per unit (sen)", "dpu"),
    ),
]

# --------------------------------------------------------------------------
# Statement of financial position
# --------------------------------------------------------------------------

BALANCE_SHEET: list[ConceptSpec] = [
    # Non-current assets
    ConceptSpec(
        "bs.ppe", Statement.BALANCE_SHEET, "Property, plant and equipment",
        parent="bs.total_non_current_assets", typical_sign=POS, is_instant=True,
        # "Property and equipment" (no "plant") - confirmed real on AFFIN
        # Bank, a plain wording variant the existing phrasings didn't cover.
        identity="ppe",
        description="Tangible fixed assets.",
        synonyms=("property, plant and equipment", "property plant and equipment",
                  "plant and equipment", "hartanah, loji dan peralatan",
                  "property and equipment"),
    ),
    ConceptSpec(
        "bs.right_of_use_assets", Statement.BALANCE_SHEET, "Right-of-use assets",
        parent="bs.total_non_current_assets", typical_sign=POS, is_instant=True,
        identity="rou_assets",
        description="Leased assets under MFRS 16.",
        synonyms=("right-of-use assets", "right of use assets"),
    ),
    ConceptSpec(
        "bs.investment_properties", Statement.BALANCE_SHEET, "Investment properties",
        parent="bs.total_non_current_assets", typical_sign=POS, is_instant=True,
        identity="investment_properties",
        description="Properties held for rental/capital appreciation.",
        synonyms=("investment properties", "investment property"),
    ),
    ConceptSpec(
        "bs.goodwill", Statement.BALANCE_SHEET, "Goodwill",
        parent="bs.total_non_current_assets", typical_sign=POS, is_instant=True,
        identity="goodwill",
        description="Acquisition premium over fair value.",
        synonyms=("goodwill", "goodwill on consolidation"),
    ),
    ConceptSpec(
        "bs.intangible_assets", Statement.BALANCE_SHEET, "Intangible assets",
        parent="bs.total_non_current_assets", typical_sign=POS, is_instant=True,
        identity="intangible_assets",
        description="Non-physical assets (licenses, patents, software).",
        synonyms=("intangible assets", "other intangible assets"),
    ),
    ConceptSpec(
        "bs.investment_in_associates", Statement.BALANCE_SHEET, "Investments in associates",
        parent="bs.total_non_current_assets", typical_sign=POS, is_instant=True,
        identity="investment_in_associates",
        description="Equity-method investments in 20-50% owned companies.",
        synonyms=("investment in associates", "investments in associates",
                  "investment in an associate"),
    ),
    ConceptSpec(
        "bs.investment_in_jv", Statement.BALANCE_SHEET, "Investments in joint ventures",
        parent="bs.total_non_current_assets", typical_sign=POS, is_instant=True,
        # Singular, no leading "a" - confirmed real on Alliance Bank.
        identity="investment_in_jv",
        description="Equity-method investments in joint ventures.",
        synonyms=("investment in joint ventures", "investments in joint ventures",
                  "investment in a joint venture", "investment in joint venture"),
    ),
    ConceptSpec(
        "bs.biological_assets", Statement.BALANCE_SHEET, "Biological assets",
        parent="bs.total_non_current_assets", typical_sign=POS, is_instant=True,
        identity="biological_assets",
        description="Oil palm, rubber trees. Fair value under MFRS 141.",
        synonyms=("biological assets", "bearer plants"),
    ),
    ConceptSpec(
        "bs.other_investments_nc", Statement.BALANCE_SHEET, "Other investments (non-current)",
        parent="bs.total_non_current_assets", typical_sign=POS, is_instant=True,
        identity="other_investments",
        description="Non-current financial investments.",
        synonyms=("other investments", "financial assets at fair value through profit or loss",
                  "long term investments"),
    ),
    ConceptSpec(
        "bs.deferred_tax_assets", Statement.BALANCE_SHEET, "Deferred tax assets",
        parent="bs.total_non_current_assets", typical_sign=POS, is_instant=True,
        identity="deferred_tax_assets",
        description="Future tax benefit from temporary differences.",
        synonyms=("deferred tax assets", "deferred tax asset"),
    ),
    ConceptSpec(
        "bs.total_non_current_assets", Statement.BALANCE_SHEET, "Total non-current assets",
        parent="bs.total_assets", is_subtotal=True, typical_sign=POS, is_instant=True,
        identity="total_nca",
        description="Sum of non-current assets.",
        synonyms=("total non-current assets", "non-current assets",
                  "total non current assets", "jumlah aset bukan semasa"),
    ),
    # Current assets
    ConceptSpec(
        "bs.inventories", Statement.BALANCE_SHEET, "Inventories",
        parent="bs.total_current_assets", typical_sign=POS, is_instant=True,
        identity="inventories",
        description="Goods for sale or in production.",
        synonyms=("inventories", "inventory", "stocks", "inventori"),
    ),
    ConceptSpec(
        "bs.trade_receivables", Statement.BALANCE_SHEET, "Trade receivables",
        parent="bs.total_current_assets", typical_sign=POS, is_instant=True,
        identity="trade_receivables",
        description="Customer amounts owed from sales.",
        synonyms=("trade receivables", "trade and other receivables", "receivables",
                  "trade debtors", "penghutang perdagangan"),
    ),
    ConceptSpec(
        "bs.other_receivables", Statement.BALANCE_SHEET, "Other receivables",
        parent="bs.total_current_assets", typical_sign=POS, is_instant=True,
        identity="other_receivables",
        description="Non-trade receivables, deposits, prepayments.",
        synonyms=("other receivables", "other receivables, deposits and prepayments",
                  "prepayments"),
    ),
    ConceptSpec(
        "bs.contract_assets", Statement.BALANCE_SHEET, "Contract assets",
        parent="bs.total_current_assets", typical_sign=POS, is_instant=True,
        identity="contract_assets",
        description="Revenue recognised but not yet billed (MFRS 15).",
        synonyms=("contract assets", "amount due from customers on contracts"),
    ),
    ConceptSpec(
        "bs.due_from_related", Statement.BALANCE_SHEET, "Amounts due from related parties",
        parent="bs.total_current_assets", typical_sign=POS, is_instant=True,
        identity="due_from_related",
        description="Intercompany receivables.",
        synonyms=("amount due from related parties", "amounts due from related companies",
                  "amount due from subsidiaries", "amount due from holding company"),
    ),
    ConceptSpec(
        "bs.tax_recoverable", Statement.BALANCE_SHEET, "Tax recoverable",
        parent="bs.total_current_assets", typical_sign=POS, is_instant=True,
        identity="tax_recoverable",
        description="Tax overpaid, refundable.",
        synonyms=("tax recoverable", "current tax assets", "tax refundable"),
    ),
    ConceptSpec(
        "bs.short_term_investments", Statement.BALANCE_SHEET, "Short-term investments",
        parent="bs.total_current_assets", typical_sign=POS, is_instant=True,
        identity="short_term_investments",
        description="Current financial investments.",
        synonyms=("short term investments", "short-term investments",
                  "other investments - current"),
    ),
    ConceptSpec(
        "bs.cash_and_equivalents", Statement.BALANCE_SHEET, "Cash and cash equivalents",
        parent="bs.total_current_assets", typical_sign=POS, is_instant=True,
        identity="cash",
        description="Cash + demand deposits + liquid investments under 3 months.",
        synonyms=("cash and cash equivalents", "cash and bank balances",
                  "deposits, cash and bank balances", "tunai dan baki bank",
                  "fixed deposits, cash and bank balances",
                  "cash and short-term funds", "cash and balances with banks",
                  "cash and short term funds"),
    ),
    ConceptSpec(
        "bs.assets_held_for_sale", Statement.BALANCE_SHEET,
        "Assets classified as held for sale",
        parent="bs.total_current_assets", typical_sign=POS, is_instant=True,
        identity="assets_held_for_sale",
        description="Non-current assets being disposed.",
        synonyms=("assets held for sale", "assets classified as held for sale",
                  "non-current assets held for sale"),
    ),
    ConceptSpec(
        "bs.total_current_assets", Statement.BALANCE_SHEET, "Total current assets",
        parent="bs.total_assets", is_subtotal=True, typical_sign=POS, is_instant=True,
        identity="total_ca",
        description="Sum of current assets.",
        synonyms=("total current assets", "current assets", "jumlah aset semasa"),
    ),
    ConceptSpec(
        "bs.total_assets", Statement.BALANCE_SHEET, "Total assets",
        is_subtotal=True, typical_sign=POS, is_instant=True,
        identity="total_assets",
        description="Total resources. yfinance: Total Assets. Equals total_equity_and_liabilities.",
        synonyms=("total assets", "jumlah aset"),
    ),
    # Equity
    ConceptSpec(
        "bs.share_capital", Statement.BALANCE_SHEET, "Share capital",
        parent="bs.equity_owners", typical_sign=POS, is_instant=True,
        # A REIT's own equity is "unitholders' capital" / "unit capital" -
        # confirmed real on IGB REIT - the same role share capital plays.
        identity="share_capital",
        description="Paid-up capital. REITs: unitholders capital.",
        synonyms=("share capital", "ordinary share capital", "modal saham",
                  "unitholders' capital", "unitholders capital", "unit capital"),
    ),
    ConceptSpec(
        "bs.treasury_shares", Statement.BALANCE_SHEET, "Treasury shares",
        parent="bs.equity_owners", typical_sign=NEG, is_instant=True,
        identity="treasury_shares",
        description="Repurchased shares. Reduces equity.",
        synonyms=("treasury shares", "treasury shares, at cost"),
    ),
    ConceptSpec(
        "bs.reserves", Statement.BALANCE_SHEET, "Reserves",
        parent="bs.equity_owners", is_instant=True,
        identity="reserves",
        description="AOCI, revaluation surplus, etc.",
        synonyms=("reserves", "other reserves", "capital reserves",
                  "foreign exchange translation reserve", "revaluation reserve"),
    ),
    ConceptSpec(
        "bs.retained_earnings", Statement.BALANCE_SHEET, "Retained earnings",
        parent="bs.equity_owners", is_instant=True,
        # "Accumulated income" - a REIT's own wording, confirmed real on
        # Pavilion REIT.
        identity="retained_earnings",
        description="Cumulative undistributed profits. REITs: accumulated income.",
        synonyms=("retained earnings", "retained profits", "accumulated losses",
                  "accumulated profits", "keuntungan tertahan", "accumulated income"),
    ),
    ConceptSpec(
        "bs.equity_owners", Statement.BALANCE_SHEET,
        "Equity attributable to owners of the parent",
        parent="bs.total_equity", is_subtotal=True, is_instant=True,
        identity="equity_owners",
        description="Parent shareholders equity. NAV numerator. yfinance: Stockholders Equity.",
        synonyms=("equity attributable to owners of the parent",
                  "equity attributable to owners of the company",
                  "total equity attributable to owners of the parent",
                  "shareholders' equity", "shareholders funds",
                  "equity attributable to equity holders of the parent"),
    ),
    ConceptSpec(
        "bs.nci", Statement.BALANCE_SHEET, "Non-controlling interests",
        parent="bs.total_equity", is_instant=True,
        identity="nci",
        description="Non-controlling interests share of subsidiary equity.",
        synonyms=("non-controlling interests", "non-controlling interest",
                  "minority interests", "minority interest"),
    ),
    ConceptSpec(
        "bs.total_equity", Statement.BALANCE_SHEET, "Total equity",
        is_subtotal=True, is_instant=True,
        identity="total_equity",
        description="Shareholders funds + NCI. Derivable: assets - liabilities.",
        # A REIT calls this its "unitholders' fund(s)" - confirmed real on
        # IGB REIT.
        synonyms=("total equity", "total equity and reserves", "jumlah ekuiti",
                  "total unitholders' fund", "total unitholders' funds",
                  "unitholders' fund", "unitholders' funds"),
    ),
    # Non-current liabilities
    ConceptSpec(
        "bs.lt_borrowings", Statement.BALANCE_SHEET, "Long-term borrowings",
        parent="bs.total_non_current_liabilities", typical_sign=POS, is_instant=True,
        identity="lt_borrowings",
        description="Long-term debt due over 12 months.",
        synonyms=("long term borrowings", "long-term borrowings",
                  "borrowings - non-current", "non-current borrowings",
                  "term loans - non-current"),
    ),
    ConceptSpec(
        "bs.lt_lease_liabilities", Statement.BALANCE_SHEET,
        "Lease liabilities (non-current)",
        parent="bs.total_non_current_liabilities", typical_sign=POS, is_instant=True,
        identity="lt_lease_liabilities",
        description="Lease obligations due over 12 months.",
        synonyms=("lease liabilities - non-current", "non-current lease liabilities"),
    ),
    ConceptSpec(
        "bs.deferred_tax_liabilities", Statement.BALANCE_SHEET, "Deferred tax liabilities",
        parent="bs.total_non_current_liabilities", typical_sign=POS, is_instant=True,
        identity="deferred_tax_liabilities",
        description="Future tax from temporary differences.",
        synonyms=("deferred tax liabilities", "deferred taxation", "deferred tax liability"),
    ),
    ConceptSpec(
        "bs.lt_provisions", Statement.BALANCE_SHEET, "Provisions (non-current)",
        parent="bs.total_non_current_liabilities", typical_sign=POS, is_instant=True,
        identity="lt_provisions",
        description="Long-term provisions (retirement, restoration).",
        synonyms=("provisions - non-current", "retirement benefit obligations",
                  "employee benefits"),
    ),
    ConceptSpec(
        "bs.total_non_current_liabilities", Statement.BALANCE_SHEET,
        "Total non-current liabilities",
        parent="bs.total_liabilities", is_subtotal=True, typical_sign=POS, is_instant=True,
        identity="total_ncl",
        description="Sum of non-current liabilities.",
        synonyms=("total non-current liabilities", "non-current liabilities",
                  "total non current liabilities"),
    ),
    # Current liabilities
    ConceptSpec(
        "bs.trade_payables", Statement.BALANCE_SHEET, "Trade payables",
        parent="bs.total_current_liabilities", typical_sign=POS, is_instant=True,
        identity="trade_payables",
        description="Amounts owed to suppliers.",
        synonyms=("trade payables", "trade and other payables", "payables",
                  "trade creditors", "pemiutang perdagangan"),
    ),
    ConceptSpec(
        "bs.other_payables", Statement.BALANCE_SHEET, "Other payables",
        parent="bs.total_current_liabilities", typical_sign=POS, is_instant=True,
        identity="other_payables",
        description="Non-trade obligations, accruals.",
        synonyms=("other payables", "other payables and accruals", "accruals"),
    ),
    ConceptSpec(
        "bs.contract_liabilities", Statement.BALANCE_SHEET, "Contract liabilities",
        parent="bs.total_current_liabilities", typical_sign=POS, is_instant=True,
        identity="contract_liabilities",
        description="Cash received, revenue not yet recognised. Deferred revenue.",
        synonyms=("contract liabilities", "amount due to customers on contracts",
                  "deferred revenue"),
    ),
    ConceptSpec(
        "bs.st_borrowings", Statement.BALANCE_SHEET, "Short-term borrowings",
        parent="bs.total_current_liabilities", typical_sign=POS, is_instant=True,
        identity="st_borrowings",
        description="Short-term debt and current portion of long-term.",
        synonyms=("short term borrowings", "short-term borrowings",
                  "borrowings - current", "current borrowings", "bank overdrafts",
                  "term loans - current"),
    ),
    ConceptSpec(
        "bs.st_lease_liabilities", Statement.BALANCE_SHEET, "Lease liabilities (current)",
        parent="bs.total_current_liabilities", typical_sign=POS, is_instant=True,
        identity="st_lease_liabilities",
        description="Current lease obligations.",
        synonyms=("lease liabilities - current", "current lease liabilities"),
    ),
    ConceptSpec(
        "bs.due_to_related", Statement.BALANCE_SHEET, "Amounts due to related parties",
        parent="bs.total_current_liabilities", typical_sign=POS, is_instant=True,
        identity="due_to_related",
        description="Intercompany payables.",
        synonyms=("amount due to related parties", "amounts due to related companies",
                  "amount due to directors", "amount due to holding company"),
    ),
    ConceptSpec(
        "bs.tax_payable", Statement.BALANCE_SHEET, "Tax payable",
        parent="bs.total_current_liabilities", typical_sign=POS, is_instant=True,
        identity="tax_payable",
        description="Income tax owing.",
        synonyms=("tax payable", "current tax liabilities", "provision for taxation",
                  "income tax payable"),
    ),
    ConceptSpec(
        "bs.total_current_liabilities", Statement.BALANCE_SHEET, "Total current liabilities",
        parent="bs.total_liabilities", is_subtotal=True, typical_sign=POS, is_instant=True,
        identity="total_cl",
        description="Sum of current liabilities.",
        synonyms=("total current liabilities", "current liabilities",
                  "jumlah liabiliti semasa"),
    ),
    ConceptSpec(
        "bs.total_liabilities", Statement.BALANCE_SHEET, "Total liabilities",
        is_subtotal=True, typical_sign=POS, is_instant=True,
        identity="total_liabilities",
        description="Sum of all obligations.",
        synonyms=("total liabilities", "jumlah liabiliti"),
    ),
    ConceptSpec(
        "bs.total_equity_and_liabilities", Statement.BALANCE_SHEET,
        "Total equity and liabilities", is_subtotal=True, typical_sign=POS, is_instant=True,
        identity="total_equity_and_liabilities",
        description="Must equal total_assets (accounting identity).",
        synonyms=("total equity and liabilities", "total liabilities and equity",
                  "jumlah ekuiti dan liabiliti",
                  "total unitholders' fund and liabilities"),
    ),
    ConceptSpec(
        "bs.nta_per_share", Statement.BALANCE_SHEET,
        "Net assets per share attributable to owners (RM)",
        is_per_share=True, is_instant=True,
        description="Bursa Appendix 9B requires this on the face of quarterly reports.",
        identity="nta_per_share",
        synonyms=("net assets per share", "net assets per share attributable to owners",
                  "net assets per ordinary share", "nta per share",
                  "net tangible assets per share", "net asset value per unit"),
    ),
    # ----------------------------------------------------------------------
    # Bank-specific balance sheet. Same source as the income-statement bank
    # additions above - a bank's balance sheet is built around deposit-
    # taking and lending, not inventory/receivables/payables.
    # ----------------------------------------------------------------------
    ConceptSpec(
        "bs.loans_advances_financing", Statement.BALANCE_SHEET,
        "Loans, advances and financing",
        parent="bs.total_assets", typical_sign=POS, is_instant=True,
        description="A bank's core asset - its loan book - not split "
        "current/non-current the way a general corporate's receivables are.",
        identity="loan_book",
        synonyms=("loans, advances and financing", "loans and advances"),
    ),
    ConceptSpec(
        "bs.statutory_deposits", Statement.BALANCE_SHEET,
        "Statutory deposits with central bank",
        parent="bs.total_non_current_assets", typical_sign=POS, is_instant=True,
        identity="statutory_deposits",
        description="Mandatory deposits with BNM.",
        synonyms=("statutory deposits", "statutory deposits with bank negara malaysia",
                  "statutory deposits with central bank"),
    ),
    ConceptSpec(
        "bs.derivative_assets", Statement.BALANCE_SHEET, "Derivative financial assets",
        typical_sign=POS, is_instant=True,
        description="Not purely bank-specific - any issuer that hedges "
        "carries this - but found via the bank filings read this round.",
        identity="derivative_assets",
        synonyms=("derivative financial assets", "derivative assets"),
    ),
    ConceptSpec(
        "bs.derivative_liabilities", Statement.BALANCE_SHEET, "Derivative financial liabilities",
        typical_sign=POS, is_instant=True,
        identity="derivative_liabilities",
        description="Fair value of derivative contracts (liability).",
        synonyms=("derivative financial liabilities", "derivative liabilities"),
    ),
    ConceptSpec(
        "bs.financial_investments_amortised_cost", Statement.BALANCE_SHEET,
        "Financial investments at amortised cost",
        typical_sign=POS, is_instant=True,
        description="An MFRS 9 classification category, mostly seen on "
        "banks' and insurers' own balance sheets.",
        identity="financial_investments_ac",
        synonyms=("financial investments at amortised cost",),
    ),
    ConceptSpec(
        "bs.customer_deposits", Statement.BALANCE_SHEET, "Deposits from customers",
        parent="bs.total_liabilities", typical_sign=POS, is_instant=True,
        description="A bank's core liability - not split current/non-current "
        "the way a general corporate's payables are.",
        identity="customer_deposits",
        synonyms=("deposits from customers",),
    ),
    ConceptSpec(
        "bs.subordinated_obligations", Statement.BALANCE_SHEET, "Subordinated obligations",
        parent="bs.total_non_current_liabilities", typical_sign=POS, is_instant=True,
        identity="subordinated_debt",
        description="Junior debt qualifying as regulatory capital.",
        synonyms=("subordinated obligations", "subordinated debt", "subordinated notes"),
    ),
    ConceptSpec(
        "bs.repo_obligations", Statement.BALANCE_SHEET,
        "Obligations on securities sold under repurchase agreements",
        parent="bs.total_current_liabilities", typical_sign=POS, is_instant=True,
        identity="repo_obligations",
        description="Securities sold under repurchase agreements.",
        synonyms=("obligations on securities sold under repurchase agreements",
                  "securities sold under repurchase agreements"),
    ),
    ConceptSpec(
        "bs.bills_acceptances_payable", Statement.BALANCE_SHEET, "Bills and acceptances payable",
        parent="bs.total_current_liabilities", typical_sign=POS, is_instant=True,
        identity="bills_payable",
        description="Trade finance instruments.",
        synonyms=("bills and acceptances payable",),
    ),
    ConceptSpec(
        "bs.provision_zakat", Statement.BALANCE_SHEET, "Provision for zakat",
        parent="bs.total_current_liabilities", typical_sign=POS, is_instant=True,
        identity="zakat",
        description="Islamic religious levy provision.",
        synonyms=("provision for zakat", "zakat payable"),
    ),
    # ----------------------------------------------------------------------
    # REIT-specific balance sheet.
    # ----------------------------------------------------------------------
    ConceptSpec(
        "bs.pledged_deposits", Statement.BALANCE_SHEET, "Pledged deposits",
        parent="bs.total_current_assets", typical_sign=POS, is_instant=True,
        description="Restricted cash held as security (a tenant deposit, a "
        "loan covenant) - not freely available the way "
        "cash_and_equivalents is, so kept as its own concept rather than "
        "folded in there.",
        identity="pledged_deposits",
        synonyms=("pledged deposits", "pledged deposits with banks",
                  "pledged deposits with licensed banks", "pledged deposit"),
    ),
    ConceptSpec(
        "bs.units_in_circulation", Statement.BALANCE_SHEET,
        "Number of units in circulation ('000 units)",
        is_instant=True,
        description="A REIT's own equivalent of shares in issue - lives on "
        "the balance sheet face, not the income statement.",
        identity="units_in_circulation",
        synonyms=("number of units in circulation", "units in circulation"),
    ),
    # ----------------------------------------------------------------------
    # General (not industry-specific) - found via the same real filings,
    # not previously covered for any issuer: an unsplit "Borrowings" line
    # (common on a simplified balance sheet that doesn't separate
    # current/non-current on the face), and the generic miscellaneous
    # catch-alls neither bs.other_receivables nor bs.other_payables are
    # really the same thing as.
    # ----------------------------------------------------------------------
    ConceptSpec(
        "bs.borrowings_total", Statement.BALANCE_SHEET, "Borrowings",
        parent="bs.total_liabilities", typical_sign=POS, is_instant=True,
        identity="borrowings_total",
        description="Unsplit borrowings (current+non-current).",
        synonyms=("borrowings",),
    ),
    ConceptSpec(
        "bs.other_assets", Statement.BALANCE_SHEET, "Other assets",
        parent="bs.total_assets", typical_sign=POS, is_instant=True,
        identity="other_assets",
        description="Miscellaneous assets.",
        synonyms=("other assets",),
    ),
    ConceptSpec(
        "bs.other_liabilities", Statement.BALANCE_SHEET, "Other liabilities",
        parent="bs.total_liabilities", typical_sign=POS, is_instant=True,
        identity="other_liabilities",
        description="Miscellaneous liabilities.",
        synonyms=("other liabilities",),
    ),
]

# --------------------------------------------------------------------------
# Statement of cash flows
# --------------------------------------------------------------------------

CASH_FLOW: list[ConceptSpec] = [
    ConceptSpec(
        "cf.profit_before_tax", Statement.CASH_FLOW, "Profit before tax (cash flow)",
        # "Income before taxation" - a REIT's own wording, confirmed real on
        # Pavilion REIT, the same reconciliation-starting-point role.
        identity="cf_pbt",
        description="Cash flow starting point. Same as is.profit_before_tax.",
        synonyms=("profit before tax", "profit before taxation",
                  "profit/(loss) before taxation",
                  "profit before zakat and taxation", "profit before zakat and tax",
                  "income before taxation", "income before tax"),
    ),
    ConceptSpec(
        "cf.operating_before_wc", Statement.CASH_FLOW,
        "Operating profit before working capital changes", is_subtotal=True,
        identity="cf_operating_before_wc",
        description="Operating CF before working capital changes.",
        synonyms=("operating profit before working capital changes",
                  "operating cash flow before working capital changes",
                  "operating profit before changes in working capital",
                  "operating income before changes in working capital"),
    ),
    ConceptSpec(
        "cf.working_capital_changes", Statement.CASH_FLOW, "Changes in working capital",
        identity="cf_wc_changes",
        description="Net working capital movement.",
        synonyms=("changes in working capital", "net changes in working capital",
                  "working capital changes"),
    ),
    ConceptSpec(
        "cf.changes_in_inventories", Statement.CASH_FLOW, "Changes in inventories",
        parent="cf.working_capital_changes",
        description="A finer-grained breakdown line some issuers show "
        "instead of (or alongside) one combined working-capital-changes "
        "figure - not industry-specific, found via a REIT filing but "
        "equally real for any indirect-method cash flow statement.",
        identity="cf_inventories",
        synonyms=("changes in inventories", "(increase)/decrease in inventories"),
    ),
    ConceptSpec(
        "cf.changes_in_receivables", Statement.CASH_FLOW, "Changes in receivables",
        parent="cf.working_capital_changes",
        identity="cf_receivables",
        description="Receivables movement.",
        synonyms=("changes in receivables", "(increase)/decrease in receivables"),
    ),
    ConceptSpec(
        "cf.changes_in_payables", Statement.CASH_FLOW, "Changes in payables",
        parent="cf.working_capital_changes",
        identity="cf_payables",
        description="Payables movement.",
        synonyms=("changes in payables", "increase/(decrease) in payables"),
    ),
    ConceptSpec(
        "cf.cash_from_operations", Statement.CASH_FLOW, "Cash generated from operations",
        is_subtotal=True,
        identity="cf_cash_from_ops",
        description="Cash from core operations.",
        synonyms=("cash generated from operations", "cash generated from/(used in) operations",
                  "net cash generated from operations"),
    ),
    ConceptSpec(
        "cf.interest_paid", Statement.CASH_FLOW, "Interest paid", typical_sign=NEG,
        identity="cf_interest_paid",
        description="Cash outflow for interest.",
        synonyms=("interest paid", "finance costs paid"),
    ),
    ConceptSpec(
        "cf.interest_received", Statement.CASH_FLOW, "Interest received", typical_sign=POS,
        identity="cf_interest_received",
        description="Cash inflow from interest.",
        synonyms=("interest received", "interest income received"),
    ),
    ConceptSpec(
        "cf.tax_paid", Statement.CASH_FLOW, "Income tax paid", typical_sign=NEG,
        identity="cf_tax_paid",
        description="Cash outflow for tax.",
        synonyms=("income tax paid", "tax paid", "taxes paid", "income taxes paid"),
    ),
    ConceptSpec(
        "cf.net_operating", Statement.CASH_FLOW,
        "Net cash from operating activities", is_subtotal=True,
        identity="cf_net_operating",
        description="Net operating cash flow.",
        synonyms=("net cash from operating activities",
                  "net cash generated from operating activities",
                  "net cash used in operating activities",
                  "net cash from/(used in) operating activities",
                  "cash flows from operating activities"),
    ),
    ConceptSpec(
        "cf.purchase_of_ppe", Statement.CASH_FLOW,
        "Purchase of property, plant and equipment",
        parent="cf.net_investing", typical_sign=NEG,
        description="Capex. The denominator of free cash flow.",
        identity="cf_capex",
        synonyms=("purchase of property, plant and equipment",
                  "acquisition of property, plant and equipment",
                  "purchase of plant and equipment", "additions to property, plant and equipment",
                  "capital expenditure"),
    ),
    ConceptSpec(
        "cf.proceeds_disposal_ppe", Statement.CASH_FLOW,
        "Proceeds from disposal of property, plant and equipment",
        parent="cf.net_investing", typical_sign=POS,
        identity="cf_disposal_proceeds",
        description="Cash from asset disposals.",
        synonyms=("proceeds from disposal of property, plant and equipment",
                  "proceeds from disposal of plant and equipment",
                  "proceeds from disposal plant and equipment",
                  "proceed from disposal of plant and equipment"),
    ),
    ConceptSpec(
        "cf.payment_for_investment_properties", Statement.CASH_FLOW,
        "Payment for investment properties",
        parent="cf.net_investing", typical_sign=NEG,
        description="A REIT's own capex-equivalent line - confirmed real on "
        "Pavilion REIT, playing purchase_of_ppe's role for a portfolio of "
        "investment properties rather than plant and equipment.",
        identity="cf_investment_property",
        synonyms=("payment for enhancement of investment properties",
                  "acquisition of investment property", "payment for investment properties"),
    ),
    ConceptSpec(
        "cf.net_investing", Statement.CASH_FLOW,
        "Net cash from investing activities", is_subtotal=True,
        identity="cf_net_investing",
        description="Net investing cash flow.",
        synonyms=("net cash from investing activities",
                  "net cash used in investing activities",
                  "net cash from/(used in) investing activities",
                  "net cash generated from investing activities",
                  "cash flows from investing activities"),
    ),
    ConceptSpec(
        "cf.dividends_paid", Statement.CASH_FLOW, "Dividends paid",
        parent="cf.net_financing", typical_sign=NEG,
        # A REIT pays "distributions" to unitholders, not "dividends" to
        # shareholders - confirmed real on Pavilion REIT, the same cash
        # outflow role.
        identity="cf_dividends_paid",
        description="Dividend cash outflow.",
        synonyms=("dividends paid", "dividend paid", "dividends paid to shareholders",
                  "distribution to unitholders", "distributions paid to unitholders"),
    ),
    ConceptSpec(
        "cf.drawdown_borrowings", Statement.CASH_FLOW, "Drawdown of borrowings",
        parent="cf.net_financing", typical_sign=POS,
        identity="cf_drawdown",
        description="Cash inflow from new borrowings.",
        synonyms=("drawdown of borrowings", "proceeds from borrowings",
                  "drawdown of term loans", "proceeds from bank borrowings",
                  "proceed from borrowings"),
    ),
    ConceptSpec(
        "cf.repayment_borrowings", Statement.CASH_FLOW, "Repayment of borrowings",
        parent="cf.net_financing", typical_sign=NEG,
        identity="cf_repayment",
        description="Cash outflow for debt repayment.",
        synonyms=("repayment of borrowings", "repayment of term loans",
                  "repayment of bank borrowings"),
    ),
    ConceptSpec(
        "cf.lease_payments", Statement.CASH_FLOW, "Payment of lease liabilities",
        parent="cf.net_financing", typical_sign=NEG,
        identity="cf_lease_payments",
        description="MFRS 16 lease payments.",
        synonyms=("payment of lease liabilities", "repayment of lease liabilities",
                  "payment of principal portion of lease liabilities"),
    ),
    ConceptSpec(
        "cf.net_financing", Statement.CASH_FLOW,
        "Net cash from financing activities", is_subtotal=True,
        identity="cf_net_financing",
        description="Net financing cash flow.",
        synonyms=("net cash from financing activities",
                  "net cash used in financing activities",
                  "net cash from/(used in) financing activities",
                  "net cash generated from financing activities",
                  "cash flows from financing activities"),
    ),
    ConceptSpec(
        "cf.net_change_in_cash", Statement.CASH_FLOW,
        "Net change in cash and cash equivalents", is_subtotal=True,
        identity="cf_net_change",
        description="Net cash increase/decrease.",
        synonyms=("net increase in cash and cash equivalents",
                  "net decrease in cash and cash equivalents",
                  "net increase/(decrease) in cash and cash equivalents",
                  "net change in cash and cash equivalents"),
    ),
    ConceptSpec(
        "cf.forex_effect", Statement.CASH_FLOW, "Effect of exchange rate changes",
        identity="cf_forex",
        description="Exchange rate effect on cash.",
        synonyms=("effect of exchange rate changes",
                  "effects of exchange rate changes on cash and cash equivalents",
                  "currency translation differences"),
    ),
    ConceptSpec(
        "cf.cash_beginning", Statement.CASH_FLOW,
        "Cash and cash equivalents at beginning of period", is_instant=True,
        identity="cf_cash_opening",
        description="Opening cash balance.",
        synonyms=("cash and cash equivalents at beginning of the period",
                  "cash and cash equivalents at beginning of year",
                  "cash and cash equivalents at beginning of the year",
                  "cash and cash equivalents at the beginning of the year",
                  "cash and cash equivalents at beginning of the financial year",
                  "cash and cash equivalents at 1 january",
                  "cash and cash equivalents at the beginning of the financial period"),
    ),
    ConceptSpec(
        "cf.cash_end", Statement.CASH_FLOW,
        "Cash and cash equivalents at end of period", is_instant=True,
        identity="cf_cash_closing",
        description="Closing cash balance. Reconciles to BS cash.",
        synonyms=("cash and cash equivalents at end of the period",
                  "cash and cash equivalents at end of year",
                  "cash and cash equivalents at end of the year",
                  "cash and cash equivalents at the end of the year",
                  "cash and cash equivalents at end of the financial year",
                  "cash and cash equivalents at 31 december",
                  "cash and cash equivalents at the end of the financial period"),
    ),
]


# --------------------------------------------------------------------------
# Statement of changes in equity
# --------------------------------------------------------------------------

EQUITY: list[ConceptSpec] = [
    ConceptSpec(
        "eq.opening_balance", Statement.EQUITY, "Balance at beginning of year",
        is_subtotal=True, is_instant=True,
        identity="eq_opening",
        description="Opening equity balance.",
        synonyms=("balance at beginning of year", "balance at beginning of the year",
                  "balance at beginning of the financial year",
                  "at beginning of year", "at 1 january", "opening balance",
                  "balance as at beginning of year", "as previously reported"),
    ),
    ConceptSpec(
        "eq.closing_balance", Statement.EQUITY, "Balance at end of year",
        is_subtotal=True, is_instant=True,
        identity="eq_closing",
        description="Closing equity balance.",
        synonyms=("balance at end of year", "balance at end of the year",
                  "balance at end of the financial year",
                  "at end of year", "at 31 december", "closing balance",
                  "balance as at end of year"),
    ),
    ConceptSpec(
        "eq.profit_for_year", Statement.EQUITY, "Profit for the year",
        identity="eq_profit",
        description="Net profit recognised in equity.",
        synonyms=("profit for the year", "profit for the financial year",
                  "net profit for the year", "profit/(loss) for the year"),
    ),
    ConceptSpec(
        "eq.other_comprehensive_income", Statement.EQUITY,
        "Other comprehensive income", identity="eq_oci",
        description="OCI recognised in equity.",
        synonyms=("other comprehensive income", "other comprehensive income/(loss)",
                  "other comprehensive income for the year"),
    ),
    ConceptSpec(
        "eq.total_comprehensive_income", Statement.EQUITY,
        "Total comprehensive income", is_subtotal=True,
        identity="eq_tci",
        description="Profit + OCI recognised in equity.",
        synonyms=("total comprehensive income", "total comprehensive income for the year",
                  "total comprehensive income/(loss)"),
    ),
    ConceptSpec(
        "eq.dividends", Statement.EQUITY, "Dividends",
        typical_sign=NEG, identity="eq_dividends",
        description="Dividends declared or paid.",
        synonyms=("dividends", "dividends paid", "dividends declared",
                  "distribution to unitholders", "distributions to unitholders"),
    ),
    ConceptSpec(
        "eq.issuance_of_shares", Statement.EQUITY, "Issuance of shares",
        typical_sign=POS, identity="eq_issuance",
        description="New shares issued.",
        synonyms=("issuance of shares", "issuance of ordinary shares",
                  "shares issued", "issuance of new units"),
    ),
    ConceptSpec(
        "eq.share_buyback", Statement.EQUITY, "Share buyback",
        typical_sign=NEG, identity="eq_buyback",
        description="Treasury share purchases.",
        synonyms=("share buyback", "purchase of treasury shares",
                  "shares bought back", "acquisition of treasury shares"),
    ),
    ConceptSpec(
        "eq.transfer_to_reserves", Statement.EQUITY, "Transfer to reserves",
        identity="eq_transfer_reserves",
        description="Transfers between equity components.",
        synonyms=("transfer to reserves", "transfer to retained earnings",
                  "transfer from reserves", "transfer to statutory reserve"),
    ),
    ConceptSpec(
        "eq.changes_in_nci", Statement.EQUITY,
        "Changes in ownership interests in subsidiaries",
        identity="eq_nci_changes",
        description="Transactions with NCI without loss of control.",
        synonyms=("changes in ownership interests in subsidiaries",
                  "acquisition of non-controlling interests",
                  "disposal of interest in a subsidiary"),
    ),
    ConceptSpec(
        "eq.share_based_payments", Statement.EQUITY, "Share-based payment transactions",
        identity="eq_sbp",
        description="ESOS/RSU expense recognised in equity.",
        synonyms=("share-based payment", "share-based payment transactions",
                  "share options", "employee share scheme", "esos"),
    ),
]

ALL_CONCEPTS: list[ConceptSpec] = [*INCOME_STATEMENT, *BALANCE_SHEET, *CASH_FLOW, *EQUITY]

CONCEPTS_BY_KEY: dict[str, ConceptSpec] = {c.key: c for c in ALL_CONCEPTS}


def concepts_for(statement: Statement) -> list[ConceptSpec]:
    return [c for c in ALL_CONCEPTS if c.statement == statement]


def _validate() -> None:
    """Fail loudly at import time if the taxonomy is internally inconsistent."""
    seen: set[str] = set()
    for spec in ALL_CONCEPTS:
        if spec.key in seen:
            raise ValueError(f"duplicate concept key: {spec.key}")
        seen.add(spec.key)
    for spec in ALL_CONCEPTS:
        if spec.parent and spec.parent not in seen:
            raise ValueError(f"{spec.key} has unknown parent {spec.parent}")


_validate()
