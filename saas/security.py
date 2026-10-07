"""CSRF tokens, per-IP rate limiting and security headers."""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

CSRF_KEY = "csrf"


def csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_KEY)
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        request.session[CSRF_KEY] = token
    return token


def check_csrf(request: Request, submitted: str) -> None:
    expected = request.session.get(CSRF_KEY)
    if not isinstance(expected, str) or not secrets.compare_digest(expected, submitted or ""):
        raise HTTPException(status_code=403, detail="Formulario caducado: recarga la página")


class RateLimiter:
    """Sliding window per (bucket, client IP), in memory.

    Enough for a single instance (one Azure Web App). With several instances each one
    counts on its own, which only makes the limit looser.
    """

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, bucket: str, client: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        hits = self._hits[(bucket, client)]
        while hits and hits[0] <= now - window_seconds:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        if len(self._hits) > 50_000:  # crude bound on memory
            self._hits.clear()
        return True


def client_ip(request: Request) -> str:
    # uvicorn --proxy-headers (FORWARDED_ALLOW_IPS) already resolved X-Forwarded-For.
    return request.client.host if request.client else "unknown"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' https: data:; style-src 'self'; "
            "script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith(("/pago", "/cuenta", "/entrar")):
            # Pages that may show a token or the secret payment URL: never cache them.
            response.headers["Cache-Control"] = "no-store"
        return response
