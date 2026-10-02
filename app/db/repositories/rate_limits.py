from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RateLimitCounter


class RateLimitRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def increment(self, key: str, window_start: datetime) -> int:
        stmt = (
            insert(RateLimitCounter)
            .values(key=key, window_start=window_start, count=1)
            .on_conflict_do_update(
                index_elements=[RateLimitCounter.key, RateLimitCounter.window_start],
                set_={"count": RateLimitCounter.count + 1},
            )
            .returning(RateLimitCounter.count)
        )
        return int((await self._s.execute(stmt)).scalar_one())

    async def purge_older_than(self, cutoff: datetime) -> int:
        result = await self._s.execute(
            delete(RateLimitCounter).where(RateLimitCounter.window_start < cutoff)
        )
        return int(getattr(result, "rowcount", 0) or 0)
