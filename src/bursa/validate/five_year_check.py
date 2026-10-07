"""Validation layer 5 - five-year summary page vs our extracted facts.

The summary page is the company's own restatement-aware, five-year view of
its headline numbers. Agreement is strong evidence that our statement
extraction (scale, period assignment, concept mapping) is right for years
yfinance never reaches; disagreement points at either a restatement or an
extraction bug on our side.

Classification, per (fiscal year, concept):

* MATCH            - within printed rounding, or <= ``match_tolerance``
* CLOSE            - <= ``close_tolerance`` (definition drift, small restatement)
* SCALE_ERROR      - off by a clean power of ten (``benchmark.compare`` rule)
* MISMATCH         - anything else
* ONLY_IN_SUMMARY  - the summary has it, our facts don't

Tolerances are tighter than the yfinance benchmark's 5% / 15%: that compares
against a third party's re-classified figures, this compares against the
same company's own numbers, which differ only by rounding (a summary printed
in RM million) or restatement. Nothing here writes to the database.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bursa.analysis.facts import AnnualFacts, load_annual_facts
from bursa.benchmark.compare import _detect_scale_error
from bursa.db.enums import DocType
from bursa.db.models import Company, Document, Fact, Period
from bursa.extract.five_year import (
    DPS_KEY,
    SummaryTable,
    SummaryValue,
    extract_five_year_summary,
)

MATCH_TOLERANCE = 0.01
CLOSE_TOLERANCE = 0.05
CLASSES = ("MATCH", "CLOSE", "MISMATCH", "SCALE_ERROR", "ONLY_IN_SUMMARY")


@dataclass(frozen=True)
class SummaryCheck:
    concept_key: str
    fiscal_year: int
    summary_value: Decimal
    our_value: Decimal | None
    deviation_pct: float | None
    classification: str
    detail: str
    page_no: int
    document_id: int | None = None
    restated: bool = False


def classify_pair(
    summary: SummaryValue,
    ours: Decimal | None,
    *,
    match_tolerance: float = MATCH_TOLERANCE,
    close_tolerance: float = CLOSE_TOLERANCE,
) -> tuple[str, float | None, str]:
    theirs = summary.value
    if ours is None:
        return "ONLY_IN_SUMMARY", None, "no annual fact for this year"
    diff = abs(ours - theirs)
    # The summary printed "1,068" in RM million: anything within half a
    # printed unit (plus a hair for our own rounding) is the same number.
    if diff <= summary.rounding * Decimal("1.01"):
        dev = float(diff / abs(theirs)) if theirs else 0.0
        return "MATCH", dev, "within printed rounding"
    if theirs == 0:
        return "MISMATCH", None, "summary reports zero"
    dev = float(diff / abs(theirs))
    if ours == -theirs:
        return "MISMATCH", dev, "sign flipped"
    hint = _detect_scale_error(ours, theirs)
    if hint:
        return "SCALE_ERROR", dev, hint
    note = " (summary column restated)" if summary.restated else ""
    if dev <= match_tolerance:
        return "MATCH", dev, f"{dev:.2%} deviation"
    if dev <= close_tolerance:
        return "CLOSE", dev, f"{dev:.2%} deviation{note}"
    return "MISMATCH", dev, f"{dev:.1%} deviation{note}"


def compare_summary(
    values: Iterable[SummaryValue],
    annual: dict[int, AnnualFacts],
    *,
    document_id: int | None = None,
    include_dps: bool = False,
    match_tolerance: float = MATCH_TOLERANCE,
    close_tolerance: float = CLOSE_TOLERANCE,
) -> list[SummaryCheck]:
    """Classify every summary value against the annual fact set."""
    checks: list[SummaryCheck] = []
    for v in values:
        if v.concept_key == DPS_KEY and not include_dps:
            continue  # no taxonomy concept to compare against
        year = annual.get(v.fiscal_year)
        ours = year.values.get(v.concept_key) if year else None
        cls, dev, detail = classify_pair(
            v, ours, match_tolerance=match_tolerance, close_tolerance=close_tolerance,
        )
        if v.unit_source == "inferred":
            detail += " (page printed no unit; inferred)"
        checks.append(SummaryCheck(
            concept_key=v.concept_key, fiscal_year=v.fiscal_year,
            summary_value=v.value, our_value=ours, deviation_pct=dev,
            classification=cls, detail=detail, page_no=v.page_no,
            document_id=document_id, restated=v.restated,
        ))
    return sorted(checks, key=lambda c: (c.fiscal_year, c.concept_key))


_CANDIDATE_SCALES = (1, 1_000, 1_000_000)


def infer_assumed_units(
    values: Iterable[SummaryValue],
    annual: dict[int, AnnualFacts],
    *,
    min_hits: int = 2,
) -> list[SummaryValue]:
    """Resolve money rows whose page printed no unit at all.

    Some summaries never say "RM million" (it's in a footnote, a graphic, or
    nowhere). Rather than report every such row as a 10^6 SCALE_ERROR - which
    would wrongly accuse our facts - pick, per page, the single power of a
    thousand that makes the most rows agree, and mark those values
    ``unit_source="inferred"``. One multiplier per page, so a genuine
    per-row disagreement still shows up.
    """
    values = list(values)
    pages: dict[int, list[int]] = {}
    for i, v in enumerate(values):
        if v.unit_source == "assumed" and v.unit == "RM":
            pages.setdefault(v.page_no, []).append(i)

    for idxs in pages.values():
        def hits(scale: int, idxs: list[int] = idxs) -> int:
            n = 0
            for i in idxs:
                v = values[i]
                year = annual.get(v.fiscal_year)
                ours = year.values.get(v.concept_key) if year else None
                scaled = replace(v, value=v.value * scale, rounding=v.rounding * scale)
                if ours is not None and classify_pair(scaled, ours)[0] == "MATCH":
                    n += 1
            return n

        scored = {s: hits(s) for s in _CANDIDATE_SCALES}
        best = max(scored, key=lambda s: scored[s])
        if best == 1 or scored[best] < min_hits or scored[best] <= scored[1]:
            continue
        for i in idxs:
            v = values[i]
            values[i] = replace(
                v, value=v.value * best, rounding=v.rounding * best,
                multiplier=best, unit_source="inferred",
            )
    return values


def detect_year_shift(
    values: Iterable[SummaryValue],
    annual: dict[int, AnnualFacts],
    *,
    min_hits: int = 3,
) -> int | None:
    """``+1`` when our facts sit one fiscal year *later* than the summary's
    (our FY N+1 == their FY N), ``-1`` for one year earlier, else None.

    A consistent shift is the signature of a wrong ``fy_end_month`` (or a
    changed financial year end) on our side, not a restatement - restatements
    don't move every concept by exactly one year.
    """
    values = list(values)

    def hits(offset: int) -> int:
        n = 0
        for v in values:
            if v.concept_key == DPS_KEY:
                continue
            year = annual.get(v.fiscal_year + offset)
            ours = year.values.get(v.concept_key) if year else None
            if ours is not None and classify_pair(v, ours)[0] == "MATCH":
                n += 1
        return n

    base = hits(0)
    for offset in (1, -1):
        shifted = hits(offset)
        if shifted >= min_hits and shifted > 2 * base:
            return offset
    return None


# --------------------------------------------------------------------------
# Company level
# --------------------------------------------------------------------------


@dataclass
class DocumentSummary:
    document_id: int
    storage_path: str
    report_year: int
    tables: list[SummaryTable] = field(default_factory=list)
    error: str | None = None


@dataclass
class CompanyFiveYearResult:
    stock_code: str
    name: str
    documents_scanned: int = 0
    documents_with_summary: int = 0
    documents: list[DocumentSummary] = field(default_factory=list)
    checks: list[SummaryCheck] = field(default_factory=list)
    # Fiscal years our facts cover; "older" = before the latest 4 of them,
    # the range yfinance can't benchmark.
    fact_years: list[int] = field(default_factory=list)
    # +1/-1 when every summary year lines up with our facts one fiscal year
    # off - see `detect_year_shift`.
    year_shift: int | None = None

    @property
    def counts(self) -> Counter[str]:
        return Counter(c.classification for c in self.checks)

    def older_cutoff(self) -> int | None:
        years = sorted({c.fiscal_year for c in self.checks} | set(self.fact_years))
        return years[-4] if len(years) >= 4 else None

    @property
    def agreement(self) -> float | None:
        """MATCH share among comparable (non ONLY_IN_SUMMARY) checks."""
        comparable = [c for c in self.checks if c.classification != "ONLY_IN_SUMMARY"]
        if not comparable:
            return None
        return sum(c.classification == "MATCH" for c in comparable) / len(comparable)


_GROUP_YEAR = re.compile(r"(?:19|20)\d{2}$")


def annual_report_documents(session: Session, company: Company) -> list[tuple[int, Document]]:
    """``(report_year, document)`` for each annual report, newest first.

    ``period_end_hint``/``filed_date`` are mostly unset on scraped reports,
    so the year comes from, in order: the period hint, the scraper's
    ``document_group_key`` ("1023-2025"), or the latest fiscal year of the
    facts extracted from that document. 0 when none is known. A report split
    into several volumes (integrated report + financial statements) yields
    several documents with the same year.
    """
    docs = session.scalars(
        select(Document).where(
            Document.company_id == company.id,
            Document.doc_type == DocType.ANNUAL_REPORT,
        )
    ).all()
    fact_year = dict(session.execute(
        select(Fact.reported_in_document_id, func.max(Period.fiscal_year))
        .join(Period, Fact.period_id == Period.id)
        .where(Fact.company_id == company.id)
        .group_by(Fact.reported_in_document_id)
    ).all())

    def year_of(doc: Document) -> int:
        if doc.period_end_hint:
            return doc.period_end_hint.year
        m = _GROUP_YEAR.search(doc.document_group_key or "")
        if m:
            return int(m.group(0))
        return int(fact_year.get(doc.id) or 0)

    ranked = [(year_of(d), d) for d in docs]
    ranked.sort(key=lambda yd: (yd[0], yd[1].id), reverse=True)
    return ranked


def check_company(
    session: Session,
    company: Company,
    *,
    max_years: int | None = 1,
    include_dps: bool = False,
) -> CompanyFiveYearResult:
    """Extract summary pages from the company's annual reports and compare.

    ``max_years`` limits the scan to the newest N report years (every volume
    of each); None scans every annual report.

    For each (fiscal year, concept) the newest document's summary value
    wins - a later report's restated figure beats the original, the same
    rule `load_annual_facts` applies to facts.
    """
    result = CompanyFiveYearResult(stock_code=company.stock_code, name=company.name)
    annual = load_annual_facts(session, company)
    result.fact_years = sorted(annual)

    chosen: dict[tuple[str, int], tuple[SummaryValue, int]] = {}
    ranked = annual_report_documents(session, company)
    if max_years is not None:
        keep = sorted({y for y, _ in ranked}, reverse=True)[:max_years]
        ranked = [(y, d) for y, d in ranked if y in keep]
    for year, doc in ranked:
        result.documents_scanned += 1
        summary = DocumentSummary(doc.id, doc.storage_path, year)
        result.documents.append(summary)
        if not Path(doc.storage_path).exists():
            summary.error = "file missing"
            continue
        try:
            summary.tables = extract_five_year_summary(
                doc.storage_path, session=session, company_id=company.id,
            )
        except Exception as exc:
            summary.error = f"{type(exc).__name__}: {exc}"
            continue
        if summary.tables:
            result.documents_with_summary += 1
        doc_values = infer_assumed_units(
            [v for table in summary.tables for v in table.values], annual,
        )
        for value in doc_values:
            chosen.setdefault((value.concept_key, value.fiscal_year), (value, doc.id))

    for value, doc_id in chosen.values():
        result.checks.extend(compare_summary(
            [value], annual, document_id=doc_id, include_dps=include_dps,
        ))
    result.checks.sort(key=lambda c: (c.fiscal_year, c.concept_key))
    result.year_shift = detect_year_shift([v for v, _ in chosen.values()], annual)
    return result
