"""Concept-review queue API (frontend/src/pages/Review.tsx).

    GET  /api/review?status=open|resolved|all&reason=&stock_code=&limit=&offset=
    POST /api/review/{id}/resolve
    GET  /api/concepts?statement=is|bs|cf

Mounted with ``prefix="/api"``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bursa.db.enums import Statement, SynonymOrigin
from bursa.db.models import Company, Concept, Document, ReviewItem
from bursa.db.session import get_session
from bursa.mapping.synonyms import record_correction
from bursa.pipeline.review import UNMAPPED_LABEL, build_suggestion_index, suggest_concepts

router = APIRouter()

_PREFIX_TO_STATEMENT = {"is": Statement.INCOME_STATEMENT, "bs": Statement.BALANCE_SHEET, "cf": Statement.CASH_FLOW}
_STATEMENT_TO_PREFIX = {v.value: k for k, v in _PREFIX_TO_STATEMENT.items()}


class ResolveRequest(BaseModel):
    action: Literal["map", "ignore", "reject"]
    concept_key: str | None = None
    add_synonym: bool = True
    company_scoped: bool = False
    resolved_by: str | None = None


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)  # SQLite func.now() is naive UTC
    return dt.isoformat()


def _detail(item: ReviewItem) -> dict:
    try:
        d = json.loads(item.detail or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _serialize(item: ReviewItem, doc: Document | None, company: Company | None, index: dict | None) -> dict:
    d = _detail(item)
    statement = _STATEMENT_TO_PREFIX.get(d.get("statement", ""))
    values = d.get("values") or {}
    cells = [{"col_index": int(k), "text": str(v)} for k, v in values.items()] if values else None
    suggestions = None
    if index is not None and item.reason == UNMAPPED_LABEL and d.get("label") and d.get("statement"):
        suggestions = suggest_concepts(d["label"], index.get(d["statement"], []))
    return {
        "id": item.id,
        "run_id": item.run_id,
        "document_id": item.document_id,
        "document_filename": doc.original_filename if doc else None,
        "stock_code": company.stock_code if company else None,
        "company_name": company.name if company else None,
        "raw_row_id": item.raw_row_id,
        "fact_id": item.fact_id,
        "reason": item.reason,
        "detail": item.detail,
        "severity": item.severity,
        "created_at": _iso(item.created_at),
        "resolved_at": _iso(item.resolved_at),
        "resolved_by": item.resolved_by,
        "resolution": item.resolution,
        "raw_label": d.get("label"),
        "page_no": d.get("page_no"),
        "statement": statement,
        "cells": cells,
        "suggestions": suggestions,
    }


def _base_query(status: str, stock_code: str | None):  # type: ignore[no-untyped-def]
    q = (
        select(ReviewItem, Document, Company)
        .join(Document, Document.id == ReviewItem.document_id)
        .outerjoin(Company, Company.id == Document.company_id)
    )
    if status == "open":
        q = q.where(ReviewItem.resolved_at.is_(None))
    elif status == "resolved":
        q = q.where(ReviewItem.resolved_at.is_not(None))
    if stock_code:
        q = q.where(Company.stock_code == stock_code)
    return q


@router.get("/review")
def list_review(
    status: Literal["open", "resolved", "all"] = "open",
    reason: str | None = None,
    stock_code: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> dict:
    base = _base_query(status, stock_code)
    # Reason chips count across the status/stock filter, not the reason one.
    reason_sub = base.with_only_columns(ReviewItem.reason).subquery()
    reasons = [
        {"reason": r, "count": n}
        for r, n in session.execute(
            select(reason_sub.c.reason, func.count()).group_by(reason_sub.c.reason).order_by(func.count().desc())
        )
    ]
    if reason:
        base = base.where(ReviewItem.reason == reason)
    total = session.scalar(select(func.count()).select_from(base.with_only_columns(ReviewItem.id).subquery())) or 0
    rows = session.execute(
        base.order_by(ReviewItem.severity.desc(), ReviewItem.id).limit(limit).offset(offset)
    ).all()
    index = build_suggestion_index(session) if rows else None
    return {
        "total": total,
        "items": [_serialize(item, doc, company, index) for item, doc, company in rows],
        "reasons": reasons,
    }


@router.post("/review/{item_id}/resolve")
def resolve_review(item_id: int, body: ResolveRequest, session: Session = Depends(get_session)) -> dict:
    item = session.get(ReviewItem, item_id)
    if item is None:
        raise HTTPException(404, f"review item {item_id} not found")
    doc = session.get(Document, item.document_id)
    company = session.get(Company, doc.company_id) if doc and doc.company_id else None
    d = _detail(item)

    resolution: dict = {"action": body.action}
    if body.action == "map":
        if not body.concept_key:
            raise HTTPException(422, "concept_key is required when action is 'map'")
        concept = session.get(Concept, body.concept_key)
        if concept is None:
            raise HTTPException(422, f"unknown concept_key {body.concept_key!r}")
        resolution["concept_key"] = concept.concept_key
        stmt_value = d.get("statement")
        if stmt_value and str(concept.statement) != stmt_value:
            raise HTTPException(
                422, f"{concept.concept_key} is a {concept.statement} concept; this row is on {stmt_value}"
            )
        if body.add_synonym and d.get("label"):
            scope_id = company.id if (body.company_scoped and company) else None
            syn = record_correction(
                session, d["label"], Statement(concept.statement), concept.concept_key,
                company_id=scope_id, origin=SynonymOrigin.REVIEWER,
            )
            resolution["synonym"] = {
                "normalized": syn.normalized if syn else None,
                "company_scoped": scope_id is not None,
            }
    elif body.action == "reject":
        resolution["note"] = "not a line item"

    item.resolved_at = datetime.now(UTC)
    item.resolved_by = body.resolved_by or "reviewer"
    item.resolution = resolution
    session.commit()
    session.refresh(item)
    return _serialize(item, doc, company, build_suggestion_index(session))


@router.get("/concepts")
def list_concepts(statement: str | None = None, session: Session = Depends(get_session)) -> list[dict]:
    q = select(Concept).order_by(Concept.statement, Concept.sort_order, Concept.concept_key)
    if statement:
        st = _PREFIX_TO_STATEMENT.get(statement.lower())
        if st is None:
            try:
                st = Statement(statement.upper())
            except ValueError as e:
                raise HTTPException(422, f"unknown statement {statement!r}") from e
        q = q.where(Concept.statement == st)
    return [
        {
            "concept_key": c.concept_key,
            "statement": _STATEMENT_TO_PREFIX.get(str(c.statement), str(c.statement).lower()),
            "label": c.label,
        }
        for c in session.scalars(q)
    ]
