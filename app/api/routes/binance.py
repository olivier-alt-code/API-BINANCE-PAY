"""Protected admin endpoints for the Binance Pay trade history API. Never returns secrets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.api.deps import get_container, rate_limit
from app.container import Container
from app.core.security import require_admin_key
from app.integrations.binance.account_api import BinanceApiError, BinancePayHistoryClient
from app.services.mail_sync import SyncMode

router = APIRouter(prefix="/v1/admin/binance", tags=["admin"])

_admin_rl = Depends(rate_limit("admin", "rate_limit_admin_per_minute", require_admin_key))


class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class BinanceTestResponse(_Camel):
    connected: bool
    source: str = "binance_pay_history"
    error: str | None = None


class BinanceSyncResponse(_Camel):
    synced: bool
    fetched: int
    incoming: int
    payments_imported: int
    duplicates: int
    busy: bool
    skipped_recent: bool
    errors: list[str]


@router.post(
    "/test",
    response_model=BinanceTestResponse,
    summary="Test the Binance API key against GET /sapi/v1/pay/transactions",
    dependencies=[_admin_rl],
)
async def test_binance(
    _: str = Depends(require_admin_key),
    container: Container = Depends(get_container),
) -> BinanceTestResponse:
    settings = container.settings
    if not settings.binance_api_configured:
        return BinanceTestResponse(connected=False, error="Binance API key not configured")
    assert settings.binance_api_key is not None and settings.binance_api_secret is not None
    client = BinancePayHistoryClient(
        api_key=settings.binance_api_key,
        api_secret=settings.binance_api_secret,
        base_url=settings.binance_api_base_url,
        timeout_seconds=settings.binance_api_timeout_seconds,
        recv_window_ms=settings.binance_api_recv_window_ms,
        max_retries=0,
    )
    now = datetime.now(UTC)
    try:
        # Tiny window: only checks signature, permissions and connectivity.
        await client.fetch_page(now - timedelta(minutes=1), now)
    except BinanceApiError as exc:
        return BinanceTestResponse(connected=False, error=exc.public_message)
    return BinanceTestResponse(connected=True)


@router.post(
    "/sync",
    response_model=BinanceSyncResponse,
    summary="Import recent incoming Binance Pay transfers now",
    dependencies=[_admin_rl],
)
async def sync_binance(
    _: str = Depends(require_admin_key),
    container: Container = Depends(get_container),
) -> BinanceSyncResponse:
    s = await container.api_sync_service.sync(SyncMode.MANUAL)
    return BinanceSyncResponse(
        synced=s.synced,
        fetched=s.fetched,
        incoming=s.incoming,
        payments_imported=s.payments_imported,
        duplicates=s.duplicates,
        busy=s.busy,
        skipped_recent=s.skipped_recent,
        errors=s.errors,
    )
