"""``PaymentEvidenceProvider`` backed by Binance notification emails stored in PostgreSQL."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.exceptions import EvidenceProviderUnavailableError, EvidenceSyncPendingError
from app.db.models import Payment, PaymentSource
from app.db.repositories.payments import PaymentRepository
from app.integrations.binance.evidence import PaymentEvidence
from app.integrations.binance.templates import PaymentStatus
from app.services.mail_sync import MailSyncService, SyncMode

logger = logging.getLogger(__name__)


def payment_to_evidence(payment: Payment) -> PaymentEvidence:
    return PaymentEvidence(
        evidence_id=payment.id,
        external_id=payment.external_id,
        payment_code=payment.payment_code,
        amount=payment.amount,
        asset=payment.asset,
        status=PaymentStatus(payment.payment_status),
        timestamp=payment.received_at,
        source=PaymentSource(payment.source),
        trusted=payment.trusted,
        ambiguous=payment.ambiguous,
    )


class BinanceEmailPaymentProvider:
    source = PaymentSource.BINANCE_EMAIL

    def __init__(
        self,
        *,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        sync_service: MailSyncService,
    ) -> None:
        self._settings = settings
        self._sessionmaker = sessionmaker
        self._sync = sync_service

    async def _lookup(self, payment_code: str) -> PaymentEvidence | None:
        async with self._sessionmaker() as session:
            payment = await PaymentRepository(session).find_best_by_code(payment_code)
            return payment_to_evidence(payment) if payment is not None else None

    async def find_payment(self, payment_code: str) -> PaymentEvidence | None:
        evidence = await self._lookup(payment_code)
        if evidence is not None and evidence.trusted:
            return evidence
        if not self._settings.mail_on_demand_sync_enabled:
            return evidence

        # Not in the DB yet (or only untrusted evidence): sync Gmail now and look again.
        summary = await self._sync.sync_all(SyncMode.ON_DEMAND)
        refreshed = await self._lookup(payment_code)
        if refreshed is not None and refreshed.trusted:
            return refreshed

        if summary.accounts_total == 0:
            raise EvidenceProviderUnavailableError("No mail account configured")
        if summary.accounts_failed == summary.accounts_total:
            raise EvidenceProviderUnavailableError("All mail accounts failed to sync")
        if summary.accounts_busy and not (
            summary.accounts_synced or summary.accounts_skipped_recent
        ):
            raise EvidenceSyncPendingError("Mailbox sync in progress")
        return refreshed
