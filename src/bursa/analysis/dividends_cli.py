"""`bursa dividends ...` - dividend history and dividend-record screen (facts only)."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from bursa.analysis.dividends import (
    DividendHistory,
    compute_dividends,
    get_company,
    screen_dividends,
)
from bursa.db.session import session_scope

app = typer.Typer(help="Dividend history, payout and streak screen.", no_args_is_help=True)
console = Console()


def _money(v: float | None) -> str:
    if v is None:
        return "-"
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:,.2f}b"
    if a >= 1e6:
        return f"{v / 1e6:,.1f}m"
    return f"{v:,.0f}"


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def _num(v: float | None, fmt: str = "{:.2f}") -> str:
    return "-" if v is None else fmt.format(v)


def render_history(h: DividendHistory, *, show_sources: bool = False) -> None:
    title = f"{h.stock_code} {h.name}" + (" (REIT)" if h.is_reit else "")
    table = Table(title=title)
    for col in ("FY", "FY end", "Dividends", "Basis", "Shares", "PATAMI", "DPS sen",
                "Payout", "Cover", "DPS YoY", "Yield"):
        table.add_column(col, justify="left" if col in ("Basis", "FY end") else "right")
    for y in h.years:
        payout = _pct(y.payout_ratio)
        if any(f.startswith("payout") for f in y.flags):
            payout = f"[red]{payout}![/]"
        basis = y.dividends_basis or ("dpu" if "dps_sen" in y.sources
                                      and y.sources["dps_sen"].startswith("is.dpu") else "-")
        if basis == "equity":
            basis = "[yellow]equity[/]"
        shares = _money(y.shares)
        if y.sources.get("shares", "").startswith("implied"):
            shares = f"[yellow]{shares}*[/]"
        table.add_row(
            str(y.fiscal_year), str(y.period_end), _money(y.dividends), basis, shares,
            _money(y.patami), _num(y.dps_sen), payout, _num(y.dividend_cover, "{:.2f}x"),
            _pct(y.dps_growth), _pct(y.dividend_yield),
        )
    console.print(table)
    end = f" ending FY{h.streak_end}" if h.streak_end else ""
    console.print(
        f"streak [bold]{h.streak}[/] yr{end}, longest {h.longest_streak}, "
        f"median payout over streak {_pct(h.median_payout)}"
    )
    for y in h.years:
        for flag in y.flags:
            console.print(f"[yellow]FY{y.fiscal_year}:[/] {flag}")
        if show_sources:
            for k, v in y.sources.items():
                console.print(f"[dim]FY{y.fiscal_year} {k}: {v}[/]")
    for note in h.notes:
        console.print(f"[dim]note: {note}[/]")


@app.command("history")
def history_cmd(
    codes: Annotated[list[str], typer.Argument(help="Stock code(s).")],
    prices: Annotated[
        bool, typer.Option("--prices", help="Fetch FY-end closes for yield."),
    ] = False,
    sources: Annotated[bool, typer.Option("--sources", help="Show each value's source.")] = False,
) -> None:
    """Per-year dividends, DPS, payout, cover, growth (and yield with --prices)."""
    with session_scope() as session:
        for code in codes:
            try:
                company = get_company(session, code)
            except LookupError as e:
                console.print(f"[red]{e}[/]")
                continue
            render_history(compute_dividends(session, company, prices=prices),
                           show_sources=sources)
        session.rollback()  # read-only


@app.command("screen")
def screen_cmd(
    min_years: Annotated[int, typer.Option("--min-years", help="Unbroken record length.")] = 5,
    min_payout: Annotated[float | None, typer.Option(
        "--min-payout", help="Minimum median payout, fraction (0.4 = 40%).")] = None,
    max_payout: Annotated[float | None, typer.Option(
        "--max-payout", help="Maximum median payout, fraction.")] = None,
) -> None:
    """Companies with an N-year unbroken dividend record, by streak then payout."""
    with session_scope() as session:
        hits = screen_dividends(session, min_years=min_years,
                                min_payout=min_payout, max_payout=max_payout)
        session.rollback()
    table = Table(title=f"Dividend record >= {min_years} years ({len(hits)} companies)")
    for col in ("Code", "Name", "Streak", "Through", "Longest", "Median payout",
                "Latest DPS sen", "Flags"):
        table.add_column(col, justify="left" if col in ("Code", "Name") else "right")
    for h in hits:
        latest = next((y for y in reversed(h.years) if y.fiscal_year == h.streak_end), None)
        n_flags = sum(len(y.flags) for y in h.years)
        table.add_row(
            h.stock_code, h.name[:28], str(h.streak), f"FY{h.streak_end}", str(h.longest_streak),
            _pct(h.median_payout), _num(latest.dps_sen if latest else None),
            str(n_flags) if n_flags else "",
        )
    console.print(table)
