from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Request, Response, status

from app.container import Container
from app.core.security import client_ip


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


def rate_limit(
    scope: str,
    limit_attr: str,
    auth: Callable[..., Awaitable[str]] | None = None,
) -> Callable[..., Awaitable[None]]:
    """Dependency enforcing a fixed-window rate limit per API key (or per IP without auth).

    When ``auth`` is given it runs first (FastAPI caches it per request), so unauthenticated
    calls are rejected before consuming the caller's quota.
    """

    async def _no_auth() -> str:
        return ""

    async def _dependency(
        request: Request, response: Response, client_id: str = Depends(auth or _no_auth)
    ) -> None:
        container = get_container(request)
        limit = int(getattr(container.settings, limit_attr))
        client = f"key:{client_id}" if client_id else f"ip:{client_ip(request)}"
        decision = await container.rate_limiter.hit(f"{scope}:{client}", limit)
        headers = {
            "X-RateLimit-Limit": str(decision.limit),
            "X-RateLimit-Remaining": str(decision.remaining),
            "X-RateLimit-Reset": str(decision.reset_seconds),
        }
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
                headers={**headers, "Retry-After": str(decision.reset_seconds)},
            )
        response.headers.update(headers)

    return _dependency
