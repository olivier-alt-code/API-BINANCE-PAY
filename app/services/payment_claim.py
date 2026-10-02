"""Atomic, idempotent payment claims (anti-replay).

Guarantees, enforced by PostgreSQL (not by process memory), so they hold across replicas:

* the ``payments`` row is locked ``FOR UPDATE`` while the claim is decided, so concurrent
  claims of the same payment are serialized;
* ``payment_claims.payment_id`` is UNIQUE: even if the lock were bypassed, the second
  INSERT fails and is reported as ``ALREADY_CLAIMED``.

Result for two concurrent requests on the same payment: exactly one ``CLAIMED``; the
other gets ``ALREADY_CLAIMED`` (or ``IDEMPOTENT`` if it is the same order reference).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Payment, PaymentClaim


class ClaimStatus(StrEnum):
    CLAIMED = "CLAIMED"
    IDEMPOTENT = "IDEMPOTENT"  # already claimed by the SAME order reference
    ALREADY_CLAIMED = "ALREADY_CLAIMED"  # claimed by another order
    PAYMENT_MISSING = "PAYMENT_MISSING"


@dataclass(frozen=True)
class ClaimResult:
    status: ClaimStatus
    order_reference: str | None = None
    claimed_at: datetime | None = None


def _same_order(claim: PaymentClaim, order_reference: str | None) -> bool:
    # A claim without order reference can never be "re-confirmed": we could not tell
    # whether the caller is the original one.
    return order_reference is not None and claim.order_reference == order_reference


class PaymentClaimService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def get_claim(self, payment_id: int) -> PaymentClaim | None:
        async with self._sessionmaker() as session:
            return await session.scalar(
                select(PaymentClaim).where(PaymentClaim.payment_id == payment_id)
            )

    async def claim(
        self,
        *,
        payment_id: int,
        order_reference: str | None,
        expected_amount: Decimal,
        asset: str,
        client_id: str | None,
    ) -> ClaimResult:
        try:
            async with self._sessionmaker() as session, session.begin():
                locked = await session.scalar(
                    select(Payment.id).where(Payment.id == payment_id).with_for_update()
                )
                if locked is None:
                    return ClaimResult(ClaimStatus.PAYMENT_MISSING)
                existing = await session.scalar(
                    select(PaymentClaim).where(PaymentClaim.payment_id == payment_id)
                )
                if existing is not None:
                    status = (
                        ClaimStatus.IDEMPOTENT
                        if _same_order(existing, order_reference)
                        else ClaimStatus.ALREADY_CLAIMED
                    )
                    return ClaimResult(status, existing.order_reference, existing.claimed_at)
                claim = PaymentClaim(
                    payment_id=payment_id,
                    order_reference=order_reference,
                    expected_amount=expected_amount,
                    asset=asset,
                    client_id=client_id,
                )
                session.add(claim)
                await session.flush()
                await session.refresh(claim)
                return ClaimResult(ClaimStatus.CLAIMED, claim.order_reference, claim.claimed_at)
        except IntegrityError:
            # UNIQUE(payment_id) backstop: someone else won the race.
            existing = await self.get_claim(payment_id)
            if existing is not None and _same_order(existing, order_reference):
                return ClaimResult(
                    ClaimStatus.IDEMPOTENT, existing.order_reference, existing.claimed_at
                )
            return ClaimResult(
                ClaimStatus.ALREADY_CLAIMED,
                existing.order_reference if existing else None,
                existing.claimed_at if existing else None,
            )
