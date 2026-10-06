"""Parsing printed figures into exact decimals.

Everything here is deliberately strict and lossless. ``Decimal`` throughout -
never ``float`` - because these values are money and rounding drift would show
up as spurious validation failures.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# Markers issuers use for "nothing here": en/em dashes, hyphens, "nil", "n/a".
_NIL_TOKENS = frozenset({"-", "--", "–", "—", "nil", "n/a", "na", "n.a.", ""})

# Footnote marks and currency symbols that ride along with the figure.
_STRIP_CHARS = re.compile(r"[*†‡#^~ \s]|rm|myr|sen", re.IGNORECASE)
_NUMERIC = re.compile(r"^[+-]?\d+(\.\d+)?$")


class NumberParseError(ValueError):
    pass


def parse_number(text: str | None) -> Decimal | None:
    """Parse one printed cell.

    Returns ``None`` for blank/nil cells and for anything that is not a number
    (a stray label, a note reference). Raises nothing - an unparseable cell is
    simply not a figure, and the caller decides whether that matters.

        >>> parse_number("1,234")
        Decimal('1234')
        >>> parse_number("(1,234.50)")
        Decimal('-1234.50')
        >>> parse_number("-")          # nil
        >>> parse_number("12.3%")
        Decimal('12.3')
    """
    if text is None:
        return None

    s = str(text).strip()
    if s.lower() in _NIL_TOKENS:
        return None

    # Accounting negatives. Detect before stripping punctuation, and accept a
    # trailing-only bracket - OCR drops the opening one surprisingly often.
    negative = False
    if ("(" in s and ")" in s) or s.endswith(")") or s.startswith("("):
        negative = True

    s = s.replace("(", "").replace(")", "")
    s = s.rstrip("%")
    s = _STRIP_CHARS.sub("", s)
    s = s.replace(",", "")

    if s.lower() in _NIL_TOKENS:
        return None

    # A trailing minus ("1,234-") is another accounting negative form.
    if s.endswith("-"):
        negative = True
        s = s[:-1]
    if s.startswith("-"):
        negative = True
        s = s[1:]
    if s.startswith("+"):
        s = s[1:]

    if not _NUMERIC.match(s):
        return None

    try:
        value = Decimal(s)
    except InvalidOperation:
        return None

    return -value if negative else value


def looks_numeric(text: str | None) -> bool:
    """Whether a cell is a figure (or an explicit nil), rather than prose.

    Used to tell a label column from a value column.
    """
    if text is None:
        return False
    s = str(text).strip()
    if s.lower() in _NIL_TOKENS:
        return True
    return parse_number(s) is not None


def to_base_units(value: Decimal, scale_multiplier: int, is_per_share: bool) -> Decimal:
    """Apply the document's scale.

    Per-share figures and ratios are printed in their own units and must never
    be multiplied by the RM'000 factor - a silent source of 1000x errors.
    """
    if is_per_share or scale_multiplier == 1:
        return value
    return value * Decimal(scale_multiplier)
