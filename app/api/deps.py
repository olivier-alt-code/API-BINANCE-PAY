from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials

from app.container import Container
from app.core.security import bearer_scheme, client_ip, unauthorized
from app.services.tenants import AuthContext


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


async def require_client(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthContext:
    """Authenticate a client by its private token (``Authorization: Bearer bpv_…``)."""
    if credentials is None:
        raise unauthorized()
    context = await get_container(request).tenants.authenticate(credentials.credentials)
    if context is None:
        raise unauthorized()
    return context


async def client_rate_key(context: AuthContext = Depends(require_client)) -> str:
    return f"token:{context.token_id}"


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
