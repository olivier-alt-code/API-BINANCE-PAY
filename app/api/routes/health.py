from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import get_container
from app.container import Container

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", summary="Readiness probe (database + essential configuration)")
async def ready(container: Container = Depends(get_container)) -> JSONResponse:
    settings = container.settings
    checks: dict[str, bool] = {}
    try:
        async with container.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            # Schema must be migrated (alembic upgrade head).
            await conn.execute(text("SELECT 1 FROM payment_claims LIMIT 1"))
        checks["database"] = True
    except Exception as exc:
        logger.warning("readiness_database_failed", extra={"error_type": type(exc).__name__})
        checks["database"] = False

    checks["api_keys_configured"] = bool(settings.api_keys)
    checks["binance_senders_configured"] = settings.binance_senders_configured
    checks["binance_templates_enabled"] = bool(container.parser.templates)
    if container.cipher is not None:
        try:
            checks["encryption_key"] = container.cipher.self_test()
        except Exception:
            checks["encryption_key"] = False
    checks["mail_account_configured"] = bool(settings.gmail_email) or (
        container.oauth_client.configured and container.cipher is not None
    )

    ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ready" if ok else "not_ready", "checks": checks},
    )
