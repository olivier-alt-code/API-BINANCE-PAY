"""Binance Pay (Merchant) API — PREPARED, NOT ENABLED.

Structure for a future ``BinancePayApiProvider`` that queries the official Binance Pay
Merchant API. It produces the same :class:`PaymentEvidence` as the Pay history
provider, so :class:`app.services.payment_verifier.PaymentVerifier` does not change.

Not wired into the application yet (no merchant credentials available). Before enabling:

* confirm endpoint, payload and response fields against the official documentation
  (``POST /binancepay/openapi/v2/order/query``);
* confirm status values (``PAID``, ``INITIAL``, ``PENDING``, ``CANCELED``, ``ERROR``,
  ``REFUNDING``, ``REFUNDED``, ``EXPIRED``) and the meaning of ``totalFee``/``currency``;
* add tests with recorded (sanitized) responses.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import httpx
from pydantic import SecretStr

from app.core.exceptions import EvidenceProviderUnavailableError
from app.db.models import PaymentSource
from app.integrations.binance.evidence import PaymentEvidence, PaymentStatus

BINANCE_PAY_BASE_URL = "https://bpay.binanceapi.com"
ORDER_QUERY_PATH = "/binancepay/openapi/v2/order/query"

_STATUS_MAP = {
    "PAID": PaymentStatus.PAID,
    "INITIAL": PaymentStatus.PENDING,
    "PENDING": PaymentStatus.PENDING,
    "REFUNDING": PaymentStatus.REFUNDED,
    "REFUNDED": PaymentStatus.REFUNDED,
    "CANCELED": PaymentStatus.FAILED,
    "ERROR": PaymentStatus.FAILED,
    "EXPIRED": PaymentStatus.FAILED,
}


@dataclass(frozen=True)
class BinancePayOrder:
    """Subset of the order-query response we rely on."""

    status: str
    transaction_id: str | None
    merchant_trade_no: str | None
    prepay_id: str | None
    currency: str
    total_fee: Decimal
    transact_time: datetime | None

    @classmethod
    def from_api(cls, data: dict[str, object]) -> BinancePayOrder:
        try:
            total_fee = Decimal(str(data["totalFee"]))
        except (KeyError, InvalidOperation) as exc:
            raise ValueError("Invalid totalFee") from exc
        transact_ms = data.get("transactTime")
        return cls(
            status=str(data.get("status", "")).upper(),
            transaction_id=str(data["transactionId"]) if data.get("transactionId") else None,
            merchant_trade_no=str(data["merchantTradeNo"]) if data.get("merchantTradeNo") else None,
            prepay_id=str(data["prepayId"]) if data.get("prepayId") else None,
            currency=str(data.get("currency", "")).upper(),
            total_fee=total_fee,
            transact_time=(
                datetime.fromtimestamp(int(str(transact_ms)) / 1000, tz=UTC)
                if transact_ms
                else None
            ),
        )

    def normalized_status(self) -> PaymentStatus | None:
        return _STATUS_MAP.get(self.status)


class BinancePayClient:
    def __init__(
        self,
        *,
        api_key: SecretStr,
        api_secret: SecretStr,
        base_url: str = BINANCE_PAY_BASE_URL,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._secret = api_secret
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._transport = transport

    def sign(self, timestamp: str, nonce: str, body: str) -> str:
        payload = f"{timestamp}\n{nonce}\n{body}\n".encode()
        return (
            hmac.new(self._secret.get_secret_value().encode(), payload, hashlib.sha512)
            .hexdigest()
            .upper()
        )

    async def query_order(
        self, *, merchant_trade_no: str | None = None, prepay_id: str | None = None
    ) -> BinancePayOrder | None:
        body_obj = {"merchantTradeNo": merchant_trade_no} if merchant_trade_no else {}
        if prepay_id:
            body_obj["prepayId"] = prepay_id
        body = json.dumps(body_obj, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        nonce = secrets.token_hex(16)
        headers = {
            "Content-Type": "application/json",
            "BinancePay-Timestamp": timestamp,
            "BinancePay-Nonce": nonce,
            "BinancePay-Certificate-SN": self._key.get_secret_value(),
            "BinancePay-Signature": self.sign(timestamp, nonce, body),
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(ORDER_QUERY_PATH, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise EvidenceProviderUnavailableError("Binance Pay API unreachable") from exc
        if response.status_code >= 500:
            raise EvidenceProviderUnavailableError("Binance Pay API error")
        data = response.json()
        if data.get("status") != "SUCCESS" or not isinstance(data.get("data"), dict):
            return None
        return BinancePayOrder.from_api(data["data"])


# Persists the order as a ``payments`` row (source=BINANCE_PAY_API) and returns its id, so
# the same transactional claim applies. To be implemented with PaymentRepository.store().
PersistOrder = Callable[[int, str, BinancePayOrder, PaymentStatus], Awaitable[int]]


class BinancePayApiProvider:
    """``PaymentEvidenceProvider`` backed by the official Binance Pay API (not enabled)."""

    source = PaymentSource.BINANCE_PAY_API

    def __init__(self, client: BinancePayClient, persist: PersistOrder) -> None:
        self._client = client
        self._persist = persist

    async def find_payment(self, tenant_id: int, payment_code: str) -> PaymentEvidence | None:
        order = await self._client.query_order(merchant_trade_no=payment_code)
        if order is None or order.merchant_trade_no != payment_code:
            return None
        status = order.normalized_status()
        if status is None or order.transact_time is None:
            return None  # unknown status: never guess
        evidence_id = await self._persist(tenant_id, payment_code, order, status)
        return PaymentEvidence(
            evidence_id=evidence_id,
            external_id=order.transaction_id or order.prepay_id or payment_code,
            payment_code=payment_code,
            amount=order.total_fee,
            asset=order.currency,
            status=status,
            timestamp=order.transact_time,
            source=PaymentSource.BINANCE_PAY_API,
            # Authenticated (signed request over TLS) response from Binance itself.
            trusted=True,
        )
