from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from bursa.analysis.brief import brief_calendar
from bursa.briefs.schema import BriefContent, NewsBrief, brief_dates, load_brief, save_brief
from bursa.config import get_settings
from bursa.db.models import Base
from bursa.db.session import get_engine, get_sessionmaker, session_scope
from tests.test_feed import Builder, yr

BIG = 100_000_000


def sample(day: date, title: str = "Bursa Morning Brief") -> NewsBrief:
    content = BriefContent.model_validate({
        "title": title,
        "tldr": ["**Ringgit firmer.** USD/MYR 4.21.", "**KLCI up.** +0.4%.", "**CPO steady.**"],
        "markets": {"as_of": "Thu close",
                    "rows": [{"asset": "FBM KLCI", "level": "1,650.2", "change": "+0.4%"}],
                    "comment": ["Foreign buying."]},
        "sections": [{"heading": "Economic data", "bullets": ["CPI 1.9% y/y."],
                      "table": {"columns": ["Release", "Actual"], "rows": [["CPI", "1.9%"]]}}],
        "corporate": [{"company": "Malayan Banking Berhad", "stock_code": "1155",
                       "headline": "Q3 net profit up 5%", "detail": "Beat consensus.",
                       "source_url": "https://example.com/maybank"}],
        "watch": ["BNM MPC decision Thursday."],
        "sources": [{"title": "The Edge", "url": "https://example.com"}],
    })
    return NewsBrief(**content.model_dump(), date=day, generated_by="test",
                     generated_at=datetime(2026, 10, 23, 7, 30, tzinfo=UTC))


# ------------------------------------------------------------------ format
def test_save_load_round_trip_and_dates(tmp_path: Path) -> None:
    for d in (date(2026, 10, 21), date(2026, 10, 23)):
        save_brief(tmp_path, sample(d))
    (tmp_path / "notes.json").write_text("{}")  # not a date: ignored
    assert brief_dates(tmp_path) == [date(2026, 10, 23), date(2026, 10, 21)]
    loaded = load_brief(tmp_path / "2026-10-23.json")
    assert loaded == sample(date(2026, 10, 23))


def test_load_rejects_bad_files(tmp_path: Path) -> None:
    path = save_brief(tmp_path, sample(date(2026, 10, 23)))
    renamed = path.rename(tmp_path / "2026-10-22.json")
    with pytest.raises(ValueError, match="does not match"):
        load_brief(renamed)
    with pytest.raises(ValidationError):
        BriefContent.model_validate({**sample(date(2026, 10, 23)).model_dump(
            exclude={"date", "generated_by", "generated_at"}), "pipeline": "nope"})


def test_usage_sums_across_continuations() -> None:
    from types import SimpleNamespace as NS

    from bursa.briefs.generate import BriefUsage

    u = BriefUsage()
    u.add(NS(input_tokens=100, output_tokens=10, cache_creation_input_tokens=50,
             cache_read_input_tokens=None,
             server_tool_use=NS(web_search_requests=3, web_fetch_requests=1)))
    u.add(NS(input_tokens=20, output_tokens=5, cache_creation_input_tokens=None,
             cache_read_input_tokens=400, server_tool_use=None))
    assert (u.requests, u.input_tokens, u.cache_write_tokens, u.cache_read_tokens,
            u.output_tokens, u.web_searches, u.web_fetches) == (2, 120, 50, 400, 15, 3, 1)


# ---------------------------------------------------------------- calendar
def test_calendar_annual_reports_and_watch(session: Session) -> None:
    b = Builder(session)
    b.company("0024", {date(2025, 6, 30): yr(5 * BIG, BIG / 2)}, fye=6)
    b.company("0025", {date(2024, 12, 31): yr(9 * BIG, BIG), date(2025, 12, 31): yr(9 * BIG, BIG)})

    cal = brief_calendar(session, date(2026, 10, 22))

    assert [(d.due, d.stock_code, d.kind) for d in cal.annual_reports] == [
        (date(2026, 10, 31), "0024", "annual"),
    ]
    # Its next quarterly (Q1 to 30 Sep) is due 30 Nov - outside the 14-day window.
    assert [(d.stock_code, d.kind) for d in cal.calendar] == [("0024", "annual")]
    assert cal.watch == ["**31 Oct filing deadline:** 1 annual report due; largest: Co 0024."]


# ------------------------------------------------------------------- routes
@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'test.sqlite'}")
    monkeypatch.setenv("BRIEFS_DIR", str(tmp_path / "briefs"))
    for cache in (get_settings, get_engine, get_sessionmaker):
        cache.cache_clear()
    Base.metadata.create_all(get_engine())
    with session_scope() as s:
        Builder(s).company("0024", {date(2025, 6, 30): yr(5 * BIG, BIG / 2)}, fye=6)
    from bursa.api.routes import feed

    app = FastAPI()
    app.include_router(feed.router, prefix="/api/feed")
    yield TestClient(app)
    get_engine().dispose()
    for cache in (get_settings, get_engine, get_sessionmaker):
        cache.cache_clear()


def test_brief_routes(client: TestClient, tmp_path: Path) -> None:
    briefs = tmp_path / "briefs"
    save_brief(briefs, sample(date(2026, 10, 22), "Brief 22"))
    save_brief(briefs, sample(date(2026, 10, 23), "Brief 23"))
    (briefs / "2026-10-20.json").write_text('{"title": "broken"}')

    listed = client.get("/api/feed/briefs").json()
    assert [(b["date"], b["title"], b["valid"]) for b in listed] == [
        ("2026-10-23", "Brief 23", True),
        ("2026-10-22", "Brief 22", True),
        ("2026-10-20", None, False),
    ]

    latest = client.get("/api/feed/brief").json()
    assert latest["date"] == "2026-10-23" and latest["news"]["title"] == "Brief 23"
    assert (latest["previous_date"], latest["next_date"]) == ("2026-10-22", None)
    assert "annual_reports" in latest["calendar"]

    missing = client.get("/api/feed/brief", params={"date": "2026-10-21"}).json()
    assert missing["news"] is None and missing["news_error"] is None
    assert [d["stock_code"] for d in missing["calendar"]["annual_reports"]] == ["0024"]

    broken = client.get("/api/feed/brief", params={"date": "2026-10-20"}).json()
    assert broken["news"] is None and "not a valid brief" in broken["news_error"]
