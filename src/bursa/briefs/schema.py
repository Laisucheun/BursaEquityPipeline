"""The brief file format, shared by every generator and the app.

``BriefContent`` is exactly what the researching model writes (it is also the
structured-output schema sent to the API); ``NewsBrief`` adds the date and
provenance stored alongside it. Bullets are plain text with optional
``[label](https://...)`` links and ``**bold**`` - nothing else is rendered.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

MYT = timezone(timedelta(hours=8))


def today_myt() -> date:
    return datetime.now(MYT).date()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarketRow(_Strict):
    asset: str = Field(description="e.g. FBM KLCI, USD/MYR, Brent, CPO 3rd month, MGS 10Y")
    level: str = Field(description="Closing level or rate, as text with units")
    change: str = Field(description="Change vs prior close, e.g. +0.42% or -3 bp")


class Markets(_Strict):
    as_of: str = Field(description="What the levels are, e.g. 'Thu 22 Oct close'")
    rows: list[MarketRow]
    comment: list[str] = Field(description="1-3 bullets on what drove the moves")


class Table(_Strict):
    columns: list[str]
    rows: list[list[str]]


class Section(_Strict):
    heading: str = Field(description="e.g. What moved markets, Economic data, BNM and policy")
    bullets: list[str]
    table: Table | None = None


class CorporateItem(_Strict):
    company: str
    stock_code: str | None = Field(description="4-digit Bursa code when known, else null")
    headline: str = Field(description="One line: what happened")
    detail: str = Field(description="1-3 sentences: numbers, context, why it matters")
    source_url: str | None


class Source(_Strict):
    title: str
    url: str


class BriefContent(_Strict):
    title: str = Field(description="e.g. 'Bursa Morning Brief: Friday, 23 October 2026'")
    tldr: list[str] = Field(description="3-6 bullets, most important first")
    markets: Markets
    sections: list[Section]
    corporate: list[CorporateItem] = Field(description="Top Bursa corporate news, 5-12 items")
    watch: list[str] = Field(description="Scheduled events and catalysts for today and the week")
    sources: list[Source]


class NewsBrief(BriefContent):
    date: date
    generated_by: str
    generated_at: datetime


def brief_path(briefs_dir: Path, day: date) -> Path:
    return Path(briefs_dir) / f"{day.isoformat()}.json"


def load_brief(path: Path) -> NewsBrief:
    """Parse and validate one brief file; raises ``pydantic.ValidationError``."""
    brief = NewsBrief.model_validate_json(Path(path).read_text(encoding="utf-8"))
    if path.stem != brief.date.isoformat():
        raise ValueError(f"{path.name}: file name does not match its date {brief.date}")
    return brief


def save_brief(briefs_dir: Path, brief: NewsBrief) -> Path:
    path = brief_path(briefs_dir, brief.date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(brief.model_dump_json(indent=2), encoding="utf-8")
    return path


def brief_dates(briefs_dir: Path) -> list[date]:
    """Dates that have a brief file, newest first. Unparseable names are skipped."""
    out = []
    for p in Path(briefs_dir).glob("*.json"):
        try:
            out.append(date.fromisoformat(p.stem))
        except ValueError:
            continue
    return sorted(out, reverse=True)


def json_schema() -> str:
    return json.dumps(NewsBrief.model_json_schema(), indent=2)
