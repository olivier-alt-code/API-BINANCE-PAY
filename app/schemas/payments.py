from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from app.services.payment_verifier import VerificationResult, VerificationStatus

_AMOUNT_RE = re.compile(r"^\d{1,20}(\.\d{1,18})?$")


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


class VerifyPaymentRequest(CamelModel):
    payment_code: str = Field(
        min_length=1,
        max_length=128,
        description="Binance payment identifier. Compared EXACTLY (no fuzzy/partial match).",
        examples=["ABC123456789"],
    )
    expected_amount: Decimal = Field(
        description="Expected amount as a decimal STRING (floats are rejected).",
        examples=["25.50"],
    )
    asset: str = Field(
        default="USDT",
        min_length=2,
        max_length=15,
        pattern=r"^[A-Za-z0-9]{2,15}$",
        examples=["USDT"],
    )
    order_reference: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:/#-]{1,128}$",
        description="Your internal order id. Enables idempotent re-verification.",
        examples=["ORDER-123"],
    )
    max_age_minutes: int | None = Field(
        default=None,
        ge=1,
        description="Maximum payment age. Defaults to DEFAULT_PAYMENT_MAX_AGE_MINUTES and "
        "may not exceed MAX_PAYMENT_AGE_MINUTES.",
        examples=[60],
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "paymentCode": "ABC123456789",
                    "expectedAmount": "25.50",
                    "asset": "USDT",
                    "orderReference": "ORDER-123",
                    "maxAgeMinutes": 60,
                }
            ]
        }
    )

    @field_validator("expected_amount", mode="before")
    @classmethod
    def _amount(cls, value: Any) -> Decimal:
        # Money must never travel as float: accept only strings (or ints) of plain digits.
        if isinstance(value, (bool, float)):
            raise ValueError('expectedAmount must be a decimal string, e.g. "25.50"')
        if isinstance(value, int):
            value = str(value)
        if not isinstance(value, str) or not _AMOUNT_RE.fullmatch(value.strip()):
            raise ValueError('expectedAmount must be a positive decimal string, e.g. "25.50"')
        try:
            amount = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError("Invalid amount") from exc
        if amount <= 0:
            raise ValueError("expectedAmount must be greater than zero")
        return amount


def format_amount(value: Decimal, like: Decimal | None = None) -> str:
    """Render without float conversion. Uses the scale of ``like`` when lossless
    (``Decimal('20.000000000000000000')`` with like=25.50 -> ``"20.00"``)."""
    if like is not None:
        exponent = like.as_tuple().exponent
        if isinstance(exponent, int) and exponent <= 0:
            quantized = value.quantize(Decimal(1).scaleb(exponent))
            if quantized == value:
                return format(quantized, "f")
    normalized = value.normalize()
    return format(normalized, "f")


class VerifyPaymentResponse(CamelModel):
    verified: bool
    status: VerificationStatus
    idempotent: bool | None = None
    payment_code: str
    expected_amount: str
    received_amount: str | None = None
    asset: str
    received_asset: str | None = None
    received_at: datetime | None = None
    order_reference: str | None = None
    retryable: bool
    detail: str | None = None

    model_config = ConfigDict(extra="ignore")

    @classmethod
    def from_result(cls, result: VerificationResult) -> VerifyPaymentResponse:
        return cls(
            verified=result.verified,
            status=result.status,
            idempotent=True if result.idempotent else None,
            payment_code=result.payment_code,
            expected_amount=format(result.expected_amount, "f"),  # echo as requested
            received_amount=(
                format_amount(result.received_amount, like=result.expected_amount)
                if result.received_amount is not None
                else None
            ),
            asset=result.asset,
            received_asset=(
                result.received_asset
                if result.received_asset and result.received_asset != result.asset
                else None
            ),
            received_at=result.received_at,
            order_reference=result.order_reference,
            retryable=result.retryable,
            detail=result.detail,
        )
