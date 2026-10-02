"""Periodic synchronization worker: imports every client's Binance Pay transfers.

Run as its own process (``python -m app.workers.sync_worker``) — any number of replicas is
safe: a per-client PostgreSQL advisory lock makes concurrent runs skip instead of
duplicating work, and unique constraints make re-importing idempotent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime, timedelta

from app.config import get_settings
from app.container import Container, build_container
from app.core.logging import configure_logging
from app.db.repositories.rate_limits import RateLimitRepository
from app.db.session import get_engine, get_sessionmaker
from app.services.binance_api_sync import SyncMode

logger = logging.getLogger(__name__)


async def _sync_cycle(container: Container) -> None:
    results = await container.api_sync_service.sync_all(SyncMode.PERIODIC)
    for tenant_id, summary in results.items():
        if summary.payments_imported or summary.failed:
            logger.info(
                "binance_api_sync_cycle",
                extra={
                    "tenant_id": tenant_id,
                    "imported": summary.payments_imported,
                    "failed": summary.failed,
                },
            )


async def run_sync_loop(container: Container, stop: asyncio.Event) -> None:
    settings = container.settings
    interval = settings.binance_api_sync_interval_seconds
    logger.info("sync_worker_started", extra={"interval_s": interval})
    while not stop.is_set():
        try:
            await _sync_cycle(container)
            async with container.sessionmaker() as session, session.begin():
                await RateLimitRepository(session).purge_older_than(
                    datetime.now(UTC) - timedelta(minutes=10)
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never die on one bad cycle (DB restart, network blip...). Details are logged
            # through the redacting logger.
            logger.exception("sync_cycle_error")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
    logger.info("sync_worker_stopped")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json, settings.secret_values())
    container = build_container(settings, get_engine(), get_sessionmaker())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await run_sync_loop(container, stop)
    finally:
        await container.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
