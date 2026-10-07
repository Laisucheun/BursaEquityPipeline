from datetime import date

from bursa.db.enums import PeriodType
from bursa.normalize.periods import resolve_duration_period_type


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
