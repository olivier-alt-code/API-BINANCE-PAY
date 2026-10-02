"""ORM models. PostgreSQL (Supabase) is the single source of truth.

Multi-client ("tenant") model:

* :class:`Tenant` — a client of this API (you, or someone you share it with).
* :class:`ApiToken` — private access tokens of a tenant. Only a SHA-256 hash is stored.
* :class:`BinanceCredential` — the tenant's read-only Binance API key, AES-GCM encrypted.
* :class:`Payment` / :class:`PaymentClaim` / :class:`EvidenceSyncState` — always scoped to
  one tenant: a tenant can only see and claim payments received on its own Binance account.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

# 38 significant digits, 18 decimals: enough for any crypto asset precision.
MONEY = Numeric(38, 18)


class PaymentSource(StrEnum):
    BINANCE_PAY_API = "BINANCE_PAY_API"  # Binance Pay Merchant API (prepared, not enabled)
    BINANCE_PAY_HISTORY = "BINANCE_PAY_HISTORY"  # account API GET /sapi/v1/pay/transactions


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ApiToken(TimestampMixin, Base):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # First characters of the token, safe to display ("bpv_Ab3dE…") to identify it.
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    # SHA-256 of the full token. The token itself is shown once and never stored.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_api_tokens_tenant_id", "tenant_id"),)


class BinanceCredential(TimestampMixin, Base):
    __tablename__ = "binance_credentials"

    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    # AES-256-GCM envelope of {"api_key": ..., "api_secret": ...}. NEVER plaintext.
    encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_hint: Mapped[str] = mapped_column(String(16), nullable=False)  # last 4 chars
    ip_restricted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    permissions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(998), nullable=False)
    payment_code: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    asset: Mapped[str] = mapped_column(String(16), nullable=False)
    payment_status: Mapped[str] = mapped_column(String(32), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trusted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Set when two trusted sources disagree about the same payment_code.
    ambiguous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Counterparty as reported by Binance (audit only; never used for matching).
    payer_name: Mapped[str | None] = mapped_column(String(128))
    payer_binance_id: Mapped[str | None] = mapped_column(String(64))

    claim: Mapped[PaymentClaim | None] = relationship(back_populates="payment", lazy="raise")

    __table_args__ = (
        Index("ix_payments_tenant_id_payment_code", "tenant_id", "payment_code"),
        Index("ix_payments_received_at", "received_at"),
        # One payment per (tenant, source, external id): re-importing is a no-op.
        UniqueConstraint("tenant_id", "source", "external_id"),
        # At most ONE trusted payment per code and tenant.
        Index(
            "uq_payments_tenant_trusted_payment_code",
            "tenant_id",
            "payment_code",
            unique=True,
            postgresql_where=text("trusted"),
        ),
        CheckConstraint("amount > 0", name="amount_positive"),
    )


class PaymentClaim(Base):
    __tablename__ = "payment_claims"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # UNIQUE: a payment can be claimed exactly once. This is the anti-replay backstop.
    payment_id: Mapped[int] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    order_reference: Mapped[str | None] = mapped_column(String(128))
    expected_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    asset: Mapped[str] = mapped_column(String(16), nullable=False)
    client_id: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    payment: Mapped[Payment] = relationship(back_populates="claim", lazy="raise")

    __table_args__ = (Index("ix_payment_claims_order_reference", "order_reference"),)


class EvidenceSyncState(Base):
    """Incremental cursor of an evidence source, per tenant (shared by all replicas)."""

    __tablename__ = "evidence_sync_state"

    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    cursor_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(255))


class RateLimitCounter(Base):
    """Fixed-window counters shared by all replicas (no Redis required)."""

    __tablename__ = "rate_limit_counters"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


__all__ = [
    "ApiToken",
    "BinanceCredential",
    "EvidenceSyncState",
    "Payment",
    "PaymentClaim",
    "PaymentSource",
    "RateLimitCounter",
    "Tenant",
]
