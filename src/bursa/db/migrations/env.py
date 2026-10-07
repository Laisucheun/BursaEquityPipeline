"""Alembic environment.

The database URL comes from ``bursa.config.get_settings().database_url``
unless the caller already put one in the Alembic config (``sqlalchemy.url``,
which is what ``bursa.db.migrate`` and the tests do to target a specific
file). A caller may also hand over a live connection via
``config.attributes["connection"]``.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, event, pool

from bursa.config import get_settings
from bursa.db.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def _configure(**kwargs) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        target_metadata=target_metadata,
        # SQLite cannot ALTER most things in place; batch mode rebuilds the
        # table instead. Harmless on Postgres.
        render_as_batch=True,
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    url = _url()
    engine = create_engine(url, poolclass=pool.NullPool, future=True)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    with engine.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
