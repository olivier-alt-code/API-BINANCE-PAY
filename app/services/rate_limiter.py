"""Fixed-window rate limiter stored in PostgreSQL (shared by all replicas, no Redis)."""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories.rate_limits import RateLimitRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int


class RateLimiter:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        window_seconds: int = 60,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessionmaker = sessionmaker
        self._window = window_seconds
        self._clock = clock

    async def hit(self, key: str, limit: int) -> RateLimitDecision:
        now = self._clock()
        epoch = int(now.timestamp())
        start = datetime.fromtimestamp(epoch - epoch % self._window, tz=UTC)
        async with self._sessionmaker() as session, session.begin():
            repo = RateLimitRepository(session)
            count = await repo.increment(key[:128], start)
            if random.random() < 0.01:  # noqa: S311 - opportunistic cleanup, not security
                await repo.purge_older_than(now - timedelta(seconds=self._window * 10))
        reset = int((start + timedelta(seconds=self._window) - now).total_seconds())
        return RateLimitDecision(count <= limit, limit, max(limit - count, 0), max(reset, 1))
