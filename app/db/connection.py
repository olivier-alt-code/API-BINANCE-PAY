"""Database connection resolution, with first-class support for Supabase.

Supabase is managed PostgreSQL, so the whole persistence layer (SQLAlchemy, Alembic,
advisory locks, unique constraints) works unchanged. This module only adapts the
connection to what Supabase offers:

* accepts the URLs exactly as copied from the Supabase dashboard
  (``postgres://`` / ``postgresql://``) and selects the psycopg 3 driver;
* enforces TLS (``sslmode=require``) for Supabase hosts unless set explicitly;
* detects the Supavisor *transaction* pooler (port 6543): there, server-side prepared
  statements and startup ``options`` are not supported, and session-level advisory locks
  are unreliable, so the app switches to transaction-scoped locks (see ``MailSyncService``).
  Session pooler (port 5432 on ``*.pooler.supabase.com``) and direct connections
  (``db.<ref>.supabase.co``) behave like plain PostgreSQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy.engine import URL, make_url

from app.core.exceptions import ConfigurationError

PoolerMode = Literal["session", "transaction"]

SUPABASE_HOST_SUFFIXES = (".supabase.co", ".supabase.com")
SUPABASE_TRANSACTION_POOLER_PORT = 6543
_DRIVER = "postgresql+psycopg"


@dataclass(frozen=True)
class DatabaseConnection:
    url: URL
    is_supabase: bool
    transaction_pooler: bool
    connect_args: dict[str, Any] = field(default_factory=dict)

    @property
    def url_string(self) -> str:
        return self.url.render_as_string(hide_password=False)


def normalize_url(raw: str) -> URL:
    raw = raw.strip()
    prefixes = ("postgres://", "postgresql://", "postgresql+psycopg2://", "postgresql+asyncpg://")
    for prefix in prefixes:
        if raw.startswith(prefix):
            raw = _DRIVER + "://" + raw[len(prefix) :]
            break
    try:
        url = make_url(raw)
    except Exception:  # never echo or chain the URL: it contains the password
        raise ConfigurationError("DATABASE_URL is not a valid PostgreSQL URL") from None
    if url.drivername != _DRIVER:
        raise ConfigurationError("DATABASE_URL must be a PostgreSQL URL")
    return url


def resolve_database(
    raw_url: str,
    *,
    ssl_mode: str | None = None,
    pooler_mode: PoolerMode | None = None,
    statement_timeout_ms: int = 15_000,
    application_name: str = "binance-pay-verifier",
) -> DatabaseConnection:
    url = normalize_url(raw_url)
    host = (url.host or "").lower()
    is_supabase = host.endswith(SUPABASE_HOST_SUFFIXES)
    if pooler_mode is None:
        transaction_pooler = is_supabase and url.port == SUPABASE_TRANSACTION_POOLER_PORT
    else:
        transaction_pooler = pooler_mode == "transaction"

    query = dict(url.query)
    if "sslmode" not in query:
        effective_ssl = ssl_mode or ("require" if is_supabase else None)
        if effective_ssl:
            query["sslmode"] = effective_ssl
    url = url.set(query=query)

    connect_args: dict[str, Any] = {"connect_timeout": 10, "application_name": application_name}
    if transaction_pooler:
        # Supavisor transaction mode: no prepared statements, no startup options.
        connect_args["prepare_threshold"] = None
    else:
        connect_args["options"] = f"-c statement_timeout={statement_timeout_ms} -c timezone=UTC"
    return DatabaseConnection(
        url=url,
        is_supabase=is_supabase,
        transaction_pooler=transaction_pooler,
        connect_args=connect_args,
    )
