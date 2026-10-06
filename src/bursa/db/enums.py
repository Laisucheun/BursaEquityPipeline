"""Controlled vocabularies used across the schema.

These are stored as plain strings in the database (portable across SQLite and
Postgres, and cheap to extend without a migration) but validated in Python.
"""

from __future__ import annotations

from enum import StrEnum


class DocType(StrEnum):
    ANNUAL_REPORT = "ANNUAL_REPORT"
    QUARTERLY_REPORT = "QUARTERLY_REPORT"
    UNKNOWN = "UNKNOWN"


class DocSource(StrEnum):
    UPLOAD = "UPLOAD"
    INBOX = "INBOX"
    BURSA = "BURSA"
    # A company's own investor-relations website, not Bursa's disclosure system.
    # bursamalaysia.com sits behind a Cloudflare CAPTCHA that cannot be automated
    # past, so IR sites are currently the only automated source - see
    # src/bursa/scrapers/.
    IR = "IR"


class DocStatus(StrEnum):
    INGESTED = "INGESTED"
    CLASSIFIED = "CLASSIFIED"
    EXTRACTED = "EXTRACTED"
    MAPPED = "MAPPED"
    VALIDATED = "VALIDATED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class Statement(StrEnum):
    """Which primary statement a concept or page belongs to."""

    INCOME_STATEMENT = "IS"
    BALANCE_SHEET = "BS"
    CASH_FLOW = "CF"
    EQUITY = "EQ"
    OTHER = "OTHER"


class PeriodType(StrEnum):
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q4 = "Q4"
    H1 = "H1"
    H2 = "H2"
    FY = "FY"
    YTD = "YTD"
    # Balance sheet figures are instants, not durations.
    INSTANT = "INSTANT"


DURATION_PERIODS = frozenset(
    {
        PeriodType.Q1,
        PeriodType.Q2,
        PeriodType.Q3,
        PeriodType.Q4,
        PeriodType.H1,
        PeriodType.H2,
        PeriodType.FY,
        PeriodType.YTD,
    }
)


class Basis(StrEnum):
    """Whether a column reports the group or the parent company alone."""

    CONSOLIDATED = "CONSOLIDATED"
    COMPANY = "COMPANY"


class Continuity(StrEnum):
    TOTAL = "TOTAL"
    CONTINUING = "CONTINUING"
    DISCONTINUED = "DISCONTINUED"


class TypicalSign(StrEnum):
    """QA hint only - values are always stored exactly as reported."""

    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    ANY = "ANY"


class SynonymOrigin(StrEnum):
    SEED = "SEED"
    REVIEWER = "REVIEWER"
    LLM = "LLM"


class ReviewStatus(StrEnum):
    AUTO = "AUTO"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class LayoutEngine(StrEnum):
    PYMUPDF = "PYMUPDF"
    DOCLING = "DOCLING"
    TEXTRACT = "TEXTRACT"


class Market(StrEnum):
    MAIN = "MAIN"
    ACE = "ACE"
    LEAP = "LEAP"


class ScrapeStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    ERROR = "ERROR"
    SKIPPED_ROBOTS = "SKIPPED_ROBOTS"
    SKIPPED_NO_URL = "SKIPPED_NO_URL"
