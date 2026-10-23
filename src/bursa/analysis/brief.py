"""The filing-calendar half of the daily brief: upcoming annual reports,
the statutory filing calendar and deadline-driven "what to watch" bullets.

The news half (macro and corporate news) is researched separately - see
``bursa.briefs``. This part is computed from each company's financial year
end, so it is available for any day, with or without a news brief.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from bursa.analysis.feed import Deadline, build_feed, size_of

WATCH_DAYS = 14
CALENDAR_ROWS = 15


@dataclass
class BriefCalendar:
    as_of: date
    days: int
    annual_reports: list[Deadline] = field(default_factory=list)
    calendar: list[Deadline] = field(default_factory=list)
    watch: list[str] = field(default_factory=list)


def _sentence(text: str) -> str:
    return text.rstrip(".") + "."


def brief_calendar(session: Session, day: date, days: int = WATCH_DAYS) -> BriefCalendar:
    feed = build_feed(session, months=12, days=days, today=day)
    size: dict[str, float] = {}
    for m in feed.months:
        for i in m.items:
            size[i.stock_code] = max(size.get(i.stock_code, 0.0), size_of(i))

    def by_due_then_size(d: Deadline) -> tuple:
        return (d.due, -size.get(d.stock_code, 0.0), d.stock_code)

    biggest = sorted(feed.upcoming, key=lambda d: -size.get(d.stock_code, 0.0))[:CALENDAR_ROWS]
    annual = [d for d in feed.upcoming if d.kind == "annual"]
    return BriefCalendar(
        as_of=day, days=days,
        annual_reports=sorted(annual, key=by_due_then_size),
        calendar=sorted(biggest, key=by_due_then_size),
        watch=_watch(feed.upcoming, size, days),
    )


def _watch(upcoming: list[Deadline], size: dict[str, float], days: int) -> list[str]:
    by_day: dict[date, list[Deadline]] = defaultdict(list)
    for d in upcoming:
        by_day[d.due].append(d)
    out = []
    for due, group in sorted(by_day.items()):
        annual = sum(1 for d in group if d.kind == "annual")
        quarterly = len(group) - annual
        filed = sum(1 for d in group if d.on_file)
        biggest = sorted(group, key=lambda d: -size.get(d.stock_code, 0.0))[:3]
        counts = [f"{n} {kind} report{'s' if n != 1 else ''}"
                  for n, kind in ((annual, "annual"), (quarterly, "quarterly")) if n]
        out.append(_sentence(
            f"**{due.day} {due:%b} filing deadline:** {' and '.join(counts)} due"
            + (f" ({filed} already filed)" if filed else "")
            + "; largest: " + ", ".join(d.name for d in biggest)
        ))
    if not out:
        out.append(f"No statutory filing deadlines in the next {days} days.")
    return out
