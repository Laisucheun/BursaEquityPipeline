from __future__ import annotations

from datetime import date

import pytest

from bursa.db.enums import PeriodType
from bursa.normalize.periods import (
    corresponding_prior_period,
    fiscal_year_of,
    fiscal_year_start,
    parse_dotted_date,
    parse_stated_period_end,
    period_bounds,
    quarter_index,
    same_month_day,
)

DEC = 12
JUN = 6


def test_december_year_end_quarters() -> None:
    assert quarter_index(date(2024, 3, 31), DEC) == 1
    assert quarter_index(date(2024, 6, 30), DEC) == 2
    assert quarter_index(date(2024, 9, 30), DEC) == 3
    assert quarter_index(date(2024, 12, 31), DEC) == 4


def test_june_year_end_quarters() -> None:
    # FY2025 runs 1 Jul 2024 - 30 Jun 2025.
    assert quarter_index(date(2024, 9, 30), JUN) == 1
    assert quarter_index(date(2024, 12, 31), JUN) == 2
    assert quarter_index(date(2025, 3, 31), JUN) == 3
    assert quarter_index(date(2025, 6, 30), JUN) == 4


def test_fiscal_year_naming_follows_the_end_year() -> None:
    assert fiscal_year_of(date(2024, 9, 30), JUN) == 2025
    assert fiscal_year_of(date(2025, 6, 30), JUN) == 2025
    assert fiscal_year_of(date(2024, 9, 30), DEC) == 2024


def test_fiscal_year_start() -> None:
    assert fiscal_year_start(2025, JUN) == date(2024, 7, 1)
    assert fiscal_year_start(2024, DEC) == date(2024, 1, 1)


def test_quarter_bounds() -> None:
    bounds = period_bounds(date(2024, 9, 30), PeriodType.Q3, DEC)
    assert bounds.period_start == date(2024, 7, 1)
    assert bounds.period_end == date(2024, 9, 30)
    assert bounds.fiscal_year == 2024


def test_ytd_starts_at_the_fiscal_year_not_january() -> None:
    bounds = period_bounds(date(2025, 3, 31), PeriodType.YTD, JUN)
    assert bounds.period_start == date(2024, 7, 1)


def test_instants_have_no_start_date() -> None:
    bounds = period_bounds(date(2024, 9, 30), PeriodType.INSTANT, DEC)
    assert bounds.period_start is None


def test_prior_period_keeps_month_end_alignment() -> None:
    bounds = period_bounds(date(2024, 2, 29), PeriodType.Q1, DEC)
    prior = corresponding_prior_period(bounds, DEC)
    # 29 Feb 2024 -> 28 Feb 2023, not 1 Mar.
    assert prior.period_end == date(2023, 2, 28)


def test_rejects_impossible_year_end() -> None:
    with pytest.raises(ValueError, match="fy_end_month"):
        quarter_index(date(2024, 1, 1), 13)


# --------------------------------------------------------------------------
# parse_stated_period_end - real phrasing confirmed this session: Frontken,
# Kerjaya Prospek, SD Guthrie, Gas Malaysia all state their period end
# verbatim in the statement's own subtitle.
# --------------------------------------------------------------------------


def test_parses_a_financial_year_ended_subtitle() -> None:
    text = "Statements of Comprehensive Income\nFor the financial year ended 31 December 2024"
    assert parse_stated_period_end(text, instant=False) == date(2024, 12, 31)


def test_parses_an_as_at_subtitle_for_a_balance_sheet() -> None:
    text = "Statement of Financial Position\nAs at 30 September 2024 (RM'000)"
    assert parse_stated_period_end(text, instant=True) == date(2024, 9, 30)


def test_falls_back_to_the_other_phrasing_when_that_one_is_absent() -> None:
    # A duration statement's own header occasionally only carries "as at"
    # text (e.g. an audit-date caption) - still usable, not a hard miss.
    text = "Cash Flow Statement\nas at 31 December 2024"
    assert parse_stated_period_end(text, instant=False) == date(2024, 12, 31)


def test_accepts_an_abbreviated_month_name() -> None:
    assert parse_stated_period_end("for the year ended 30 Jun 2024", instant=False) == date(2024, 6, 30)


def test_returns_none_when_no_recognisable_date_is_present() -> None:
    assert parse_stated_period_end("Statement of Financial Position", instant=True) is None


def test_returns_none_for_an_impossible_calendar_date() -> None:
    # Never fabricate - a malformed match must not be silently coerced.
    assert parse_stated_period_end("ended 31 February 2024", instant=False) is None


# --------------------------------------------------------------------------
# same_month_day
# --------------------------------------------------------------------------


def test_same_month_day_reuses_the_month_and_day() -> None:
    assert same_month_day(2023, 12, 31) == date(2023, 12, 31)


def test_same_month_day_clamps_a_leap_day_into_a_non_leap_year() -> None:
    assert same_month_day(2023, 2, 29) == date(2023, 2, 28)


# --------------------------------------------------------------------------
# parse_dotted_date - a column's own DD.MM.YYYY date, confirmed real on
# Tenaga Nasional's restated MFRS-transition opening-balance column
# ("1.1.2024"), which carries a different day/month than the statement's own
# "as at 31 December 2024" subtitle.
# --------------------------------------------------------------------------


def test_parses_a_column_s_own_dotted_date() -> None:
    assert parse_dotted_date("31.12.2024 RM'million (Restated)") == date(2024, 12, 31)


def test_parses_a_single_digit_day_and_month() -> None:
    assert parse_dotted_date("1.1.2024 RM'million (Restated)") == date(2024, 1, 1)


def test_returns_none_when_no_dotted_date_is_present() -> None:
    assert parse_dotted_date("2024") is None
