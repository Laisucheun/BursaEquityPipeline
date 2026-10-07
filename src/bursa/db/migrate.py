"""Schema migrations (Alembic) and the ``bursa db`` sub-commands.

Two ways a database reaches the current schema:

* **Fresh database** - ``upgrade_to_head()`` creates everything, including
  the ``facts_current`` view that ``Base.metadata.create_all`` cannot make.
* **Database created by the old ``bursa init-db`` (``create_all``)** - it
  already has the baseline tables but no ``alembic_version`` row.
  ``stamp_existing_db()`` checks the live schema against the baseline,
  records the baseline revision, then upgrades through every later revision
  (the first of which adds ``facts_current``).
"""

from __future__ import annotations

from pathlib import Path

import typer
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, pool

from bursa.config import PROJECT_ROOT, get_settings
from bursa.db.models import Base

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# The revision that matches a database built by Base.metadata.create_all.
BASELINE_REVISION = "0001_baseline"


def make_config(database_url: str | None = None) -> Config:
    """An Alembic Config pointing at the in-package scripts.

    Independent of the current directory and of ``alembic.ini``.
    """
    ini = PROJECT_ROOT / "alembic.ini"
    cfg = Config(str(ini)) if ini.exists() else Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    url = database_url or get_settings().database_url
    # ConfigParser interpolation: a literal % (e.g. in a password) must be doubled.
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def _url(cfg: Config) -> str:
    return cfg.get_main_option("sqlalchemy.url") or get_settings().database_url


def current_revision(database_url: str | None = None) -> str | None:
    """The revision recorded in the database, or None if it was never stamped."""
    engine = create_engine(_url(make_config(database_url)), poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def head_revision() -> str | None:
    return ScriptDirectory.from_config(make_config()).get_current_head()


def schema_diff(database_url: str | None = None) -> list:
    """Differences between the live database and ``Base.metadata``.

    Empty means the tables, columns, indexes and unique constraints match.
    """
    engine = create_engine(_url(make_config(database_url)), poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            mc = MigrationContext.configure(conn, opts={"compare_type": True})
            return compare_metadata(mc, Base.metadata)
    finally:
        engine.dispose()


def upgrade_to_head(database_url: str | None = None) -> None:
    """Apply every pending revision. Safe to call repeatedly."""
    command.upgrade(make_config(database_url), "head")


def stamp_existing_db(database_url: str | None = None, *, force: bool = False) -> str:
    """Adopt a database that ``create_all`` built, then upgrade it to head.

    Returns a short description of what happened. Raises RuntimeError if the
    schema does not match the baseline (unless ``force``) - stamping a
    drifted database would make later migrations fail in confusing ways.
    """
    if current_revision(database_url) is not None:
        upgrade_to_head(database_url)
        return "already under Alembic control; upgraded to head"

    engine = create_engine(_url(make_config(database_url)), poolclass=pool.NullPool)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    if not tables:
        upgrade_to_head(database_url)
        return "empty database; created schema at head"

    diffs = schema_diff(database_url)
    missing = _missing_tables_only(diffs)
    if missing:
        # Purely additive drift (e.g. a model table added after the DB was
        # built): create just those tables, which is what create_all would do.
        engine = create_engine(_url(make_config(database_url)), poolclass=pool.NullPool)
        try:
            Base.metadata.create_all(engine, tables=[Base.metadata.tables[t] for t in missing])
        finally:
            engine.dispose()
        diffs = schema_diff(database_url)
    if diffs and not force:
        lines = "\n".join(f"  {d}" for d in diffs)
        raise RuntimeError(
            "Existing schema differs from the baseline; refusing to stamp.\n"
            f"{lines}\n"
            "Fix the schema (or pass force=True / --force if you are sure)."
        )
    cfg = make_config(database_url)
    command.stamp(cfg, BASELINE_REVISION)
    command.upgrade(cfg, "head")
    created = f"created missing tables {sorted(missing)}; " if missing else ""
    return f"{created}stamped {BASELINE_REVISION} and upgraded to head"


def _missing_tables_only(diffs: list) -> set[str]:
    """Names of absent tables, if *all* the drift is absent tables.

    Returns an empty set when there is no drift or when any diff is something
    other than a missing table (or an index on one), which must not be
    papered over automatically.
    """
    missing = {d[1].name for d in diffs if isinstance(d, tuple) and d[0] == "add_table"}
    for d in diffs:
        if isinstance(d, tuple) and d[0] == "add_table":
            continue
        if isinstance(d, tuple) and d[0] == "add_index" and d[1].table.name in missing:
            continue
        return set()
    return missing


# --------------------------------------------------------------------------
# CLI: `bursa db ...`
# --------------------------------------------------------------------------

app = typer.Typer(help="Schema migrations (Alembic).", no_args_is_help=True)

_URL_OPT = typer.Option(None, "--url", help="Database URL (default: DATABASE_URL).")


@app.command("upgrade")
def upgrade_cmd(
    revision: str = typer.Argument("head"),
    url: str | None = _URL_OPT,
) -> None:
    """Upgrade the schema to REVISION (default head)."""
    command.upgrade(make_config(url), revision)
    typer.echo(f"at {current_revision(url)}")


@app.command("downgrade")
def downgrade_cmd(revision: str = typer.Argument(...), url: str | None = _URL_OPT) -> None:
    """Downgrade the schema to REVISION."""
    command.downgrade(make_config(url), revision)
    typer.echo(f"at {current_revision(url)}")


@app.command("stamp")
def stamp_cmd(
    revision: str | None = typer.Argument(
        None, help="Revision to record. Omit to adopt a create_all database safely."
    ),
    url: str | None = _URL_OPT,
    force: bool = typer.Option(False, "--force", help="Stamp even if the schema differs."),
) -> None:
    """Record a revision without running it.

    With no REVISION: check the schema matches the baseline, stamp it, and
    upgrade through any later revisions.
    """
    if revision is not None:
        command.stamp(make_config(url), revision)
        typer.echo(f"stamped {revision}")
        return
    try:
        typer.echo(stamp_existing_db(url, force=force))
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command("current")
def current_cmd(url: str | None = _URL_OPT) -> None:
    """Show the database's revision and the latest available one."""
    typer.echo(f"current: {current_revision(url) or '(not stamped)'}")
    typer.echo(f"head:    {head_revision()}")


@app.command("check")
def check_cmd(url: str | None = _URL_OPT) -> None:
    """Report differences between the database and the models."""
    diffs = schema_diff(url)
    if not diffs:
        typer.echo("schema matches models")
        return
    for d in diffs:
        typer.echo(str(d))
    raise typer.Exit(1)


@app.command("revision")
def revision_cmd(
    message: str = typer.Option(..., "-m", "--message"),
    autogenerate: bool = typer.Option(True, "--autogenerate/--empty"),
    url: str | None = _URL_OPT,
) -> None:
    """Create a new revision script (autogenerated against --url by default)."""
    command.revision(make_config(url), message=message, autogenerate=autogenerate)
