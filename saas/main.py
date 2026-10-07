"""Application factory: ``uvicorn --factory saas.main:create_app``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware

from saas.billing import BillingService
from saas.config import Settings, get_settings
from saas.db import create_engine, create_sessionmaker, init_db, utcnow
from saas.security import RateLimiter, SecurityHeadersMiddleware, csrf_token
from saas.verifier import HttpVerifier, Verifier
from saas.web import router, templates

logger = logging.getLogger("saas")

_STD_ATTRS = set(vars(logging.makeLogRecord({})))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **{k: v for k, v in vars(record).items() if k not in _STD_ATTRS},
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def _configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


async def _sweeper(app: FastAPI, interval: int) -> None:
    while True:
        try:
            await app.state.billing.sweep_expired()
        except Exception:
            logger.exception("sweep_failed")
        await asyncio.sleep(interval)


def create_app(
    settings: Settings | None = None,
    *,
    verifier: Verifier | None = None,
    clock: Callable[[], datetime] = utcnow,
    run_sweeper: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        await init_db(engine, settings)
        app.state.verifier = verifier or HttpVerifier(
            settings.verifier_api_url,
            settings.verifier_admin_key.get_secret_value(),
            settings.verifier_token.get_secret_value(),
            timeout=settings.verifier_timeout_seconds,
        )
        app.state.billing = BillingService(
            settings, create_sessionmaker(engine), app.state.verifier, clock=clock
        )
        task = (
            asyncio.create_task(_sweeper(app, settings.sweep_interval_seconds))
            if run_sweeper
            else None
        )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await app.state.verifier.aclose()
            await engine.dispose()

    app = FastAPI(
        title=settings.site_name, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.settings = settings
    app.state.rate_limiter = RateLimiter()

    secret = settings.session_secret.get_secret_value() or secrets.token_urlsafe(32)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="saas_session",
        max_age=7 * 24 * 3600,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(router)

    @app.exception_handler(HTTPException)
    @app.exception_handler(FastAPIHTTPException)
    async def http_error(request: Request, exc: HTTPException) -> HTMLResponse:
        messages = {404: "Página no encontrada.", 405: "Método no permitido."}
        detail = exc.detail if isinstance(exc.detail, str) and exc.status_code != 404 else None
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "settings": settings,
                "csrf": csrf_token(request),
                "logged_in": "sub" in request.session,
                "flash": None,
                "message": detail or messages.get(exc.status_code, "Algo salió mal."),
                "code": exc.status_code,
            },
            status_code=exc.status_code,
        )

    return app
