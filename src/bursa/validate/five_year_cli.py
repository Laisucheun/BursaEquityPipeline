"""`bursa fiveyear ...` - cross-check facts against annual-report 5-year summaries.

Read-only: extracts summary pages on the fly and compares; writes nothing.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from bursa.db.models import Company, Fact
from bursa.db.session import session_scope
from bursa.validate.five_year_check import CLASSES, CompanyFiveYearResult, check_company

app = typer.Typer(help="Five-year summary page cross-validation (read-only).", no_args_is_help=True)
console = Console()

_STYLE = {
    "MATCH": "green", "CLOSE": "yellow", "MISMATCH": "red",
    "SCALE_ERROR": "magenta", "ONLY_IN_SUMMARY": "dim",
}


@app.callback()
def _main() -> None:
    """Five-year summary page cross-validation (read-only)."""


def _fmt(value) -> str:  # type: ignore[no-untyped-def]
    if value is None:
        return "-"
    v = float(value)
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:,.1f}m"
    return f"{v:,.2f}"


@app.command("check")
def check(
    company: Annotated[
        list[str] | None, typer.Option("--company", "-c", help="Stock code(s). Repeatable."),
    ] = None,
    all_companies: Annotated[
        bool, typer.Option("--all", help="Every company that has facts."),
    ] = False,
    years: Annotated[
        int,
        typer.Option("--years", help="Newest N report years per company, all volumes (0 = all)."),
    ] = 1,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="List every non-matching check."),
    ] = False,
    dps: Annotated[bool, typer.Option("--dps", help="Also list dividend-per-share rows.")] = False,
) -> None:
    """Compare each company's 5-year summary page(s) with its extracted facts."""
    results: list[CompanyFiveYearResult] = []
    with session_scope() as session:
        if all_companies:
            ids = select(Fact.company_id).distinct()
            companies = session.scalars(
                select(Company).where(Company.id.in_(ids)).order_by(Company.stock_code)
            ).all()
        else:
            if not company:
                raise typer.BadParameter("pass --company CODE (repeatable) or --all")
            companies = session.scalars(
                select(Company).where(Company.stock_code.in_(company)).order_by(Company.stock_code)
            ).all()
            missing = set(company) - {c.stock_code for c in companies}
            for code in sorted(missing):
                console.print(f"[red]unknown company {code}[/red]")

        for comp in companies:
            res = check_company(
                session, comp, max_years=years or None, include_dps=dps,
            )
            results.append(res)
            _print_company(res, verbose=verbose)
        session.rollback()  # read-only: never commit anything

    if len(results) > 1:
        _print_overall(results)


def _print_company(res: CompanyFiveYearResult, *, verbose: bool) -> None:
    counts = res.counts
    agreement = res.agreement
    pages = sorted({t.page_no for d in res.documents for t in d.tables})
    console.print(
        f"[bold]{res.stock_code}[/bold] {res.name}: "
        f"{res.documents_with_summary}/{res.documents_scanned} docs with summary"
        + (f" (pages {pages})" if pages else "")
        + (f", agreement {agreement:.0%}" if agreement is not None else "")
    )
    for d in res.documents:
        if d.error:
            console.print(f"  [red]doc {d.document_id}: {d.error}[/red]")
    if not res.checks:
        return
    if res.year_shift:
        console.print(
            f"  [bold red]our facts sit {res.year_shift:+d} fiscal year vs the summary "
            f"(check fy_end_month / FYE change)[/bold red]"
        )
    console.print("  " + "  ".join(
        f"[{_STYLE[k]}]{k}={counts.get(k, 0)}[/{_STYLE[k]}]" for k in CLASSES
    ))
    if verbose:
        table = Table(show_header=True, header_style="bold", pad_edge=False)
        for col in ("FY", "Concept", "Summary", "Ours", "Class", "Detail", "Page"):
            table.add_column(col)
        for c in res.checks:
            if c.classification == "MATCH":
                continue
            table.add_row(
                str(c.fiscal_year), c.concept_key, _fmt(c.summary_value), _fmt(c.our_value),
                f"[{_STYLE[c.classification]}]{c.classification}[/{_STYLE[c.classification]}]",
                c.detail, str(c.page_no),
            )
        console.print(table)


def _print_overall(results: list[CompanyFiveYearResult]) -> None:
    by_concept: dict[str, Counter[str]] = defaultdict(Counter)
    older: Counter[str] = Counter()
    recent: Counter[str] = Counter()
    for res in results:
        cutoff = res.older_cutoff()
        for c in res.checks:
            by_concept[c.concept_key][c.classification] += 1
            bucket = older if cutoff is not None and c.fiscal_year < cutoff else recent
            bucket[c.classification] += 1

    with_summary = sum(1 for r in results if r.documents_with_summary)
    console.print(f"\n[bold]{with_summary}/{len(results)} companies yielded a summary[/bold]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Concept")
    for k in CLASSES:
        table.add_column(k, justify="right")
    for concept in sorted(by_concept):
        table.add_row(concept, *(str(by_concept[concept].get(k, 0)) for k in CLASSES))
    table.add_row("[bold]recent 4 yrs[/bold]", *(str(recent.get(k, 0)) for k in CLASSES))
    table.add_row("[bold]older yrs[/bold]", *(str(older.get(k, 0)) for k in CLASSES))
    console.print(table)
