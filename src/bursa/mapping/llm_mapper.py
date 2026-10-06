"""Stage 4 - assign a canonical concept to each row and a period to each column.

The single most important property of this module: **it never sends or receives
a financial figure.** The model is given row labels and column header text only,
and returns concept keys and period definitions. Numbers are copied from the
OCR cells in ``normalize``. A model cannot hallucinate a number it was never
shown and is never asked to produce.

``assert_no_figures`` enforces that at runtime rather than by convention.

Cost shape: the system prompt (the whole concept vocabulary for one statement)
is byte-stable across every call, so it caches; only the row labels vary. The
synonym pre-pass in ``resolve_table`` means most rows never reach the model at
all, and that share grows as reviewers correct things.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from bursa.config import get_settings
from bursa.db.enums import Basis, Continuity, PeriodType, Statement
from bursa.extract.layout import ExtractedTable
from bursa.mapping.synonyms import lookup
from bursa.mapping.taxonomy import concepts_for
from bursa.normalize.numbers import parse_number

log = logging.getLogger(__name__)

PROMPT_VERSION = "2026-09-29.1"

_PERIOD_TYPES = [p.value for p in PeriodType]


class ColumnAssignment(BaseModel):
    col_index: int
    period_end: str = Field(description="ISO date, YYYY-MM-DD")
    period_type: Literal["Q1", "Q2", "Q3", "Q4", "H1", "H2", "FY", "YTD", "INSTANT"]
    basis: Literal["CONSOLIDATED", "COMPANY"] = "CONSOLIDATED"
    continuity: Literal["TOTAL", "CONTINUING", "DISCONTINUED"] = "TOTAL"
    confidence: float

    def parsed_period_end(self) -> date:
        return date.fromisoformat(self.period_end)


class RowAssignment(BaseModel):
    row_index: int
    # None means "this row is not one of the canonical concepts" - a section
    # heading, a subtotal we do not track, or something genuinely unusual.
    concept_key: str | None = None
    confidence: float


class MappingResult(BaseModel):
    columns: list[ColumnAssignment] = Field(default_factory=list)
    rows: list[RowAssignment] = Field(default_factory=list)


@dataclass
class ResolvedTable:
    """Everything needed to turn an ``ExtractedTable`` into facts."""

    columns: list[ColumnAssignment] = field(default_factory=list)
    row_concepts: dict[int, tuple[str | None, float]] = field(default_factory=dict)
    synonym_hits: int = 0
    llm_rows: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class FigureLeakError(RuntimeError):
    """Raised when a prompt payload would contain a financial figure."""


def assert_no_figures(payload: str, allow: set[str] | None = None) -> None:
    """Guard the invariant that the model is never shown a figure.

    Dates in header text ("30.09.2024") are not figures and are needed for
    period assignment, so tokens containing more than one separator are exempt,
    as are bare years and small column indices.
    """
    allow = allow or set()
    for token in payload.replace("|", " ").split():
        cleaned = token.strip(".,:;()[]")
        if cleaned in allow or not cleaned:
            continue
        # Dates: 30.09.2024, 30/09/2024, 2024-09-30.
        if cleaned.count(".") > 1 or cleaned.count("/") > 1 or cleaned.count("-") > 1:
            continue
        value = parse_number(cleaned)
        if value is None:
            continue
        # Bare years and row/column indices are structural, not financial.
        if value == value.to_integral_value() and 0 <= abs(value) <= 2100:
            continue
        raise FigureLeakError(
            f"refusing to send a financial figure to the model: {token!r}"
        )


@lru_cache(maxsize=8)
def build_system_prompt(statement: Statement) -> str:
    """The stable, cacheable half of the prompt.

    Deterministically ordered so it is byte-identical on every call and the
    prompt cache actually hits.
    """
    lines = [
        "You classify line items in Malaysian (Bursa Malaysia) financial statements",
        "prepared under MFRS. You are given the printed ROW LABELS and COLUMN HEADERS",
        "of one statement table. You never see the figures, and you must never invent any.",
        "",
        "Your job is exactly two things:",
        "1. Assign each row label to one canonical concept key, or null if none fits.",
        "2. Assign each numeric column to the reporting period it represents.",
        "",
        "## Column assignment",
        "Bursa quarterly reports (Listing Requirements Appendix 9B) typically print four",
        "columns in this order: current quarter, preceding year corresponding quarter,",
        "current cumulative year-to-date, preceding year corresponding cumulative.",
        "Headers such as 'INDIVIDUAL QUARTER' and 'CUMULATIVE QUARTER' span two columns each.",
        "Getting this wrong corrupts every derived metric, so if a column's period is",
        "genuinely ambiguous give it a confidence below 0.5 rather than guessing.",
        "",
        "Rules:",
        "- period_type INSTANT is for statement of financial position columns only.",
        "- Q1-Q4 mean a single three-month period; YTD means cumulative from the start",
        "  of the financial year; FY means a full financial year.",
        "- A column headed 'Audited' with a prior year end date is the comparative.",
        "- basis is COMPANY only when the column is explicitly labelled Company",
        "  (as opposed to Group/Consolidated); otherwise CONSOLIDATED.",
        "",
        "## Row assignment",
        "- Match on meaning, not wording. Malay labels are common.",
        "- Return null for section headings ('ASSETS'), narrative rows ('Attributable to:'),",
        "  and any line item that is not in the vocabulary below. Do not force a fit.",
        "- Indentation is given as a hint: indented rows are components of the",
        "  subtotal above them.",
        "- Use the concept whose meaning matches, even when the issuer's subtotal",
        "  structure differs from the vocabulary's.",
        "- confidence is your own calibrated probability that the mapping is correct.",
        "  Below 0.8 sends the row to a human reviewer, which is the correct outcome",
        "  for anything you are unsure about.",
        "",
        f"## Concept vocabulary for the {statement.name.replace('_', ' ').lower()}",
        "",
    ]

    for spec in concepts_for(statement):
        note = " [subtotal]" if spec.is_subtotal else ""
        note += " [per-share/ratio]" if spec.is_per_share else ""
        desc = f" - {spec.description}" if spec.description else ""
        lines.append(f"{spec.key}: {spec.label}{note}{desc}")

    return "\n".join(lines)


def build_user_payload(
    table: ExtractedTable,
    row_indices: list[int] | None = None,
    fy_end_month: int | None = None,
) -> str:
    """The volatile half: this table's headers and labels. No figures."""
    wanted = set(row_indices) if row_indices is not None else None

    parts: list[str] = []
    if fy_end_month:
        parts.append(f"Issuer financial year ends in month {fy_end_month}.")
    parts.append(f"STATEMENT: {table.statement.name if table.statement else 'UNKNOWN'}")
    parts.append(f"NUMERIC COLUMNS: {len(table.columns)} (indices 0..{len(table.columns) - 1})")
    parts.append("")
    parts.append("HEADER TEXT (verbatim):")
    parts.append(table.header_text or "(none)")
    parts.append("")
    parts.append("COLUMN HEADER CELLS (col_index | text):")
    for row in table.header_rows:
        for cell in sorted(row.cells, key=lambda c: c.col_index):
            if cell.text.strip():
                parts.append(f"{cell.col_index} | {cell.text}")
    parts.append("")
    parts.append("ROW LABELS (row_index | indent | label | which columns hold a value):")
    for row in table.rows:
        if wanted is not None and row.row_index not in wanted:
            continue
        if not row.label:
            continue
        cols = ",".join(str(c.col_index) for c in sorted(row.cells, key=lambda c: c.col_index))
        parts.append(f"{row.row_index} | {row.indent_level} | {row.label} | [{cols}]")

    payload = "\n".join(parts)
    assert_no_figures(payload)
    return payload


_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "columns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "col_index": {"type": "integer"},
                    "period_end": {
                        "type": "string",
                        "description": "ISO date YYYY-MM-DD of the period end",
                    },
                    "period_type": {"type": "string", "enum": _PERIOD_TYPES},
                    "basis": {"type": "string", "enum": ["CONSOLIDATED", "COMPANY"]},
                    "continuity": {
                        "type": "string",
                        "enum": ["TOTAL", "CONTINUING", "DISCONTINUED"],
                    },
                    "confidence": {"type": "number"},
                },
                "required": [
                    "col_index",
                    "period_end",
                    "period_type",
                    "basis",
                    "continuity",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        },
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "row_index": {"type": "integer"},
                    "concept_key": {
                        "type": ["string", "null"],
                        "description": "A concept key from the vocabulary, or null.",
                    },
                    "confidence": {"type": "number"},
                },
                "required": ["row_index", "concept_key", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["columns", "rows"],
    "additionalProperties": False,
}


def get_client():  # type: ignore[no-untyped-def]
    """An Anthropic client.

    With no ``ANTHROPIC_API_KEY`` the SDK falls back to an ``ant auth login``
    profile, so a bare constructor is correct.
    """
    import anthropic

    settings = get_settings()
    if settings.anthropic_api_key:
        return anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return anthropic.Anthropic()


def map_table(
    table: ExtractedTable,
    statement: Statement,
    row_indices: list[int] | None = None,
    fy_end_month: int | None = None,
    client: Any | None = None,
    model: str | None = None,
) -> tuple[MappingResult, dict[str, int]]:
    """One mapping call. Returns the result and token usage."""
    client = client or get_client()
    model = model or get_settings().mapper_model

    system = build_system_prompt(statement)
    user = build_user_payload(table, row_indices, fy_end_month)

    response = client.messages.create(
        model=model,
        max_tokens=16000,
        # The vocabulary is identical on every call for this statement, so it
        # caches; the volatile rows sit after it in the user message.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": _JSON_SCHEMA}},
        thinking={"type": "adaptive"},
    )

    if getattr(response, "stop_reason", None) == "refusal":
        raise RuntimeError(f"model declined the mapping request: {response.stop_details}")

    text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    try:
        result = MappingResult.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise RuntimeError(f"mapper returned unusable output: {exc}") from exc

    usage = {
        "input_tokens": getattr(response.usage, "input_tokens", 0),
        "output_tokens": getattr(response.usage, "output_tokens", 0),
    }
    return result, usage


def resolve_table(
    session: Session,
    table: ExtractedTable,
    statement: Statement,
    company_id: int | None = None,
    fy_end_month: int | None = None,
    client: Any | None = None,
    use_llm: bool = True,
) -> ResolvedTable:
    """Resolve a whole table: synonyms first, the model only for what is left.

    The deterministic pre-pass is what keeps this affordable. Every reviewer
    correction adds a synonym, so the share of rows resolved for free rises with
    each filing processed from the same issuer.
    """
    resolved = ResolvedTable()
    unresolved: list[int] = []

    for row in table.rows:
        if not row.label or not row.cells:
            continue
        hit = lookup(session, row.label, statement, company_id)
        if hit is not None:
            resolved.row_concepts[row.row_index] = (hit, 1.0)
            resolved.synonym_hits += 1
        else:
            unresolved.append(row.row_index)

    # Columns always need the model: period assignment cannot be memoised,
    # since the dates differ in every filing.
    if not use_llm:
        log.info(
            "llm disabled: %d rows resolved by synonym, %d left unresolved",
            resolved.synonym_hits,
            len(unresolved),
        )
        return resolved

    # Always a list, never None: an empty list means "send no rows, I only need
    # the column assignments". `or None` here would send the entire table back
    # to the model precisely when the synonym table had already solved it all.
    result, usage = map_table(
        table,
        statement,
        row_indices=unresolved,
        fy_end_month=fy_end_month,
        client=client,
    )

    resolved.columns = result.columns
    resolved.llm_calls = 1
    resolved.llm_rows = len(unresolved)
    resolved.input_tokens = usage["input_tokens"]
    resolved.output_tokens = usage["output_tokens"]

    valid_keys = {spec.key for spec in concepts_for(statement)}
    for assignment in result.rows:
        key = assignment.concept_key
        if key is not None and key not in valid_keys:
            # A concept key outside the vocabulary is a model error, not a new
            # concept. Drop it to review rather than writing an unknown key.
            log.warning("mapper returned unknown concept %r; sending row to review", key)
            key, confidence = None, 0.0
        else:
            confidence = assignment.confidence
        resolved.row_concepts[assignment.row_index] = (key, confidence)

    return resolved


def to_enum_columns(
    columns: list[ColumnAssignment],
) -> dict[int, tuple[date, PeriodType, Basis, Continuity, float]]:
    """Convert the model's column assignments into typed values."""
    out: dict[int, tuple[date, PeriodType, Basis, Continuity, float]] = {}
    for column in columns:
        try:
            period_end = column.parsed_period_end()
        except ValueError:
            log.warning(
                "column %s has unparseable period_end %r",
                column.col_index,
                column.period_end,
            )
            continue
        out[column.col_index] = (
            period_end,
            PeriodType(column.period_type),
            Basis(column.basis),
            Continuity(column.continuity),
            column.confidence,
        )
    return out
