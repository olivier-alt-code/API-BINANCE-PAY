"""Source-agnostic payment evidence.

Every source (the Binance Pay trade history API today, the Binance Pay Merchant API later)
produces the same :class:`PaymentEvidence`. :class:`app.services.payment_verifier.PaymentVerifier`
only depends on :class:`PaymentEvidenceProvider`, never on a concrete source.

Evidence is always persisted in the ``payments`` table first, and ``evidence_id`` is that
row's id: the transactional claim (anti-replay) is enforced on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.db.models import PaymentSource


class PaymentStatus(StrEnum):
    PAID = "PAID"
    PENDING = "PENDING"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


@dataclass(frozen=True)
class PaymentEvidence:
    evidence_id: int  # payments.id
    external_id: str  # Binance transactionId / prepayId
    payment_code: str
    amount: Decimal
    asset: str
    status: PaymentStatus
    timestamp: datetime  # UTC
    source: PaymentSource
    trusted: bool
    ambiguous: bool = False


class PaymentEvidenceProvider(Protocol):
    source: PaymentSource

    async def find_payment(self, tenant_id: int, payment_code: str) -> PaymentEvidence | None:
        """Return evidence for exactly ``payment_code`` on the tenant's account, or ``None``.

        Implementations may refresh their source (e.g. query Binance) when the code is
        unknown.

        Raises:
            EvidenceProviderUnavailableError: the source cannot be reached.
            EvidenceNotConfiguredError: the tenant has not configured the source.
            EvidenceSyncPendingError: a refresh is in progress elsewhere; retry later.
        """
        ...
