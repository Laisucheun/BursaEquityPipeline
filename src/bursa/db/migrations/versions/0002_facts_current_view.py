"""facts_current view: the most recently filed value per fact identity.

One row per (company_id, concept_key, period_id, basis, continuity). Among
the facts sharing that identity - the same figure as reported by different
documents, e.g. FY2023 as originally filed and as restated in the FY2024
report - the winner is the one whose reporting document has the latest
``filed_date`` (NULL filed dates rank lowest), then the latest
``period_end_hint`` (NULLs lowest), then the highest document id.

NULL ordering is spelled out with CASE rather than ``NULLS LAST`` so the
same SQL runs on SQLite and Postgres (which disagree on the default).

Revision ID: 0002_facts_current
Revises: 0001_baseline
Create Date: 2026-10-07

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_facts_current"
down_revision: str | Sequence[str] | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FACT_COLUMNS = (
    "id",
    "company_id",
    "concept_key",
    "period_id",
    "value",
    "currency",
    "value_as_printed",
    "scale_multiplier",
    "basis",
    "continuity",
    "reported_in_document_id",
    "source_raw_row_id",
    "source_col_index",
    "run_id",
    "confidence",
    "review_status",
    "created_at",
    "updated_at",
)

_inner_cols = ",\n        ".join(f"f.{c}" for c in FACT_COLUMNS)
_outer_cols = ",\n    ".join(f"r.{c}" for c in FACT_COLUMNS)

CREATE_VIEW = f"""
CREATE VIEW facts_current AS
SELECT
    {_outer_cols},
    r.filed_date
FROM (
    SELECT
        {_inner_cols},
        d.filed_date,
        ROW_NUMBER() OVER (
            PARTITION BY f.company_id, f.concept_key, f.period_id, f.basis, f.continuity
            ORDER BY
                CASE WHEN d.filed_date IS NULL THEN 0 ELSE 1 END DESC,
                d.filed_date DESC,
                CASE WHEN d.period_end_hint IS NULL THEN 0 ELSE 1 END DESC,
                d.period_end_hint DESC,
                d.id DESC
        ) AS rn
    FROM facts f
    JOIN documents d ON d.id = f.reported_in_document_id
) r
WHERE r.rn = 1
"""


def upgrade() -> None:
    op.execute(CREATE_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS facts_current")
