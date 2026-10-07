"""Weighted average number of shares, read from the EPS note.

The face of the income statement prints basic/diluted EPS but almost never
the EPS denominator; that lives in the "Earnings per share" note:

    28. EARNINGS PER SHARE
    Profit attributable to owners of the Company (RM'000)        30,820   41,611
    Weighted average number of ordinary shares in issue ('000) 1,109,067 1,109,067
    Basic earnings per share (sen)                                  2.78     3.75

Deterministic, no LLM: find the note by its heading, cut the page down to
that note, run the ordinary layout extractor over just those words, and pick
three rows by label - PATAMI, weighted shares, basic EPS. The share count is
read in the note's *own* unit ("('000)", "million", "(unit)"), never the
statement's RM'000 scale. When the unit is not stated, it is inferred only
from the note's own arithmetic (PATAMI / shares = EPS), and the share-count
row is likewise chosen by that arithmetic when several rows qualify (opening
shares, effect of issuance, the weighted total).

`write_weighted_shares_facts` turns the result into `is.weighted_avg_shares`
facts on the same periods the income statement's own facts were written to.
Confidence is 1.0 only when the note's basic EPS equals the face EPS within
printing precision and the note's own arithmetic reproduces it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from bursa.db.enums import Basis, Continuity
from bursa.db.models import Fact
from bursa.extract.layout import ExtractedTable, Word, _extract_from_words, group_rows, words_from_page
from bursa.normalize.numbers import parse_number
from bursa.normalize.scale import detect_scale

# --------------------------------------------------------------------------
# Label patterns
# --------------------------------------------------------------------------

_EPS_PHRASE = re.compile(
    r"\b(earnings|loss(es)?|profits?)\s*(/\s*\(?\s*(loss|earnings)\s*\)?\s*)?per\s+"
    r"(ordinary\s+|stapled\s+)?(share|unit|security)s?\b|\bep[su]\b",
    re.IGNORECASE,
)
# The note's own heading: "28. EARNINGS PER SHARE", "29 EARNINGS/(LOSS) PER
# SHARE (Cont'd)", "B12 Earnings per share", "19 EARNINGS PER UNIT ("EPU")".
_HEADING = re.compile(
    r"^\s*(?P<num>[A-Z]?\d{1,2})(\.\d{1,2})*\.?\s+"
    r"((basic\s+(and|&)\s+diluted\s+)?(\(?loss\)?\s*/\s*)?(earnings|loss|profit)"
    r"(\s*/\s*\(?loss\)?)?\s+per\s+(ordinary\s+|stapled\s+)?(share|unit|security))",
    re.IGNORECASE,
)
_PAGE_SIGNAL = re.compile(r"weighted\s+average\s+number", re.IGNORECASE)

_SHARES = re.compile(
    r"weighted\s+average|number\s+of\s+(issued\s+)?(ordinary\s+)?(shares|units|stapled)|"
    r"(shares|units)\s+in\s+(issue|circulation)",
    re.IGNORECASE,
)
_PATAMI = re.compile(
    r"\b(profit|earnings|loss|income)\b.*\b(attributable|owners?|equity\s+holders|"
    r"shareholders|unitholders|holders\s+of)\b"
    r"|^[\s\-–—•]*(net\s+)?(\(?(profit|loss)\)?\s*(/\s*\(?(profit|loss)\)?)?)\s+"
    r"(after\s+tax(ation)?|for\s+the\s+(financial\s+)?(year|period))",
    re.IGNORECASE,
)
_PER_SHARE = re.compile(r"\bper\s+(ordinary\s+|stapled\s+)?(share|unit|security)", re.IGNORECASE)
_DILUTED = re.compile(r"dilut", re.IGNORECASE)
_BASIC = re.compile(r"\bbasic\b", re.IGNORECASE)
# Rows that are a component of the figure, not the figure itself.
_COMPONENT = re.compile(
    r"realis|continuing|discontinu|\beffects?\b|\badjust|interest\s+expense|issuance|"
    r"conversion|\bexercise",
    re.IGNORECASE,
)
_BARE_YEAR = re.compile(r"^(19[89]\d|20[0-4]\d)$")
_TOTAL_LABEL = re.compile(r"^\s*(total|\(?unit\)?s?|\(?'?000\)?|\(\s*sen\s*\))?\s*:?$", re.IGNORECASE)

_THOUSANDS = re.compile(r"(?<!rm)(?<!rm )(?<!myr)\(?\s*'\s*000\s*\)?|\bthousands?\b", re.IGNORECASE)
_MILLIONS = re.compile(r"\b(million|mil|mn)\b|\(\s*m\s*\)", re.IGNORECASE)
_UNITS = re.compile(r"\(\s*units?\s*\)|\(\s*no\.?\s*\)", re.IGNORECASE)
_EPS_RM = re.compile(r"\(\s*rm\s*\)", re.IGNORECASE)
_SEN = re.compile(r"\b(sen|cents?)\b", re.IGNORECASE)

# Plausible share counts for a Bursa issuer, in units.
_MIN_SHARES = Decimal(1_000_000)
_MAX_SHARES = Decimal(500_000_000_000)

_SHARE_MULTIPLIERS = (1, 1_000, 1_000_000)
_MAX_EPS_SEN = Decimal(5_000)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)  # "Proﬁt" ligatures
    return text.replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass
class EpsNoteColumn:
    col_index: int
    year: int | None
    basis: Basis | None
    patami: Decimal | None = None  # RM, base units
    patami_from_face: bool = False  # the note had none; the face PATAMI was used
    weighted_shares: Decimal | None = None  # units
    shares_printed: str | None = None
    shares_multiplier: int | None = None
    shares_unit_stated: bool = False
    basic_eps_sen: Decimal | None = None
    eps_printed: str | None = None
    # PATAMI / shares reproduces the note's own basic EPS within rounding.
    arithmetic_ok: bool = False


@dataclass
class EpsNote:
    page_no: int
    heading: str
    columns: list[EpsNoteColumn] = field(default_factory=list)
    patami_label: str | None = None
    shares_label: str | None = None
    eps_label: str | None = None


@dataclass
class WeightedSharesValue:
    """One is.weighted_avg_shares figure aligned to an income statement column."""

    is_col_index: int
    shares: Decimal
    printed: str
    multiplier: int
    confidence: float
    note_eps_sen: Decimal | None
    face_eps_sen: Decimal | None


# --------------------------------------------------------------------------
# Finding and cutting out the note
# --------------------------------------------------------------------------


def _row_text(words: list[Word]) -> str:
    return " ".join(w.text for w in words)


def _note_words(page) -> tuple[list[Word], str, bool] | None:  # type: ignore[no-untyped-def]
    """The words of this page that belong to the EPS note: from its heading
    down to the next note's heading (``num + 1``), and whether the note runs
    on past the page's end. ``None`` when the page has no EPS note heading."""
    rows = group_rows(words_from_page(page))
    for start, row in enumerate(rows):
        heading = _fold(_row_text(row))
        match = _HEADING.match(heading)
        if match and len(heading.split()) <= 14:
            break
    else:
        return None
    num = match.group("num")
    number = int(re.sub(r"\D", "", num))
    prefix = num[: len(num) - len(str(number))]
    next_heading = re.compile(rf"^\s*{re.escape(prefix)}0?{number + 1}\.?\s+[A-Za-z(]")
    end = next(
        (j for j in range(start + 1, len(rows)) if next_heading.match(_fold(_row_text(rows[j])))),
        None,
    )
    region = [w for r in rows[start:end] for w in r]
    return region, heading, end is None


def _candidate_pages(doc, after_page: int | None) -> list[int]:  # type: ignore[no-untyped-def]
    """0-based page indexes mentioning a weighted average share count next
    to an EPS phrase - notes after the income statement first."""
    hits = []
    for index in range(doc.page_count):
        text = doc[index].get_text()
        if _PAGE_SIGNAL.search(text) and _EPS_PHRASE.search(text):
            hits.append(index)
    if after_page is not None:
        hits.sort(key=lambda i: (i + 1 <= after_page, i))
    return hits


# --------------------------------------------------------------------------
# Reading the note's rows
# --------------------------------------------------------------------------


@dataclass
class _Line:
    label: str  # own label
    context: str  # own label plus the valueless line(s) just above it
    values: dict[int, str]
    role: str | None


def _classify(text: str) -> str | None:
    if _SHARES.search(text) and not _DILUTED.search(text):
        return "shares"
    if _SHARES.search(text):
        return "shares_diluted"
    if _PATAMI.search(text) and not _PER_SHARE.search(text):
        return None if _DILUTED.search(text) and not _BASIC.search(text) else "patami"
    if _EPS_PHRASE.search(text):
        if _DILUTED.search(text) and not _BASIC.search(text):
            return "eps_diluted"
        return "eps"
    return None


def _row_label(row, values: dict[int, str], words: list[Word]) -> str:  # type: ignore[no-untyped-def]
    """The row's own text minus its figures, in reading order. A label word
    that strayed into a column band becomes a "cell" in the layout output and
    would otherwise be appended out of order ("Profit after comprehensive
    income taxation/total")."""
    y0, y1 = row.bbox[1], row.bbox[3]
    figure_edges = [c.bbox for c in row.cells if c.col_index in values]
    own = [
        w for w in words
        if y0 - 0.5 <= w.y_mid <= y1 + 0.5
        and not any(b[0] - 0.5 <= w.x0 and w.x1 <= b[2] + 0.5 for b in figure_edges)
    ]
    own.sort(key=lambda w: w.x0)
    return " ".join(w.text for w in own) if own else row.label


def _lines(table: ExtractedTable, words: list[Word] | None = None) -> list[_Line]:
    """Each value row with its role. A row's role comes from its own label,
    else from its own label joined to the wrapped line(s) above it, else -
    for a "Total"/blank/"(unit)" row - from the block header above it."""
    out: list[_Line] = []
    pending: list[str] = []
    block: str | None = None
    # Header bands too: above the first figure, a label line whose words
    # strayed into a column band is filed as a header row, not a body row.
    for row in sorted([*table.header_rows, *table.rows], key=lambda r: r.bbox[1]):
        values = {
            c.col_index: c.text for c in row.cells
            if (parse_number(c.text) is not None and not _BARE_YEAR.match(c.text.strip()))
            or c.text.strip() in ("-", "–", "—")
        }
        if words is not None:
            label = _fold(_row_label(row, values, words)).strip()
        else:
            strays = [c.text for c in row.cells if c.col_index not in values]
            label = _fold(" ".join([row.label, *strays])).strip()
        if not values:
            # Prose ("...divided by the weighted average number of Units.")
            # is not a block header.
            if label and not label.endswith(".") and len(label.split()) <= 15:
                pending = [*pending[-1:], label]
                role = _classify(label)
                if role is not None:
                    block = role
            continue
        context = " ".join([*pending, label])
        pending = []
        if _COMPONENT.search(label):
            role = None
        elif _TOTAL_LABEL.match(label) and block is not None:
            role = block
        else:
            role = _classify(label) or _classify(context)
        if role is not None and _classify(label) is not None:
            block = role
        out.append(_Line(label=label, context=context, values=values, role=role))
    return out


def _decimals(text: str) -> int:
    match = re.search(r"\.(\d+)", text)
    return len(match.group(1)) if match else 0


def _eps_close(a: Decimal, b: Decimal, decimals: int) -> bool:
    tolerance = Decimal(5) / Decimal(10) ** (decimals + 1)
    return abs(a - b) <= max(tolerance, abs(b) * Decimal("0.006")) + Decimal("0.0001")


def _shares_multiplier_stated(context: str, header: str) -> int | None:
    if _MILLIONS.search(context):
        return 1_000_000
    if _THOUSANDS.search(context):
        return 1_000
    if _UNITS.search(context):
        return 1
    folded = _fold(header)
    if _THOUSANDS.search(folded) and not re.search(r"\b(rm|myr)\b", folded, re.IGNORECASE):
        return 1_000
    return None


def _patami_multiplier(context: str, header: str, fallback: int) -> int:
    for text in (context, header):
        scale = detect_scale(_fold(text))
        if scale.token is not None:
            return scale.multiplier
        if re.search(r"\b(rm|myr)\b", text, re.IGNORECASE):
            return 1
    return fallback


def _parse_table(
    table: ExtractedTable, page_no: int, heading: str, *, fallback_multiplier: int,
    words: list[Word] | None = None, face_patami: dict[int, Decimal] | None = None,
) -> EpsNote | None:
    from bursa.extract.statement_extract import RowInfo, _column_header_text, _resolve_columns

    lines = _lines(table, words)
    if not lines:
        return None
    row_infos = [
        RowInfo(row_index=i, label=line.label, concept_key=None, indent_level=0, values=line.values)
        for i, line in enumerate(lines)
    ]
    columns = [c for c in _resolve_columns(table, row_infos) if not c.is_note]
    note = EpsNote(page_no=page_no, heading=heading)

    def first(role: str, col_index: int) -> _Line | None:
        for ln in lines:
            value = parse_number(ln.values.get(col_index))
            if ln.role != role or value is None:
                continue
            # EPS in sen is small; a share count or profit figure that
            # landed on an EPS-labelled row is not EPS.
            if role == "eps" and abs(value) > _MAX_EPS_SEN:
                continue
            return ln
        return None

    share_lines = [ln for ln in lines if ln.role == "shares"]
    # Preference order: the label itself says "weighted", then its wrapped
    # context does, then any other share-count row.
    share_lines.sort(key=lambda ln: (
        0 if re.search(r"weighted", ln.label, re.IGNORECASE) else
        1 if re.search(r"weighted", ln.context, re.IGNORECASE) else 2
    ))
    if not share_lines:
        return None

    for col in columns:
        eps_line = first("eps", col.col_index)
        if eps_line is None:
            continue
        eps_text = eps_line.values[col.col_index]
        eps = parse_number(eps_text)
        if _EPS_RM.search(eps_line.context) and not _SEN.search(eps_line.context):
            eps = eps * 100
        header = _column_header_text(table, col.col_index)
        out = EpsNoteColumn(
            col_index=col.col_index, year=col.year, basis=col.basis,
            basic_eps_sen=eps, eps_printed=eps_text,
        )
        note.eps_label = note.eps_label or eps_line.context
        patami_line = first("patami", col.col_index)
        if patami_line is not None:
            mult = _patami_multiplier(patami_line.context, header, fallback_multiplier)
            out.patami = parse_number(patami_line.values[col.col_index]) * mult
            note.patami_label = note.patami_label or patami_line.context
        elif face_patami and col.year in face_patami:
            out.patami, out.patami_from_face = face_patami[col.year], True

        chosen: tuple[_Line, Decimal, int, bool] | None = None
        for line in share_lines:
            printed = line.values.get(col.col_index)
            raw = parse_number(printed)
            if raw is None or raw <= 0:
                continue
            stated = _shares_multiplier_stated(line.context, header)
            for mult in ((stated,) if stated else _SHARE_MULTIPLIERS):
                shares = raw * mult
                if not (_MIN_SHARES <= shares <= _MAX_SHARES):
                    continue
                if chosen is None:
                    chosen = (line, raw, mult, False)
                if out.patami is not None and _eps_close(
                    out.patami / shares * 100, eps, _decimals(eps_text or "")
                ):
                    chosen = (line, raw, mult, True)
                    break
            if chosen is not None and chosen[3]:
                break
        if chosen is None:
            continue
        line, raw, mult, ok = chosen
        out.weighted_shares = raw * mult
        out.shares_printed = line.values[col.col_index]
        out.shares_multiplier = mult
        out.shares_unit_stated = _shares_multiplier_stated(line.context, header) is not None
        out.arithmetic_ok = ok
        note.shares_label = note.shares_label or line.context
        note.columns.append(out)
    return note if note.columns else None


def extract_eps_note(
    pdf_path: Path, *, after_page: int | None = None, fallback_multiplier: int = 1,
    face_patami: dict[int, Decimal] | None = None, max_candidates: int = 6,
) -> EpsNote | None:
    """Find and read the EPS note. ``after_page`` (1-based, the income
    statement's page) puts the notes that follow it first;
    ``fallback_multiplier`` is the statement's own scale, used for the note's
    PATAMI only when the note states no unit at all; ``face_patami`` (year ->
    RM) stands in when the note prints no PATAMI line."""
    import pymupdf

    best: EpsNote | None = None
    with pymupdf.open(pdf_path) as doc:
        for index in _candidate_pages(doc, after_page)[:max_candidates]:
            page = doc[index]
            cut = _note_words(page)
            if cut is None:
                continue
            words, heading, runs_on = cut
            # A note that runs over the page break: append the top of the
            # next page, shifted below this page's words.
            if runs_on and index + 1 < doc.page_count:
                shift = page.rect.height
                words = words + [
                    Word(w.text, w.x0, w.y0 + shift, w.x1, w.y1 + shift)
                    for w in _continuation_words(doc[index + 1])
                ]
            table = _extract_from_words(words, index + 1, None, 0)
            if table is None:
                continue
            note = _parse_table(
                table, index + 1, heading, words=words,
                fallback_multiplier=fallback_multiplier, face_patami=face_patami,
            )
            if note is None:
                continue
            if any(c.arithmetic_ok for c in note.columns):
                return note
            best = best or note
    return best


_ANY_NOTE_HEADING = re.compile(r"^\s*[A-Z]?\d{1,2}\.?\s+[A-Z][A-Za-z]")


def _continuation_words(page) -> list[Word]:  # type: ignore[no-untyped-def]
    """The top of the next page, up to its first note heading - only when
    that page continues the EPS note rather than starting a new one."""
    rows = group_rows(words_from_page(page))
    out: list[Word] = []
    for row in rows:
        text = _fold(_row_text(row))
        if _ANY_NOTE_HEADING.match(text) and not _HEADING.match(text):
            break
        out.extend(row)
    return out


# --------------------------------------------------------------------------
# Aligning to the face and writing facts
# --------------------------------------------------------------------------


def _face_values(extracted, concept_key: str) -> dict[int, tuple[Decimal, str]]:  # type: ignore[no-untyped-def]
    """col_index -> (value, printed) for the statement's row of this concept."""
    row = next((r for r in extracted.rows if r.concept_key == concept_key), None)
    if row is None:
        return {}
    out = {}
    for col, text in row.values.items():
        number = parse_number(text)
        if number is not None:
            out[col] = (number, text)
    return out


def face_patami_by_year(extracted) -> dict[int, Decimal]:  # type: ignore[no-untyped-def]
    """Year -> PATAMI in RM, from the income statement's own consolidated columns."""
    values = _face_values(extracted, "is.pat_owners")
    out: dict[int, Decimal] = {}
    for col in extracted.columns:
        if col.is_note or col.year is None or col.col_index not in values:
            continue
        if col.basis is not None and col.basis != Basis.CONSOLIDATED:
            continue
        out.setdefault(col.year, values[col.col_index][0] * extracted.scale.multiplier)
    return out


def _same_printed_eps(a: Decimal, a_text: str, b: Decimal, b_text: str) -> bool:
    """Equal to the coarser of the two printed precisions."""
    decimals = min(_decimals(a_text), _decimals(b_text))
    return abs(a - b) <= Decimal(5) / Decimal(10) ** (decimals + 1) + Decimal("0.0001")


def align_to_statement(note: EpsNote, extracted) -> list[WeightedSharesValue]:  # type: ignore[no-untyped-def]
    """Match each note column to an income statement column - by year (and
    basis), else by an identical basic EPS figure - and grade it."""
    face_eps = _face_values(extracted, "is.eps_basic")
    out: list[WeightedSharesValue] = []
    used: set[int] = set()
    for nc in note.columns:
        if nc.weighted_shares is None:
            continue
        target = None
        for col in extracted.columns:
            if col.is_note or col.col_index in used or nc.year is None or col.year != nc.year:
                continue
            col_basis = col.basis or Basis.CONSOLIDATED
            if col_basis != (nc.basis or Basis.CONSOLIDATED):
                continue
            target = col
            break
        note_eps, note_text = nc.basic_eps_sen, nc.eps_printed or ""
        if target is None and note_eps is not None:
            matches = [
                col for col in extracted.columns
                if not col.is_note and col.col_index not in used and col.col_index in face_eps
                and _same_printed_eps(*face_eps[col.col_index], note_eps, note_text)
            ]
            target = matches[0] if len(matches) == 1 else None
        if target is None:
            continue
        used.add(target.col_index)
        face = face_eps.get(target.col_index)
        face_match = (
            face is not None and note_eps is not None and _same_printed_eps(*face, note_eps, note_text)
        )
        if face_match and nc.arithmetic_ok:
            confidence = 1.0
        elif face_match or nc.arithmetic_ok:
            confidence = 0.8
        else:
            confidence = 0.5
        out.append(WeightedSharesValue(
            is_col_index=target.col_index, shares=nc.weighted_shares,
            printed=nc.shares_printed or "", multiplier=nc.shares_multiplier or 1,
            confidence=confidence, note_eps_sen=note_eps, face_eps_sen=face[0] if face else None,
        ))
    return out


def write_weighted_shares_facts(
    session: Session, company_id: int, document_id: int, run_id: int, extracted,  # type: ignore[no-untyped-def]
    result=None,  # type: ignore[no-untyped-def]
) -> set[int]:
    """Write `is.weighted_avg_shares` for an income statement whose facts
    this run has already written. Each value lands on the same period and
    basis as the face facts from its aligned column, so no period logic is
    repeated here. Returns the fact ids touched (for stale-fact cleanup)."""
    note: EpsNote | None = getattr(extracted, "eps_note", None)
    if note is None:
        return set()
    touched: set[int] = set()
    for value in align_to_statement(note, extracted):
        anchor = session.execute(
            select(Fact.period_id, Fact.basis).where(
                Fact.reported_in_document_id == document_id,
                Fact.run_id == run_id,
                Fact.source_col_index == value.is_col_index,
                Fact.concept_key.like("is.%"),
                Fact.concept_key != "is.weighted_avg_shares",
            ).limit(1)
        ).first()
        if anchor is None:
            continue
        period_id, basis = anchor
        existing = session.execute(
            select(Fact).where(
                Fact.company_id == company_id,
                Fact.concept_key == "is.weighted_avg_shares",
                Fact.period_id == period_id,
                Fact.basis == basis,
                Fact.continuity == Continuity.TOTAL,
                Fact.reported_in_document_id == document_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.value = value.shares
            existing.value_as_printed = value.printed
            existing.scale_multiplier = value.multiplier
            existing.source_col_index = value.is_col_index
            existing.run_id = run_id
            existing.confidence = value.confidence
            if result is not None:
                result.facts_updated += 1
            touched.add(existing.id)
            continue
        fact = Fact(
            company_id=company_id, concept_key="is.weighted_avg_shares", period_id=period_id,
            value=value.shares, value_as_printed=value.printed, scale_multiplier=value.multiplier,
            basis=basis, continuity=Continuity.TOTAL, reported_in_document_id=document_id,
            source_col_index=value.is_col_index, run_id=run_id, confidence=value.confidence,
        )
        session.add(fact)
        session.flush()
        if result is not None:
            result.facts_written += 1
        touched.add(fact.id)
    return touched
