"""Owner-only endpoints (ADMIN_API_KEYS): manage clients and their private tokens.

Typical flow::

    POST /v1/admin/clients {"name": "Ana"}         -> returns Ana's first token (once)
    POST /v1/admin/clients/{id}/tokens {"name": …}  -> another token for Ana (once)
    DELETE /v1/admin/tokens/{token_id}              -> revoke a token immediately
    PATCH /v1/admin/clients/{id} {"enabled": false} -> cut all of Ana's access
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_container, rate_limit
from app.container import Container
from app.core.security import require_admin_key
from app.schemas.clients import (
    ClientOut,
    CreateClientRequest,
    CreateClientResponse,
    CreateTokenRequest,
    IssuedTokenOut,
    RevokeTokenResponse,
    TokenOut,
    UpdateClientRequest,
)
from app.services.tenants import IssuedToken, TenantNameTakenError, TenantNotFoundError

router = APIRouter(prefix="/v1/admin", tags=["admin"])

_admin = [
    Depends(require_admin_key),
    Depends(rate_limit("admin", "rate_limit_admin_per_minute", require_admin_key)),
]


def _issued(token: IssuedToken) -> IssuedTokenOut:
    return IssuedTokenOut(
        id=token.id,
        name=token.name,
        prefix=token.prefix,
        token=token.token,
        expires_at=token.expires_at,
    )


@router.post(
    "/clients",
    response_model=CreateClientResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a client and its first private token (shown once)",
    dependencies=_admin,
)
async def create_client(
    body: CreateClientRequest, container: Container = Depends(get_container)
) -> CreateClientResponse:
    try:
        tenant, token = await container.tenants.create_tenant(
            body.name, token_name=body.token_name, expires_in_days=body.expires_in_days
        )
    except TenantNameTakenError:
        raise HTTPException(
            status_code=409, detail="A client with this name already exists"
        ) from None
    return CreateClientResponse(
        client=ClientOut(
            id=tenant.id,
            name=tenant.name,
            enabled=tenant.enabled,
            created_at=tenant.created_at,
            active_tokens=1,
            binance_configured=False,
        ),
        token=_issued(token),
    )


@router.get("/clients", response_model=list[ClientOut], summary="List clients", dependencies=_admin)
async def list_clients(container: Container = Depends(get_container)) -> list[ClientOut]:
    return [
        ClientOut(
            id=t.id,
            name=t.name,
            enabled=t.enabled,
            created_at=t.created_at,
            active_tokens=t.active_tokens,
            binance_configured=t.binance_configured,
        )
        for t in await container.tenants.list_tenants()
    ]


@router.patch(
    "/clients/{client_id}",
    response_model=ClientOut,
    summary="Enable or disable a client (disabled clients cannot use any token)",
    dependencies=_admin,
)
async def update_client(
    client_id: int, body: UpdateClientRequest, container: Container = Depends(get_container)
) -> ClientOut:
    try:
        tenant = await container.tenants.set_enabled(client_id, body.enabled)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="Client not found") from None
    return ClientOut(
        id=tenant.id, name=tenant.name, enabled=tenant.enabled, created_at=tenant.created_at
    )


@router.post(
    "/clients/{client_id}/tokens",
    response_model=IssuedTokenOut,
    status_code=status.HTTP_201_CREATED,
    summary="Issue another private token for a client (shown once)",
    dependencies=_admin,
)
async def create_token(
    client_id: int, body: CreateTokenRequest, container: Container = Depends(get_container)
) -> IssuedTokenOut:
    try:
        token = await container.tenants.issue_token(client_id, body.name, body.expires_in_days)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="Client not found") from None
    return _issued(token)


@router.get(
    "/clients/{client_id}/tokens",
    response_model=list[TokenOut],
    summary="List a client's tokens (never their values)",
    dependencies=_admin,
)
async def list_tokens(
    client_id: int, container: Container = Depends(get_container)
) -> list[TokenOut]:
    try:
        tokens = await container.tenants.list_tokens(client_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail="Client not found") from None
    now = datetime.now(UTC)
    return [
        TokenOut(
            id=t.id,
            name=t.name,
            prefix=t.prefix,
            created_at=t.created_at,
            expires_at=t.expires_at,
            revoked_at=t.revoked_at,
            last_used_at=t.last_used_at,
            active=t.revoked_at is None and (t.expires_at is None or t.expires_at > now),
        )
        for t in tokens
    ]


@router.delete(
    "/tokens/{token_id}",
    response_model=RevokeTokenResponse,
    summary="Revoke a token immediately",
    dependencies=_admin,
)
async def revoke_token(
    token_id: int, container: Container = Depends(get_container)
) -> RevokeTokenResponse:
    if not await container.tenants.revoke_token(token_id):
        raise HTTPException(status_code=404, detail="Token not found or already revoked") from None
    return RevokeTokenResponse(revoked=True)
