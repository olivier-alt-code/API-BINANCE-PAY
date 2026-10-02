from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.routes import binance, gmail, health, payments
from app.config import Settings, get_settings
from app.container import Container, build_container
from app.core.logging import configure_logging
from app.core.security import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from app.db.session import get_engine, get_sessionmaker
from app.workers.mail_sync_worker import run_sync_loop

logger = logging.getLogger("app")

DESCRIPTION = """
Verifies payments received on Binance by reading **authenticated** Binance notification
emails from Gmail (IMAP over SSL, App Password or OAuth2/XOAUTH2).

* `POST /v1/payments/verify` — exact code + Decimal amount + asset + time window, then an
  **atomic claim** so one payment can never pay two orders.
* Authentication: `Authorization: Bearer <API_KEY>` (admin endpoints use admin keys).
"""


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json, settings.secret_values())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "container", None) is None:
            app.state.container = build_container(settings, get_engine(), get_sessionmaker())
        stop = asyncio.Event()
        worker: asyncio.Task[None] | None = None
        if settings.run_sync_worker_in_api:
            worker = asyncio.create_task(run_sync_loop(app.state.container, stop))
        logger.info("api_started", extra={"env": settings.app_env.value})
        try:
            yield
        finally:
            stop.set()
            if worker is not None:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
            if container is None:
                await app.state.container.engine.dispose()

    docs = settings.docs_enabled
    app = FastAPI(
        title="Binance Payment Verifier",
        version="0.1.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.container = container

    # Middleware (last added = outermost).
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)
    if settings.cors_allowed_origins:  # CORS closed by default
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
            allow_credentials=False,
        )
    if settings.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.middleware("http")
    async def access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid.uuid4().hex
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("unhandled_error", extra={"request_id": request_id})
            response = JSONResponse(
                {"detail": "Internal server error", "requestId": request_id}, status_code=500
            )
        response.headers["X-Request-ID"] = request_id
        # Only method/path/status: never headers (Authorization) nor bodies.
        logger.info(
            "http_request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            },
        )
        return response

    app.include_router(health.router)
    app.include_router(payments.router)
    app.include_router(gmail.router)
    app.include_router(binance.router)
    return app


def app_factory() -> FastAPI:
    return create_app()
