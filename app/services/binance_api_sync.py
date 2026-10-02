"""Binance Pay trade history -> PostgreSQL, and the matching ``PaymentEvidenceProvider``.

The real Binance "Payment Receive Successful" email has the amount, time and payer
nickname, but NOT the transaction id. The account API ``GET /sapi/v1/pay/transactions``
does, signed by Binance, so it is the source of truth for verification:

* Incoming transfers (positive amount, allowed ``orderType``, default ``C2C``) are stored
  in ``payments`` with ``source=BINANCE_PAY_HISTORY`` and ``payment_code=transactionId``.
* Incremental: a cursor in ``evidence_sync_state`` plus an overlap window; duplicates are
  impossible thanks to ``UNIQUE(source, external_id)``.
* Multi-replica safe and weight-friendly (3000 per call): an advisory lock serializes
  syncs and ``BINANCE_API_MIN_INTERVAL_SECONDS`` is enforced through PostgreSQL, so N
  replicas never multiply the calls to Binance.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.exceptions import EvidenceProviderUnavailableError, EvidenceSyncPendingError
from app.db.locks import BINANCE_API_SYNC_LOCK_NAMESPACE, advisory_lock
from app.db.models import EvidenceSyncState, PaymentSource
from app.db.repositories.payments import NewPayment, PaymentRepository, StoreOutcome
from app.integrations.binance.account_api import (
    BinanceApiError,
    PayTransaction,
    filter_incoming,
)
from app.integrations.binance.evidence import PaymentEvidence
from app.integrations.binance.templates import PaymentStatus
from app.services.email_evidence import payment_to_evidence
from app.services.mail_sync import SyncMode

logger = logging.getLogger(__name__)

SOURCE = PaymentSource.BINANCE_PAY_HISTORY
_MAX_WINDOW = timedelta(days=89)


class PayHistoryClient(Protocol):
    async def fetch_transactions(
        self, start: datetime, end: datetime, *, max_pages: int = 20
    ) -> list[PayTransaction]: ...


@dataclass
class ApiSyncSummary:
    fetched: int = 0
    incoming: int = 0
    payments_imported: int = 0
    duplicates: int = 0
    synced: bool = False
    failed: bool = False
    busy: bool = False
    skipped_recent: bool = False
    errors: list[str] = field(default_factory=list)


def utcnow() -> datetime:
    return datetime.now(UTC)


class BinanceApiSyncService:
    def __init__(
        self,
        *,
        settings: Settings,
        engine: AsyncEngine,
        sessionmaker: async_sessionmaker[AsyncSession],
        client: PayHistoryClient | None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._sessionmaker = sessionmaker
        self._client = client
        self._clock = clock

    @property
    def configured(self) -> bool:
        return self._client is not None

    def _normalize_code(self, code: str) -> str:
        code = code.strip()
        return code.upper() if self._settings.payment_code_case_insensitive else code

    async def _state(self, session: AsyncSession) -> EvidenceSyncState | None:
        return await session.scalar(
            select(EvidenceSyncState).where(EvidenceSyncState.source == SOURCE)
        )

    async def _save_state(
        self, *, cursor: datetime | None, synced_at: datetime, error: str | None
    ) -> None:
        values: dict[str, object] = {"last_synced_at": synced_at, "last_error": error}
        if cursor is not None:
            values["cursor_time"] = cursor
        async with self._sessionmaker() as session, session.begin():
            stmt = insert(EvidenceSyncState).values(source=SOURCE, **values)
            await session.execute(
                stmt.on_conflict_do_update(index_elements=[EvidenceSyncState.source], set_=values)
            )

    async def sync(self, mode: SyncMode) -> ApiSyncSummary:
        summary = ApiSyncSummary()
        if self._client is None:
            summary.failed = True
            summary.errors.append("Binance API key not configured")
            return summary
        wait = 0.0 if mode is SyncMode.PERIODIC else self._settings.mail_sync_lock_wait_seconds
        async with advisory_lock(
            self._engine,
            BINANCE_API_SYNC_LOCK_NAMESPACE,
            1,
            wait_seconds=wait,
            transaction_pooler=self._settings.database.transaction_pooler,
        ) as acquired:
            if not acquired:
                summary.busy = True
                return summary
            async with self._sessionmaker() as session:
                state = await self._state(session)
            now = self._clock()
            if (
                state is not None
                and state.last_synced_at is not None
                and (now - state.last_synced_at).total_seconds()
                < self._settings.binance_api_min_interval_seconds
            ):
                # Synced moments ago (maybe by the replica we waited for): protects the
                # API weight limit no matter how many verify requests arrive.
                summary.skipped_recent = True
                return summary

            if state is not None and state.cursor_time is not None:
                start = state.cursor_time - timedelta(
                    seconds=self._settings.binance_api_overlap_seconds
                )
            else:
                start = now - timedelta(hours=self._settings.binance_api_initial_lookback_hours)
            start = max(start, now - _MAX_WINDOW)

            try:
                transactions = await self._client.fetch_transactions(start, now)
            except BinanceApiError as exc:
                summary.failed = True
                summary.errors.append(exc.public_message)
                logger.warning(
                    "binance_api_sync_failed",
                    extra={"error_type": type(exc).__name__, "binance_code": exc.code},
                )
                await self._save_state(cursor=None, synced_at=now, error=exc.public_message)
                return summary

            summary.fetched = len(transactions)
            incoming = filter_incoming(transactions, self._settings.binance_pay_order_types)
            summary.incoming = len(incoming)
            async with self._sessionmaker() as session, session.begin():
                repo = PaymentRepository(session)
                for tx in incoming:
                    outcome = await repo.store(
                        NewPayment(
                            source=SOURCE,
                            external_id=tx.transaction_id,
                            email_message_id=None,
                            payment_code=self._normalize_code(tx.transaction_id),
                            amount=tx.amount,
                            asset=tx.currency,
                            # Pay trade history only lists settled transfers.
                            payment_status=PaymentStatus.PAID,
                            received_at=tx.transaction_time,
                            trusted=True,  # signed API response from Binance itself
                            template=None,
                            payer_name=tx.payer_name,
                            payer_binance_id=tx.payer_binance_id,
                        )
                    )
                    if outcome is StoreOutcome.INSERTED:
                        summary.payments_imported += 1
                    else:
                        summary.duplicates += 1
            await self._save_state(cursor=now, synced_at=now, error=None)
            summary.synced = True
            logger.info(
                "binance_api_sync_done",
                extra={
                    "mode": mode.value,
                    "fetched": summary.fetched,
                    "incoming": summary.incoming,
                    "imported": summary.payments_imported,
                },
            )
            return summary


class BinancePayHistoryProvider:
    """``PaymentEvidenceProvider`` backed by the Binance Pay trade history API."""

    source = SOURCE

    def __init__(
        self,
        *,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        sync_service: BinanceApiSyncService,
    ) -> None:
        self._settings = settings
        self._sessionmaker = sessionmaker
        self._sync = sync_service

    async def _lookup(self, payment_code: str) -> PaymentEvidence | None:
        async with self._sessionmaker() as session:
            payment = await PaymentRepository(session).find_best_by_code(
                payment_code, source=SOURCE
            )
            return payment_to_evidence(payment) if payment is not None else None

    async def find_payment(self, payment_code: str) -> PaymentEvidence | None:
        evidence = await self._lookup(payment_code)
        if evidence is not None:
            return evidence
        if not self._sync.configured:
            raise EvidenceProviderUnavailableError("Binance API key not configured")
        # Not stored yet (e.g. paid seconds ago): query Binance now and look again.
        summary = await self._sync.sync(SyncMode.ON_DEMAND)
        refreshed = await self._lookup(payment_code)
        if refreshed is not None:
            return refreshed
        if summary.failed:
            raise EvidenceProviderUnavailableError("Binance API sync failed")
        if summary.busy:
            raise EvidenceSyncPendingError("Binance API sync in progress")
        return None
