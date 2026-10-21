"""Results feed: per-company annual results reports, month-by-month digests of
each reporting cohort, and upcoming Bursa filing deadlines.

Everything is computed from extracted facts (``load_annual_facts`` rules) -
no LLM and no external data, so every sentence traces back to a fact row.

* The feed is grouped by **financial year end**, not by ingestion time:
  documents carry no filing date and ``created_at`` only records when the
  backfill happened to run.
* Upcoming dates are **statutory deadlines** under the Main/ACE Market
  Listing Requirements (quarterly report within 2 months of quarter end,
  annual report within 4 months of year end), not announcement dates -
  issuers often file earlier.
* A year is only compared with the previous one when both are full years
  (``MIN_FULL_YEAR_DAYS``) - interim figures mislabelled as FY upstream would
  otherwise show up as -50% revenue "results".
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from bursa.analysis.dividends import is_reit, year_dividend
from bursa.analysis.facts import AnnualFacts, load_annual_facts, load_annual_facts_many
from bursa.analysis.peers import MIN_FULL_YEAR_DAYS, companies_with_facts, sector_of
from bursa.db.enums import Market, PeriodType
from bursa.db.models import Company, Period, ValidationResult

# A gap this long between year ends is a change of financial year end (an
# 18-month period), not a comparable year.
MAX_FULL_YEAR_DAYS = 400
# Movers lists ignore companies whose prior PATAMI was below this (RM): a
# +900% on a RM 50k base is noise, not news.
MOVER_MIN_BASE = 5_000_000
MOVERS = 5
# A profit change smaller than this either way reads as "flat".
FLAT_BAND = 0.05

FEED_CONCEPTS = (
    "is.revenue", "is.gross_profit", "is.profit_before_tax", "is.tax_expense",
    "is.pat_owners", "is.profit_for_period", "is.eps_basic",
    "cf.net_operating", "cf.purchase_of_ppe",
)


# ------------------------------------------------------------------ figures


def _n(f: AnnualFacts, *keys: str) -> float | None:
    v = f.get(*keys)
    return None if v is None else float(v)


def _div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b <= 0:
        return None
    return a / b


@dataclass
class YearFigures:
    fiscal_year: int
    period_end: date
    revenue: float | None = None
    gross_profit: float | None = None
    pbt: float | None = None
    tax: float | None = None
    patami: float | None = None
    eps_sen: float | None = None
    ocf: float | None = None
    capex: float | None = None  # positive outflow
    fcf: float | None = None
    equity: float | None = None
    borrowings: float | None = None
    cash: float | None = None
    net_debt: float | None = None

    @property
    def gross_margin(self) -> float | None:
        return _div(self.gross_profit, self.revenue)

    @property
    def pbt_margin(self) -> float | None:
        return _div(self.pbt, self.revenue)

    @property
    def net_margin(self) -> float | None:
        return _div(self.patami, self.revenue)

    @property
    def roe(self) -> float | None:
        return _div(self.patami, self.equity)

    @property
    def effective_tax_rate(self) -> float | None:
        return None if self.tax is None else _div(abs(self.tax), self.pbt)

    @property
    def gearing(self) -> float | None:
        return _div(self.net_debt, self.equity)


def figures(f: AnnualFacts) -> YearFigures:
    y = YearFigures(fiscal_year=f.fiscal_year, period_end=f.period_end)
    y.revenue = _n(f, "is.revenue")
    y.gross_profit = _n(f, "is.gross_profit")
    y.pbt = _n(f, "is.profit_before_tax")
    y.tax = _n(f, "is.tax_expense")
    y.patami = _n(f, "is.pat_owners", "is.profit_for_period")
    y.eps_sen = _n(f, "is.eps_basic")
    y.ocf = _n(f, "cf.net_operating")
    capex = _n(f, "cf.purchase_of_ppe")
    y.capex = None if capex is None else abs(capex)
    if y.ocf is not None and y.capex is not None:
        y.fcf = y.ocf - y.capex
    y.equity = _n(f, "bs.equity_owners", "bs.total_equity")
    y.borrowings = _n(f, "bs.borrowings_total")
    if y.borrowings is None:
        parts = [_n(f, "bs.lt_borrowings"), _n(f, "bs.st_borrowings")]
        if any(p is not None for p in parts):
            y.borrowings = sum(abs(p) for p in parts if p is not None)
    y.cash = _n(f, "bs.cash_and_equivalents")
    if y.borrowings is not None and y.cash is not None:
        y.net_debt = abs(y.borrowings) - y.cash
    return y


def is_full_year(annual: dict[int, AnnualFacts], fy: int) -> bool:
    prev = annual.get(fy - 1)
    if prev is None:
        return True  # nothing to measure against; trust the FY label
    return (annual[fy].period_end - prev.period_end).days >= MIN_FULL_YEAR_DAYS


def comparable_prior(annual: dict[int, AnnualFacts], fy: int) -> int | None:
    prev = annual.get(fy - 1)
    if prev is None or not is_full_year(annual, fy - 1):
        return None
    days = (annual[fy].period_end - prev.period_end).days
    return fy - 1 if MIN_FULL_YEAR_DAYS <= days <= MAX_FULL_YEAR_DAYS else None


# ------------------------------------------------------------ change words


@dataclass
class Change:
    """How a figure moved from the prior year."""

    kind: str  # growth | turnaround | to_loss | loss_narrowed | loss_widened | none
    growth: float | None = None


def change(cur: float | None, prev: float | None) -> Change:
    if cur is None or prev is None:
        return Change("none")
    if prev > 0 and cur > 0:
        return Change("growth", cur / prev - 1)
    if prev <= 0 < cur:
        return Change("turnaround")
    if prev > 0 >= cur:
        return Change("to_loss")
    if prev == 0:
        return Change("none")
    return Change("loss_narrowed" if cur > prev else "loss_widened")


def tone_of(revenue: Change, patami: Change) -> str:
    """up / down / flat - the colour of a feed item."""
    if patami.kind in ("turnaround", "loss_narrowed"):
        return "up"
    if patami.kind in ("to_loss", "loss_widened"):
        return "down"
    g = patami.growth if patami.kind == "growth" else revenue.growth
    if g is None:
        return "flat"
    if g >= FLAT_BAND:
        return "up"
    if g <= -FLAT_BAND:
        return "down"
    return "flat"


def rm(v: float | None) -> str:
    if v is None:
        return "n/a"
    a, sign = abs(v), "-" if v < 0 else ""
    if a >= 1e9:
        return f"{sign}RM {a / 1e9:.2f}b"
    if a >= 1e6:
        return f"{sign}RM {a / 1e6:.1f}m"
    if a >= 1e3:
        return f"{sign}RM {a / 1e3:.0f}k"
    return f"{sign}RM {a:,.0f}"


def _pct(v: float, digits: int = 1) -> str:
    return f"{v * 100:.{digits}f}%"


def _signed(v: float) -> str:
    return f"{v * 100:+.1f}%"


def short_summary(revenue: Change, patami: Change) -> str:
    parts = []
    if revenue.kind == "growth" and revenue.growth is not None:
        parts.append(f"revenue {_signed(revenue.growth)}")
    words = {
        "turnaround": "back in profit", "to_loss": "swung to a loss",
        "loss_narrowed": "loss narrowed", "loss_widened": "loss widened",
    }
    if patami.kind == "growth" and patami.growth is not None:
        parts.append(f"PATAMI {_signed(patami.growth)}")
    elif patami.kind in words:
        parts.append(words[patami.kind])
    return ", ".join(parts) if parts else "first year on file"


def _moved(label: str, ch: Change, value: float | None) -> str | None:
    if value is None:
        return None
    if ch.kind == "growth" and ch.growth is not None:
        if abs(ch.growth) < 0.005:
            return f"{label} was flat at {rm(value)}"
        verb = "rose" if ch.growth > 0 else "fell"
        return f"{label} {verb} {abs(ch.growth) * 100:.1f}% to {rm(value)}"
    return {
        "turnaround": f"{label} returned to a profit of {rm(value)}",
        "to_loss": f"{label} swung to a loss of {rm(-value)}",
        "loss_narrowed": f"the {label} loss narrowed to {rm(-value)}",
        "loss_widened": f"the {label} loss widened to {rm(-value)}",
    }.get(ch.kind, f"{label} was a loss of {rm(-value)}" if value < 0
          else f"{label} was {rm(value)}")


# ---------------------------------------------------------- results report


@dataclass
class HeadlineRow:
    measure: str
    kind: str  # money | sen | pct
    current: float | None
    prior: float | None
    change: float | None  # fraction for money/sen, percentage points for pct
    change_kind: str  # growth | pp | turnaround | to_loss | loss_narrowed | loss_widened | none


@dataclass
class Observation:
    level: str  # good | watch | data
    title: str
    text: str


@dataclass
class SectorContext:
    sector: str
    companies: int
    median_revenue_growth: float | None
    median_net_margin: float | None
    revenue_growth_rank: int | None  # 1 = fastest
    revenue_growth_n: int
    net_margin_rank: int | None
    net_margin_n: int


@dataclass
class ResultsReport:
    stock_code: str
    name: str
    sector: str | None
    fiscal_year: int
    period_end: date
    prior_fiscal_year: int | None
    tone: str
    headline: str
    bottom_line: str
    rows: list[HeadlineRow] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    sector_context: SectorContext | None = None
    validation: dict = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    available_years: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _row(measure: str, kind: str, cur: float | None, prev: float | None) -> HeadlineRow:
    if kind == "pct":
        delta = None if cur is None or prev is None else (cur - prev) * 100
        return HeadlineRow(measure, kind, cur, prev, delta, "pp" if delta is not None else "none")
    ch = change(cur, prev)
    return HeadlineRow(measure, kind, cur, prev, ch.growth, ch.kind)


def _rows(cur: YearFigures, prev: YearFigures | None, dps: tuple[float | None, float | None],
          payout: tuple[float | None, float | None]) -> list[HeadlineRow]:
    p = prev or YearFigures(fiscal_year=0, period_end=cur.period_end)
    rows = [
        _row("Revenue", "money", cur.revenue, p.revenue),
        _row("Gross profit", "money", cur.gross_profit, p.gross_profit),
        _row("Profit before tax", "money", cur.pbt, p.pbt),
        _row("PATAMI", "money", cur.patami, p.patami),
        _row("Basic EPS (sen)", "sen", cur.eps_sen, p.eps_sen),
        _row("Gross margin", "pct", cur.gross_margin, p.gross_margin),
        _row("Pre-tax margin", "pct", cur.pbt_margin, p.pbt_margin),
        _row("Net margin", "pct", cur.net_margin, p.net_margin),
        _row("ROE (PATAMI / equity)", "pct", cur.roe, p.roe),
        _row("Effective tax rate", "pct", cur.effective_tax_rate, p.effective_tax_rate),
        _row("Operating cash flow", "money", cur.ocf, p.ocf),
        _row("Capex (PPE purchases)", "money", cur.capex, p.capex),
        _row("Free cash flow", "money", cur.fcf, p.fcf),
        _row("Net debt (cash)", "money", cur.net_debt, p.net_debt),
        _row("Net gearing", "pct", cur.gearing, p.gearing),
        _row("DPS paid (sen)", "sen", dps[0], dps[1]),
        _row("Payout ratio", "pct", payout[0], payout[1]),
    ]
    return [r for r in rows if r.current is not None or r.prior is not None]


def _observations(cur: YearFigures, prev: YearFigures | None, rev: Change, pat: Change,
                  payout: float | None, history: list[YearFigures]) -> list[Observation]:
    obs: list[Observation] = []

    if cur.patami and cur.patami > 0 and cur.ocf is not None:
        conv = cur.ocf / cur.patami
        if conv < 0.5:
            obs.append(Observation(
                "watch", "Earnings not converting to cash",
                f"Operating cash flow of {rm(cur.ocf)} is only {conv:.1f}x PATAMI of "
                f"{rm(cur.patami)}. Check receivables, inventory and contract assets - "
                "is profit being booked ahead of cash?",
            ))
        elif conv > 1.5:
            obs.append(Observation(
                "good", "Strong cash conversion",
                f"Operating cash flow of {rm(cur.ocf)} is {conv:.1f}x PATAMI.",
            ))

    if cur.fcf is not None and cur.fcf < 0 and cur.patami and cur.patami > 0:
        obs.append(Observation(
            "watch", "Negative free cash flow",
            f"Capex of {rm(cur.capex)} exceeded operating cash flow of {rm(cur.ocf)}, "
            f"leaving free cash flow of {rm(cur.fcf)} despite a profit. How is the gap "
            "funded - borrowings, cash on hand, or a placement?",
        ))

    if prev is not None and cur.net_margin is not None and prev.net_margin is not None:
        d = (cur.net_margin - prev.net_margin) * 100
        if abs(d) >= 3:
            obs.append(Observation(
                "good" if d > 0 else "watch",
                "Margin expansion" if d > 0 else "Margin compression",
                f"Net margin moved {d:+.1f} pp to {_pct(cur.net_margin)}. Is this pricing, "
                "input costs, mix, or a one-off item (impairment, disposal gain, fair-value "
                "change)?",
            ))

    if (rev.kind == "growth" and pat.kind == "growth" and rev.growth is not None
            and pat.growth is not None and abs(pat.growth - rev.growth) >= 0.25):
        lever = pat.growth > rev.growth
        obs.append(Observation(
            "watch",
            "Profit outpacing revenue" if lever else "Profit lagging revenue",
            f"Revenue {_signed(rev.growth)} but PATAMI {_signed(pat.growth)}. "
            + ("Operating leverage, or non-operating gains? Look at other income and "
               "exceptional items." if lever else
               "Cost pressure, finance costs or one-off charges? Look at the expense lines."),
        ))

    if pat.kind == "turnaround":
        obs.append(Observation("good", "Back in profit",
                               f"PATAMI of {rm(cur.patami)} after a loss of "
                               f"{rm(-(prev.patami or 0) if prev else None)} the year before."))
    elif pat.kind == "to_loss":
        obs.append(Observation("watch", "Swung to a loss",
                               f"PATAMI of {rm(cur.patami)} after a profit of "
                               f"{rm(prev.patami if prev else None)}. Operating, or impairments?"))

    if cur.gearing is not None and cur.gearing > 1:
        obs.append(Observation(
            "watch", "High net gearing",
            f"Net debt of {rm(cur.net_debt)} is {cur.gearing:.2f}x shareholders' equity.",
        ))
    elif (prev is not None and cur.net_debt is not None and prev.net_debt is not None
          and prev.net_debt > 0 and cur.net_debt > prev.net_debt * 1.25
          and cur.net_debt - prev.net_debt > 10_000_000):
        obs.append(Observation(
            "watch", "Net debt rising",
            f"Net debt rose from {rm(prev.net_debt)} to {rm(cur.net_debt)}.",
        ))

    if payout is not None and payout > 1:
        obs.append(Observation(
            "watch", "Dividends above earnings",
            f"Dividends paid were {payout:.0%} of PATAMI. Sustainable from retained cash, "
            "or a special dividend?",
        ))

    etr = cur.effective_tax_rate
    if etr is not None and (etr < 0.10 or etr > 0.40):
        obs.append(Observation(
            "watch", "Unusual tax rate",
            f"Effective tax rate of {_pct(etr)} vs the 24% statutory rate. Look for "
            "incentives, unrecognised losses, non-deductible items or prior-year adjustments.",
        ))

    revs = [(h.fiscal_year, h.revenue) for h in history if h.revenue]
    if len(revs) >= 4 and rev.kind == "growth" and rev.growth is not None:
        (y0, r0), (y1, r1) = revs[-4], revs[-1]
        if r0 > 0 and y1 - y0 == 3:
            cagr = (r1 / r0) ** (1 / 3) - 1
            if abs(rev.growth - cagr) >= 0.10:
                faster = rev.growth > cagr
                obs.append(Observation(
                    "good" if faster else "watch",
                    "Growth accelerating" if faster else "Growth slowing",
                    f"Revenue grew {_signed(rev.growth)} this year vs a 3-year CAGR of "
                    f"{_signed(cagr)} (FY{y0}-FY{y1}).",
                ))
    return obs


def _validation(session: Session, company: Company, fy: int) -> dict:
    rows = session.execute(
        select(ValidationResult.rule_key, ValidationResult.passed,
               ValidationResult.detail, Period.period_end)
        .join(Period, ValidationResult.period_id == Period.id)
        .where(Period.company_id == company.id, Period.fiscal_year == fy)
        .order_by(ValidationResult.id)
    ).all()
    latest: dict[tuple, tuple] = {}
    for rule, passed, detail, end in rows:
        latest[(rule, end)] = (rule, passed, detail)
    results = list(latest.values())
    return {
        "run": len(results),
        "passed": sum(1 for r in results if r[1]),
        "failures": [{"rule": r[0], "detail": r[2]} for r in results if not r[1]],
    }


def _sector_context(session: Session, company: Company, fy: int,
                    rev_growth: float | None, net_margin: float | None) -> SectorContext | None:
    sector = sector_of(company)
    if not sector:
        return None
    wanted = sector.casefold()
    members = [
        c for c in companies_with_facts(session) if (sector_of(c) or "").casefold() == wanted
    ]
    annual = load_annual_facts_many(session, [c.id for c in members], FEED_CONCEPTS)
    growths, margins = [], []
    for c in members:
        years = annual.get(c.id, {})
        if fy not in years or not is_full_year(years, fy):
            continue
        cur = figures(years[fy])
        if cur.net_margin is not None:
            margins.append(cur.net_margin)
        prior = comparable_prior(years, fy)
        if prior is not None:
            ch = change(cur.revenue, figures(years[prior]).revenue)
            if ch.kind == "growth" and ch.growth is not None:
                growths.append(ch.growth)
    if len(growths) < 3 and len(margins) < 3:
        return None

    def rank(v: float | None, pop: list[float]) -> int | None:
        return None if v is None or len(pop) < 3 else 1 + sum(1 for x in pop if x > v)

    return SectorContext(
        sector=sector, companies=len(members),
        median_revenue_growth=median(growths) if len(growths) >= 3 else None,
        median_net_margin=median(margins) if len(margins) >= 3 else None,
        revenue_growth_rank=rank(rev_growth, growths), revenue_growth_n=len(growths),
        net_margin_rank=rank(net_margin, margins), net_margin_n=len(margins),
    )


def latest_full_year(annual: dict[int, AnnualFacts]) -> int | None:
    for fy in sorted(annual, reverse=True):
        f = annual[fy]
        if f.get("is.pat_owners", "is.profit_for_period", "is.revenue") is None:
            continue
        if is_full_year(annual, fy):
            return fy
    return None


def results_report(
    session: Session, company: Company, fiscal_year: int | None = None,
) -> ResultsReport:
    annual = load_annual_facts(session, company)
    fy = fiscal_year if fiscal_year is not None else latest_full_year(annual)
    if fy is None or fy not in annual:
        raise LookupError(f"No annual results for {company.stock_code}"
                          + (f" FY{fiscal_year}" if fiscal_year else ""))

    cur = figures(annual[fy])
    prior_fy = comparable_prior(annual, fy)
    prev = figures(annual[prior_fy]) if prior_fy is not None else None
    rev = change(cur.revenue, prev.revenue if prev else None)
    pat = change(cur.patami, prev.patami if prev else None)

    reit = is_reit(company, annual)
    div_cur = year_dividend(annual[fy], reit=reit)
    div_prev = year_dividend(annual[prior_fy], reit=reit) if prior_fy is not None else None

    full_years = [y for y in annual if is_full_year(annual, y)]
    history = [figures(annual[y]) for y in sorted(full_years) if y <= fy][-6:]

    report = ResultsReport(
        stock_code=company.stock_code, name=company.name, sector=sector_of(company),
        fiscal_year=fy, period_end=cur.period_end, prior_fiscal_year=prior_fy,
        tone=tone_of(rev, pat) if prev else "new", headline=short_summary(rev, pat), bottom_line="",
        available_years=sorted(full_years, reverse=True),
    )
    report.rows = _rows(
        cur, prev,
        (div_cur.dps_sen, div_prev.dps_sen if div_prev else None),
        (div_cur.payout_ratio, div_prev.payout_ratio if div_prev else None),
    )
    report.sector_context = _sector_context(
        session, company, fy, rev.growth if rev.kind == "growth" else None, cur.net_margin,
    )
    report.observations = _observations(cur, prev, rev, pat, div_cur.payout_ratio, history)
    report.validation = _validation(session, company, fy)
    if report.validation["failures"]:
        report.observations.append(Observation(
            "data", "Accounting checks failed",
            f"{len(report.validation['failures'])} of {report.validation['run']} validation "
            "rules failed for this year - some figures above may be mis-extracted.",
        ))
    report.history = [
        {"fiscal_year": h.fiscal_year, "revenue": h.revenue, "patami": h.patami,
         "net_margin": h.net_margin}
        for h in history
    ]
    if fiscal_year is not None and not is_full_year(annual, fy):
        report.notes.append(f"FY{fy} ends less than {MIN_FULL_YEAR_DAYS} days after FY{fy - 1} "
                            "- likely an interim period, not a full year.")
    if prev is None:
        report.notes.append("No comparable prior full year on file - no year-on-year changes.")
    report.notes.extend(div_cur.flags)
    report.bottom_line = _bottom_line(report, cur, prev, rev, pat, div_cur.dps_sen,
                                      div_cur.payout_ratio)
    return report


def _bottom_line(r: ResultsReport, cur: YearFigures, prev: YearFigures | None,
                 rev: Change, pat: Change, dps: float | None, payout: float | None) -> str:
    end = f"{cur.period_end.day} {cur.period_end:%B %Y}"
    out = []
    moves = (_moved("revenue", rev, cur.revenue), _moved("PATAMI", pat, cur.patami))
    first = [s for s in moves if s]
    if first:
        out.append(f"For the year ended {end}, " + " and ".join(first) + ".")
    else:
        out.append(f"Results for the year ended {end}.")

    if cur.net_margin is not None and prev is not None and prev.net_margin is not None:
        d = (cur.net_margin - prev.net_margin) * 100
        if abs(d) < 0.5:
            out.append(f"Net margin held at {_pct(cur.net_margin)}.")
        else:
            out.append(f"Net margin {'widened' if d > 0 else 'narrowed'} to "
                       f"{_pct(cur.net_margin)} from {_pct(prev.net_margin)}.")

    if cur.ocf is not None:
        s = f"Operating cash flow was {rm(cur.ocf)}"
        if cur.fcf is not None:
            s += (f" and free cash flow {rm(cur.fcf)} after {rm(cur.capex)} of capex"
                  if cur.fcf >= 0 else
                  f", but {rm(cur.capex)} of capex left free cash flow negative at {rm(cur.fcf)}")
        out.append(s + ".")

    if dps:
        s = f"Dividends paid in the year came to {dps:.2f} sen a share"
        out.append(s + (f" ({payout:.0%} of PATAMI)." if payout is not None else "."))

    sc = r.sector_context
    if (sc and sc.median_revenue_growth is not None
            and rev.kind == "growth" and rev.growth is not None):
        side = "ahead of" if rev.growth > sc.median_revenue_growth else "behind"
        out.append(f"Revenue growth was {side} the {sc.sector} median of "
                   f"{_signed(sc.median_revenue_growth)} for FY{r.fiscal_year}.")
    return " ".join(out)


# --------------------------------------------------------------------- feed


@dataclass
class FeedItem:
    stock_code: str
    name: str
    sector: str | None
    fiscal_year: int
    period_end: date
    tone: str
    summary: str
    revenue: float | None
    patami: float | None
    prior_patami: float | None
    revenue_growth: float | None
    patami_growth: float | None
    patami_change: str


@dataclass
class Mover:
    stock_code: str
    name: str
    fiscal_year: int
    value: float | None


@dataclass
class MonthGroup:
    month: str  # YYYY-MM
    label: str
    companies: int
    compared: int
    up: int
    down: int
    flat: int
    median_revenue_growth: float | None
    median_patami_growth: float | None
    gainers: list[Mover] = field(default_factory=list)
    decliners: list[Mover] = field(default_factory=list)
    turnarounds: list[Mover] = field(default_factory=list)
    to_loss: list[Mover] = field(default_factory=list)
    items: list[FeedItem] = field(default_factory=list)


@dataclass
class Deadline:
    due: date
    kind: str  # quarterly | annual
    label: str
    period_end: date
    stock_code: str
    name: str
    on_file: bool
    context: str | None


@dataclass
class Feed:
    as_of: date
    latest_period_end: date | None
    months: list[MonthGroup] = field(default_factory=list)
    upcoming: list[Deadline] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _item(c: Company, sector: str | None, annual: dict[int, AnnualFacts], fy: int) -> FeedItem:
    cur = figures(annual[fy])
    prior = comparable_prior(annual, fy)
    prev = figures(annual[prior]) if prior is not None else None
    rev = change(cur.revenue, prev.revenue if prev else None)
    pat = change(cur.patami, prev.patami if prev else None)
    return FeedItem(
        stock_code=c.stock_code, name=c.name, sector=sector, fiscal_year=fy,
        period_end=cur.period_end, tone=tone_of(rev, pat) if prev else "new",
        summary=short_summary(rev, pat), revenue=cur.revenue, patami=cur.patami,
        prior_patami=prev.patami if prev else None,
        revenue_growth=rev.growth, patami_growth=pat.growth, patami_change=pat.kind,
    )


def _group(month: str, items: list[FeedItem]) -> MonthGroup:
    y, m = map(int, month.split("-"))
    compared = [i for i in items if i.tone != "new"]
    revg = [i.revenue_growth for i in compared if i.revenue_growth is not None]
    patg = [i.patami_growth for i in compared if i.patami_growth is not None]

    def mv(i: FeedItem, v: float | None) -> Mover:
        return Mover(i.stock_code, i.name, i.fiscal_year, v)

    sized = [i for i in compared if i.patami_growth is not None
             and (i.prior_patami or 0) >= MOVER_MIN_BASE]
    by_growth = sorted(sized, key=lambda i: i.patami_growth or 0)
    g = MonthGroup(
        month=month, label=f"Year ended {calendar.month_name[m]} {y}",
        companies=len(items), compared=len(compared),
        up=sum(1 for i in compared if i.tone == "up"),
        down=sum(1 for i in compared if i.tone == "down"),
        flat=sum(1 for i in compared if i.tone == "flat"),
        median_revenue_growth=median(revg) if revg else None,
        median_patami_growth=median(patg) if patg else None,
        gainers=[mv(i, i.patami_growth) for i in reversed(by_growth[-MOVERS:])
                 if (i.patami_growth or 0) > 0],
        decliners=[mv(i, i.patami_growth) for i in by_growth[:MOVERS]
                   if (i.patami_growth or 0) < 0],
        turnarounds=[mv(i, i.patami) for i in compared if i.patami_change == "turnaround"],
        to_loss=[mv(i, i.patami) for i in compared if i.patami_change == "to_loss"],
    )
    g.items = sorted(items, key=lambda i: -(i.revenue or 0))
    return g


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def _add_months(y: int, m: int, k: int) -> tuple[int, int]:
    t = y * 12 + (m - 1) + k
    return t // 12, t % 12 + 1


def upcoming_deadlines(
    companies: list[Company],
    today: date,
    days: int,
    on_file: set[tuple[int, date]] | None = None,
    last_year: dict[int, YearFigures] | None = None,
) -> list[Deadline]:
    """Statutory filing deadlines falling in ``[today, today + days]``.

    Quarterly report: two months after each quarter end. Annual report: four
    months after the financial year end. Both read as month-end dates.
    """
    on_file = on_file or set()
    last_year = last_year or {}
    horizon = date.fromordinal(today.toordinal() + days)
    out: list[Deadline] = []
    for c in companies:
        fye = c.fy_end_month
        if not fye:
            continue
        ly = last_year.get(c.id)
        context = None
        if ly is not None:
            context = f"FY{ly.fiscal_year}: revenue {rm(ly.revenue)}, PATAMI {rm(ly.patami)}"
        # Quarter ends from 5 months back (their deadlines can still be ahead).
        for back in range(-5, days // 28 + 2):
            qy, qm = _add_months(today.year, today.month, back)
            if (qm - fye) % 3:
                continue
            q_end = _month_end(qy, qm)
            quarter = ((qm - fye - 1) % 12) // 3 + 1
            fy_label = qy if qm <= fye else qy + 1
            if c.market != Market.LEAP:
                due = _month_end(*_add_months(qy, qm, 2))
                if today <= due <= horizon:
                    out.append(Deadline(
                        due=due, kind="quarterly",
                        label=f"Q{quarter} FY{fy_label} quarterly report",
                        period_end=q_end, stock_code=c.stock_code, name=c.name,
                        on_file=(c.id, q_end) in on_file, context=context,
                    ))
            if quarter == 4:
                due = _month_end(*_add_months(qy, qm, 4))
                if today <= due <= horizon:
                    out.append(Deadline(
                        due=due, kind="annual", label=f"FY{fy_label} annual report",
                        period_end=q_end, stock_code=c.stock_code, name=c.name,
                        on_file=(c.id, q_end) in on_file, context=context,
                    ))
    return sorted(out, key=lambda d: (d.due, d.kind, d.stock_code))


def build_feed(session: Session, *, months: int = 12, days: int = 45,
               today: date | None = None) -> Feed:
    today = today or date.today()
    companies = companies_with_facts(session)
    annual = load_annual_facts_many(session, [c.id for c in companies], FEED_CONCEPTS)

    by_month: dict[str, list[FeedItem]] = {}
    latest_year: dict[int, YearFigures] = {}
    sectors: set[str] = set()
    all_items: list[FeedItem] = []
    for c in companies:
        years = annual.get(c.id, {})
        sector = sector_of(c)
        if sector:
            sectors.add(sector)
        for fy, f in years.items():
            if f.period_end > today or not is_full_year(years, fy):
                continue
            if f.get("is.pat_owners", "is.profit_for_period", "is.revenue") is None:
                continue
            all_items.append(_item(c, sector, years, fy))
        if (lfy := latest_full_year(years)) is not None:
            latest_year[c.id] = figures(years[lfy])

    feed = Feed(as_of=today, latest_period_end=max((i.period_end for i in all_items), default=None),
                sectors=sorted(sectors))
    if feed.latest_period_end is not None:
        ly, lm = feed.latest_period_end.year, feed.latest_period_end.month
        cutoff = _add_months(ly, lm, -(months - 1))
        for i in all_items:
            if (i.period_end.year, i.period_end.month) >= cutoff:
                by_month.setdefault(f"{i.period_end:%Y-%m}", []).append(i)
    feed.months = [_group(m, items) for m, items in sorted(by_month.items(), reverse=True)]

    on_file = {
        (cid, end) for cid, end in session.execute(
            select(Period.company_id, Period.period_end).where(
                Period.period_type.in_((PeriodType.FY, PeriodType.Q1, PeriodType.Q2,
                                        PeriodType.Q3, PeriodType.Q4))
            )
        )
    }
    feed.upcoming = upcoming_deadlines(companies, today, days, on_file, latest_year)
    feed.notes.append(
        "Grouped by financial year end. Upcoming dates are Listing Requirements deadlines "
        "(quarterly: 2 months after quarter end; annual report: 4 months after year end), "
        "not announcement dates."
    )
    return feed
