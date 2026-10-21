from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from bursa.analysis.facts import load_annual_facts, load_annual_facts_many
from bursa.analysis.feed import (
    build_feed,
    change,
    results_report,
    tone_of,
    upcoming_deadlines,
)
from bursa.db.enums import Basis, Continuity, DocSource, PeriodType
from bursa.db.models import Company, Document, ExtractionRun, Fact, Period
from bursa.mapping.synonyms import seed_concepts


class Builder:
    def __init__(self, session: Session) -> None:
        self.s = session
        seed_concepts(session)

    def company(self, code: str, years: dict[date, dict[str, float]], *,
                sector: str | None = "Industrials", fye: int = 12) -> Company:
        c = Company(stock_code=code, name=f"Co {code}", sector=sector, fy_end_month=fye)
        self.s.add(c)
        self.s.flush()
        for end, values in years.items():
            tag = f"{code}-{end}"
            d = Document(company_id=c.id, source=DocSource.UPLOAD, original_filename=tag,
                         file_sha256=tag, file_size=1, storage_path=tag)
            self.s.add(d)
            self.s.flush()
            run = ExtractionRun(document_id=d.id)
            self.s.add(run)
            self.s.flush()
            dur = Period(company_id=c.id, period_start=None, period_end=end,
                         period_type=PeriodType.FY, fiscal_year=end.year)
            inst = Period(company_id=c.id, period_start=None, period_end=end,
                          period_type=PeriodType.INSTANT, fiscal_year=end.year)
            self.s.add_all([dur, inst])
            self.s.flush()
            for key, v in values.items():
                self.s.add(Fact(
                    company_id=c.id, concept_key=key,
                    period_id=(inst if key.startswith("bs.") else dur).id,
                    value=Decimal(str(v)), basis=Basis.CONSOLIDATED, continuity=Continuity.TOTAL,
                    reported_in_document_id=d.id, run_id=run.id, confidence=1.0,
                ))
        self.s.flush()
        return c


def yr(revenue: float, pat: float, **extra: float) -> dict[str, float]:
    return {"is.revenue": revenue, "is.pat_owners": pat, **extra}


@pytest.fixture
def b(session: Session) -> Builder:
    return Builder(session)


def test_change_kinds_and_tone() -> None:
    assert change(120, 100).growth == pytest.approx(0.2)
    assert change(10, -5).kind == "turnaround"
    assert change(-5, 10).kind == "to_loss"
    assert change(-5, -10).kind == "loss_narrowed"
    assert change(-10, -5).kind == "loss_widened"
    assert change(None, 5).kind == "none"
    assert tone_of(change(110, 100), change(102, 100)) == "flat"
    assert tone_of(change(90, 100), change(120, 100)) == "up"
    assert tone_of(change(130, 100), change(-1, 5)) == "down"


def test_bulk_loader_matches_single(b: Builder) -> None:
    c1 = b.company("0001", {date(2024, 12, 31): yr(100, 10), date(2025, 12, 31): yr(120, 12)})
    c2 = b.company("0002", {date(2025, 12, 31): yr(50, 5)})
    many = load_annual_facts_many(b.s, [c1.id, c2.id])
    for c in (c1, c2):
        single = load_annual_facts(b.s, c)
        bulk = {fy: f.values for fy, f in many[c.id].items()}
        assert bulk == {fy: f.values for fy, f in single.items()}
    only_rev = load_annual_facts_many(b.s, [c1.id], ["is.revenue"])
    assert set(only_rev[c1.id][2025].values) == {"is.revenue"}


def test_report_growth_bottom_line_and_flags(b: Builder) -> None:
    c = b.company("0011", {
        date(2024, 12, 31): yr(1000, 100, **{"cf.net_operating": 120, "cf.purchase_of_ppe": -50}),
        date(2025, 12, 31): yr(1100, 60, **{"cf.net_operating": 20, "cf.purchase_of_ppe": -200}),
    })
    r = results_report(b.s, c)

    assert r.fiscal_year == 2025 and r.prior_fiscal_year == 2024
    assert r.tone == "down"
    assert r.headline == "revenue +10.0%, PATAMI -40.0%"
    assert r.bottom_line.startswith("For the year ended 31 December 2025, revenue rose 10.0%")
    assert "free cash flow negative" in r.bottom_line
    titles = {o.title for o in r.observations}
    assert {"Earnings not converting to cash", "Negative free cash flow",
            "Margin compression", "Profit lagging revenue"} <= titles
    rows = {row.measure: row for row in r.rows}
    assert rows["Net margin"].change == pytest.approx((60 / 1100 - 0.1) * 100)
    assert rows["Free cash flow"].current == pytest.approx(-180)


def test_interim_year_is_not_latest_or_compared(b: Builder) -> None:
    c = b.company("0012", {
        date(2024, 12, 31): yr(1000, 100),
        date(2025, 12, 31): yr(1200, 150),
        date(2026, 6, 30): yr(500, 70),  # 6-month YTD mislabelled FY
    })
    r = results_report(b.s, c)
    assert r.fiscal_year == 2025
    assert 2026 not in r.available_years

    first = results_report(b.s, b.company("0013", {date(2025, 12, 31): yr(100, -7)}))
    assert first.tone == "new"
    assert "PATAMI was a loss of RM 7" in first.bottom_line

    with pytest.raises(LookupError):
        results_report(b.s, c, 2019)


def test_feed_groups_by_year_end_month_with_movers(b: Builder) -> None:
    big = 100_000_000
    y24, y25 = date(2024, 12, 31), date(2025, 12, 31)
    b.company("0021", {y24: yr(10 * big, big), y25: yr(12 * big, 2 * big)})
    b.company("0022", {y24: yr(10 * big, big), y25: yr(9 * big, big / 2)})
    b.company("0023", {y24: yr(100, -5), y25: yr(150, 8)})
    b.company("0024", {date(2025, 6, 30): yr(500, 50)}, fye=6)

    feed = build_feed(b.s, today=date(2026, 1, 15), months=12, days=30)

    assert [m.month for m in feed.months] == ["2025-12", "2025-06"]
    dec = feed.months[0]
    assert dec.label == "Year ended December 2025"
    assert (dec.companies, dec.up, dec.down) == (3, 2, 1)
    assert [g.stock_code for g in dec.gainers] == ["0021"]
    assert [g.stock_code for g in dec.decliners] == ["0022"]
    assert [t.stock_code for t in dec.turnarounds] == ["0023"]  # too small for movers
    assert feed.months[1].items[0].tone == "new"


def test_upcoming_deadlines() -> None:
    dec = Company(id=1, stock_code="1000", name="Dec Co", fy_end_month=12, market="MAIN")
    jun = Company(id=2, stock_code="2000", name="Jun Co", fy_end_month=6, market="MAIN")
    on_file = {(2, date(2026, 6, 30))}
    out = upcoming_deadlines([dec, jun], date(2026, 10, 22), 45, on_file)
    got = [(d.due, d.stock_code, d.label, d.on_file) for d in out]
    assert got == [
        (date(2026, 10, 31), "2000", "FY2026 annual report", True),
        (date(2026, 11, 30), "1000", "Q3 FY2026 quarterly report", False),
        (date(2026, 11, 30), "2000", "Q1 FY2027 quarterly report", False),
    ]
