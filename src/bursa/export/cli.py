"""`bursa export ...` - Excel workbooks and long CSV for analysts."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from sqlalchemy import select

from bursa.db.models import Company
from bursa.db.session import session_scope
from bursa.export.long_csv import facts_long_csv

app = typer.Typer(help="Export facts to Excel/CSV for analysts.", no_args_is_help=True)
console = Console()


def _company(session, code: str) -> Company:
    company = session.scalar(select(Company).where(Company.stock_code == code))
    if company is None:
        console.print(f"[red]Unknown stock code {code}[/red]")
        raise typer.Exit(1)
    return company


@app.command("excel")
def excel(
    company: list[str] = typer.Option(..., "--company", "-c", help="Stock code (repeatable)."),
    out: Path = typer.Option(Path("exports"), "--out", "-o", help="Output directory."),
    scale: int = typer.Option(1000, "--scale", help="Divide amounts by this (1000 = RM '000)."),
) -> None:
    """Write one .xlsx per company."""
    from bursa.export.excel import company_workbook  # needs the optional openpyxl extra

    out.mkdir(parents=True, exist_ok=True)
    with session_scope() as session:
        for code in company:
            c = _company(session, code)
            path = out / f"{c.stock_code}_{(c.short_name or c.name).replace(' ', '_')}.xlsx"
            company_workbook(session, c, scale=scale).save(path)
            console.print(f"[green]wrote[/green] {path}")


@app.command("csv")
def csv_cmd(
    out: Path = typer.Option(..., "--out", "-o", help="Output CSV file."),
    company: list[str] = typer.Option(None, "--company", "-c",
                                      help="Stock code (repeatable). Default: all companies."),
) -> None:
    """Write a tidy long CSV (one row per company/year/concept)."""
    with session_scope() as session:
        if company:
            companies = [_company(session, code) for code in company]
        else:
            companies = list(session.scalars(select(Company).order_by(Company.stock_code)))
        n = facts_long_csv(session, companies, out)
    console.print(f"[green]wrote[/green] {n} rows for {len(companies)} companies -> {out}")
