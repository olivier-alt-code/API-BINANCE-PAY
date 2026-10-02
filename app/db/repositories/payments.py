from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment
from app.integrations.binance.templates import PaymentStatus


class StoreOutcome(StrEnum):
    INSERTED = "INSERTED"
    DUPLICATE = "DUPLICATE"  # same evidence already stored
    UPGRADED = "UPGRADED"  # PENDING -> final status for the same payment
    CONFLICT = "CONFLICT"  # contradicting trusted evidence: payment marked ambiguous


@dataclass(frozen=True)
class NewPayment:
    source: str
    external_id: str
    email_message_id: int | None
    payment_code: str
    amount: Decimal
    asset: str
    payment_status: str
    received_at: datetime
    trusted: bool
    template: str | None
    payer_name: str | None = None
    payer_binance_id: str | None = None


class PaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def find_best_by_code(
        self, payment_code: str, *, source: str | None = None
    ) -> Payment | None:
        """Exact (``=``) match only. Trusted evidence wins; otherwise the newest untrusted."""
        stmt = select(Payment).where(Payment.payment_code == payment_code)
        if source is not None:
            stmt = stmt.where(Payment.source == source)
        return await self._s.scalar(
            stmt.order_by(
                Payment.trusted.desc(), Payment.received_at.desc(), Payment.id.desc()
            ).limit(1)
        )

    async def store(self, new: NewPayment) -> StoreOutcome:
        """Insert evidence, deduplicating by (source, external_id) and by trusted code.

        Must run inside a transaction. Concurrency-safe thanks to the unique indexes:
        two workers processing the same email can never create two payments.
        """
        stmt = (
            insert(Payment)
            .values(
                source=new.source,
                external_id=new.external_id,
                email_message_id=new.email_message_id,
                payment_code=new.payment_code,
                amount=new.amount,
                asset=new.asset,
                payment_status=new.payment_status,
                received_at=new.received_at,
                trusted=new.trusted,
                ambiguous=False,
                template=new.template,
                payer_name=new.payer_name,
                payer_binance_id=new.payer_binance_id,
            )
            .on_conflict_do_nothing()
            .returning(Payment.id)
        )
        if (await self._s.execute(stmt)).scalar_one_or_none() is not None:
            return StoreOutcome.INSERTED

        same_source = await self._s.scalar(
            select(Payment.id).where(
                Payment.source == new.source, Payment.external_id == new.external_id
            )
        )
        if same_source is not None or not new.trusted:
            return StoreOutcome.DUPLICATE

        # Another trusted payment already exists with this code: compare.
        existing = await self._s.scalar(
            select(Payment)
            .where(Payment.payment_code == new.payment_code, Payment.trusted)
            .with_for_update()
        )
        if existing is None:  # pragma: no cover - deleted concurrently
            return StoreOutcome.DUPLICATE
        same_money = existing.amount == new.amount and existing.asset == new.asset
        if same_money and existing.payment_status == new.payment_status:
            return StoreOutcome.DUPLICATE
        if (
            same_money
            and existing.payment_status == PaymentStatus.PENDING
            and new.payment_status != PaymentStatus.PENDING
        ):
            await self._s.execute(
                update(Payment)
                .where(Payment.id == existing.id)
                .values(payment_status=new.payment_status, received_at=new.received_at)
            )
            return StoreOutcome.UPGRADED
        if (
            same_money
            and new.payment_status == PaymentStatus.PENDING
            and existing.payment_status == PaymentStatus.PAID
        ):
            return StoreOutcome.DUPLICATE  # late "pending" notice after completion
        # Different amount/asset, or e.g. PAID followed by REFUNDED: never guess.
        await self._s.execute(
            update(Payment).where(Payment.id == existing.id).values(ambiguous=True)
        )
        return StoreOutcome.CONFLICT
