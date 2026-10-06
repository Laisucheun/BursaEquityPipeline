from __future__ import annotations

from sqlalchemy.orm import Session

from bursa.db.enums import Statement
from bursa.db.models import Company
from bursa.mapping.synonyms import lookup, normalize_label, record_correction, seed_concepts


def test_normalisation_folds_the_variations_that_do_not_matter() -> None:
    assert normalize_label("Revenue") == "revenue"
    assert normalize_label("  REVENUE  ") == "revenue"
    assert normalize_label("Profit/(loss) before tax") == "profit before tax"
    assert normalize_label("Profit before taxation (Note 7)") == "profit before taxation"
    assert normalize_label("- Revenue") == "revenue"
    assert normalize_label("(a) Revenue") == "revenue"
    assert normalize_label("Revenue .......") == "revenue"
    assert normalize_label("Total assets") == "total assets"


def test_normalisation_is_stable_across_typographic_noise() -> None:
    assert normalize_label("Shareholders’ equity") == normalize_label("Shareholders' equity")
    assert normalize_label("Non–controlling interests") == normalize_label(
        "Non-controlling interests"
    )


def test_normalisation_strips_a_parenthetical_alternative_in_either_slash_order() -> None:
    """"X/(Y)" and "(Y)/X" are the same accounting convention (showing the
    opposite-sign alternative in brackets) - confirmed real on a REIT's cash
    flow statement phrasing it "(used in)/from", the reverse of the already-
    handled "from/(used in)" order, which silently never matched the seeded
    synonym before this fix."""
    assert normalize_label("Net cash from/(used in) financing activities") == normalize_label(
        "Net cash (used in)/from financing activities"
    )
    assert normalize_label("Fair value gain/(loss)") == normalize_label("Fair value (loss)/gain")


def test_seed_is_idempotent(session: Session) -> None:
    first = seed_concepts(session)
    session.commit()
    second = seed_concepts(session)
    assert first[0] > 0 and first[1] > 0
    assert second == (0, 0)


def test_lookup_resolves_seeded_phrasings(session: Session) -> None:
    seed_concepts(session)
    session.commit()

    for phrasing in ("Revenue", "Turnover", "HASIL", "Revenue (Note 3)"):
        assert lookup(session, phrasing, Statement.INCOME_STATEMENT) == "is.revenue"

    assert lookup(session, "Total assets", Statement.BALANCE_SHEET) == "bs.total_assets"
    assert (
        lookup(session, "Net cash from operating activities", Statement.CASH_FLOW)
        == "cf.net_operating"
    )


def test_lookup_is_scoped_by_statement(session: Session) -> None:
    # The same words mean different concepts on different statements - this is
    # why the synonym table is keyed by statement, not label alone.
    seed_concepts(session)
    session.commit()

    assert (
        lookup(session, "Profit before tax", Statement.INCOME_STATEMENT)
        == "is.profit_before_tax"
    )
    assert lookup(session, "Profit before tax", Statement.CASH_FLOW) == "cf.profit_before_tax"
    assert lookup(session, "Non-controlling interests", Statement.BALANCE_SHEET) == "bs.nci"
    assert lookup(session, "Non-controlling interests", Statement.INCOME_STATEMENT) == "is.pat_nci"


def test_comprehensive_income_resolves_with_or_without_the_total_prefix(session: Session) -> None:
    # Some issuers drop "Total" entirely ("Comprehensive income for the
    # financial year") - confirmed real on Vitrox Corporation's filing,
    # where the previously-missing bare phrasing left this row unmapped and
    # broke the section-aware owners/NCI split in statement_extract.py.
    seed_concepts(session)
    session.commit()

    for phrasing in (
        "Total comprehensive income",
        "Total comprehensive income for the year",
        "Comprehensive income for the financial year",
        "Comprehensive income",
    ):
        assert (
            lookup(session, phrasing, Statement.INCOME_STATEMENT) == "is.total_comprehensive_income"
        )


def test_bank_specific_vocabulary_resolves(session: Session) -> None:
    """Confirmed real on RHB Bank/Alliance Bank/AFFIN Bank's own filings - a
    conventional bank's statements are built around different vocabulary
    from the start, not just a few missing general-corporate synonyms."""
    seed_concepts(session)
    session.commit()

    assert lookup(session, "Net interest income", Statement.INCOME_STATEMENT) == "is.net_interest_income"
    assert lookup(session, "Fee and commission income", Statement.INCOME_STATEMENT) == "is.fee_commission_income"
    assert (
        lookup(session, "Operating profit before allowances", Statement.INCOME_STATEMENT)
        == "is.operating_profit_before_allowances"
    )
    assert (
        lookup(session, "Profit before taxation and zakat", Statement.INCOME_STATEMENT)
        == "is.profit_before_tax"
    )
    assert lookup(session, "Taxation and zakat", Statement.INCOME_STATEMENT) == "is.tax_expense"
    assert lookup(session, "Deposits from customers", Statement.BALANCE_SHEET) == "bs.customer_deposits"
    assert (
        lookup(session, "Loans, advances and financing", Statement.BALANCE_SHEET)
        == "bs.loans_advances_financing"
    )
    assert lookup(session, "Subordinated obligations", Statement.BALANCE_SHEET) == "bs.subordinated_obligations"
    assert lookup(session, "Property and equipment", Statement.BALANCE_SHEET) == "bs.ppe"


def test_reit_specific_vocabulary_resolves(session: Session) -> None:
    """Confirmed real on IGB REIT/Pavilion REIT's own filings - "Revenue" is
    a synonym fix (rental income plays the same role), but a REIT's
    statements also have several lines with no general-corporate
    equivalent at all."""
    seed_concepts(session)
    session.commit()

    assert lookup(session, "Rental income", Statement.INCOME_STATEMENT) == "is.revenue"
    assert lookup(session, "Lease revenue", Statement.INCOME_STATEMENT) == "is.revenue"
    assert lookup(session, "Net property income", Statement.INCOME_STATEMENT) == "is.net_property_income"
    assert lookup(session, "Distribution per unit", Statement.INCOME_STATEMENT) == "is.dpu"
    assert lookup(session, "Basic earnings per unit", Statement.INCOME_STATEMENT) == "is.eps_basic"
    assert lookup(session, "Unitholders' capital", Statement.BALANCE_SHEET) == "bs.share_capital"
    assert lookup(session, "Total unitholders' fund", Statement.BALANCE_SHEET) == "bs.total_equity"
    assert lookup(session, "Accumulated income", Statement.BALANCE_SHEET) == "bs.retained_earnings"
    assert (
        lookup(session, "Payment for enhancement of investment properties", Statement.CASH_FLOW)
        == "cf.payment_for_investment_properties"
    )
    assert lookup(session, "Distribution to unitholders", Statement.CASH_FLOW) == "cf.dividends_paid"
    assert lookup(session, "Income before taxation", Statement.CASH_FLOW) == "cf.profit_before_tax"


def test_unknown_label_falls_through_to_the_llm(session: Session) -> None:
    seed_concepts(session)
    session.commit()
    unknown = "Gain on bargain purchase of a subsidiary"
    assert lookup(session, unknown, Statement.INCOME_STATEMENT) is None


def test_reviewer_correction_becomes_a_reusable_synonym(session: Session) -> None:
    seed_concepts(session)
    company = Company(stock_code="5285", name="Test Bhd")
    session.add(company)
    session.flush()

    label = "Segment revenue - external customers"
    assert lookup(session, label, Statement.INCOME_STATEMENT, company.id) is None

    record_correction(session, label, Statement.INCOME_STATEMENT, "is.revenue", company.id)
    session.commit()

    # Resolved for this issuer from now on...
    assert lookup(session, label, Statement.INCOME_STATEMENT, company.id) == "is.revenue"
    # ...but not silently applied to every other issuer.
    assert lookup(session, label, Statement.INCOME_STATEMENT, company_id=None) is None


def test_company_synonym_overrides_the_global_one(session: Session) -> None:
    seed_concepts(session)
    company = Company(stock_code="1023", name="Odd Bhd")
    session.add(company)
    session.flush()

    record_correction(
        session, "Turnover", Statement.INCOME_STATEMENT, "is.other_income", company.id
    )
    session.commit()

    assert lookup(session, "Turnover", Statement.INCOME_STATEMENT, company.id) == "is.other_income"
    assert lookup(session, "Turnover", Statement.INCOME_STATEMENT) == "is.revenue"
