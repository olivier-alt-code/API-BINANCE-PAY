"""Client self-service endpoints, authenticated with the client's private token."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse

from app.api.deps import client_rate_key, get_container, rate_limit, require_client
from app.container import Container
from app.core.exceptions import ConfigurationError
from app.db.models import BinanceCredential
from app.integrations.binance.account_api import BinanceApiError
from app.schemas.clients import (
    BinanceCredentialsOut,
    BinanceCredentialsRequest,
    BinanceSyncOut,
    CredentialsRejectedOut,
    MeOut,
)
from app.services.binance_api_sync import SyncMode
from app.services.binance_credentials import CredentialsRejectedError
from app.services.tenants import AuthContext

router = APIRouter(prefix="/v1/me", tags=["me"])

_credentials_rl = Depends(
    rate_limit("credentials", "rate_limit_credentials_per_minute", client_rate_key)
)


def _out(row: BinanceCredential | None) -> BinanceCredentialsOut:
    if row is None:
        return BinanceCredentialsOut(configured=False)
    return BinanceCredentialsOut(
        configured=True,
        api_key_hint="…" + row.api_key_hint,
        ip_restricted=row.ip_restricted,
        verified_at=row.verified_at,
        permissions={k: bool(v) for k, v in row.permissions.items()},
    )


@router.get("", response_model=MeOut, summary="Who am I (client and token in use)")
async def me(auth: AuthContext = Depends(require_client)) -> MeOut:
    return MeOut(
        client_id=auth.tenant_id,
        client_name=auth.tenant_name,
        token_id=auth.token_id,
        token_name=auth.token_name,
    )


@router.get(
    "/binance-credentials",
    response_model=BinanceCredentialsOut,
    summary="Status of your Binance API key (never returns the key or secret)",
)
async def get_credentials(
    auth: AuthContext = Depends(require_client), container: Container = Depends(get_container)
) -> BinanceCredentialsOut:
    return _out(await container.credentials.get(auth.tenant_id))


@router.put(
    "/binance-credentials",
    response_model=BinanceCredentialsOut,
    summary="Register or replace your READ-ONLY Binance API key",
    description=(
        "The key is checked against Binance before being stored: it must have **Enable "
        "Reading** and **no** trading, withdrawal or transfer permission, and must be able "
        "to read your Binance Pay history. It is stored encrypted (AES-256-GCM) and never "
        "returned. If you restricted the key by IP, whitelist this server's IP in Binance."
    ),
    dependencies=[_credentials_rl],
    responses={422: {"model": CredentialsRejectedOut}, 503: {"description": "Binance down"}},
)
async def put_credentials(
    body: BinanceCredentialsRequest,
    auth: AuthContext = Depends(require_client),
    container: Container = Depends(get_container),
) -> BinanceCredentialsOut | JSONResponse:
    try:
        row = await container.credentials.save(auth.tenant_id, body.api_key, body.api_secret)
    except CredentialsRejectedError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=CredentialsRejectedOut(
                detail=exc.public_message, problems=exc.problems
            ).model_dump(),
        )
    except ConfigurationError:
        raise HTTPException(
            status_code=503, detail="Credential storage is not configured"
        ) from None
    except BinanceApiError as exc:
        raise HTTPException(status_code=503, detail=exc.public_message) from None
    return _out(row)


@router.delete(
    "/binance-credentials",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete your stored Binance API key",
)
async def delete_credentials(
    auth: AuthContext = Depends(require_client), container: Container = Depends(get_container)
) -> Response:
    if not await container.credentials.delete(auth.tenant_id):
        raise HTTPException(status_code=404, detail="No Binance API key stored") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/binance/sync",
    response_model=BinanceSyncOut,
    summary="Import your recent incoming Binance Pay transfers now",
    dependencies=[_credentials_rl],
)
async def sync_now(
    auth: AuthContext = Depends(require_client), container: Container = Depends(get_container)
) -> BinanceSyncOut:
    s = await container.api_sync_service.sync(auth.tenant_id, SyncMode.MANUAL)
    if s.not_configured:
        raise HTTPException(status_code=409, detail="Register your Binance API key first") from None
    return BinanceSyncOut(
        synced=s.synced,
        fetched=s.fetched,
        incoming=s.incoming,
        payments_imported=s.payments_imported,
        duplicates=s.duplicates,
        busy=s.busy,
        skipped_recent=s.skipped_recent,
        errors=s.errors,
    )
