"""Tidy long-format CSV: one row per (company, fiscal year, concept)."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from bursa.analysis.facts import load_annual_facts
from bursa.db.models import Company
from bursa.export._common import concept_catalog, concept_info, concept_sort_key

COLUMNS = ["stock_code", "name", "fiscal_year", "period_end", "concept_key",
           "label", "statement", "value"]


def _plain(value: Decimal) -> str:
    """1500000.0000 -> '1500000', 12.50 -> '12.5' (never scientific notation)."""
    return format(Decimal(value).normalize(), "f")


def facts_long_csv(session: Session, companies: Iterable[Company], path: str | Path) -> int:
    """Write the CSV and return the number of data rows.

    Values are full RM base units (per-share concepts as stored, e.g. EPS in
    sen) - no scaling, so the file round-trips without loss.
    """
    catalog = concept_catalog(session)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(COLUMNS)
        for company in companies:
            for fy, f in load_annual_facts(session, company).items():
                infos = sorted((concept_info(catalog, k) for k in f.values), key=concept_sort_key)
                for info in infos:
                    writer.writerow([
                        company.stock_code, company.name, fy, f.period_end.isoformat(),
                        info.key, info.label, info.statement, _plain(f.values[info.key]),
                    ])
                    n += 1
    return n
