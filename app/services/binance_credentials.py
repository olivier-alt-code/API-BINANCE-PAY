"""Per-client Binance API credentials: validated against Binance, stored encrypted.

* The key must be READ-ONLY: ``GET /sapi/v1/account/apiRestrictions`` is checked and any key
  allowing trading, withdrawals or transfers is rejected (and never stored).
* The key must be able to read the Pay trade history (tested with a tiny window).
* Key and secret are stored together as an AES-256-GCM envelope bound to the client id
  (associated data), so a ciphertext copied to another client's row cannot be decrypted.
* Neither the key nor the secret is ever returned, logged or included in errors; only the
  last 4 characters of the key are kept in clear as a hint.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.encryption import CredentialCipher
from app.core.exceptions import AppError, ConfigurationError
from app.db.models import BinanceCredential, EvidenceSyncState
from app.integrations.binance.account_api import (
    BinanceApiError,
    BinanceAuthError,
    KeyPermissions,
    PayTransaction,
)

logger = logging.getLogger(__name__)


class BinanceAccountClient(Protocol):
    async def key_permissions(self) -> KeyPermissions: ...

    async def fetch_page(self, start: datetime, end: datetime) -> list[PayTransaction]: ...

    async def fetch_transactions(
        self, start: datetime, end: datetime, *, max_pages: int = 20
    ) -> list[PayTransaction]: ...


ClientFactory = Callable[[SecretStr, SecretStr], BinanceAccountClient]


class CredentialsRejectedError(AppError):
    public_message = "Binance API key rejected"

    def __init__(self, message: str, problems: list[str]) -> None:
        super().__init__(message)
        self.problems = problems


def credentials_aad(tenant_id: int) -> str:
    return f"tenant:{tenant_id}:binance-credentials"


def utcnow() -> datetime:
    return datetime.now(UTC)


class BinanceCredentialService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        cipher: CredentialCipher | None,
        client_factory: ClientFactory,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._cipher = cipher
        self._factory = client_factory
        self._clock = clock

    def _require_cipher(self) -> CredentialCipher:
        if self._cipher is None:
            raise ConfigurationError("CREDENTIALS_ENCRYPTION_KEY is not configured")
        return self._cipher

    async def save(
        self, tenant_id: int, api_key: SecretStr, api_secret: SecretStr
    ) -> BinanceCredential:
        cipher = self._require_cipher()
        client = self._factory(api_key, api_secret)
        try:
            permissions = await client.key_permissions()
        except BinanceAuthError:
            raise CredentialsRejectedError(
                "Binance rejected the key",
                ["Invalid API key/secret, or this server's IP is not whitelisted"],
            ) from None
        if not permissions.read_only:
            # Never store a key that can trade, withdraw or transfer funds.
            raise CredentialsRejectedError("Key is not read-only", permissions.problems)
        now = self._clock()
        try:
            await client.fetch_page(now - timedelta(minutes=1), now)
        except BinanceAuthError:
            raise CredentialsRejectedError(
                "Key cannot read Pay history", ["Key cannot read the Binance Pay history"]
            ) from None
        # Other BinanceApiError (network, 5xx) propagate as "Binance unavailable".

        encrypted = cipher.encrypt_json(
            {"api_key": api_key.get_secret_value(), "api_secret": api_secret.get_secret_value()},
            credentials_aad(tenant_id),
        )
        values = {
            "encrypted": encrypted,
            "api_key_hint": api_key.get_secret_value()[-4:],
            "ip_restricted": permissions.ip_restricted,
            "permissions": permissions.as_dict(),
            "verified_at": now,
        }
        async with self._sessionmaker() as session, session.begin():
            await session.execute(
                insert(BinanceCredential)
                .values(tenant_id=tenant_id, **values)
                .on_conflict_do_update(index_elements=[BinanceCredential.tenant_id], set_=values)
            )
            # New key (maybe another Binance account): restart the import from the lookback.
            await session.execute(
                delete(EvidenceSyncState).where(EvidenceSyncState.tenant_id == tenant_id)
            )
            row = await session.get(BinanceCredential, tenant_id, populate_existing=True)
            assert row is not None
        logger.info("binance_credentials_saved", extra={"tenant_id": tenant_id})
        return row

    async def get(self, tenant_id: int) -> BinanceCredential | None:
        async with self._sessionmaker() as session:
            return await session.get(BinanceCredential, tenant_id)

    async def delete(self, tenant_id: int) -> bool:
        async with self._sessionmaker() as session, session.begin():
            result = await session.execute(
                delete(BinanceCredential)
                .where(BinanceCredential.tenant_id == tenant_id)
                .returning(BinanceCredential.tenant_id)
            )
            return result.scalar_one_or_none() is not None

    async def client_for(self, tenant_id: int) -> BinanceAccountClient | None:
        row = await self.get(tenant_id)
        if row is None:
            return None
        data = self._require_cipher().decrypt_json(row.encrypted, credentials_aad(tenant_id))
        return self._factory(SecretStr(str(data["api_key"])), SecretStr(str(data["api_secret"])))


__all__ = [
    "BinanceAccountClient",
    "BinanceApiError",
    "BinanceCredentialService",
    "ClientFactory",
    "CredentialsRejectedError",
]
