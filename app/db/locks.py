"""Cross-replica locks backed by PostgreSQL advisory locks (no Redis, no process memory)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# Advisory lock namespaces (first int of the two-int form).
BINANCE_API_SYNC_LOCK_NAMESPACE = 727_002


@asynccontextmanager
async def advisory_lock(
    engine: AsyncEngine,
    namespace: int,
    key: int,
    *,
    wait_seconds: float,
    transaction_pooler: bool,
) -> AsyncIterator[bool]:
    """Try to take a lock on a dedicated connection; yields whether it was acquired.

    * Direct connection / session pooler: session-level advisory lock (released on unlock,
      or automatically if the connection drops).
    * Supabase transaction pooler (Supavisor, port 6543): a backend connection is only
      pinned for the duration of a transaction, so a *transaction-scoped* advisory lock is
      taken inside a transaction kept open while the lock is held.
    """
    fn = "pg_try_advisory_xact_lock" if transaction_pooler else "pg_try_advisory_lock"
    params = {"ns": namespace, "id": key}
    async with engine.connect() as conn:
        deadline = time.monotonic() + wait_seconds
        acquired = False
        while True:
            acquired = bool(
                (
                    await conn.execute(text(f"SELECT {fn}(:ns, CAST(:id AS integer))"), params)
                ).scalar()
            )
            if not (transaction_pooler and acquired):
                await conn.commit()  # keep the transaction open only to hold an xact lock
            if acquired or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.2)
        try:
            yield acquired
        finally:
            if acquired and transaction_pooler:
                await conn.rollback()  # ends the transaction -> releases the xact lock
            elif acquired:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:ns, CAST(:id AS integer))"), params
                )
                await conn.commit()
