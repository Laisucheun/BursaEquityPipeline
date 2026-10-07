"""Concept-review queue: unmapped statement rows -> `ReviewItem`.

`extract_statements` leaves `concept_key=None` on a row whose label the
synonym table doesn't recognise. Those figures are silently dropped from the
fact table, so each one that carries a real number becomes an open
`ReviewItem` (reason ``UNMAPPED_LABEL``). A reviewer resolves it in the UI;
"map" promotes the label into a `ConceptSynonym` (`record_correction`), so the
next normalize run resolves it deterministically and no new item appears.

Survival across re-runs. Normalize deletes every other `ExtractionRun` of a
re-processed document, and `ReviewItem.run_id` is ``ON DELETE CASCADE``. So
before the old runs go, this module re-points the document's existing
unmapped-label items onto the new run: open ones whose label is still
unmapped and every resolved one (an "ignore"/"reject" decision must not be
re-asked). Open items whose label no longer shows up (now mapped, or the page
changed) are deleted as stale.

Dedupe key: (company, statement, normalized label). One open item per key,
whichever document first raised it.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from bursa.db.enums import Statement
from bursa.db.models import Company, Document, ReviewItem
from bursa.extract.statement_extract import RowInfo, StatementExtraction
from bursa.mapping.synonyms import lookup, normalize_label
from bursa.normalize.numbers import parse_number

UNMAPPED_LABEL = "UNMAPPED_LABEL"

# Statements whose rows are one-label-per-line-item. The equity statement is a
# component x movement matrix handled by `pipeline.equity`.
REVIEWABLE_STATEMENTS = (Statement.INCOME_STATEMENT, Statement.BALANCE_SHEET, Statement.CASH_FLOW)

_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
    "|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
# "31 December 2024", "as at 30.6.2024", "2024", "financial year ended ..."
_DATE_LIKE = re.compile(
    rf"^(as at |at |year ended |financial year ended )?(\d{{1,2}} )?({_MONTHS})?( \d{{1,2}})? ?(19|20)\d\d$"
    rf"|^(as at|at) \d|^\d{{1,2}} ({_MONTHS})\b|^(financial )?(year|period) ended\b"
)
_HEADER_LIKE = re.compile(
    r"^(group|company|bank|the group|the company|note|notes|rm|rm 000|rm'000|rm mil|rm million"
    r"|restated|audited|unaudited|current|non current)$"
)
# EPS / per-share rows: the extractor keeps the non-total EPS block rows
# unmapped on purpose (basic vs diluted, continuing vs discontinued), and the
# EPS note pipeline owns them.
_PER_SHARE = re.compile(r"\b(per (ordinary )?share|per unit|sen|eps|basic|diluted|earnings per)\b")


def _numeric_values(row: RowInfo, note_cols: set[int]) -> dict[int, float]:
    out: dict[int, float] = {}
    for col, text in row.values.items():
        if col in note_cols:
            continue
        parsed = parse_number(text)
        if parsed is None:
            continue
        out[col] = float(parsed)
    return out


def is_reviewable_label(label: str, numbers: Iterable[float] = ()) -> bool:
    """Conservative filter: is this unmapped row plausibly a missing line item
    (worth a reviewer's time) rather than a header, date or EPS fragment?"""
    stripped = label.strip()
    if stripped[:1].islower():
        return False  # tail of a wrapped label ("equipment", "capital changes")
    norm = normalize_label(label)
    if len(norm) < 3 or not re.search(r"[a-z]{3}", norm):
        return False
    if norm.startswith("note ") or norm == "note":
        return False
    if _DATE_LIKE.search(norm) or _HEADER_LIKE.match(norm):
        return False
    if _PER_SHARE.search(norm):
        return False
    nums = [abs(n) for n in numbers]
    if nums and all(1900 <= n <= 2100 and float(n).is_integer() for n in nums):
        return False  # a year header row ("2024  2023") read as figures
    return True


def unmapped_candidates(
    session: Session | None,
    company_id: int | None,
    statement: Statement,
    extracted: StatementExtraction,
) -> list[dict]:
    """The rows of one extracted statement that would become review items,
    deduped by normalized label within the statement. No DB writes.

    A row whose label *does* resolve via `lookup` was deliberately left
    unmapped by the extractor's context rules (a repeated PAT in the OCI
    section, a pre-PAT-only concept below PAT, ...) - not a taxonomy gap."""
    if statement not in REVIEWABLE_STATEMENTS:
        return []
    note_cols = {c.col_index for c in extracted.columns if c.is_note}
    multiplier = getattr(extracted.scale, "multiplier", 1) or 1
    seen: set[str] = set()
    out: list[dict] = []
    for row in extracted.rows:
        if row.concept_key is not None:
            continue
        numbers = _numeric_values(row, note_cols)
        if not numbers or not any(numbers.values()):
            continue
        if not is_reviewable_label(row.label, numbers.values()):
            continue
        norm = normalize_label(row.label)
        if norm in seen:
            continue
        if session is not None and lookup(session, row.label, statement, company_id=company_id):
            continue
        seen.add(norm)
        magnitude = max(abs(v) for v in numbers.values()) * multiplier
        out.append({
            "label": row.label.strip(),
            "normalized": norm,
            "statement": statement.value,
            "page_no": extracted.page_no,
            "row_index": row.row_index,
            "values": {str(k): v for k, v in sorted(row.values.items()) if k not in note_cols},
            "magnitude": magnitude,
        })
    return out


def severity_for(magnitude: float, companies_with_label: int) -> int:
    """0-100. Larger figures and labels seen at many issuers first: one synonym
    there fixes the most facts."""
    mag_score = 0.0 if magnitude <= 0 else min(50.0, max(0.0, (math.log10(magnitude) - 4) * 8))
    freq_score = min(30.0, 6.0 * max(0, companies_with_label - 1))
    return int(round(20 + mag_score + freq_score))


def _detail(item: ReviewItem) -> dict:
    try:
        d = json.loads(item.detail or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def record_unmapped_rows(
    session: Session,
    company: Company,
    document_id: int,
    run_id: int,
    statement: Statement,
    extracted: StatementExtraction,
) -> int:
    """Raise one open `UNMAPPED_LABEL` review item per unmapped, figure-bearing
    label of this statement. Idempotent; returns the number of items created."""
    if statement not in REVIEWABLE_STATEMENTS:
        return 0
    candidates = unmapped_candidates(session, company.id, statement, extracted)
    current = {c["normalized"]: c for c in candidates}

    existing = list(
        session.scalars(
            select(ReviewItem)
            .join(Document, Document.id == ReviewItem.document_id)
            .where(Document.company_id == company.id, ReviewItem.reason == UNMAPPED_LABEL)
        )
    )
    open_keys: set[str] = set()
    for item in existing:
        d = _detail(item)
        if d.get("statement") != statement.value:
            continue
        norm = d.get("normalized") or normalize_label(d.get("label", ""))
        if item.document_id == document_id and item.run_id != run_id:
            # This document is being re-run: its old runs are about to be
            # deleted (CASCADE). Keep what still matters on the new run.
            if item.resolved_at is None and norm not in current:
                session.delete(item)
                continue
            item.run_id = run_id
        # Open: already queued. Resolved: an ignore/reject must not be
        # re-asked (a map can't recur - its synonym now resolves the label).
        open_keys.add(norm)

    if not current:
        session.flush()
        return 0

    frequency = _label_frequency(session, statement, set(current) - open_keys)
    created = 0
    for norm, cand in current.items():
        if norm in open_keys:
            continue
        companies = frequency.get(norm, 0) + 1
        session.add(ReviewItem(
            run_id=run_id,
            document_id=document_id,
            reason=UNMAPPED_LABEL,
            detail=json.dumps({k: v for k, v in cand.items() if k != "magnitude"}),
            severity=severity_for(cand["magnitude"], companies),
        ))
        created += 1
    session.flush()
    return created


def _label_frequency(session: Session, statement: Statement, norms: set[str]) -> dict[str, int]:
    """How many *other* companies already have an item for each label."""
    if not norms:
        return {}
    by_norm: dict[str, set[int]] = {}
    rows = session.execute(
        select(ReviewItem.detail, Document.company_id)
        .join(Document, Document.id == ReviewItem.document_id)
        .where(ReviewItem.reason == UNMAPPED_LABEL)
    )
    for detail, company_id in rows:
        try:
            d = json.loads(detail or "{}")
        except ValueError:
            continue
        if d.get("statement") == statement.value and d.get("normalized") in norms:
            by_norm.setdefault(d["normalized"], set()).add(company_id)
    return {k: len(v) for k, v in by_norm.items()}


# --------------------------------------------------------------------------
# Suggestions (deterministic, token overlap)
# --------------------------------------------------------------------------

_STOPWORDS = frozenset({
    "the", "of", "and", "or", "to", "for", "in", "on", "from", "at", "by", "a", "an",
    "net", "other", "total", "s", "dan", "yang",
})


def _tokens(text: str) -> set[str]:
    return {t for t in normalize_label(text).split() if t not in _STOPWORDS and len(t) > 1}


def suggest_concepts(
    label: str,
    index: list[tuple[str, str, set[str]]],
    k: int = 3,
) -> list[dict]:
    """Top-k concepts by Jaccard overlap between the label's tokens and any of
    a concept's label/synonym token sets. `index` is `build_suggestion_index`
    output for the label's statement."""
    toks = _tokens(label)
    if not toks:
        return []
    best: dict[str, tuple[float, str]] = {}
    for concept_key, concept_label, syn_toks in index:
        if not syn_toks:
            continue
        inter = len(toks & syn_toks)
        if not inter:
            continue
        score = inter / len(toks | syn_toks)
        if score > best.get(concept_key, (0.0, ""))[0]:
            best[concept_key] = (score, concept_label)
    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))[:k]
    return [{"concept_key": ck, "label": lbl, "score": round(sc, 3)} for ck, (sc, lbl) in ranked]


def build_suggestion_index(session: Session) -> dict[str, list[tuple[str, str, set[str]]]]:
    """statement value -> [(concept_key, concept label, token set)], one entry
    per concept label and per global synonym."""
    from bursa.db.models import Concept, ConceptSynonym

    labels = {c.concept_key: (c.statement, c.label) for c in session.scalars(select(Concept))}
    out: dict[str, list[tuple[str, str, set[str]]]] = {}
    for key, (stmt, label) in labels.items():
        out.setdefault(str(stmt), []).append((key, label, _tokens(label)))
    syns = session.execute(
        select(ConceptSynonym.concept_key, ConceptSynonym.statement, ConceptSynonym.pattern)
        .where(ConceptSynonym.company_id.is_(None))
    )
    for key, stmt, pattern in syns:
        if key in labels:
            out.setdefault(str(stmt), []).append((key, labels[key][1], _tokens(pattern)))
    return out
