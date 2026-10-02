from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context
from app.config import get_settings
from app.db import models  # noqa: F401 - register models on the metadata
from app.db.base import Base
from app.db.connection import DatabaseConnection, resolve_database

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database() -> DatabaseConnection:
    # Allow `alembic -x url=...` (tests); default to DATABASE_URL from the environment.
    settings = get_settings()
    raw = context.get_x_argument(as_dictionary=True).get("url")
    raw = raw or settings.database_url.get_secret_value()
    return resolve_database(
        raw,
        ssl_mode=settings.database_ssl_mode,
        pooler_mode=settings.database_pooler_mode,
        statement_timeout_ms=max(settings.database_statement_timeout_ms, 120_000),
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_database().url_string,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # psycopg 3 supports both sync and async; migrations run synchronously.
    # Works with Supabase too (TLS enforced, transaction-pooler compatible).
    db = _database()
    engine = create_engine(
        db.url, poolclass=pool.NullPool, hide_parameters=True, connect_args=db.connect_args
    )
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
