"""Compare extracted facts against yfinance figures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from bursa.benchmark.yfinance_fetch import YfinanceFigure

ZERO = Decimal(0)


@dataclass(frozen=True)
class BenchmarkOutcome:
    concept_key: str
    fiscal_year: int
    our_value: Decimal | None
    external_value: Decimal | None
    deviation_pct: float | None
    classification: str  # MATCH, CLOSE, MISMATCH, SCALE_ERROR, MISSING
    detail: str


def compare_facts(
    our_facts: dict[tuple[str, int], Decimal],
    yf_figures: list[YfinanceFigure],
    *,
    match_tolerance: float = 0.05,
    close_tolerance: float = 0.15,
) -> list[BenchmarkOutcome]:
    """Compare our extracted facts against yfinance figures.

    ``our_facts`` is keyed by ``(concept_key, fiscal_year)``.
    """
    yf_by_key: dict[tuple[str, int], YfinanceFigure] = {}
    for fig in yf_figures:
        fy = fig.period_end.year
        key = (fig.concept_key, fy)
        if key not in yf_by_key:
            yf_by_key[key] = fig

    outcomes: list[BenchmarkOutcome] = []
    seen: set[tuple[str, int]] = set()

    yf_years = {fig.period_end.year for fig in yf_figures}

    all_keys = set(our_facts.keys()) | set(yf_by_key.keys())

    for key in sorted(all_keys):
        concept_key, fy = key
        seen.add(key)

        ours = our_facts.get(key)
        theirs_fig = yf_by_key.get(key)
        theirs = theirs_fig.value if theirs_fig else None

        if ours is None and theirs is not None:
            outcomes.append(BenchmarkOutcome(
                concept_key=concept_key, fiscal_year=fy,
                our_value=None, external_value=theirs,
                deviation_pct=None, classification="MISSING",
                detail="not in our extraction",
            ))
            continue

        if theirs is None and ours is not None:
            if fy not in yf_years:
                continue
            outcomes.append(BenchmarkOutcome(
                concept_key=concept_key, fiscal_year=fy,
                our_value=ours, external_value=None,
                deviation_pct=None, classification="MISSING",
                detail="not in yfinance",
            ))
            continue

        if ours is None or theirs is None:
            continue

        if theirs == ZERO:
            classification = "MATCH" if ours == ZERO else "MISMATCH"
            outcomes.append(BenchmarkOutcome(
                concept_key=concept_key, fiscal_year=fy,
                our_value=ours, external_value=theirs,
                deviation_pct=None, classification=classification,
                detail="yfinance reports zero",
            ))
            continue

        deviation = abs(ours - theirs) / abs(theirs)
        dev_pct = float(deviation)

        scale_hint = _detect_scale_error(ours, theirs)
        if scale_hint:
            classification = "SCALE_ERROR"
            detail = scale_hint
        elif dev_pct <= match_tolerance:
            classification = "MATCH"
            detail = f"{dev_pct:.1%} deviation"
        elif dev_pct <= close_tolerance:
            classification = "CLOSE"
            detail = f"{dev_pct:.1%} deviation"
        else:
            classification = "MISMATCH"
            detail = f"{dev_pct:.1%} deviation"

        outcomes.append(BenchmarkOutcome(
            concept_key=concept_key, fiscal_year=fy,
            our_value=ours, external_value=theirs,
            deviation_pct=dev_pct, classification=classification,
            detail=detail,
        ))

    return outcomes


def _detect_scale_error(ours: Decimal, theirs: Decimal) -> str | None:
    if theirs == ZERO:
        return None
    ratio = ours / theirs
    for factor, label in [
        (Decimal(1000), "1000x"),
        (Decimal("0.001"), "1/1000x"),
        (Decimal(100), "100x"),
        (Decimal("0.01"), "1/100x"),
        (Decimal(10), "10x"),
        (Decimal("0.1"), "1/10x"),
    ]:
        if abs(ratio - factor) / factor < Decimal("0.05"):
            return f"looks like a {label} scale error (ratio={float(ratio):.2f})"
    return None
