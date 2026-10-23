"""`bursa brief ...` - generate, validate and inspect daily news briefs."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console

from bursa.briefs.schema import brief_dates, json_schema, load_brief, save_brief, today_myt
from bursa.config import get_settings

app = typer.Typer(help="Daily Bursa news brief (macro + corporate news).", no_args_is_help=True)
console = Console()


@app.command("generate")
def generate(
    day: Annotated[
        str | None, typer.Option("--date", help="YYYY-MM-DD; default today (MYT).")
    ] = None,
    force: Annotated[bool, typer.Option(help="Overwrite an existing brief for that day.")] = False,
    model: Annotated[str | None, typer.Option(
        help="e.g. claude-haiku-5-5; default BRIEF_MODEL (claude-sonnet-5-5).")] = None,
) -> None:
    """Research and write a brief via the Anthropic API (web search). Needs credentials."""
    from bursa.briefs.generate import BriefGenerationError, BriefUsage, generate_brief

    settings = get_settings()
    target = date.fromisoformat(day) if day else today_myt()
    existing = settings.briefs_dir / f"{target.isoformat()}.json"
    if existing.exists() and not force:
        console.print(f"[yellow]{existing} already exists; pass --force to overwrite.[/yellow]")
        raise typer.Exit(1)
    usage = BriefUsage()
    try:
        brief = generate_brief(target, model=model or settings.brief_model,
                               effort=settings.brief_effort,
                               api_key=settings.anthropic_api_key, usage=usage)
    except BriefGenerationError as e:
        console.print(f"[red]Brief not generated: {e}[/red]")
        raise typer.Exit(1) from e
    finally:
        console.print(
            f"Usage: {usage.requests} request(s), input {usage.input_tokens:,} + cache write "
            f"{usage.cache_write_tokens:,} + cache read {usage.cache_read_tokens:,} tokens, "
            f"output {usage.output_tokens:,} tokens, {usage.web_searches} searches, "
            f"{usage.web_fetches} fetches"
        )
    console.print(f"[green]Wrote {save_brief(settings.briefs_dir, brief)}[/green]")


@app.command("validate")
def validate(
    path: Annotated[
        Path | None, typer.Argument(help="A brief file; default: every file in briefs_dir.")
    ] = None,
) -> None:
    """Check brief files against the format. Exit code 1 if any is invalid."""
    paths = [path] if path else sorted(get_settings().briefs_dir.glob("*.json"))
    bad = 0
    for p in paths:
        try:
            b = load_brief(p)
            console.print(f"[green]ok[/green] {p.name}: {len(b.tldr)} TL;DR, "
                          f"{len(b.sections)} sections, {len(b.corporate)} corporate items, "
                          f"{len(b.sources)} sources")
        except (ValidationError, ValueError, OSError) as e:
            bad += 1
            console.print(f"[red]invalid[/red] {p.name}:\n{e}")
    if not paths:
        console.print("No brief files found.")
    if bad:
        raise typer.Exit(1)


@app.command("schema")
def schema() -> None:
    """Print the JSON Schema a brief file must follow."""
    typer.echo(json_schema())


@app.command("list")
def list_briefs() -> None:
    """Dates with a brief, newest first."""
    for d in brief_dates(get_settings().briefs_dir):
        console.print(d.isoformat())
