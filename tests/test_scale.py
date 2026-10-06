from __future__ import annotations

import pytest

from bursa.normalize.scale import detect_scale, mentions_per_share


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("CONDENSED CONSOLIDATED INCOME STATEMENT (RM'000)", 1_000),
        ("RM '000", 1_000),
        ("(RM’000)", 1_000),
        ("All figures in thousands", 1_000),
        ("RM million", 1_000_000),
        ("RM'mil", 1_000_000),
        ("RM Mn", 1_000_000),
        ("RM'000,000", 1_000_000),
        ("RM billion", 1_000_000_000),
        ("STATEMENT OF FINANCIAL POSITION", 1),
        ("", 1),
    ],
)
def test_scale_detection(header: str, expected: int) -> None:
    assert detect_scale(header).multiplier == expected


def test_scale_reads_from_any_of_the_supplied_texts() -> None:
    info = detect_scale("Income Statement", None, "Unaudited, RM'000")
    assert info.multiplier == 1_000
    assert info.currency == "MYR"


def test_currency_detection() -> None:
    assert detect_scale("USD'000").currency == "USD"
    assert detect_scale("SGD million").currency == "SGD"
    # Malaysian filings are MYR unless they say otherwise.
    assert detect_scale("Income statement").currency == "MYR"


def test_per_share_columns_are_flagged() -> None:
    assert mentions_per_share("Basic earnings per share (sen)")
    assert mentions_per_share("Sen")
    assert not mentions_per_share("Revenue")
