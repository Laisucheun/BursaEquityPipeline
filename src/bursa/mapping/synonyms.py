"""Label normalisation and the deterministic concept lookup.

This is the pre-pass that runs before the LLM mapper. A normalised exact match
resolves a row for free; anything else falls through to the model.

The matching is deliberately *strict* - exact match on the normalised form,
no fuzzy scoring. A wrong-but-confident mapping writes a bad number into the
fact table silently, which is far more expensive than one extra LLM call. Fuzzy
judgement is what the model is for.
"""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from bursa.db.enums import Statement, SynonymOrigin
from bursa.db.models import Concept, ConceptSynonym
from bursa.mapping.taxonomy import ALL_CONCEPTS

# "profit/(loss) before tax" -> "profit before tax". Only strips a parenthesised
# group introduced by a slash, so "(sen)" and "(RM'000)" survive for the
# separate rules below to handle.
_SLASH_PAREN = re.compile(r"/\s*\([^)]*\)")
# The same accounting convention, reversed order: "net cash (used in)/from
# financing activities" - confirmed real on a REIT's cash flow statement,
# silently never matching the seeded "net cash from/(used in) financing
# activities" synonym because _SLASH_PAREN above only strips "slash-then-
# paren", never "paren-then-slash". Not industry-specific - any issuer that
# phrases the parenthetical alternative *before* the slash hits this.
_PAREN_SLASH = re.compile(r"\([^)]*\)\s*/\s*")
# Trailing note references: "Revenue (Note 3)", "Revenue - note 3", "Revenue 12".
_NOTE_REF = re.compile(r"[\s\-–—]*\(?\s*note[s]?\.?\s*[\dA-Za-z().,\s]*\)?\s*$", re.IGNORECASE)
_TRAILING_NUM = re.compile(r"\s+\d{1,3}$")
# Leading bullets, dashes, and outline markers: "- Revenue", "(a) Revenue".
_LEADING_MARKER = re.compile(
    r"^[\s\-–—•*·]*(\([a-z0-9]{1,3}\)|[a-z0-9]{1,2}[.)])?\s*", re.IGNORECASE
)
_PUNCT = re.compile(r"[^a-z0-9%' ]+")
_WS = re.compile(r"\s+")

# Phrasings that vary freely and carry no meaning for identification.
_STOP_PHRASES = (
    "for the financial period",
    "for the financial year",
    "for the period",
    "for the year",
    "net of tax",
    "attributable to",
    "of the group",
    "continuing operations",
)


def normalize_label(text: str) -> str:
    """Fold a printed row label into its lookup key.

    Must be used identically when seeding and when looking up - the two are the
    same function precisely so they cannot drift apart.
    """
    if not text:
        return ""

    s = unicodedata.normalize("NFKD", text)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("’", "'").replace("‘", "'")
    s = s.replace("–", "-").replace("—", "-")
    s = s.lower()

    s = _NOTE_REF.sub("", s)
    s = _SLASH_PAREN.sub("", s)
    s = _PAREN_SLASH.sub("", s)
    s = _LEADING_MARKER.sub("", s)

    # Dotted leaders used to run a label out to its column.
    s = re.sub(r"\.{2,}", " ", s)

    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    s = _TRAILING_NUM.sub("", s).strip()

    for phrase in _STOP_PHRASES:
        s = s.replace(phrase, " ")

    return _WS.sub(" ", s).strip()


def seed_concepts(session: Session) -> tuple[int, int]:
    """Insert the taxonomy and its seed synonyms. Idempotent.

    Returns ``(concepts_written, synonyms_written)``.
    """
    existing_concepts = {k for (k,) in session.execute(select(Concept.concept_key))}
    concepts_written = 0

    for order, spec in enumerate(ALL_CONCEPTS):
        if spec.key in existing_concepts:
            continue
        session.add(
            Concept(
                concept_key=spec.key,
                statement=spec.statement,
                label=spec.label,
                parent_key=spec.parent,
                is_subtotal=spec.is_subtotal,
                typical_sign=spec.typical_sign,
                is_instant=spec.is_instant,
                is_per_share=spec.is_per_share,
                sort_order=order,
                description=spec.description,
            )
        )
        concepts_written += 1
    session.flush()

    existing_syn = {
        (norm, stmt)
        for norm, stmt in session.execute(
            select(ConceptSynonym.normalized, ConceptSynonym.statement).where(
                ConceptSynonym.company_id.is_(None)
            )
        )
    }
    synonyms_written = 0
    batch: dict[tuple[str, str], str] = {}

    for spec in ALL_CONCEPTS:
        # The concept's own label is always a synonym for itself.
        for raw in (spec.label, *spec.synonyms):
            norm = normalize_label(raw)
            if not norm:
                continue
            key = (norm, str(spec.statement))
            if key in existing_syn:
                continue
            if key in batch and batch[key] != spec.key:
                raise ValueError(
                    f"seed synonym {norm!r} is ambiguous within {spec.statement}: "
                    f"{batch[key]} vs {spec.key}"
                )
            if key in batch:
                continue
            batch[key] = spec.key
            session.add(
                ConceptSynonym(
                    concept_key=spec.key,
                    statement=spec.statement,
                    pattern=raw,
                    normalized=norm,
                    lang="ms" if _looks_malay(raw) else "en",
                    origin=SynonymOrigin.SEED,
                    company_id=None,
                )
            )
            synonyms_written += 1

    session.flush()
    return concepts_written, synonyms_written


_MALAY_MARKERS = frozenset(
    {"hasil", "untung", "kos", "jumlah", "aset", "liabiliti", "ekuiti", "tunai",
     "belanja", "cukai", "modal", "saham", "bersih", "kasar", "semasa", "bukan",
     "inventori", "penghutang", "pemiutang", "keuntungan", "tertahan", "baki",
     "pendapatan", "pentadbiran", "jualan", "operasi", "sebelum", "hartanah",
     "loji", "peralatan", "dan", "lain"}
)


def _looks_malay(text: str) -> bool:
    tokens = set(normalize_label(text).split())
    return bool(tokens) and len(tokens & _MALAY_MARKERS) >= max(1, len(tokens) // 2)


def lookup(
    session: Session,
    label: str,
    statement: Statement,
    company_id: int | None = None,
) -> str | None:
    """Resolve a printed label to a concept key without calling the LLM.

    A company-specific synonym wins over a global one, so an issuer's private
    wording can be corrected once in the review UI and never asked about again.
    """
    norm = normalize_label(label)
    if not norm:
        return None

    from sqlalchemy import or_

    company_filter = (
        or_(ConceptSynonym.company_id == company_id, ConceptSynonym.company_id.is_(None))
        if company_id is not None
        else ConceptSynonym.company_id.is_(None)
    )
    stmt = (
        select(ConceptSynonym.concept_key, ConceptSynonym.company_id)
        .where(
            ConceptSynonym.normalized == norm,
            ConceptSynonym.statement == statement,
            company_filter,
        )
        # NULLs last: the company-specific row, when present, comes first.
        .order_by(ConceptSynonym.company_id.is_(None))
    )
    row = session.execute(stmt).first()
    return row[0] if row else None


def record_correction(
    session: Session,
    label: str,
    statement: Statement,
    concept_key: str,
    company_id: int | None = None,
    origin: SynonymOrigin = SynonymOrigin.REVIEWER,
) -> ConceptSynonym | None:
    """Promote a reviewer's fix into a reusable synonym.

    This is the flywheel: each correction removes one future LLM call and makes
    the next filing from that issuer resolve deterministically.
    """
    norm = normalize_label(label)
    if not norm:
        return None

    existing = session.execute(
        select(ConceptSynonym).where(
            ConceptSynonym.normalized == norm,
            ConceptSynonym.statement == statement,
            ConceptSynonym.company_id.is_(company_id)
            if company_id is None
            else ConceptSynonym.company_id == company_id,
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.concept_key = concept_key
        existing.origin = origin
        return existing

    syn = ConceptSynonym(
        concept_key=concept_key,
        statement=statement,
        pattern=label.strip()[:300],
        normalized=norm,
        lang="ms" if _looks_malay(label) else "en",
        origin=origin,
        company_id=company_id,
    )
    session.add(syn)
    return syn
