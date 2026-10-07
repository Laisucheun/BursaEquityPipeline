"""`bursa peers ...` - sector and peer comparison tables (offline, facts only)."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from bursa.analysis.peers import (
    METRICS,
    MONEY_METRICS,
    PERCENT_METRICS,
    PeerComparison,
    compare_peers,
    compare_sector,
    list_sectors,
)
from bursa.db.session import session_scope

app = typer.Typer(help="Peer comparison and sector analysis.", no_args_is_help=True)
console = Console()

FyOption = Annotated[
    int | None, typer.Option("--fy", help="Fiscal year (default: each company's latest)."),
]

LABELS = {
    "revenue": "Revenue",
    "patami": "PATAMI",
    "roe": "ROE",
    "roa": "ROA",
    "net_margin": "Net mgn",
    "operating_margin": "Op mgn",
    "asset_turnover": "Asset T/O",
    "equity_multiplier": "Eq mult",
    "revenue_cagr_3y": "Rev 3Y",
    "earnings_cagr_3y": "Earn 3Y",
    "net_debt_ebitda": "ND/EBITDA",
    "fcf_margin": "FCF mgn",
}


def fmt(metric: str, value: float | None) -> str:
    if value is None:
        return "-"
    if metric in PERCENT_METRICS:
        return f"{value * 100:.1f}%"
    if metric in MONEY_METRICS:
        a = abs(value)
        if a >= 1e9:
            return f"{value / 1e9:,.2f}b"
        if a >= 1e6:
            return f"{value / 1e6:,.1f}m"
        return f"{value:,.0f}"
    return f"{value:.2f}x"


def render(result: PeerComparison, highlight: str | None = None) -> None:
    table = Table(title=result.title, show_lines=False)
    table.add_column("Code")
    table.add_column("Name", max_width=22, overflow="ellipsis", no_wrap=True)
    table.add_column("FY", justify="right")
    for m in METRICS:
        table.add_column(LABELS[m], justify="right")

    for r in result.rows:
        cells = []
        for m in METRICS:
            v = r.metrics.get(m)
            if m in r.not_applicable:
                cells.append("[dim]n/a[/]")
            elif m in r.flags:
                cells.append(f"[red]{fmt(m, v)}![/]")
            else:
                pct = r.percentiles.get(m)
                cells.append(fmt(m, v) + (f" [dim]p{pct:.0f}[/]" if pct is not None else ""))
        style = "bold" if r.stock_code == highlight else None
        table.add_row(r.stock_code, r.name, str(r.fiscal_year), *cells, style=style)

    table.add_section()
    for label, attr in (("Q1", "q1"), ("Median", "median"), ("Q3", "q3")):
        table.add_row(
            "", f"[cyan]{label}[/]", "",
            *(f"[cyan]{fmt(m, getattr(result.stats[m], attr))}[/]" for m in METRICS),
        )
    table.add_row("", "[dim]n (flagged)[/]", "", *(
        f"[dim]{s.n}" + (f" ({s.n_flagged})" if s.n_flagged else "") + "[/]"
        for s in (result.stats[m] for m in METRICS)
    ))
    console.print(table)

    for r in result.rows:
        for m, why in r.flags.items():
            console.print(f"[red]![/] {r.stock_code} {LABELS[m]} = {fmt(m, r.metrics[m])}: {why}")
    for note in result.notes:
        console.print(f"[yellow]note:[/] {note}")
    console.print("[dim]pNN = percentile rank within the group; ! = flagged outlier, "
                  "excluded from statistics; n/a = not meaningful for this business model.[/]")


@app.command("sector")
def sector_cmd(
    name: Annotated[str, typer.Argument(help="Sector name, e.g. 'Industrials' (any case).")],
    fy: FyOption = None,
) -> None:
    """Compare every company with facts in a sector."""
    with session_scope() as session:
        result = compare_sector(session, name, fy)
    if not result.rows and not result.missing:
        console.print(f"[red]No companies with facts in sector {name!r}.[/] Try `sectors`.")
        raise typer.Exit(1)
    render(result)


@app.command("peers")
def peers_cmd(
    code: Annotated[str, typer.Argument(help="Stock code to compare.")],
    with_: Annotated[list[str] | None, typer.Option(
        "--with", "-w", help="Peer stock code (repeatable). Default: sector peers.")] = None,
    fy: FyOption = None,
) -> None:
    """Compare one company with explicit or same-sector peers."""
    with session_scope() as session:
        try:
            result = compare_peers(session, code, with_ or None, fy)
        except LookupError as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(1) from e
    render(result, highlight=code)


@app.command("sectors")
def sectors_cmd() -> None:
    """List sectors with company counts and how many have facts."""
    with session_scope() as session:
        rows = list_sectors(session)
    table = Table(title="Sectors")
    table.add_column("Sector")
    table.add_column("Companies", justify="right")
    table.add_column("With facts", justify="right")
    for sector, total, covered in rows:
        table.add_row(sector, str(total), str(covered))
    console.print(table)
