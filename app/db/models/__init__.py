"""ORM models. PostgreSQL is the single source of truth for messages, payments and claims."""

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


class MailAuthType(StrEnum):
    APP_PASSWORD = "app_password"  # noqa: S105 - enum label
    OAUTH2 = "oauth2"
    ACCOUNT_PASSWORD = "account_password"  # noqa: S105 - enum label


class PaymentSource(StrEnum):
    BINANCE_EMAIL = "BINANCE_EMAIL"
    BINANCE_PAY_API = "BINANCE_PAY_API"


class MessageParseStatus(StrEnum):
    PARSED = "PARSED"
    NOT_BINANCE_SENDER = "NOT_BINANCE_SENDER"
    NO_TEMPLATE = "NO_TEMPLATE"
    PARSE_ERROR = "PARSE_ERROR"


class MailAccount(TimestampMixin, Base):
    __tablename__ = "mail_accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="gmail")
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    auth_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # AES-256-GCM envelope (see app.core.encryption). NEVER plaintext.
    encrypted_credentials: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    mailbox: Mapped[str] = mapped_column(String(255), nullable=False, default="INBOX")
    imap_uidvalidity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_imap_uid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_error: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "auth_type IN ('app_password', 'oauth2', 'account_password')", name="auth_type"
        ),
    )


class EmailMessage(TimestampMixin, Base):
    __tablename__ = "email_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    mail_account_id: Mapped[int] = mapped_column(
        ForeignKey("mail_accounts.id", ondelete="CASCADE"), nullable=False
    )
    imap_uidvalidity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    imap_uid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(998))
    sender: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(String(500))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trusted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trust_details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False)
    template: Mapped[str | None] = mapped_column(String(64))
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Only populated when STORE_RAW_EMAILS=true, and encrypted even then.
    raw_encrypted: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("mail_account_id", "imap_uidvalidity", "imap_uid"),
        UniqueConstraint("mail_account_id", "message_id"),
        Index("ix_email_messages_received_at", "received_at"),
    )


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(998), nullable=False)
    email_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("email_messages.id", ondelete="RESTRICT"), nullable=True
    )
    payment_code: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    asset: Mapped[str] = mapped_column(String(16), nullable=False)
    payment_status: Mapped[str] = mapped_column(String(32), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trusted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Set when two trusted sources disagree about the same payment_code.
    ambiguous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    template: Mapped[str | None] = mapped_column(String(64))

    claim: Mapped[PaymentClaim | None] = relationship(back_populates="payment", lazy="raise")

    __table_args__ = (
        Index("ix_payments_payment_code", "payment_code"),
        Index("ix_payments_received_at", "received_at"),
        Index("ix_payments_email_message_id", "email_message_id"),
        # One payment per (source, external id): re-processing the same email is a no-op.
        UniqueConstraint("source", "external_id"),
        # At most ONE trusted payment per code. Prevents double evidence for the same code.
        Index(
            "uq_payments_trusted_payment_code",
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


class RateLimitCounter(Base):
    """Fixed-window counters shared by all replicas (no Redis required)."""

    __tablename__ = "rate_limit_counters"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


__all__ = [
    "EmailMessage",
    "MailAccount",
    "MailAuthType",
    "MessageParseStatus",
    "Payment",
    "PaymentClaim",
    "PaymentSource",
    "RateLimitCounter",
]
