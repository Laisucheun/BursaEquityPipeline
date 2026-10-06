"""The mapper's contract: labels in, concepts out, never a figure either way."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from sqlalchemy.orm import Session

from bursa.db.enums import Statement
from bursa.extract.layout import extract_page
from bursa.mapping.llm_mapper import (
    FigureLeakError,
    assert_no_figures,
    build_system_prompt,
    build_user_payload,
    map_table,
    resolve_table,
    to_enum_columns,
)
from bursa.mapping.synonyms import seed_concepts
from tests.fixtures.synthetic import QUARTERLY_INCOME_STATEMENT, build_statement_pdf


@pytest.fixture
def income_table(tmp_path: Path):
    pdf = build_statement_pdf(tmp_path / "is.pdf", QUARTERLY_INCOME_STATEMENT)
    with pymupdf.open(pdf) as doc:
        return extract_page(doc[0], 1, Statement.INCOME_STATEMENT)


def index_of(table, label: str) -> int:
    for row in table.rows:
        if row.label.lower() == label.lower():
            return row.row_index
    raise AssertionError(f"no row labelled {label!r}")


class FakeClient:
    """Records the request and returns a canned structured response."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(self.payload))],
            usage=SimpleNamespace(input_tokens=1200, output_tokens=300),
            stop_reason="end_turn",
        )


# --------------------------------------------------------------------------
# The invariant
# --------------------------------------------------------------------------


def test_figure_guard_rejects_money() -> None:
    with pytest.raises(FigureLeakError):
        assert_no_figures("Revenue | 125,430")
    with pytest.raises(FigureLeakError):
        assert_no_figures("Cost of sales (92,318)")


def test_figure_guard_allows_dates_years_and_indices() -> None:
    assert_no_figures("0 | 2 | Revenue | [0,1,2,3]")
    assert_no_figures("30.09.2024 30.09.2023 2024-09-30")
    assert_no_figures("For the year ended 2024")


def test_user_payload_contains_no_figures(income_table) -> None:
    payload = build_user_payload(income_table)

    # The guard runs inside build_user_payload; assert the outcome directly too.
    for figure in ("125,430", "125430", "(92,318)", "33,112", "2.45"):
        assert figure not in payload, f"{figure} leaked into the prompt"

    # ...while everything the model actually needs is present.
    assert "Revenue" in payload
    assert "Cost of sales" in payload
    assert "30.09.2024" in payload
    assert "NUMERIC COLUMNS: 4" in payload


def test_payload_tells_the_model_which_columns_a_row_uses(income_table) -> None:
    payload = build_user_payload(income_table)
    revenue_line = next(
        ln for ln in payload.splitlines() if re.match(r"^\d+ \| \d+ \| Revenue \|", ln)
    )
    assert revenue_line.strip().endswith("[0,1,2,3]")


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------


def test_system_prompt_is_byte_stable_for_caching() -> None:
    # If this ever varies between calls the prompt cache never hits.
    assert build_system_prompt(Statement.INCOME_STATEMENT) == build_system_prompt(
        Statement.INCOME_STATEMENT
    )


def test_system_prompt_carries_only_the_relevant_vocabulary() -> None:
    prompt = build_system_prompt(Statement.BALANCE_SHEET)
    assert "bs.total_assets" in prompt
    assert "is.revenue" not in prompt
    assert "cf.net_operating" not in prompt


def test_system_prompt_is_marked_cacheable(income_table) -> None:
    client = FakeClient({"columns": [], "rows": []})
    map_table(income_table, Statement.INCOME_STATEMENT, client=client, model="test-model")

    system = client.requests[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    # This filing's volatile content must sit after the cached prefix, not in it.
    assert "30.09.2024" not in system[0]["text"]
    assert "30.09.2024" in client.requests[0]["messages"][0]["content"]


# --------------------------------------------------------------------------
# Resolution: synonyms first, model for the remainder
# --------------------------------------------------------------------------


def test_known_labels_never_reach_the_model(session: Session, income_table) -> None:
    seed_concepts(session)
    session.commit()

    client = FakeClient({"columns": [], "rows": []})
    resolved = resolve_table(
        session, income_table, Statement.INCOME_STATEMENT, client=client
    )

    assert resolved.synonym_hits > 0
    assert resolved.row_concepts[index_of(income_table, "Revenue")][0] == "is.revenue"

    # Only the rows the synonym table could not place were sent.
    sent = client.requests[0]["messages"][0]["content"]
    assert "Revenue |" not in sent
    assert resolved.llm_rows < len(income_table.rows)


def test_column_assignments_are_typed(session: Session, income_table) -> None:
    seed_concepts(session)
    session.commit()

    client = FakeClient(
        {
            "columns": [
                {
                    "col_index": 0,
                    "period_end": "2024-09-30",
                    "period_type": "Q3",
                    "basis": "CONSOLIDATED",
                    "continuity": "TOTAL",
                    "confidence": 0.97,
                },
                {
                    "col_index": 2,
                    "period_end": "2024-09-30",
                    "period_type": "YTD",
                    "basis": "CONSOLIDATED",
                    "continuity": "TOTAL",
                    "confidence": 0.95,
                },
            ],
            "rows": [],
        }
    )
    resolved = resolve_table(session, income_table, Statement.INCOME_STATEMENT, client=client)

    columns = to_enum_columns(resolved.columns)
    assert columns[0][1].value == "Q3"
    assert columns[2][1].value == "YTD"


def test_a_concept_outside_the_vocabulary_is_sent_to_review(
    session: Session, income_table
) -> None:
    """A hallucinated key must never be written as if it were real."""
    seed_concepts(session)
    session.commit()

    row_index = index_of(income_table, "Attributable to:")
    client = FakeClient(
        {
            "columns": [],
            "rows": [
                {"row_index": row_index, "concept_key": "is.made_up_concept", "confidence": 0.99}
            ],
        }
    )
    resolved = resolve_table(session, income_table, Statement.INCOME_STATEMENT, client=client)

    assert resolved.row_concepts[row_index] == (None, 0.0)


def test_offline_mode_resolves_what_it_can_without_calling_out(
    session: Session, income_table
) -> None:
    seed_concepts(session)
    session.commit()

    resolved = resolve_table(session, income_table, Statement.INCOME_STATEMENT, use_llm=False)

    assert resolved.llm_calls == 0
    assert resolved.synonym_hits > 0
    assert resolved.row_concepts[index_of(income_table, "Revenue")][0] == "is.revenue"
    assert resolved.row_concepts[index_of(income_table, "Cost of sales")][0] == "is.cost_of_sales"
