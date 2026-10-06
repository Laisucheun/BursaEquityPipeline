from __future__ import annotations

from decimal import Decimal

import pytest

from bursa.normalize.numbers import looks_numeric, parse_number, to_base_units


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        ("1,234", Decimal("1234")),
        ("1,234,567", Decimal("1234567")),
        ("1,234.56", Decimal("1234.56")),
        ("0", Decimal("0")),
        ("0.00", Decimal("0.00")),
        # Accounting negatives, in every form seen in the wild.
        ("(1,234)", Decimal("-1234")),
        ("(1,234.50)", Decimal("-1234.50")),
        ("1,234-", Decimal("-1234")),
        ("-1,234", Decimal("-1234")),
        # OCR routinely drops one bracket of a pair.
        ("(1,234", Decimal("-1234")),
        ("1,234)", Decimal("-1234")),
        # Currency and footnote marks ride along with the figure.
        ("RM 1,234", Decimal("1234")),
        ("1,234 *", Decimal("1234")),
        ("12.3%", Decimal("12.3")),
    ],
)
def test_parse_number(printed: str, expected: Decimal) -> None:
    assert parse_number(printed) == expected


@pytest.mark.parametrize("printed", ["-", "–", "—", "Nil", "N/A", "", "   ", None])
def test_nil_markers_parse_as_none(printed: str | None) -> None:
    assert parse_number(printed) is None


@pytest.mark.parametrize("printed", ["Revenue", "Note 5", "abc", "1,2,3.4.5"])
def test_prose_is_not_a_number(printed: str) -> None:
    assert parse_number(printed) is None


def test_looks_numeric_treats_nil_as_a_figure() -> None:
    # A dash sits in a value column, so it must not be mistaken for a label.
    assert looks_numeric("-") is True
    assert looks_numeric("1,234") is True
    assert looks_numeric("Revenue") is False


def test_scaling_applies_to_money() -> None:
    assert to_base_units(Decimal("1234"), 1000, is_per_share=False) == Decimal("1234000")


def test_scaling_never_applies_to_per_share_figures() -> None:
    # The 1000x trap: EPS printed as 4.25 sen must stay 4.25 in an RM'000 table.
    assert to_base_units(Decimal("4.25"), 1000, is_per_share=True) == Decimal("4.25")
