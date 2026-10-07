"""Peer comparison and sector analysis endpoints (offline, facts only)."""

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from bursa.db.session import session_scope

router = APIRouter()


@router.get("/sectors")
def sectors():
    from bursa.analysis.peers import list_sectors

    with session_scope() as session:
        return [
            {"sector": s, "companies": total, "with_facts": covered}
            for s, total, covered in list_sectors(session)
        ]


@router.get("/sector/{sector}")
def sector_comparison(sector: str, fy: int | None = None):
    from bursa.analysis.peers import compare_sector

    with session_scope() as session:
        result = compare_sector(session, sector, fy)
    if not result.rows and not result.missing:
        raise HTTPException(404, f"No companies with facts in sector {sector}")
    return asdict(result)


@router.get("/{stock_code}")
def peer_comparison(
    stock_code: str,
    peers: Annotated[
        list[str] | None, Query(description="Peer stock codes (default: sector peers)"),
    ] = None,
    fy: int | None = None,
):
    from bursa.analysis.peers import compare_peers

    with session_scope() as session:
        try:
            return asdict(compare_peers(session, stock_code, peers, fy))
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
