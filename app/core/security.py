"""API authentication, security headers and request size limits."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Awaitable, Callable, Sequence

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.config import Settings, get_settings

_bearer = HTTPBearer(auto_error=False, description="API key: `Authorization: Bearer <API_KEY>`")


def _matches_any(candidate: str, keys: Sequence[SecretStr]) -> bool:
    # Compare digests so comparison time does not depend on key length; check every key
    # (no early exit) to avoid leaking which one matched through timing.
    digest = hashlib.sha256(candidate.encode()).digest()
    matched = False
    for key in keys:
        expected = hashlib.sha256(key.get_secret_value().encode()).digest()
        matched |= hmac.compare_digest(digest, expected)
    return matched


def api_key_fingerprint(key: str) -> str:
    """Non-reversible identifier for a key, safe for logs and rate limit buckets."""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _settings_for(request: Request) -> Settings:
    container = getattr(request.app.state, "container", None)
    return container.settings if container is not None else get_settings()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    settings = _settings_for(request)
    if credentials is None or not settings.api_keys:
        raise _unauthorized()
    if not _matches_any(credentials.credentials, settings.api_keys):
        raise _unauthorized()
    fingerprint = api_key_fingerprint(credentials.credentials)
    request.state.client_id = fingerprint
    return fingerprint


async def require_admin_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    settings = _settings_for(request)
    if credentials is None or not settings.admin_api_keys:
        raise _unauthorized()
    if not _matches_any(credentials.credentials, settings.admin_api_keys):
        raise _unauthorized()
    fingerprint = api_key_fingerprint(credentials.credentials)
    request.state.client_id = fingerprint
    return fingerprint


_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, hsts: bool) -> None:
        super().__init__(app)
        self._hsts = hsts

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        if not request.url.path.startswith(_DOCS_PATHS):
            headers.setdefault(
                "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
            )
            headers.setdefault("Cache-Control", "no-store")
        if self._hsts:
            headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        super().__init__(app)
        self._max = max_bytes

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        length = request.headers.get("content-length")
        if length is not None:
            try:
                too_big = int(length) > self._max
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
            if too_big:
                return JSONResponse({"detail": "Request body too large"}, status_code=413)
        elif request.method in {"POST", "PUT", "PATCH"}:
            body = await request.body()  # chunked: read (bounded by server) and check
            if len(body) > self._max:
                return JSONResponse({"detail": "Request body too large"}, status_code=413)
        return await call_next(request)


def client_ip(request: Request, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if settings.trust_forwarded_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"
