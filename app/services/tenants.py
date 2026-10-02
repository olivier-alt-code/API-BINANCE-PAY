"""Clients (tenants) and their private access tokens.

Tokens look like ``bpv_<43 random url-safe chars>`` (256 bits of entropy). Only their
SHA-256 hash is stored, so a database leak does not reveal usable tokens; the plaintext
is returned exactly once, when the token is created.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import AppError
from app.db.models import ApiToken, BinanceCredential, Tenant

TOKEN_PREFIX = "bpv_"  # noqa: S105 - public token prefix, not a secret
_DISPLAY_PREFIX_LEN = 12  # "bpv_" + 8 chars: enough to recognize a token in a list


class TenantError(AppError):
    public_message = "Client operation failed"


class TenantNameTakenError(TenantError):
    public_message = "A client with this name already exists"


class TenantNotFoundError(TenantError):
    public_message = "Client not found"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class IssuedToken:
    id: int
    tenant_id: int
    name: str
    prefix: str
    token: str  # plaintext: show once, never stored
    expires_at: datetime | None


@dataclass(frozen=True)
class AuthContext:
    tenant_id: int
    tenant_name: str
    token_id: int
    token_name: str


@dataclass(frozen=True)
class TenantSummary:
    id: int
    name: str
    enabled: bool
    created_at: datetime
    active_tokens: int
    binance_configured: bool


class TenantService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        last_used_update_seconds: int = 60,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._last_used_every = timedelta(seconds=last_used_update_seconds)
        self._clock = clock

    # --- clients ----------------------------------------------------------------------
    async def create_tenant(
        self,
        name: str,
        *,
        token_name: str = "default",  # noqa: S107 - a label, not a secret
        expires_in_days: int | None = None,
    ) -> tuple[Tenant, IssuedToken]:
        try:
            async with self._sessionmaker() as session, session.begin():
                tenant = Tenant(name=name.strip(), enabled=True)
                session.add(tenant)
                await session.flush()
                issued = await self._issue(session, tenant.id, token_name, expires_in_days)
        except IntegrityError:
            raise TenantNameTakenError("Client name already exists") from None
        return tenant, issued

    async def list_tenants(self, tenant_id: int | None = None) -> list[TenantSummary]:
        now = self._clock()
        async with self._sessionmaker() as session:
            active = (
                select(func.count())
                .select_from(ApiToken)
                .where(
                    ApiToken.tenant_id == Tenant.id,
                    ApiToken.revoked_at.is_(None),
                    (ApiToken.expires_at.is_(None)) | (ApiToken.expires_at > now),
                )
                .scalar_subquery()
            )
            configured = (
                select(func.count())
                .select_from(BinanceCredential)
                .where(BinanceCredential.tenant_id == Tenant.id)
                .scalar_subquery()
            )
            stmt = select(Tenant, active, configured).order_by(Tenant.id)
            if tenant_id is not None:
                stmt = stmt.where(Tenant.id == tenant_id)
            rows = await session.execute(stmt)
            return [
                TenantSummary(t.id, t.name, t.enabled, t.created_at, int(a), bool(c))
                for t, a, c in rows.all()
            ]

    async def get_summary(self, tenant_id: int) -> TenantSummary:
        found = await self.list_tenants(tenant_id)
        if not found:
            raise TenantNotFoundError("Client not found")
        return found[0]

    async def set_enabled(self, tenant_id: int, enabled: bool) -> Tenant:
        async with self._sessionmaker() as session, session.begin():
            tenant = await session.get(Tenant, tenant_id)
            if tenant is None:
                raise TenantNotFoundError("Client not found")
            tenant.enabled = enabled
            return tenant

    # --- tokens -----------------------------------------------------------------------
    async def _issue(
        self, session: AsyncSession, tenant_id: int, name: str, expires_in_days: int | None
    ) -> IssuedToken:
        token = generate_token()
        expires_at = self._clock() + timedelta(days=expires_in_days) if expires_in_days else None
        row = ApiToken(
            tenant_id=tenant_id,
            name=name.strip() or "default",
            prefix=token[:_DISPLAY_PREFIX_LEN],
            token_hash=hash_token(token),
            expires_at=expires_at,
        )
        session.add(row)
        await session.flush()
        return IssuedToken(row.id, tenant_id, row.name, row.prefix, token, expires_at)

    async def issue_token(
        self, tenant_id: int, name: str, expires_in_days: int | None = None
    ) -> IssuedToken:
        async with self._sessionmaker() as session, session.begin():
            if await session.get(Tenant, tenant_id) is None:
                raise TenantNotFoundError("Client not found")
            return await self._issue(session, tenant_id, name, expires_in_days)

    async def list_tokens(self, tenant_id: int) -> list[ApiToken]:
        async with self._sessionmaker() as session:
            if await session.get(Tenant, tenant_id) is None:
                raise TenantNotFoundError("Client not found")
            result = await session.scalars(
                select(ApiToken).where(ApiToken.tenant_id == tenant_id).order_by(ApiToken.id)
            )
            return list(result)

    async def revoke_token(self, token_id: int) -> bool:
        async with self._sessionmaker() as session, session.begin():
            result = await session.execute(
                update(ApiToken)
                .where(ApiToken.id == token_id, ApiToken.revoked_at.is_(None))
                .values(revoked_at=self._clock())
                .returning(ApiToken.id)
            )
            return result.scalar_one_or_none() is not None

    async def authenticate(self, token: str) -> AuthContext | None:
        """Resolve a bearer token. ``None`` for unknown, revoked, expired or disabled."""
        if not token.startswith(TOKEN_PREFIX) or len(token) > 200:
            return None
        now = self._clock()
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(ApiToken, Tenant)
                    .join(Tenant, Tenant.id == ApiToken.tenant_id)
                    .where(ApiToken.token_hash == hash_token(token))
                )
            ).first()
            if row is None:
                return None
            api_token, tenant = row
            if (
                api_token.revoked_at is not None
                or (api_token.expires_at is not None and api_token.expires_at <= now)
                or not tenant.enabled
            ):
                return None
            context = AuthContext(tenant.id, tenant.name, api_token.id, api_token.name)
            last = api_token.last_used_at
        if last is None or now - last >= self._last_used_every:
            # Throttled: at most one write per token per interval, not one per request.
            async with self._sessionmaker() as session, session.begin():
                await session.execute(
                    update(ApiToken).where(ApiToken.id == context.token_id).values(last_used_at=now)
                )
        return context
