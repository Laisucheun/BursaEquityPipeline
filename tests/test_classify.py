from __future__ import annotations

from bursa.db.enums import Statement
from bursa.extract.classify import classify_page_text

FIGURES = "\n" + " ".join(["1,234", "(5,678)", "9,012"] * 4)


def page(heading: str, body: str = FIGURES) -> str:
    return f"{heading}\n{body}"


def test_recognises_each_primary_statement() -> None:
    cases = {
        "CONDENSED CONSOLIDATED STATEMENT OF PROFIT OR LOSS": Statement.INCOME_STATEMENT,
        "INCOME STATEMENTS": Statement.INCOME_STATEMENT,
        "Statements of Comprehensive Income": Statement.INCOME_STATEMENT,
        "STATEMENTS OF FINANCIAL POSITION": Statement.BALANCE_SHEET,
        "Balance Sheets": Statement.BALANCE_SHEET,
        "CONDENSED CONSOLIDATED STATEMENTS OF CASH FLOWS": Statement.CASH_FLOW,
        "STATEMENT OF CHANGES IN EQUITY": Statement.EQUITY,
    }
    for heading, expected in cases.items():
        result = classify_page_text(1, page(heading))
        assert result.statement is expected, heading
        assert result.confidence > 0.9


def test_recognises_malay_headings() -> None:
    assert (
        classify_page_text(1, page("PENYATA KEDUDUKAN KEWANGAN")).statement
        is Statement.BALANCE_SHEET
    )
    assert classify_page_text(1, page("PENYATA ALIRAN TUNAI")).statement is Statement.CASH_FLOW


def test_contents_page_is_not_a_statement() -> None:
    text = page("TABLE OF CONTENTS\nStatements of Financial Position .... 42")
    assert classify_page_text(1, text).statement is None


def test_auditors_report_is_not_a_statement() -> None:
    text = page("INDEPENDENT AUDITORS' REPORT\nStatements of financial position")
    assert classify_page_text(1, text).statement is None


def test_heading_without_figures_scores_low() -> None:
    # A divider page announcing the statements is not the statements.
    result = classify_page_text(1, "STATEMENTS OF FINANCIAL POSITION\n\nGroup")
    assert result.statement is Statement.BALANCE_SHEET
    assert result.confidence < 0.5


def test_cross_reference_deep_in_a_note_is_ignored() -> None:
    prose = "\n".join(f"Note {i}: explanatory paragraph about the business." for i in range(30))
    text = f"{prose}\nSee the statements of cash flows for details.\n{FIGURES}"
    assert classify_page_text(1, text).statement is None


def test_blank_page_has_no_text_layer() -> None:
    result = classify_page_text(1, "")
    assert result.has_text_layer is False
    assert result.statement is None
