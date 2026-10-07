"""Command line interface: ``bursa <command>``."""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
import os
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from bursa.analysis.peers_cli import app as peers_app
from bursa.config import get_settings
from bursa.db.enums import DocSource, DocStatus, DocType, Market, ScrapeStatus, Statement
from bursa.db.migrate import app as db_app
from bursa.db.models import Company, Concept, ConceptSynonym, Document, Fact, ScrapeAttempt
from bursa.db.session import get_engine, session_scope
from bursa.export.cli import app as export_app
from bursa.mapping.synonyms import seed_concepts
from bursa.pipeline.ingest import scan_inbox
from bursa.storage import is_remote, materialize
from bursa.storage.migrate import app as storage_app
from bursa.validate.five_year_cli import app as fiveyear_app

app = typer.Typer(help="Bursa Malaysia equity pipeline.", no_args_is_help=True)
company_app = typer.Typer(help="Manage the company watchlist.", no_args_is_help=True)
scrape_app = typer.Typer(help="Automated annual report acquisition.", no_args_is_help=True)
extract_app = typer.Typer(help="Deterministic (no-API) statement extraction.", no_args_is_help=True)
normalize_app = typer.Typer(help="Write Fact rows from extraction (no-API).", no_args_is_help=True)
validate_app = typer.Typer(help="Run accounting-identity checks over facts.", no_args_is_help=True)
benchmark_app = typer.Typer(help="Cross-validate facts against external sources.", no_args_is_help=True)
valuation_app = typer.Typer(help="Compute valuation metrics (FCFF, FCFE, EV/EBITDA).", no_args_is_help=True)
app.add_typer(company_app, name="company")
app.add_typer(scrape_app, name="scrape")
app.add_typer(extract_app, name="extract")
app.add_typer(normalize_app, name="normalize")
app.add_typer(validate_app, name="validate")
app.add_typer(benchmark_app, name="benchmark")
app.add_typer(valuation_app, name="valuation")
analysis_app = typer.Typer(help="DuPont, growth, and market-price ratios.", no_args_is_help=True)
app.add_typer(analysis_app, name="analysis")
app.add_typer(db_app, name="db")
app.add_typer(export_app, name="export")
app.add_typer(peers_app, name="peers")
app.add_typer(storage_app, name="storage")
app.add_typer(fiveyear_app, name="fiveyear")

console = Console()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@app.command("init-db")
def init_db(
    seed: Annotated[bool, typer.Option(help="Also seed the concept taxonomy.")] = True,
) -> None:
    """Bring the schema to the latest Alembic revision.

    A fresh database is migrated from scratch; one built earlier by
    ``create_all`` (never stamped) is adopted via ``bursa db stamp`` logic.
    """
    from sqlalchemy import inspect

    from bursa.db.migrate import current_revision, stamp_existing_db, upgrade_to_head

    if current_revision() is None and inspect(get_engine()).has_table("companies"):
        console.print(stamp_existing_db())
    else:
        upgrade_to_head()
    console.print(f"[green]schema at head[/] for {get_settings().database_url}")
    if seed:
        seed_taxonomy()


@app.command("seed")
def seed_taxonomy() -> None:
    """Insert (or top up) the canonical concepts and their seed synonyms."""
    with session_scope() as session:
        concepts, synonyms = seed_concepts(session)
    console.print(f"[green]seeded[/] {concepts} concepts, {synonyms} synonyms")


@app.command("ingest")
def ingest(
    path: Annotated[
        Path | None,
        typer.Argument(help="A PDF or a directory. Defaults to the configured inbox."),
    ] = None,
) -> None:
    """Ingest PDFs from the drop folder (or a given path)."""
    target = path or get_settings().inbox_dir
    with session_scope() as session:
        if target.is_file():
            from bursa.pipeline.ingest import ingest_file

            results = [ingest_file(session, target)]
        else:
            results = scan_inbox(session, target)

    created = sum(1 for _, was_new in results if was_new)
    console.print(
        f"[green]ingested[/] {created} new, "
        f"{len(results) - created} already present, from {target}"
    )


@app.command("upload")
def upload(
    pdf_path: Annotated[
        Path,
        typer.Argument(help="Path to the PDF annual report file."),
    ],
    stock_code: Annotated[
        str,
        typer.Option("--company", "-c", help="Bursa 4-digit stock code."),
    ],
    skip_validation: Annotated[
        bool,
        typer.Option("--skip-validation", help="Skip accounting-identity validation."),
    ] = False,
    ocr: Annotated[
        bool,
        typer.Option("--ocr", help="Enable OCR fallback for scanned pages (requires Tesseract + Poppler)."),
    ] = False,
) -> None:
    """Upload a PDF annual report and run the full extraction pipeline.

    Ingests the file, checks it contains financial statements, extracts
    the three primary statements, writes Fact rows, derives missing facts,
    and runs validation — all in one shot.

    Example::

        bursa upload "C:/Downloads/AnnualReport2024.pdf" --company 1295
    """
    from bursa.extract.statement_extract import extract_statements
    from bursa.pipeline.derive import derive_facts_for_company
    from bursa.pipeline.normalize import write_facts_for_company
    from bursa.pipeline.validate import validate_company
    from bursa.scrapers.content_filter import has_financial_statements

    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        console.print(f"[red]file not found:[/] {pdf_path}")
        raise typer.Exit(code=1)
    if pdf_path.suffix.lower() != ".pdf":
        console.print(f"[red]not a PDF file:[/] {pdf_path}")
        raise typer.Exit(code=1)

    with session_scope() as session:
        company = session.execute(
            select(Company).where(Company.stock_code == stock_code)
        ).scalar_one_or_none()
        if company is None:
            console.print(f"[red]no company with stock code {stock_code}[/]")
            raise typer.Exit(code=1)

        console.print(f"uploading for [bold]{stock_code}[/] {company.name}")

        # 1. Ingest
        from bursa.pipeline.ingest import ingest_file

        doc, created = ingest_file(
            session, pdf_path,
            source=DocSource.UPLOAD,
            company_id=company.id,
            doc_type=DocType.ANNUAL_REPORT,
        )
        if not created:
            console.print(f"[yellow]already ingested[/] (document {doc.id})")
        else:
            console.print(f"[green]ingested[/] as document {doc.id} ({doc.page_count} pages)")

        # 2. Content check
        storage = materialize(doc)
        if not storage.is_file():
            storage = pdf_path
        if not has_financial_statements(storage):
            console.print(
                "[red]no financial statements detected[/] — this PDF may be a "
                "narrative report, sustainability report, or chairman's statement. "
                "The extraction will proceed but may yield no data."
            )

        # 3. Extract
        console.print("extracting statements…")
        result = extract_statements(session, doc.id, storage, company_id=company.id, ocr=ocr)
        found = list(result.statements.keys())
        if found:
            labels = ", ".join(s.value for s in found)
            console.print(f"[green]found:[/] {labels}")
        else:
            console.print("[yellow]no statements found in this document[/]")
            raise typer.Exit(code=0)

        # 4. Normalize
        console.print("writing facts…")
        norm = write_facts_for_company(session, company)
        console.print(
            f"[green]facts:[/] {norm.facts_written} written, "
            f"{norm.facts_updated} updated, {norm.periods_created} periods"
        )

        # 5. Derive
        derive = derive_facts_for_company(session, company)
        if derive.derived:
            console.print(f"[green]derived:[/] {derive.derived} facts from accounting identities")

        # 6. Validate
        if not skip_validation:
            val = validate_company(session, company)
            if val.rules_run:
                style = "green" if val.rules_failed == 0 else "red"
                console.print(
                    f"[{style}]validation:[/] {val.rules_passed}/{val.rules_run} passed"
                )
                if val.rules_failed:
                    for f in val.failures:
                        console.print(f"  [red]✗[/] {f.rule_key}: {f.detail}")
                if val.comparative and (val.comparative.rounding + val.comparative.restatement) > 0:
                    console.print(
                        f"[yellow]comparative:[/] {val.comparative.match} match, "
                        f"{val.comparative.rounding} rounding, "
                        f"{val.comparative.restatement} restatement"
                    )

    console.print("[green]done[/]")


@company_app.command("add")
def company_add(
    stock_code: Annotated[str, typer.Argument(help="Bursa 4-digit stock code.")],
    name: Annotated[str, typer.Argument()],
    fy_end_month: Annotated[
        int | None, typer.Option(help="Month (1-12) the financial year ends.")
    ] = None,
    market: Annotated[Market, typer.Option()] = Market.MAIN,
    sector: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Add a company to the watchlist."""
    with session_scope() as session:
        existing = session.execute(
            select(Company).where(Company.stock_code == stock_code)
        ).scalar_one_or_none()
        if existing is not None:
            console.print(f"[yellow]{stock_code} already exists[/]: {existing.name}")
            raise typer.Exit(code=1)
        session.add(
            Company(
                stock_code=stock_code,
                name=name,
                market=market,
                sector=sector,
                fy_end_month=fy_end_month,
            )
        )
    console.print(f"[green]added[/] {stock_code} {name}")


@company_app.command("set-ir-url")
def company_set_ir_url(
    stock_code: Annotated[str, typer.Argument(help="Bursa 4-digit stock code.")],
    url: Annotated[str, typer.Argument(help="The company's IR/investor-relations homepage.")],
) -> None:
    """Register a company's investor-relations homepage for the IR scraper.

    There is no reliable way to auto-discover an arbitrary corporate website,
    so this is a manual, one-time step per company - verify the URL actually
    resolves before registering it.
    """
    with session_scope() as session:
        company = session.execute(
            select(Company).where(Company.stock_code == stock_code)
        ).scalar_one_or_none()
        if company is None:
            console.print(f"[red]no company with stock code {stock_code}[/]")
            raise typer.Exit(code=1)
        company.ir_homepage_url = url
    console.print(f"[green]set[/] {stock_code} ir_homepage_url = {url}")


@company_app.command("import-csv")
def company_import_csv(
    csv_path: Annotated[str, typer.Argument(help="Path to CSV with stock_code,company_name,industry_group,primary_sector columns.")],
    watchlist: Annotated[bool, typer.Option("--watchlist/--no-watchlist", help="Add to watchlist.")] = True,
) -> None:
    """Bulk-import companies from a CSV file."""
    import csv as csv_mod

    with open(csv_path, encoding="utf-8") as f:
        reader = csv_mod.DictReader(f)
        rows = list(reader)

    added = 0
    skipped = 0
    with session_scope() as session:
        existing_codes = set(
            session.scalars(select(Company.stock_code)).all()
        )
        for row in rows:
            code = row["stock_code"].strip()
            name = row["company_name"].strip()
            sector = row.get("primary_sector", "").strip() or None
            if code in existing_codes:
                skipped += 1
                continue
            session.add(Company(
                stock_code=code,
                name=name,
                market=Market.MAIN,
                sector=sector,
                is_watchlist=watchlist,
            ))
            existing_codes.add(code)
            added += 1

    console.print(f"[green]added[/] {added} companies, [dim]skipped {skipped} existing[/]")


@company_app.command("list")
def company_list() -> None:
    """Show the watchlist and how many documents each company has."""
    table = Table("Code", "Name", "Market", "FY end", "Docs", "IR URL")
    with session_scope() as session:
        rows = session.execute(
            select(Company, func.count(Document.id))
            .outerjoin(Document, Document.company_id == Company.id)
            .group_by(Company.id)
            .order_by(Company.stock_code)
        ).all()
        for company, doc_count in rows:
            table.add_row(
                company.stock_code,
                company.name,
                str(company.market),
                str(company.fy_end_month or "-"),
                str(doc_count),
                "[green]set[/]" if company.ir_homepage_url else "[dim]-[/]",
            )
    console.print(table)


@scrape_app.command("annual-reports")
def scrape_annual_reports_cmd(
    stock_codes: Annotated[
        list[str] | None,
        typer.Option("--company", help="Limit to these stock codes. Default: the whole watchlist."),
    ] = None,
    sector: Annotated[
        str | None,
        typer.Option("--sector", help="Limit to companies in this sector (e.g. 'Financials')."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Resolve PDF URLs without downloading or ingesting.")
    ] = False,
    years: Annotated[
        int, typer.Option("--years", help="How many of the most recent years to fetch per company.")
    ] = 1,
    pdf_dir: Annotated[
        Path | None,
        typer.Option("--pdf-dir", help="Save downloaded PDFs to this directory (stock_code subfolders). Non-AR PDFs are deleted automatically."),
    ] = None,
) -> None:
    """Sniff each watchlist company's IR site for its annual report(s).

    `--years N` fetches the N most recent distinct years found on each
    site, not just the newest - a company with fewer years available just
    yields fewer attempts, not a padded failure.

    bursamalaysia.com sits behind a CAPTCHA and is never targeted - see
    src/bursa/scrapers/. A company with no `ir_homepage_url` set (via
    `bursa company set-ir-url`) is logged as skipped, not silently ignored.
    """
    from bursa.scrapers.run_annual_reports import scrape_annual_reports

    with session_scope() as session:
        query = select(Company).where(Company.is_watchlist.is_(True))
        if stock_codes:
            query = select(Company).where(Company.stock_code.in_(stock_codes))
        if sector:
            query = query.where(Company.sector == sector)
        companies = list(session.scalars(query))

        if not companies:
            console.print("[yellow]no matching companies[/]")
            raise typer.Exit(code=1)

        if pdf_dir:
            pdf_dir.mkdir(parents=True, exist_ok=True)
            console.print(f"[dim]saving PDFs to {pdf_dir}/[/]")

        attempts = asyncio.run(
            scrape_annual_reports(session, companies, dry_run=dry_run, years=years, pdf_dir=pdf_dir)
        )

    counts: dict[str, int] = {}
    for attempt in attempts:
        counts[attempt.status] = counts.get(attempt.status, 0) + 1
    summary = ", ".join(f"{status}={n}" for status, n in sorted(counts.items()))
    prefix = "[yellow]dry run[/]" if dry_run else "[green]done[/]"
    console.print(f"{prefix} - {len(attempts)} companies: {summary}")
    console.print(f"run label: {attempts[0].run_label} (see `bursa scrape report`)")


@scrape_app.command("discover-ir-urls")
def discover_ir_urls_cmd(
    count: Annotated[
        int, typer.Option(help="How many companies to discover if the watchlist is empty.")
    ] = 20,
    out: Annotated[
        Path, typer.Option(help="Where to write the full report as JSON.")
    ] = Path("ir_url_discovery.json"),
    commit: Annotated[
        bool,
        typer.Option(
            "--commit",
            help="Write high/medium-confidence results to the database. "
            "Without this flag, nothing is written - review the report first.",
        ),
    ] = False,
    min_confidence: Annotated[
        str, typer.Option(help="Minimum confidence to --commit: high, medium, or low.")
    ] = "medium",
    model: Annotated[str, typer.Option(help="Anthropic model to use.")] = "claude-sonnet-5",
    batch_size: Annotated[
        int, typer.Option("--batch-size", help="Max companies per run (0 = all).")
    ] = 50,
    offset: Annotated[
        int, typer.Option(help="Skip this many companies (ordered by stock_code).")
    ] = 0,
    skip_existing: Annotated[
        bool,
        typer.Option(
            "--skip-existing/--include-existing",
            help="Skip companies that already have an IR URL set.",
        ),
    ] = True,
    batch_api: Annotated[
        int,
        typer.Option(
            "--batch-api",
            help="Companies per API call (batches multiple lookups into one prompt). "
            "1 = one call per company (original behavior).",
        ),
    ] = 10,
) -> None:
    """Use web-search-grounded Claude calls to propose company IR homepages.

    Never auto-trusted: every result carries the model's own confidence and
    reasoning, written to `out` for review. Guessing is a demonstrated failure
    mode in this project (a plain recalled URL was simply wrong during
    recon) - `--commit` only writes high/medium-confidence hits, and even
    those are worth spot-checking before a real scrape run.
    """
    from bursa.scrapers.discover_ir_via_llm import (
        CompanyCandidate,
        discover_watchlist_companies,
        find_ir_urls,
        find_ir_urls_batched,
    )

    with session_scope() as session:
        query = select(Company).order_by(Company.stock_code)
        if skip_existing:
            query = query.where(Company.ir_homepage_url.is_(None))
        existing = list(session.scalars(query))

    if existing:
        if offset:
            existing = existing[offset:]
        if batch_size > 0:
            existing = existing[:batch_size]
        console.print(f"using {len(existing)} companies (offset={offset}, batch_size={batch_size})")
        companies = [
            CompanyCandidate(stock_code=c.stock_code, name=c.name, sector=c.sector)
            for c in existing
        ]
    else:
        console.print(
            f"[yellow]watchlist is empty[/] - discovering {count} companies via Claude + web search"
        )
        companies = discover_watchlist_companies(count=count, model=model)
        console.print(f"discovered {len(companies)} candidate companies (unverified - see report)")

    console.print(f"looking up IR homepages for {len(companies)} companies (this calls the API)...")
    if batch_api > 1:
        results = find_ir_urls_batched(companies, batch_size=batch_api, model=model)
    else:
        results = find_ir_urls(companies, model=model)

    if out.exists():
        prev = json.loads(out.read_text(encoding="utf-8"))
        prev_by_code = {r["stock_code"]: r for r in prev}
        for r in results:
            prev_by_code[r.stock_code] = r.model_dump()
        merged = list(prev_by_code.values())
        out.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        console.print(f"[dim]merged {len(results)} new results into {out} ({len(merged)} total)[/]")
    else:
        out.write_text(json.dumps([r.model_dump() for r in results], indent=2), encoding="utf-8")

    _report_and_commit_ir_urls(results, out, commit=commit, min_confidence=min_confidence)


@scrape_app.command("import-ir-urls")
def import_ir_urls_cmd(
    report: Annotated[
        Path,
        typer.Argument(
            help="A JSON file of IR URL candidates - the same shape "
            "discover-ir-urls writes, e.g. brought back from a claude.ai chat."
        ),
    ],
    commit: Annotated[
        bool,
        typer.Option(
            "--commit",
            help="Write high/medium-confidence results to the database. "
            "Without this flag, nothing is written - review the report first.",
        ),
    ] = False,
    min_confidence: Annotated[
        str, typer.Option(help="Minimum confidence to --commit: high, medium, or low.")
    ] = "medium",
) -> None:
    """Ingest IR URL candidates from a JSON file instead of calling the API.

    For a claude.ai chat's JSON answer: save it to a file (or paste it into
    one), each item shaped like
    `{"stock_code": "...", "company_name": "...", "ir_homepage_url": "...",
    "confidence": "high|medium|low|none", "reasoning": "..."}`, then run this.
    Same "never auto-trusted without review" rule as `discover-ir-urls` - only
    `--commit` writes anything, and only at or above `--min-confidence`.
    """
    from bursa.scrapers.discover_ir_via_llm import IrUrlCandidate

    try:
        raw = json.loads(report.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        console.print(f"[red]could not read {report} as JSON[/]: {exc}")
        raise typer.Exit(code=1) from exc

    try:
        results = [IrUrlCandidate.model_validate(item) for item in raw]
    except Exception as exc:
        console.print(f"[red]{report} does not match the expected shape[/]: {exc}")
        raise typer.Exit(code=1) from exc

    _report_and_commit_ir_urls(results, report, commit=commit, min_confidence=min_confidence)


def _report_and_commit_ir_urls(
    results: list, source: Path, commit: bool, min_confidence: str
) -> None:
    """Shared by `discover-ir-urls` and `import-ir-urls`: print the table,
    and write only what clears the confidence bar under `--commit`."""
    order = {"high": 3, "medium": 2, "low": 1, "none": 0}
    threshold = order.get(min_confidence.lower(), 2)

    table = Table("Code", "Name", "Confidence", "URL", "Reasoning")
    committed = 0
    with session_scope() as session:
        for result in results:
            style = {"high": "green", "medium": "yellow", "low": "red"}.get(
                result.confidence, "dim"
            )
            table.add_row(
                result.stock_code,
                result.company_name,
                f"[{style}]{result.confidence}[/]",
                result.ir_homepage_url or "-",
                (result.reasoning or "")[:60],
            )

            if not commit or not result.ir_homepage_url:
                continue
            if order.get(result.confidence, 0) < threshold:
                continue

            company = session.execute(
                select(Company).where(Company.stock_code == result.stock_code)
            ).scalar_one_or_none()
            if company is None:
                company = Company(stock_code=result.stock_code, name=result.company_name)
                session.add(company)
                session.flush()
            company.ir_homepage_url = result.ir_homepage_url
            committed += 1

    console.print(table)
    console.print(f"source: {source}")
    if commit:
        console.print(f"[green]committed[/] {committed} of {len(results)} to the database")
    else:
        console.print(
            "[yellow]nothing written to the database[/] - re-run with --commit after reviewing"
        )


@scrape_app.command("report")
def scrape_report(
    run_label: Annotated[
        str | None, typer.Option(help="Show one run. Default: the most recent.")
    ] = None,
) -> None:
    """Summarise a scrape run: found/missed/error, per company, with why."""
    with session_scope() as session:
        if run_label is None:
            run_label = session.scalar(
                select(ScrapeAttempt.run_label)
                .order_by(ScrapeAttempt.checked_at.desc())
                .limit(1)
            )
        if run_label is None:
            console.print("[yellow]no scrape runs recorded yet[/]")
            raise typer.Exit(code=1)

        rows = session.execute(
            select(ScrapeAttempt, Company.stock_code, Company.name)
            .join(Company, Company.id == ScrapeAttempt.company_id)
            .where(ScrapeAttempt.run_label == run_label)
            .order_by(ScrapeAttempt.status, Company.stock_code)
        ).all()

    if not rows:
        console.print(f"[yellow]no attempts found for run {run_label!r}[/]")
        raise typer.Exit(code=1)

    counts: dict[str, int] = {}
    table = Table("Code", "Name", "Status", "Detail")
    for attempt, stock_code, name in rows:
        counts[attempt.status] = counts.get(attempt.status, 0) + 1
        style = {
            ScrapeStatus.FOUND: "green",
            ScrapeStatus.NOT_FOUND: "yellow",
            ScrapeStatus.ERROR: "red",
        }.get(attempt.status, "dim")
        table.add_row(
            stock_code, name, f"[{style}]{attempt.status}[/]", (attempt.detail or "")[:80]
        )

    console.print(f"run: {run_label}")
    console.print(", ".join(f"{status}={n}" for status, n in sorted(counts.items())))
    console.print(table)


@scrape_app.command("prune")
def scrape_prune(
    stock_codes: Annotated[
        list[str] | None,
        typer.Option("--company", help="Limit to these stock codes. Default: every company."),
    ] = None,
    apply_: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Actually delete. Without this, only reports what would be deleted.",
        ),
    ] = False,
) -> None:
    """Delete already-ingested documents that contain no primary financial
    statement - decoys that slipped in while link-text filtering was still
    strict (`ir_fallback.py`'s old, replaced strategy) or that got swept in
    once it was loosened (chairman/MD statements, sustainability reports, a
    narrative-only "annual report" bundled under the same year's listing as
    the real financial-statements volume - see that module's docstring for
    why link text alone can't tell these apart before download).

    Never deletes a company's *only* document, even if it fails the check -
    that would leave the company with nothing at all, which is worse than
    leaving a questionable document in place for a human to look at; those
    are reported separately instead. Defaults to a dry run; pass --apply to
    actually delete.
    """
    from bursa.scrapers.content_filter import has_financial_statements

    with session_scope() as session:
        query = select(Company)
        if stock_codes:
            query = query.where(Company.stock_code.in_(stock_codes))
        companies = list(session.scalars(query))

        to_delete: list[tuple[Company, Document]] = []
        only_document_failed: list[tuple[Company, Document]] = []

        for company in companies:
            docs = list(session.scalars(select(Document).where(Document.company_id == company.id)))
            if len(docs) <= 1:
                continue  # nothing to prune against - would leave zero documents

            relevant, irrelevant = [], []
            for doc in docs:
                path = materialize(doc)
                if path.is_file() and has_financial_statements(path):
                    relevant.append(doc)
                else:
                    irrelevant.append(doc)

            if not relevant:
                # every document for this company failed the check - deleting
                # any of them would leave zero, so report instead of deleting.
                only_document_failed.extend((company, doc) for doc in irrelevant)
                continue

            to_delete.extend((company, doc) for doc in irrelevant)

        if to_delete:
            table = Table("Code", "Name", "Document", "Source URL")
            for company, doc in to_delete:
                table.add_row(
                    company.stock_code, company.name, Path(doc.storage_path).name, doc.source_url or ""
                )
            console.print(
                f"{'Deleting' if apply_ else 'Would delete'} {len(to_delete)} irrelevant document(s):"
            )
            console.print(table)
        else:
            console.print("[green]nothing to prune[/]")

        if only_document_failed:
            console.print(
                f"\n[yellow]{len(only_document_failed)} document(s) failed the check but are "
                "their company's only document - left in place, not deleted:[/]"
            )
            for company, doc in only_document_failed:
                console.print(f"  {company.stock_code} {company.name}: {Path(doc.storage_path).name}")

        if not to_delete:
            return

        if not apply_:
            console.print("\n[dim]dry run - pass --apply to actually delete[/]")
            return

        for _company, doc in to_delete:
            try:
                if not is_remote(doc):
                    Path(doc.storage_path).unlink(missing_ok=True)
            except OSError as exc:
                console.print(f"[red]could not delete file for document {doc.id}: {exc}[/]")
            session.delete(doc)
        session.flush()
        console.print(f"[green]deleted {len(to_delete)} document(s)[/]")


@app.command("status")
def status() -> None:
    """Pipeline health at a glance."""
    with session_scope() as session:
        concepts = session.scalar(select(func.count(Concept.concept_key))) or 0
        synonyms = session.scalar(select(func.count(ConceptSynonym.id))) or 0
        companies = session.scalar(select(func.count(Company.id))) or 0
        by_status = session.execute(
            select(Document.status, func.count(Document.id)).group_by(Document.status)
        ).all()

    console.print(f"database   : {get_settings().database_url}")
    console.print(f"concepts   : {concepts}")
    console.print(f"synonyms   : {synonyms}")
    console.print(f"companies  : {companies}")

    table = Table("Document status", "Count")
    counts = dict(by_status)
    for state in DocStatus:
        table.add_row(str(state), str(counts.get(state, 0)))
    console.print(table)


@extract_app.command("statements")
def extract_statements_cmd(
    stock_codes: Annotated[
        list[str] | None,
        typer.Option("--company", help="Limit to these stock codes. Default: every company."),
    ] = None,
    out: Annotated[
        Path, typer.Option(help="Where to write the full extraction as JSON.")
    ] = Path("statement_extraction.json"),
    ocr: Annotated[
        bool,
        typer.Option("--ocr", help="Enable OCR fallback for scanned pages (requires Tesseract + Poppler)."),
    ] = False,
) -> None:
    """Extract the three primary statements from each company's ingested
    documents - deterministic only, no LLM call, no API credentials needed.

    A company can have several scraped PDFs (a narrative-heavy "integrated
    report" plus a separate, smaller "financial report" volume - confirmed
    real for Public Bank, whose 452-page main report never contains the
    audited statements at all, which instead live in a separate 328-page
    filing). Picking only the largest document by page count silently used
    the wrong one. Every document over a trivial page-count floor is tried,
    and each statement is kept from whichever document scores it highest -
    independently, since a company's cash flow statement and its balance
    sheet need not come from the same file.

    Concept mapping uses the seeded synonym table; a row it doesn't recognise
    keeps its raw printed label rather than a fabricated concept key. Column
    years come from each column's own header text where one is written
    there. Nothing is written to the `facts` table - real period/basis
    assignment needs the LLM mapper (`bursa.mapping.llm_mapper`), which this
    does not call. See `src/bursa/extract/statement_extract.py`.
    """
    from bursa.extract.statement_extract import extract_statements

    # Below this many pages a document is almost certainly a standalone
    # chairman/MD statement (confirmed real: several 3-6 page filings
    # alongside a company's real reports), never worth a full 3-statement scan.
    MIN_CANDIDATE_PAGES = 10

    summary = Table("Code", "Name", "IS", "BS", "CF", "EQ")
    report: list[dict] = []

    with session_scope() as session:
        query = select(Company)
        if stock_codes:
            query = query.where(Company.stock_code.in_(stock_codes))
        companies = list(session.scalars(query))

        for company in companies:
            docs = list(
                session.scalars(
                    select(Document)
                    .where(Document.company_id == company.id)
                    .order_by(Document.page_count.desc())
                )
            )
            docs = [d for d in docs if d.page_count >= MIN_CANDIDATE_PAGES]
            if not docs:
                continue

            best: dict[Statement, tuple[float, object, Document]] = {}
            skipped_by_stmt: dict[Statement, str] = {}
            tried_any = False

            for doc in docs:
                pdf_path = materialize(doc)
                if not pdf_path.is_file():
                    console.print(f"[red]missing file for {company.stock_code}[/]: {pdf_path}")
                    continue

                tried_any = True
                console.print(f"extracting {company.stock_code} {company.name} ({pdf_path.name})...")
                result = extract_statements(session, doc.id, pdf_path, company_id=company.id, ocr=ocr)

                for stmt, s in result.statements.items():
                    current = best.get(stmt)
                    if current is None or s.final_score > current[0]:
                        best[stmt] = (s.final_score, s, doc)
                for stmt, why in result.skipped_pages.items():
                    if stmt not in best:
                        skipped_by_stmt.setdefault(stmt, why)

            if not tried_any:
                continue

            def cell(stmt: Statement) -> str:
                if stmt in best:
                    s = best[stmt][1]
                    span = f"+{s.continuation_page_no}" if s.continuation_page_no else ""
                    return f"p{s.page_no}{span} ({s.mapped_row_count}/{len(s.rows)})"
                return "[dim]-[/]"

            summary.add_row(
                company.stock_code,
                company.name,
                cell(Statement.INCOME_STATEMENT),
                cell(Statement.BALANCE_SHEET),
                cell(Statement.CASH_FLOW),
                cell(Statement.EQUITY),
            )

            report.append(
                {
                    "stock_code": company.stock_code,
                    "company_name": company.name,
                    "statements": {
                        stmt.value: {
                            "document_id": doc.id,
                            "source_file": Path(doc.storage_path).name,
                            "page_no": s.page_no,
                            "final_score": s.final_score,
                            "row_keyword_hits": s.row_keyword_hits,
                            "continuation_page_no": s.continuation_page_no,
                            "scale_token": s.scale.token,
                            "scale_multiplier": s.scale.multiplier,
                            "currency": s.scale.currency,
                            "columns": [
                                {"col_index": c.col_index, "header": c.header_text, "year": c.year}
                                for c in s.columns
                            ],
                            "rows": [
                                {
                                    "label": r.label,
                                    "concept_key": r.concept_key,
                                    "indent_level": r.indent_level,
                                    "values": r.values,
                                }
                                for r in s.rows
                            ],
                        }
                        for stmt, (_, s, doc) in best.items()
                    },
                    "skipped": {stmt.value: why for stmt, why in skipped_by_stmt.items()},
                }
            )

    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    console.print(summary)
    console.print(f"full extraction written to {out}")


@normalize_app.command("facts")
def normalize_facts_cmd(
    stock_codes: Annotated[
        list[str] | None,
        typer.Option("--company", help="Limit to these stock codes. Default: every company."),
    ] = None,
    only_without_facts: Annotated[
        bool, typer.Option("--only-without-facts", help="Skip companies that already have facts."),
    ] = False,
    workers: Annotated[
        int,
        typer.Option(help="Extraction processes. 1 = sequential. Default: CPU count - 2."),
    ] = max(1, (os.cpu_count() or 2) - 2),
) -> None:
    """Write `Fact` rows from each company's ingested documents.

    Deterministic only, same as `extract statements`: concept mapping from
    the seed synonym table, period end dates read directly off each
    statement's own subtitle text ("for the financial year ended ...", "as
    at ..."). No LLM call, no API credentials needed. A statement or column
    that can't be resolved this way (quarterly column semantics, a label the
    synonym table doesn't recognise) is skipped and reported, not guessed -
    see `src/bursa/pipeline/normalize.py`.

    PDF extraction (the slow, CPU-bound part) runs in ``--workers`` processes;
    this process alone writes, one transaction per company, because SQLite
    allows a single writer. A company that fails is reported and skipped.
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed

    from bursa.pipeline.jobs import track_job
    from bursa.pipeline.normalize import (
        CompanyExtraction,
        extract_company,
        extract_company_by_id,
        write_company_extraction,
    )

    table = Table("Code", "Name", "Written", "Updated", "Deleted", "Periods", "Skipped")
    totals = {"written": 0, "updated": 0, "deleted": 0, "stale_runs": 0}

    with session_scope() as session:
        query = (
            select(Company.id, Company.stock_code, Company.name)
            .where(Company.id.in_(select(Document.company_id)))
            .order_by(Company.stock_code)
        )
        if stock_codes:
            query = query.where(Company.stock_code.in_(stock_codes))
        if only_without_facts:
            query = query.where(Company.id.not_in(select(Fact.company_id)))
        targets = session.execute(query).all()
    names = {cid: (code, name) for cid, code, name in targets}

    def write(company_id: int, extracted: CompanyExtraction) -> None:
        code, name = names[company_id]
        with session_scope() as session:
            result = write_company_extraction(session, session.get(Company, company_id), extracted)
        if not result.documents_used and not result.skipped_statements:
            return
        totals["written"] += result.facts_written
        totals["updated"] += result.facts_updated
        totals["deleted"] += result.facts_deleted
        totals["stale_runs"] += result.stale_runs_deleted
        skipped_bits = [f"{s.value}: {why}" for s, why in result.skipped_statements.items()]
        skipped_bits.extend(result.skipped_columns)
        table.add_row(
            code, name, str(result.facts_written), str(result.facts_updated),
            str(result.facts_deleted) if result.facts_deleted else "-",
            str(result.periods_created), "; ".join(skipped_bits)[:70] or "-",
        )

    def fail(job, company_id: int, exc: BaseException) -> None:  # type: ignore[no-untyped-def]
        code, name = names[company_id]
        job.errors.append(f"{code}: {exc!r}"[:200])
        console.print(f"[red]{code} {name} failed:[/] {exc!r}")

    n = len(targets)
    with track_job("normalize facts", n) as job:
        if workers <= 1 or n <= 1:
            for i, (company_id, code, name) in enumerate(targets):
                job.step(i, f"{code} {name}")
                console.print(f"[dim][{i + 1}/{n}][/] {code} {name}")
                try:
                    with session_scope() as session:
                        extracted = extract_company(session, session.get(Company, company_id))
                    write(company_id, extracted)
                except Exception as exc:
                    fail(job, company_id, exc)
        else:
            console.print(f"extracting {n} companies with {workers} worker processes")
            job.step(0, f"starting {workers} workers")
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(extract_company_by_id, cid): cid for cid, _, _ in targets}
                for done, future in enumerate(as_completed(futures), start=1):
                    company_id = futures[future]
                    code, name = names[company_id]
                    try:
                        write(company_id, future.result())
                    except Exception as exc:
                        fail(job, company_id, exc)
                    job.step(done, f"{code} {name} (last finished)")
                    console.print(f"[dim][{done}/{n}][/] {code} {name}")

    console.print(table)
    console.print(
        f"[green]done[/] {totals['written']} facts written, {totals['updated']} updated, "
        f"{totals['deleted']} stale fact(s) deleted, {totals['stale_runs']} stale extraction run(s) cleaned up"
        + (f", [red]{len(job.errors)} company(ies) failed[/]" if job.errors else "")
    )


@normalize_app.command("derive")
def derive_facts_cmd(
    stock_codes: Annotated[
        list[str] | None,
        typer.Option("--company", help="Limit to these stock codes. Default: every company."),
    ] = None,
) -> None:
    """Derive missing facts from accounting identities.

    Computes bs.total_assets from bs.total_equity_and_liabilities,
    bs.total_equity from assets minus liabilities, and
    is.profit_before_tax / is.profit_for_period from each other + tax.
    Safe to re-run — skips facts that already exist.
    """
    from bursa.pipeline.derive import derive_facts_for_company

    table = Table("Code", "Name", "Derived", "Skipped (exist)")
    total_derived = 0

    with session_scope() as session:
        query = select(Company)
        if stock_codes:
            query = query.where(Company.stock_code.in_(stock_codes))
        companies = list(session.scalars(query))

        for company in companies:
            result = derive_facts_for_company(session, company)
            if result.derived == 0:
                continue
            total_derived += result.derived
            table.add_row(
                company.stock_code,
                company.name,
                str(result.derived),
                str(result.skipped_existing),
            )

    console.print(table)
    console.print(f"[green]done[/] {total_derived} facts derived from accounting identities")


@validate_app.command("facts")
def validate_facts_cmd(
    stock_codes: Annotated[
        list[str] | None,
        typer.Option("--company", help="Limit to these stock codes. Default: every company."),
    ] = None,
) -> None:
    """Run the accounting-identity rules in `bursa.validate.rules` against
    each company's already-written facts (`bursa normalize facts`).

    Balance sheet footing (assets = liabilities + equity), income statement
    ladder checks, EPS consistency, cash flow roll-forward, and a
    period-on-period magnitude check (catches the RM'000 1000x scale-error
    class of bug). Safe to re-run - replaces this company's previous results
    rather than duplicating them. See `src/bursa/pipeline/validate.py` for
    what is and isn't checkable with the data currently ingested.
    """
    from bursa.pipeline.validate import validate_company

    summary = Table("Code", "Name", "Rules run", "Passed", "Failed")
    failures = Table("Code", "Rule", "Detail", "Expected", "Actual", "Delta")
    total_run = total_passed = total_failed = 0

    def fmt(v) -> str:
        return f"{v:,.0f}" if v is not None else "-"

    with session_scope() as session:
        query = select(Company)
        if stock_codes:
            query = query.where(Company.stock_code.in_(stock_codes))
        companies = list(session.scalars(query))

        for company in companies:
            result = validate_company(session, company)
            if result.rules_run == 0:
                continue

            total_run += result.rules_run
            total_passed += result.rules_passed
            total_failed += result.rules_failed
            style = "green" if result.rules_failed == 0 else "red"
            summary.add_row(
                company.stock_code,
                company.name,
                str(result.rules_run),
                str(result.rules_passed),
                f"[{style}]{result.rules_failed}[/]",
            )
            for outcome in result.failures:
                failures.add_row(
                    company.stock_code,
                    outcome.rule_key,
                    outcome.detail,
                    fmt(outcome.expected),
                    fmt(outcome.actual),
                    fmt(outcome.delta),
                )

    console.print(summary)
    if total_failed:
        console.print("\n[red]Failures:[/]")
        console.print(failures)
    console.print(f"\n[green]done[/] {total_run} rules run, {total_passed} passed, {total_failed} failed")


@validate_app.command("comparative")
def validate_comparative_cmd(
    stock_codes: Annotated[
        list[str] | None,
        typer.Option("--company", help="Limit to these stock codes."),
    ] = None,
) -> None:
    """Cross-document consistency check — compare the same period's facts
    extracted from different annual reports (original vs restated).
    """
    from bursa.validate.comparative import compare_company

    summary_table = Table("Code", "Name", "Match", "Rounding", "Restatement")
    details_table = Table("Code", "Concept", "Period", "Original", "Comparative", "Diff", "Class")
    totals = {"MATCH": 0, "ROUNDING": 0, "RESTATEMENT": 0}

    def fmt(v: object) -> str:
        return f"{v:,.0f}" if v is not None else "-"

    with session_scope() as session:
        query = select(Company).where(Company.is_watchlist.is_(True))
        if stock_codes:
            query = select(Company).where(Company.stock_code.in_(stock_codes))
        companies = list(session.scalars(query))

        for company in companies:
            result = compare_company(session, company)
            total = result.match + result.rounding + result.restatement
            if total == 0:
                continue

            totals["MATCH"] += result.match
            totals["ROUNDING"] += result.rounding
            totals["RESTATEMENT"] += result.restatement

            r_style = "yellow" if result.restatement > 0 else "green"
            summary_table.add_row(
                company.stock_code,
                company.name,
                str(result.match),
                str(result.rounding),
                f"[{r_style}]{result.restatement}[/]",
            )

            if result.results:
                for r in result.results:
                    if r.classification == "MATCH":
                        continue
                    details_table.add_row(
                        company.stock_code,
                        r.concept_key,
                        r.period_end,
                        fmt(r.original_value),
                        fmt(r.comparative_value),
                        fmt(r.abs_diff),
                        r.classification,
                    )

    console.print(summary_table)
    if totals["ROUNDING"] + totals["RESTATEMENT"] > 0:
        console.print("\n[yellow]Non-matching details:[/]")
        console.print(details_table)

    grand = sum(totals.values())
    console.print(
        f"\n[green]done[/] {grand} comparative pairs: "
        f"{totals['MATCH']} match, {totals['ROUNDING']} rounding, "
        f"{totals['RESTATEMENT']} restatement"
    )


@benchmark_app.command("facts")
def benchmark_facts_cmd(
    stock_codes: Annotated[
        list[str] | None,
        typer.Option("--company", help="Limit to these stock codes. Default: every company."),
    ] = None,
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Re-fetch from yfinance even if cached."),
    ] = False,
    tolerance: Annotated[
        float,
        typer.Option("--tolerance", help="Match threshold as a fraction (default 0.05 = 5%%)."),
    ] = 0.05,
) -> None:
    """Cross-validate extracted facts against yfinance data.

    Fetches annual financials from Yahoo Finance for each company (via its
    Bursa stock code + '.KL' suffix) and compares revenue, net income, PBT,
    total assets, and total equity against our extracted Fact rows.

    Results are cached locally. Use --refresh to force a re-fetch.
    """
    try:
        import yfinance  # noqa: F401
    except ImportError:
        console.print(
            "[red]yfinance not installed.[/] Run: "
            "[bold]uv pip install yfinance --python .venv/Scripts/python.exe[/]"
        )
        raise typer.Exit(1)

    from bursa.benchmark.runner import benchmark_company

    summary = Table("Code", "Name", "Sector", "Match", "Close", "Mismatch", "Scale Err", "Missing", "Skipped")
    details = Table("Code", "Concept", "FY", "Ours", "yfinance", "Dev%", "Class")
    totals = {"MATCH": 0, "CLOSE": 0, "MISMATCH": 0, "SCALE_ERROR": 0, "MISSING": 0}
    total_skipped = 0

    def fmt(v) -> str:
        return f"{v:,.0f}" if v is not None else "-"

    with session_scope() as session:
        query = select(Company)
        if stock_codes:
            query = query.where(Company.stock_code.in_(stock_codes))
        companies = list(session.scalars(query))

        for company in companies:
            result = benchmark_company(
                session, company,
                refresh=refresh,
                match_tolerance=tolerance,
                close_tolerance=tolerance * 3,
            )
            total = result.match + result.close + result.mismatch + result.scale_error + result.missing
            if total == 0:
                continue

            totals["MATCH"] += result.match
            totals["CLOSE"] += result.close
            totals["MISMATCH"] += result.mismatch
            totals["SCALE_ERROR"] += result.scale_error
            totals["MISSING"] += result.missing
            total_skipped += result.skipped

            m_style = "green" if result.mismatch == 0 and result.scale_error == 0 else "red"
            summary.add_row(
                company.stock_code,
                company.name,
                result.sector or "-",
                str(result.match),
                str(result.close),
                f"[{m_style}]{result.mismatch}[/]",
                f"[red]{result.scale_error}[/]" if result.scale_error else "0",
                str(result.missing),
                str(result.skipped) if result.skipped else "-",
            )

            if result.outcomes:
                for o in result.outcomes:
                    if o.classification in ("MATCH",):
                        continue
                    dev = f"{o.deviation_pct:.1%}" if o.deviation_pct is not None else "-"
                    details.add_row(
                        company.stock_code,
                        o.concept_key,
                        str(o.fiscal_year),
                        fmt(o.our_value),
                        fmt(o.external_value),
                        dev,
                        o.classification,
                    )

    console.print(summary)
    non_match = totals["CLOSE"] + totals["MISMATCH"] + totals["SCALE_ERROR"]
    if non_match:
        console.print("\n[yellow]Non-matching details:[/]")
        console.print(details)

    grand = sum(totals.values())
    console.print(
        f"\n[green]done[/] {grand} comparisons: "
        f"{totals['MATCH']} match, {totals['CLOSE']} close, "
        f"{totals['MISMATCH']} mismatch, {totals['SCALE_ERROR']} scale errors, "
        f"{totals['MISSING']} missing, {total_skipped} sector-skipped"
    )


@valuation_app.command("metrics")
def valuation_metrics_cmd(
    stock_codes: Annotated[
        list[str] | None,
        typer.Option("--company", help="Limit to these stock codes."),
    ] = None,
) -> None:
    """Compute FCFF, FCFE, EBITDA, and EV components per company per FY."""
    from bursa.valuation.metrics import compute_valuation

    def fmt(v: Decimal | None) -> str:
        if v is None:
            return "-"
        return f"{v / 1_000_000:,.1f}"

    def pct(v: Decimal | None) -> str:
        if v is None:
            return "-"
        return f"{v * 100:.1f}%"

    with session_scope() as session:
        query = select(Company)
        if stock_codes:
            query = query.where(Company.stock_code.in_(stock_codes))
        companies = list(session.scalars(query))

        for company in companies:
            result = compute_valuation(session, company)
            if not result.years:
                continue

            tbl = Table(
                title=f"{company.stock_code} {company.name}",
                show_header=True,
            )
            tbl.add_column("FY", justify="right")
            tbl.add_column("EBIT (RM m)", justify="right")
            tbl.add_column("EBITDA (RM m)", justify="right")
            tbl.add_column("Tax Rate", justify="right")
            tbl.add_column("NOPAT (RM m)", justify="right")
            tbl.add_column("D&A (RM m)", justify="right")
            tbl.add_column("CapEx (RM m)", justify="right")
            tbl.add_column("FCFF (RM m)", justify="right")
            tbl.add_column("FCFE (RM m)", justify="right")
            tbl.add_column("Net Debt (RM m)", justify="right")

            for m in result.years:
                tbl.add_row(
                    str(m.fiscal_year),
                    fmt(m.ebit),
                    fmt(m.ebitda),
                    pct(m.effective_tax_rate),
                    fmt(m.nopat),
                    fmt(m.dep_amort),
                    fmt(m.capex),
                    fmt(m.fcff),
                    fmt(m.fcfe),
                    fmt(m.net_debt),
                )
            console.print(tbl)
            console.print()


CompanyFilter = Annotated[
    list[str] | None,
    typer.Option("--company", help="Limit to these stock codes."),
]


def _companies(session, stock_codes: list[str] | None) -> list[Company]:  # type: ignore[no-untyped-def]
    query = select(Company).order_by(Company.stock_code)
    if stock_codes:
        query = query.where(Company.stock_code.in_(stock_codes))
    return list(session.scalars(query))


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def _x(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}x"


@analysis_app.command("dupont")
def analysis_dupont_cmd(stock_codes: CompanyFilter = None) -> None:
    """3- and 5-factor DuPont decomposition of ROE per fiscal year."""
    from bursa.analysis.dupont import compute_dupont

    with session_scope() as session:
        for company in _companies(session, stock_codes):
            result = compute_dupont(session, company)
            if not result.three_factor:
                continue
            five = {d.fiscal_year: d for d in result.five_factor}
            tbl = Table(title=f"{company.stock_code} {company.name}")
            for col in ("FY", "ROE", "Net margin", "Asset turn", "Eq mult",
                        "Tax burden", "Int burden", "Op margin"):
                tbl.add_column(col, justify="right")
            for d in result.three_factor:
                d5 = five.get(d.fiscal_year)
                tbl.add_row(
                    str(d.fiscal_year), _pct(d.roe), _pct(d.net_margin),
                    _x(d.asset_turnover), _x(d.equity_multiplier),
                    _pct(d5.tax_burden if d5 else None),
                    _pct(d5.interest_burden if d5 else None),
                    _pct(d5.operating_margin if d5 else None),
                )
            console.print(tbl)


@analysis_app.command("growth")
def analysis_growth_cmd(stock_codes: CompanyFilter = None) -> None:
    """Revenue / earnings / asset CAGR (3Y, 5Y) and ROE trend."""
    from bursa.analysis.growth import compute_growth

    tbl = Table(title="Growth (CAGR)")
    for col in ("Code", "Name", "Rev 3Y", "Rev 5Y", "Earn 3Y", "Earn 5Y",
                "Assets 3Y", "Assets 5Y", "ROE (latest)"):
        tbl.add_column(col, justify="right" if col not in ("Code", "Name") else "left")

    with session_scope() as session:
        for company in _companies(session, stock_codes):
            g = compute_growth(session, company)
            metrics = (g.revenue_cagr_3y, g.revenue_cagr_5y, g.earnings_cagr_3y,
                       g.earnings_cagr_5y, g.asset_cagr_3y, g.asset_cagr_5y)
            if not any(metrics) and not g.roe_trend:
                continue
            latest_roe = g.roe_trend[-1] if g.roe_trend else None
            tbl.add_row(
                company.stock_code, company.name[:30],
                *(_pct(m.cagr if m else None) for m in metrics),
                f"{_pct(latest_roe.roe)} ({latest_roe.fiscal_year})" if latest_roe else "-",
            )
    console.print(tbl)


@analysis_app.command("prices")
def analysis_prices_cmd(stock_codes: CompanyFilter = None) -> None:
    """P/E, P/B, EV/EBITDA, dividend yield at each FY-end close (Yahoo Finance)."""
    from bursa.analysis.prices import compute_price_ratios

    with session_scope() as session:
        for company in _companies(session, stock_codes):
            result = compute_price_ratios(session, company)
            if not any(y.price is not None for y in result.years):
                continue
            current = f"{result.current_price:.2f}" if result.current_price is not None else "-"
            tbl = Table(title=f"{company.stock_code} {company.name} ({result.ticker}, now RM {current})")
            for col in ("FY", "Year end", "Price", "EPS (RM)", "Mkt cap (RM m)",
                        "P/E", "P/B", "EV/EBITDA", "Div yield"):
                tbl.add_column(col, justify="right")
            for y in result.years:
                tbl.add_row(
                    str(y.fiscal_year), str(y.period_end),
                    "-" if y.price is None else f"{y.price:.2f}",
                    "-" if y.eps is None else f"{y.eps:.4f}",
                    "-" if y.market_cap is None else f"{y.market_cap / 1e6:,.0f}",
                    _x(y.pe_ratio), _x(y.pb_ratio), _x(y.ev_ebitda), _pct(y.dividend_yield),
                )
            console.print(tbl)


@app.command("serve")
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port number.")] = 8000,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code changes.")] = False,
) -> None:
    """Start the FastAPI development server."""
    import uvicorn

    console.print(f"[green]starting API server[/] at http://{host}:{port}")
    console.print("[dim]API docs at /docs, OpenAPI schema at /openapi.json[/]")
    uvicorn.run("bursa.api.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":  # pragma: no cover
    app()
