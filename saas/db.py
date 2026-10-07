"""Tables and database engine.

Two tables, created on startup (``CREATE ... IF NOT EXISTS``), inside their own Postgres
schema (``DB_SCHEMA``, default ``saas``) so they never mix with the API's tables, even
when both share the same Supabase project. That schema is not exposed by Supabase's REST
API (only ``public`` is).
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import DateTime, ForeignKey, String, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from saas.config import Settings


class Base(DeclarativeBase):
    pass


class SubscriptionStatus:
    PENDING = "pending"  # created, first payment not confirmed yet
    ACTIVE = "active"
    EXPIRED = "expired"  # API client disabled until renewed


class CheckoutKind:
    NEW = "new"
    RENEWAL = "renewal"


class CheckoutStatus:
    PENDING = "pending"
    FULFILLED = "fulfilled"


class Subscription(Base):
    __tablename__ = "saas_subscriptions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # uuid4 hex
    email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    paid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Client in the verification API (created on the first confirmed payment).
    api_client_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    api_client_id: Mapped[int | None] = mapped_column()
    api_token_id: Mapped[int | None] = mapped_column()
    # SHA-256 of the customer's API token: lets them log in here with it. The token itself
    # is never stored.
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    token_prefix: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Checkout(Base):
    """One payment attempt: the first month or a renewal."""

    __tablename__ = "saas_checkouts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # orderReference suffix
    # Secret part of the payment page URL (the id above is sent to the API and logged there).
    access_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("saas_subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Binance "Order ID" that paid this checkout (unique: one payment, one checkout).
    payment_code: Mapped[str | None] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; everything here is UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def normalize_url(url: str) -> str:
    """Accept plain ``postgresql://`` URLs (as Supabase shows them)."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def create_engine(settings: Settings) -> AsyncEngine:
    url = normalize_url(settings.database_url.get_secret_value())
    if url.startswith("sqlite"):
        return create_async_engine(url)
    parts = urlsplit(url)
    connect_args: dict[str, object] = {}
    if (parts.hostname or "").endswith(("supabase.com", "supabase.co")) and "sslmode=" not in url:
        connect_args["sslmode"] = "require"
    options: dict[str, object] = {
        "pool_pre_ping": True,
        "execution_options": {"schema_translate_map": {None: settings.db_schema}},
    }
    if parts.port == 6543:
        # Supabase transaction pooler: no server-side prepared statements, and Supavisor
        # already pools connections, so keep none idle here.
        connect_args["prepare_threshold"] = None
        options["poolclass"] = NullPool
    else:
        options |= {"pool_size": settings.db_pool_max, "max_overflow": 0}
    return create_async_engine(url, connect_args=connect_args, **options)


async def init_db(engine: AsyncEngine, settings: Settings) -> None:
    async with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{settings.db_schema}"'))
        await conn.run_sync(Base.metadata.create_all)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
