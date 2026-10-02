"""Periodic mailbox synchronization worker.

Run as its own process (``python -m app.workers.mail_sync_worker``) — any number of
replicas is safe: the per-account PostgreSQL advisory lock makes concurrent runs skip
instead of duplicating work, and unique constraints make re-processing idempotent.
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
from app.services.mail_sync import SyncMode

logger = logging.getLogger(__name__)


async def run_sync_loop(container: Container, stop: asyncio.Event) -> None:
    interval = container.settings.mail_sync_interval_seconds
    logger.info("mail_sync_worker_started", extra={"interval_s": interval})
    while not stop.is_set():
        try:
            summary = await container.sync_service.sync_all(SyncMode.PERIODIC)
            if summary.payments_imported or summary.accounts_failed:
                logger.info(
                    "mail_sync_cycle",
                    extra={
                        "imported": summary.payments_imported,
                        "failed": summary.accounts_failed,
                        "busy": summary.accounts_busy,
                    },
                )
            async with container.sessionmaker() as session, session.begin():
                await RateLimitRepository(session).purge_older_than(
                    datetime.now(UTC) - timedelta(minutes=10)
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never die on one bad cycle (DB restart, network blip...). Details are logged
            # through the redacting logger.
            logger.exception("mail_sync_cycle_error")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
    logger.info("mail_sync_worker_stopped")


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
