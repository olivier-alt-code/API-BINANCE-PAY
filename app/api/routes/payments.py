from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_container, rate_limit
from app.container import Container
from app.core.security import require_api_key
from app.schemas.payments import VerifyPaymentRequest, VerifyPaymentResponse
from app.services.payment_verifier import VerificationRequest

router = APIRouter(prefix="/v1/payments", tags=["payments"])

_STATUS_DOC = """
Verifies a Binance payment using authenticated Binance notification emails and, on
success, **atomically claims** it so it can never pay a second order.

Every processed verification returns **HTTP 200** with `verified` and `status`:

| status | meaning | retryable |
|---|---|---|
| `VERIFIED` | Trusted, exact code/amount/asset, recent, claimed for this order (`idempotent: true` when the same `orderReference` asks again). | – |
| `NOT_FOUND` | No payment with exactly this code (after on-demand mailbox sync). | yes |
| `PENDING_SYNC` | Another replica is syncing the mailbox; retry in a few seconds. | yes |
| `AMOUNT_MISMATCH` | Received amount differs from `expectedAmount` (exact Decimal comparison). | no |
| `ASSET_MISMATCH` | Received asset differs (e.g. USDC vs USDT). | no |
| `UNTRUSTED_EMAIL` | Only evidence found failed sender/DKIM/SPF/DMARC checks. | no |
| `EXPIRED_PAYMENT` | Older than `maxAgeMinutes` (or timestamp in the future). | no |
| `ALREADY_CLAIMED` | Payment already used by another order (anti-replay). | no |
| `INVALID_PAYMENT_CODE` | Code format is invalid. | no |
| `MAIL_PROVIDER_UNAVAILABLE` | Gmail/IMAP unreachable, timed out or auth failed. | yes |
| `PAYMENT_NOT_COMPLETED` | Notification found but status is pending/failed/refunded. | yes |
| `AMBIGUOUS_PAYMENT` | Contradicting trusted evidence for the same code. Manual review. | no |

Other HTTP codes: `401` bad/missing API key, `422` invalid request body, `413` body too
large, `429` rate limit exceeded.
"""

_EXAMPLES: dict[str, Any] = {
    "verified": {
        "summary": "VERIFIED",
        "value": {
            "verified": True,
            "status": "VERIFIED",
            "paymentCode": "ABC123456789",
            "expectedAmount": "25.50",
            "receivedAmount": "25.50",
            "asset": "USDT",
            "receivedAt": "2026-10-01T20:15:31Z",
            "orderReference": "ORDER-123",
            "retryable": False,
        },
    },
    "idempotent": {
        "summary": "VERIFIED (idempotent repeat of the same order)",
        "value": {
            "verified": True,
            "status": "VERIFIED",
            "idempotent": True,
            "paymentCode": "ABC123456789",
            "expectedAmount": "25.50",
            "receivedAmount": "25.50",
            "asset": "USDT",
            "receivedAt": "2026-10-01T20:15:31Z",
            "orderReference": "ORDER-123",
            "retryable": False,
        },
    },
    "amount_mismatch": {
        "summary": "AMOUNT_MISMATCH",
        "value": {
            "verified": False,
            "status": "AMOUNT_MISMATCH",
            "paymentCode": "ABC123456789",
            "expectedAmount": "25.50",
            "receivedAmount": "20.00",
            "asset": "USDT",
            "receivedAt": "2026-10-01T20:15:31Z",
            "retryable": False,
        },
    },
    "already_claimed": {
        "summary": "ALREADY_CLAIMED",
        "value": {
            "verified": False,
            "status": "ALREADY_CLAIMED",
            "paymentCode": "PAY-82919381",
            "expectedAmount": "25",
            "receivedAmount": "25",
            "asset": "USDT",
            "orderReference": "SUBSCRIPTION-9999",
            "retryable": False,
        },
    },
    "not_found": {
        "summary": "NOT_FOUND",
        "value": {
            "verified": False,
            "status": "NOT_FOUND",
            "paymentCode": "ABC123456789",
            "expectedAmount": "25.50",
            "asset": "USDT",
            "retryable": True,
        },
    },
}


@router.post(
    "/verify",
    response_model=VerifyPaymentResponse,
    response_model_exclude_none=True,
    summary="Verify and claim a Binance payment",
    description=_STATUS_DOC,
    dependencies=[Depends(rate_limit("verify", "rate_limit_verify_per_minute", require_api_key))],
    responses={
        200: {"content": {"application/json": {"examples": _EXAMPLES}}},
        401: {"description": "Missing or invalid API key"},
        422: {"description": "Invalid request (e.g. float amount, too long strings)"},
        429: {"description": "Rate limit exceeded"},
    },
)
async def verify_payment(
    body: VerifyPaymentRequest,
    client_id: str = Depends(require_api_key),
    container: Container = Depends(get_container),
) -> VerifyPaymentResponse:
    settings = container.settings
    max_age = body.max_age_minutes or settings.default_payment_max_age_minutes
    if max_age > settings.max_payment_age_minutes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"maxAgeMinutes may not exceed {settings.max_payment_age_minutes}",
        )
    result = await container.verifier.verify(
        VerificationRequest(
            payment_code=body.payment_code,
            expected_amount=body.expected_amount,
            asset=body.asset,
            order_reference=body.order_reference,
            max_age_minutes=max_age,
            client_id=client_id,
        )
    )
    return VerifyPaymentResponse.from_result(result)
