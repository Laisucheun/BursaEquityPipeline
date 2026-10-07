from datetime import date

from bursa.db.enums import PeriodType
import pytest

from bursa.normalize.periods import (
    is_interim_header,
    parse_dotted_date,
    quarterly_column_durations,
    parse_stated_period_end,
    resolve_duration_period_type,
)


@pytest.mark.parametrize("text, instant, expected", [
    ("FOR THE FINANCIAL YEAR ENDED 31 DECEMBER 2024", False, date(2024, 12, 31)),
    ("FOR THE YEAR ENDED DECEMBER 31, 2023", False, date(2023, 12, 31)),  # CSC Steel
    ("Financial Year Ended\n31/12/2024 31/12/2023#", False, date(2024, 12, 31)),
    ("AS AT 31.3.2026", True, date(2026, 3, 31)),
    ("as at 30 Sept. 2024", True, date(2024, 9, 30)),
    ("as at 30 Septembre 2024", True, None),  # unknown month word - no guess
    ("Statement of Financial Position", True, None),
])
def test_stated_period_end_forms(text: str, instant: bool, expected: date | None) -> None:
    assert parse_stated_period_end(text, instant=instant) == expected


def test_quarterly_column_durations() -> None:
    # S P Setia: numeric durations
    text = "FOR THE FINANCIAL PERIOD ENDED 31 DECEMBER 2025 | 3 MONTHS ENDED 12 MONTHS ENDED"
    assert quarterly_column_durations(text, date(2025, 12, 31), 12) == [3, 3, 12, 12]
    # bands only: cumulative length from the period end (Q3 of a Dec year)
    bands = "INDIVIDUAL QUARTER CUMULATIVE QUARTER | 30.09.2024 30.09.2023"
    assert quarterly_column_durations(bands, date(2024, 9, 30), 12) == [3, 3, 9, 9]
    assert quarterly_column_durations("For the financial year ended 31 December 2024", date(2024, 12, 31), 12) is None
    assert is_interim_header(bands) and is_interim_header(text)
    assert not is_interim_header("FOR THE FINANCIAL YEAR ENDED 31 DECEMBER 2024")


def test_dotted_date_range_reads_the_end_date() -> None:
    assert parse_dotted_date("1.4.2025 to 31.3.2026 RM'000") == date(2026, 3, 31)
    assert parse_dotted_date("31.12.2024") == date(2024, 12, 31)
    assert parse_dotted_date("Restated 1.1.2024") == date(2024, 1, 1)


def test_stated_twelve_months_is_fy() -> None:
    assert resolve_duration_period_type(12, date(2025, 3, 31), 3) == PeriodType.FY


def test_unstated_duration_ending_on_fy_end_is_fy() -> None:
    assert resolve_duration_period_type(None, date(2025, 3, 31), 3) == PeriodType.FY


def test_unstated_duration_ending_off_fy_end_is_skipped() -> None:
    # AMMB (March FYE): a December-dated interim with no parseable duration
    # was being stored as a 9-month "FY".
    assert resolve_duration_period_type(None, date(2024, 12, 31), 3) is None


def test_nine_months_is_ytd() -> None:
    assert resolve_duration_period_type(9, date(2024, 12, 31), 3) == PeriodType.YTD
