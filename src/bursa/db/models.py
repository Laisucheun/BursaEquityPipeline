"""SQLAlchemy models.

Design notes that matter:

* ``RawRow`` is the immutable OCR truth. Numbers enter the system here and
  nowhere else - the LLM mapper never emits a figure, only a concept key.
* A ``Fact`` is keyed by *which document reported it*
  (``reported_in_document_id``). That single decision makes restatements and
  prior-period comparatives fall out naturally instead of colliding: FY2023 as
  originally filed and FY2023 as restated in the FY2024 report are two rows,
  both true. ``facts_current`` (a view, see the Alembic migration) picks the
  most recently filed value per (company, concept, period).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from bursa.db.enums import (
    Basis,
    Continuity,
    DocSource,
    DocStatus,
    DocType,
    LayoutEngine,
    Market,
    PeriodType,
    ReviewStatus,
    RunStatus,
    ScrapeStatus,
    Statement,
    SynonymOrigin,
    TypicalSign,
)

# JSONB on Postgres, plain JSON on SQLite.
JSONType = JSON().with_variant(JSONB(), "postgresql")

# Money. 4 decimal places so per-share figures (EPS, NTA) survive intact.
Money = Numeric(24, 4)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------


class Company(Base, TimestampMixin):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_code: Mapped[str] = mapped_column(String(8), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(64))
    market: Mapped[Market] = mapped_column(String(8), default=Market.MAIN, nullable=False)
    sector: Mapped[str | None] = mapped_column(String(128))
    # Month (1-12) in which the financial year ends. Drives quarter labelling.
    fy_end_month: Mapped[int | None] = mapped_column(Integer)
    is_watchlist: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Seed URL for the IR scraper (bursa.scrapers.ir_fallback). Populated by hand
    # (`bursa company set-ir-url`) - there is no reliable way to auto-discover an
    # arbitrary corporate homepage, and guessing produces wrong URLs with false
    # confidence often enough not to be worth it.
    ir_homepage_url: Mapped[str | None] = mapped_column(Text)

    documents: Mapped[list[Document]] = relationship(back_populates="company")


class Concept(Base, TimestampMixin):
    """One canonical line item. The vocabulary the whole system agrees on."""

    __tablename__ = "concepts"

    concept_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    statement: Mapped[Statement] = mapped_column(String(8), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    parent_key: Mapped[str | None] = mapped_column(
        ForeignKey("concepts.concept_key", ondelete="SET NULL")
    )
    is_subtotal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    typical_sign: Mapped[TypicalSign] = mapped_column(
        String(16), default=TypicalSign.ANY, nullable=False
    )
    # Balance-sheet concepts are instants; IS/CF concepts are durations.
    is_instant: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Per-share and ratio concepts must not be rescaled by the RM'000 multiplier.
    is_per_share: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    synonyms: Mapped[list[ConceptSynonym]] = relationship(back_populates="concept")


class ConceptSynonym(Base, TimestampMixin):
    """Label text that maps to a concept without asking the LLM.

    Seeded from the taxonomy, then grown by the review UI. Every reviewer
    correction lands here, so the same label from the same issuer is resolved
    deterministically - and for free - next quarter.
    """

    __tablename__ = "concept_synonyms"
    __table_args__ = (
        UniqueConstraint(
            "normalized", "statement", "company_id", name="uq_synonym_norm_stmt_company"
        ),
        Index("ix_synonym_lookup", "normalized", "statement"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    concept_key: Mapped[str] = mapped_column(
        ForeignKey("concepts.concept_key", ondelete="CASCADE"), nullable=False
    )
    # Scoping matters: "profit before tax" means is.profit_before_tax on the
    # income statement and cf.profit_before_tax in the cash flow reconciliation,
    # and "non-controlling interests" is a different concept on IS vs BS.
    statement: Mapped[Statement] = mapped_column(String(8), nullable=False)
    pattern: Mapped[str] = mapped_column(String(300), nullable=False)
    # Case/punctuation/whitespace-folded form of `pattern`. See mapping.synonyms.
    normalized: Mapped[str] = mapped_column(String(300), nullable=False)
    lang: Mapped[str] = mapped_column(String(8), default="en", nullable=False)
    origin: Mapped[SynonymOrigin] = mapped_column(String(16), nullable=False)
    # NULL means the synonym applies to every issuer.
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE")
    )

    concept: Mapped[Concept] = relationship(back_populates="synonyms")


# --------------------------------------------------------------------------
# Documents and raw extraction
# --------------------------------------------------------------------------


class Document(Base, TimestampMixin):
    __tablename__ = "documents"
    __table_args__ = (Index("ix_documents_company_filed", "company_id", "filed_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="RESTRICT"))
    doc_type: Mapped[DocType] = mapped_column(String(24), default=DocType.UNKNOWN, nullable=False)
    source: Mapped[DocSource] = mapped_column(String(16), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    # Content hash is the dedupe key: the same PDF never gets ingested twice,
    # whether it arrived by upload, inbox, or scraper.
    file_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    filed_date: Mapped[date | None] = mapped_column(Date)
    period_end_hint: Mapped[date | None] = mapped_column(Date)
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[DocStatus] = mapped_column(
        String(16), default=DocStatus.INGESTED, nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text)
    # Ties together a multi-part filing (e.g. an issuer's "Part 1 - Business
    # Review" / "Part 2 - Financial Statements" split across two PDFs). NULL
    # for an ordinary single-file document. Not unique - every part of one
    # filing shares the same key.
    document_group_key: Mapped[str | None] = mapped_column(String(128))

    company: Mapped[Company | None] = relationship(back_populates="documents")
    pages: Mapped[list[DocumentPage]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    runs: Mapped[list[ExtractionRun]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentPage(Base):
    __tablename__ = "document_pages"
    __table_args__ = (UniqueConstraint("document_id", "page_no", name="uq_page_per_doc"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    has_text_layer: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Which primary statement this page appears to contain, if any.
    statement_guess: Mapped[Statement | None] = mapped_column(String(8))
    guess_confidence: Mapped[float | None] = mapped_column(Float)
    heading_text: Mapped[str | None] = mapped_column(Text)
    width: Mapped[float | None] = mapped_column(Float)
    height: Mapped[float | None] = mapped_column(Float)

    document: Mapped[Document] = relationship(back_populates="pages")


class ExtractionRun(Base, TimestampMixin):
    """One attempt at turning a document into facts.

    Facts point at the run that produced them, so a re-extraction with a newer
    prompt or engine can be compared against the previous one before it is
    published.
    """

    __tablename__ = "extraction_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    layout_engine: Mapped[LayoutEngine | None] = mapped_column(String(16))
    engine_versions: Mapped[dict | None] = mapped_column(JSONType)
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64))
    llm_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    synonym_hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_cents: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    status: Mapped[RunStatus] = mapped_column(String(16), default=RunStatus.RUNNING, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    document: Mapped[Document] = relationship(back_populates="runs")
    raw_rows: Mapped[list[RawRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class RawRow(Base):
    """A table row exactly as the layout engine saw it. Never edited.

    ``cells`` is ``[{"col_index": int, "text": str, "bbox": [x0,y0,x1,y1]}, ...]``.
    Every published number traces back to a cell here, which is what makes a
    figure defensible when it looks wrong.
    """

    __tablename__ = "raw_rows"
    __table_args__ = (
        Index("ix_raw_rows_run_page", "run_id", "page_no"),
        UniqueConstraint("run_id", "page_no", "table_index", "row_index", name="uq_raw_row"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="CASCADE"), nullable=False
    )
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    table_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    label_text: Mapped[str] = mapped_column(Text, nullable=False)
    cells: Mapped[list] = mapped_column(JSONType, nullable=False)
    bbox: Mapped[list | None] = mapped_column(JSONType)
    # Indent depth, used as a hint for subtotal/child structure.
    indent_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    run: Mapped[ExtractionRun] = relationship(back_populates="raw_rows")


class StatementTable(Base):
    """A detected statement table and its resolved column -> period mapping.

    Column semantics are the single biggest source of silent error in Bursa
    quarterlies (current quarter / preceding-year corresponding quarter /
    cumulative YTD / preceding-year cumulative), so they are stored explicitly
    and reviewed, not inferred at query time.
    """

    __tablename__ = "statement_tables"
    __table_args__ = (
        UniqueConstraint("run_id", "page_no", "table_index", name="uq_statement_table"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="CASCADE"), nullable=False
    )
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    table_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    statement: Mapped[Statement] = mapped_column(String(8), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="MYR", nullable=False)
    # Multiplier implied by the header, e.g. 1000 for RM'000.
    scale_multiplier: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    scale_token: Mapped[str | None] = mapped_column(String(64))
    # [{"col_index":1,"period_type":"Q3","period_start":"2024-07-01",
    #   "period_end":"2024-09-30","basis":"CONSOLIDATED","continuity":"TOTAL",
    #   "confidence":0.95}, ...]
    column_map: Mapped[list] = mapped_column(JSONType, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)


# --------------------------------------------------------------------------
# Facts
# --------------------------------------------------------------------------


class Period(Base):
    __tablename__ = "periods"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "period_start", "period_end", "period_type", name="uq_period"
        ),
        Index("ix_periods_company_end", "company_id", "period_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    # NULL for instants (balance sheet dates).
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    period_type: Mapped[PeriodType] = mapped_column(String(8), nullable=False)
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)


class Fact(Base, TimestampMixin):
    __tablename__ = "facts"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "concept_key",
            "period_id",
            "basis",
            "continuity",
            "reported_in_document_id",
            name="uq_fact_identity",
        ),
        Index("ix_facts_lookup", "company_id", "concept_key", "period_id"),
        Index("ix_facts_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    concept_key: Mapped[str] = mapped_column(
        ForeignKey("concepts.concept_key", ondelete="RESTRICT"), nullable=False
    )
    period_id: Mapped[int] = mapped_column(
        ForeignKey("periods.id", ondelete="RESTRICT"), nullable=False
    )

    # Normalised to base currency units (RM, not RM'000), sign as reported.
    value: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="MYR", nullable=False)
    # What the document actually printed, kept for audit.
    value_as_printed: Mapped[str | None] = mapped_column(String(64))
    scale_multiplier: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    basis: Mapped[Basis] = mapped_column(String(16), default=Basis.CONSOLIDATED, nullable=False)
    continuity: Mapped[Continuity] = mapped_column(
        String(16), default=Continuity.TOTAL, nullable=False
    )

    # Provenance. Every one of these is required to defend a number.
    reported_in_document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    source_raw_row_id: Mapped[int | None] = mapped_column(
        ForeignKey("raw_rows.id", ondelete="SET NULL")
    )
    source_col_index: Mapped[int | None] = mapped_column(Integer)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="CASCADE"), nullable=False
    )

    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    review_status: Mapped[ReviewStatus] = mapped_column(
        String(16), default=ReviewStatus.AUTO, nullable=False
    )


# --------------------------------------------------------------------------
# Quality control
# --------------------------------------------------------------------------


class ValidationResult(Base):
    __tablename__ = "validation_results"
    __table_args__ = (Index("ix_validation_run", "run_id", "passed"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="CASCADE"), nullable=False
    )
    rule_key: Mapped[str] = mapped_column(String(48), nullable=False)
    period_id: Mapped[int | None] = mapped_column(ForeignKey("periods.id", ondelete="CASCADE"))
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expected: Mapped[Decimal | None] = mapped_column(Money)
    actual: Mapped[Decimal | None] = mapped_column(Money)
    delta: Mapped[Decimal | None] = mapped_column(Money)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BenchmarkCache(Base):
    """Cached yfinance figures to avoid redundant API calls."""

    __tablename__ = "benchmark_cache"
    __table_args__ = (
        UniqueConstraint("ticker", "field_name", "period_end", name="uq_yf_cache"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Decimal] = mapped_column(Money, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BenchmarkResult(Base):
    __tablename__ = "benchmark_results"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "concept_key", "fiscal_year", "source",
            name="uq_benchmark",
        ),
        Index("ix_benchmark_company", "company_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    concept_key: Mapped[str] = mapped_column(String(64), nullable=False)
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    our_value: Mapped[Decimal | None] = mapped_column(Money)
    external_value: Mapped[Decimal | None] = mapped_column(Money)
    deviation_pct: Mapped[float | None] = mapped_column(Float)
    classification: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class JobProgress(Base):
    """Live progress of one long-running CLI job, polled by the dashboard."""

    __tablename__ = "job_progress"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[RunStatus] = mapped_column(String(16), default=RunStatus.RUNNING, nullable=False)
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current: Mapped[str | None] = mapped_column(String(256))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewItem(Base, TimestampMixin):
    __tablename__ = "review_items"
    __table_args__ = (Index("ix_review_open", "resolved_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    raw_row_id: Mapped[int | None] = mapped_column(ForeignKey("raw_rows.id", ondelete="CASCADE"))
    fact_id: Mapped[int | None] = mapped_column(ForeignKey("facts.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    # Ordering hint for the review queue: fix the worst first.
    severity: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(128))
    resolution: Mapped[dict | None] = mapped_column(JSONType)


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------


class ScrapeAttempt(Base):
    """One company's outcome in one scrape run.

    This is what makes an automated bulk download trustworthy: after a run you
    can see exactly which companies got a report, which didn't, and why
    (``status``), instead of a single pass/fail count. See
    ``src/bursa/scrapers/``.
    """

    __tablename__ = "scrape_attempts"
    __table_args__ = (Index("ix_scrape_attempts_run", "run_label", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[DocSource] = mapped_column(String(16), nullable=False)
    status: Mapped[ScrapeStatus] = mapped_column(String(24), nullable=False)
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    detail: Mapped[str | None] = mapped_column(Text)
    # Groups every attempt made in one `bursa scrape annual-reports` invocation,
    # so `bursa scrape report` can summarise one run at a time.
    run_label: Mapped[str | None] = mapped_column(String(64))
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
