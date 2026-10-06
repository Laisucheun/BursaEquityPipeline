"""Tests for the benchmark cross-validation module."""

from decimal import Decimal

from bursa.benchmark.compare import BenchmarkOutcome, compare_facts
from bursa.benchmark.yfinance_fetch import YfinanceFigure, _to_ticker

from datetime import date


def test_ticker_construction() -> None:
    assert _to_ticker("1295") == "1295.KL"
    assert _to_ticker("5347") == "5347.KL"


def test_compare_exact_match() -> None:
    our = {("is.revenue", 2024): Decimal("1000000000")}
    yf = [YfinanceFigure("is.revenue", date(2024, 12, 31), Decimal("1000000000"), "Total Revenue")]
    results = compare_facts(our, yf)
    assert len(results) == 1
    assert results[0].classification == "MATCH"
    assert results[0].deviation_pct == 0.0


def test_compare_within_tolerance() -> None:
    our = {("is.revenue", 2024): Decimal("1030000000")}
    yf = [YfinanceFigure("is.revenue", date(2024, 12, 31), Decimal("1000000000"), "Total Revenue")]
    results = compare_facts(our, yf, match_tolerance=0.05)
    assert results[0].classification == "MATCH"


def test_compare_close() -> None:
    our = {("is.revenue", 2024): Decimal("1100000000")}
    yf = [YfinanceFigure("is.revenue", date(2024, 12, 31), Decimal("1000000000"), "Total Revenue")]
    results = compare_facts(our, yf, match_tolerance=0.05, close_tolerance=0.15)
    assert results[0].classification == "CLOSE"


def test_compare_mismatch() -> None:
    our = {("is.revenue", 2024): Decimal("2000000000")}
    yf = [YfinanceFigure("is.revenue", date(2024, 12, 31), Decimal("1000000000"), "Total Revenue")]
    results = compare_facts(our, yf)
    assert results[0].classification == "MISMATCH"


def test_compare_scale_error_1000x() -> None:
    our = {("is.revenue", 2024): Decimal("1000000000000")}
    yf = [YfinanceFigure("is.revenue", date(2024, 12, 31), Decimal("1000000000"), "Total Revenue")]
    results = compare_facts(our, yf)
    assert results[0].classification == "SCALE_ERROR"
    assert "1000x" in results[0].detail


def test_compare_scale_error_inverse_1000x() -> None:
    our = {("is.revenue", 2024): Decimal("1000000")}
    yf = [YfinanceFigure("is.revenue", date(2024, 12, 31), Decimal("1000000000"), "Total Revenue")]
    results = compare_facts(our, yf)
    assert results[0].classification == "SCALE_ERROR"
    assert "1/1000x" in results[0].detail


def test_compare_missing_ours() -> None:
    our: dict[tuple[str, int], Decimal] = {}
    yf = [YfinanceFigure("is.revenue", date(2024, 12, 31), Decimal("1000000000"), "Total Revenue")]
    results = compare_facts(our, yf)
    assert results[0].classification == "MISSING"
    assert "not in our extraction" in results[0].detail


def test_compare_missing_yfinance() -> None:
    our = {("is.revenue", 2024): Decimal("1000000000")}
    yf = [YfinanceFigure("bs.total_assets", date(2024, 12, 31), Decimal("5000000"), "Total Assets")]
    results = compare_facts(our, yf)
    missing = [r for r in results if r.concept_key == "is.revenue" and r.classification == "MISSING"]
    assert len(missing) == 1
    assert "not in yfinance" in missing[0].detail


def test_compare_missing_yfinance_skips_uncovered_years() -> None:
    our = {("is.revenue", 2019): Decimal("1000000000")}
    yf = [YfinanceFigure("is.revenue", date(2024, 12, 31), Decimal("5000000"), "Total Revenue")]
    results = compare_facts(our, yf)
    missing_2019 = [r for r in results if r.fiscal_year == 2019 and r.detail == "not in yfinance"]
    assert len(missing_2019) == 0


def test_compare_multiple_concepts() -> None:
    our = {
        ("is.revenue", 2024): Decimal("5000000000"),
        ("bs.total_assets", 2024): Decimal("20000000000"),
    }
    yf = [
        YfinanceFigure("is.revenue", date(2024, 12, 31), Decimal("5000000000"), "Total Revenue"),
        YfinanceFigure("bs.total_assets", date(2024, 12, 31), Decimal("20000000000"), "Total Assets"),
    ]
    results = compare_facts(our, yf)
    assert all(r.classification == "MATCH" for r in results)
    assert len(results) == 2
