"""Fiscal calendar arithmetic.

Malaysian issuers use a wide range of financial year ends, so nothing here may
assume a December year end. Convention followed throughout: a financial year is
named for the calendar year in which it *ends* - a year ending 30 June 2025 is
FY2025.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

from bursa.db.enums import DURATION_PERIODS, PeriodType

QUARTER_TYPES: tuple[PeriodType, ...] = (
    PeriodType.Q1,
    PeriodType.Q2,
    PeriodType.Q3,
    PeriodType.Q4,
)


def add_months(d: date, months: int) -> date:
    """Shift by whole months, clamping to the last valid day."""
    total = (d.year * 12 + d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def fiscal_year_of(period_end: date, fy_end_month: int) -> int:
    """Which financial year a date falls in."""
    _check_month(fy_end_month)
    return period_end.year if period_end.month <= fy_end_month else period_end.year + 1


def fiscal_year_start(fiscal_year: int, fy_end_month: int) -> date:
    """First day of the financial year."""
    _check_month(fy_end_month)
    if fy_end_month == 12:
        return date(fiscal_year, 1, 1)
    return date(fiscal_year - 1, fy_end_month + 1, 1)


def fiscal_year_end(fiscal_year: int, fy_end_month: int) -> date:
    _check_month(fy_end_month)
    return month_end(fiscal_year, fy_end_month)


def quarter_index(period_end: date, fy_end_month: int) -> int:
    """Which quarter of the financial year a period end belongs to (1-4).

    Returns the quarter containing ``period_end``; a period end that is not on
    a quarter boundary still resolves to its containing quarter, which is what
    you want for the odd 53-week or transition-period filing.
    """
    fy = fiscal_year_of(period_end, fy_end_month)
    start = fiscal_year_start(fy, fy_end_month)
    months_elapsed = (period_end.year - start.year) * 12 + (period_end.month - start.month)
    return min(4, months_elapsed // 3 + 1)


def quarter_type(period_end: date, fy_end_month: int) -> PeriodType:
    return QUARTER_TYPES[quarter_index(period_end, fy_end_month) - 1]


@dataclass(frozen=True)
class PeriodBounds:
    period_start: date | None
    period_end: date
    period_type: PeriodType
    fiscal_year: int


def period_bounds(
    period_end: date, period_type: PeriodType, fy_end_month: int
) -> PeriodBounds:
    """Resolve a period end plus a type into full bounds.

    Balance sheet figures are instants and carry no start date - conflating
    them with durations is what makes a fact table ambiguous later.
    """
    _check_month(fy_end_month)
    fy = fiscal_year_of(period_end, fy_end_month)

    if period_type == PeriodType.INSTANT:
        return PeriodBounds(None, period_end, period_type, fy)

    if period_type in QUARTER_TYPES:
        start = add_months(date(period_end.year, period_end.month, 1), -2)
    elif period_type == PeriodType.H1 or period_type == PeriodType.H2:
        start = add_months(date(period_end.year, period_end.month, 1), -5)
    elif period_type in (PeriodType.FY, PeriodType.YTD):
        start = fiscal_year_start(fy, fy_end_month)
    else:  # pragma: no cover - exhaustive over the enum
        raise ValueError(f"unhandled period type: {period_type}")

    return PeriodBounds(start, period_end, period_type, fy)


def is_duration(period_type: PeriodType) -> bool:
    return period_type in DURATION_PERIODS


def corresponding_prior_period(bounds: PeriodBounds, fy_end_month: int) -> PeriodBounds:
    """The preceding-year corresponding period.

    This is what the ``comparative_match`` validation rule compares against:
    the prior-year column in one filing must agree with the as-reported figure
    from the filing a year earlier, or something has been restated (or
    misextracted).
    """
    prior_end = add_months(bounds.period_end, -12)
    # Keep month-end alignment when the shift lands on a shorter month.
    if bounds.period_end.day == calendar.monthrange(
        bounds.period_end.year, bounds.period_end.month
    )[1]:
        prior_end = month_end(prior_end.year, prior_end.month)
    return period_bounds(prior_end, bounds.period_type, fy_end_month)


def infer_fy_end_month(period_ends: list[date]) -> int | None:
    """Guess an issuer's year end from observed annual period ends.

    Used when onboarding a company from filings alone. Returns ``None`` when
    the evidence is inconsistent rather than guessing.
    """
    months = {d.month for d in period_ends}
    if len(months) == 1:
        return months.pop()
    return None


def _check_month(fy_end_month: int) -> None:
    if not 1 <= fy_end_month <= 12:
        raise ValueError(f"fy_end_month must be 1-12, got {fy_end_month}")


# --------------------------------------------------------------------------
# Reading a period end directly off a statement's own subtitle text.
# --------------------------------------------------------------------------

_MONTHS: dict[str, int] = {
    **{name.lower(): i for i, name in enumerate(calendar.month_name) if name},
    **{abbr.lower(): i for i, abbr in enumerate(calendar.month_abbr) if abbr},
    "sept": 9,
}

# Confirmed real phrasing this project has read directly off filings: "for
# the financial year ended 31 December 2024" (duration statements - income
# statement, cash flow) and "as at 30 September 2024" (balance sheet).
# Each lead-in ("ended" / "as at") is followed by one of three date forms:
# "31 December 2024", "December 31, 2023" (CSC Steel), or "31/12/2024" /
# "31.12.2024" - possibly on the next line, hence \s+ throughout.
_DATE_FORMS = (
    (r"(?P<d>\d{1,2})\s+(?P<m>[A-Za-z]+)\.?,?\s+(?P<y>\d{4})"),
    (r"(?P<m>[A-Za-z]+)\.?\s+(?P<d>\d{1,2}),?\s+(?P<y>\d{4})"),
    (r"(?P<d>\d{1,2})[./](?P<m>\d{1,2})[./](?P<y>\d{4})"),
)
_DURATION_ENDED = tuple(re.compile(r"\bended\s+" + f, re.IGNORECASE) for f in _DATE_FORMS)
_INSTANT_AS_AT = tuple(re.compile(r"\bas\s+at\s+" + f, re.IGNORECASE) for f in _DATE_FORMS)


def parse_stated_period_end(text: str, *, instant: bool) -> date | None:
    """Read a statement's own exact period-end date from its subtitle text.

    Tries the phrasing appropriate to ``instant`` first ("as at ..." for a
    balance sheet, "... ended ..." for a duration statement), then the other
    as a fallback - a page occasionally carries both. Returns ``None``,
    never a guess, when neither phrasing is found or the date is not a real
    calendar date - matching the rest of this module and
    ``bursa.extract.statement_extract``'s "report raw, don't fabricate" rule.
    """
    groups = (_INSTANT_AS_AT, _DURATION_ENDED) if instant else (_DURATION_ENDED, _INSTANT_AS_AT)
    for patterns in groups:
        for pattern in patterns:
            for match in pattern.finditer(text):
                m = match.group("m")
                month = int(m) if m.isdigit() else _MONTHS.get(m.lower())
                if month is None:
                    continue
                try:
                    return date(int(match.group("y")), month, int(match.group("d")))
                except ValueError:
                    continue
    return None


_WORD_MONTHS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
# "Three Months Ended 31 March 2022" (a single quarter), "Nine Months Ended
# 30 September 2024" (a cumulative YTD column) - the statement's own duration
# phrasing, read the same "trust the document's own words" way as
# `parse_stated_period_end`. Checked before `_FINANCIAL_YEAR_ENDED` since a
# caption can carry both ("...for the financial year ended 31 December 2024"
# next to a quarter's own "Three Months Ended" caption on the same page).
_MONTHS_ENDED = re.compile(
    r"\b(" + "|".join(_WORD_MONTHS) + r")\s+months?\s+ended", re.IGNORECASE
)
_FINANCIAL_YEAR_ENDED = re.compile(r"\b(?:financial\s+)?year\s+ended", re.IGNORECASE)


def parse_statement_duration_months(text: str) -> int | None:
    """How many months of activity this statement covers, read from its own
    caption - never guessed from the stated end-date's month, which is a
    quarter/half-year end for an interim report, not the company's real
    fiscal year end (confirmed real bug: United Plantations' Q1 report was
    written as a fake 12-month "financial year" because nothing distinguished
    its "Three Months Ended" caption from a genuine annual report's).

    Returns ``None`` when no duration phrase is found at all - treated by
    `resolve_duration_period_type` as a full financial year, matching every
    annual-report subtitle this project has seen in practice (not every real
    filing literally says "Twelve Months Ended").
    """
    match = _MONTHS_ENDED.search(text)
    if match:
        return _WORD_MONTHS[match.group(1).lower()]
    if _FINANCIAL_YEAR_ENDED.search(text):
        return 12
    return None


def resolve_duration_period_type(
    duration_months: int | None, period_end: date, fy_end_month: int
) -> PeriodType | None:
    """Classify a duration statement's real period type from its own stated
    duration length plus the company's real fiscal year end - the fix for the
    bug above. Returns ``None`` for a duration this project doesn't yet have
    a rule for (e.g. a garbled OCR duration, or a genuine 1/2/4/5/7/8/10/11
    month transition-period filing), to be skipped rather than guessed -
    same discipline as everywhere else in this module.
    """
    if duration_months == 12:
        return PeriodType.FY
    if duration_months is None:
        # An unstated duration is only safely annual when it ends on the fiscal
        # year end - AMMB's Dec-dated subsidiary interim (March FYE) was being
        # written as a 9-month "FY".
        return PeriodType.FY if period_end.month == fy_end_month else None
    if duration_months == 3:
        return quarter_type(period_end, fy_end_month)
    if duration_months == 6:
        return PeriodType.H1 if quarter_index(period_end, fy_end_month) <= 2 else PeriodType.H2
    if duration_months == 9:
        return PeriodType.YTD
    return None


def same_month_day(year: int, month: int, day: int) -> date:
    """``day``/``month`` in a different ``year``, clamped to that year's
    month length (29 Feb in a leap year, carried into a non-leap comparative
    year, becomes 28 Feb rather than raising)."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


# DD.MM.YYYY - the numeric form a column's own header commonly carries
# (e.g. "31.12.2024", "30.09.2024"). Read directly rather than assumed to
# share the statement's own subtitle month/day: a column can legitimately
# carry a different date of its own - confirmed real on Tenaga Nasional's
# balance sheet, which prints a restated MFRS-transition opening-balance
# column dated "1.1.2024" alongside the ordinary "31.12.2024" comparative.
_DOTTED_DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")


def parse_dotted_date(text: str) -> date | None:
    """A column's own DD.MM.YYYY end date, if its header states one explicitly.

    The *last* date wins: a duration column is often headed with its range,
    "1.4.2025 to 31.3.2026" (IRIS), whose first date is the start - reading
    that as the end made one-day "fiscal years" a year early."""
    matches = list(_DOTTED_DATE.finditer(text))
    if not matches:
        return None
    day, month, year = (int(g) for g in matches[-1].groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None
