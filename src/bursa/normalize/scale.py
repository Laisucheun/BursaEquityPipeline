"""Detecting the multiplier a statement is printed in.

Getting this wrong is a 1000x error on every figure in the table, so it is
resolved once per table from the header text and stored on ``StatementTable``
rather than guessed per row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class ScaleInfo:
    multiplier: int
    token: str | None
    currency: str


# Ordered most-specific first: "RM'000,000" must not match the "'000" rule.
_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"(rm|myr)?\s*'?\s*0{3}\s*,?\s*0{3}", re.IGNORECASE), 1_000_000),
    (re.compile(r"\b(billion|bil\b|b'?\s*n)\b", re.IGNORECASE), 1_000_000_000),
    (re.compile(r"\b(million|mil\b|mn\b|m'?\s*n)\b", re.IGNORECASE), 1_000_000),
    (re.compile(r"(rm|myr)?\s*'\s*0{3}", re.IGNORECASE), 1_000),
    (re.compile(r"\b(thousand|thousands|'?000s?)\b", re.IGNORECASE), 1_000),
)

_CURRENCIES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(rm|myr|ringgit)\b", re.IGNORECASE), "MYR"),
    (re.compile(r"\b(usd|us\$|dollar)\b", re.IGNORECASE), "USD"),
    (re.compile(r"\b(sgd|s\$)\b", re.IGNORECASE), "SGD"),
)

# Per-share and ratio figures are printed in their own units regardless of the
# table's scale.
_PER_SHARE = re.compile(r"\b(sen|cents?|per\s+share)\b", re.IGNORECASE)


def detect_scale(*texts: str | None, default_currency: str = "MYR") -> ScaleInfo:
    """Read the scale from header/caption text.

    Pass everything that could carry it - the table caption, the column header
    band, the page heading. First match wins, most specific first.

        >>> detect_scale("Unaudited ... (RM'000)").multiplier
        1000
        >>> detect_scale("RM million").multiplier
        1000000
        >>> detect_scale("Statement of Financial Position").multiplier
        1
    """
    blob = " ".join(_WS.sub(" ", t) for t in texts if t)

    currency = default_currency
    for pattern, code in _CURRENCIES:
        if pattern.search(blob):
            currency = code
            break

    for pattern, multiplier in _PATTERNS:
        match = pattern.search(blob)
        if match:
            return ScaleInfo(multiplier=multiplier, token=match.group(0).strip(), currency=currency)

    return ScaleInfo(multiplier=1, token=None, currency=currency)


def mentions_per_share(text: str | None) -> bool:
    """Whether a header band marks its column as per-share units."""
    return bool(text) and bool(_PER_SHARE.search(text or ""))
