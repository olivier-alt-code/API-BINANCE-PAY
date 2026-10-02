from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.encryption import CredentialCipher
from app.db.models import EmailMessage, MailAccount, MessageParseStatus, Payment
from app.services.mail_sync import SyncMode
from tests.conftest import ENCRYPTION_KEY, FakeMailbox, fixture_bytes
from tests.emails import make_email


async def _count(sm: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with sm() as s:
        return int(await s.scalar(select(func.count()).select_from(model)) or 0)


async def test_sync_imports_trusted_payment(
    build: Callable[..., Any], mailbox: FakeMailbox, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    c = build()
    mailbox.add(make_email(code="SYNC0001"))
    summary = await c.sync_service.sync_all(SyncMode.MANUAL)
    assert (summary.messages_scanned, summary.binance_messages, summary.payments_imported) == (
        1,
        1,
        1,
    )
    async with sessionmaker() as s:
        payment = await s.scalar(select(Payment))
        message = await s.scalar(select(EmailMessage))
        account = await s.scalar(select(MailAccount))
    assert payment is not None and payment.trusted and payment.payment_code == "SYNC0001"
    assert message is not None and message.parse_status == MessageParseStatus.PARSED
    assert message.raw_encrypted is None  # STORE_RAW_EMAILS=false by default
    assert account is not None and account.last_imap_uid == 1
    assert account.encrypted_credentials is None  # App Password is never persisted


async def test_incremental_sync_uses_cursor_and_lookback(
    build: Callable[..., Any], mailbox: FakeMailbox
) -> None:
    c = build()
    mailbox.add(make_email(code="CURSOR01"))
    await c.sync_service.sync_all(SyncMode.MANUAL)
    first_search = mailbox.last_search
    assert first_search["after_uid"] is None
    assert first_search["since"] is not None  # bootstrap is limited by MAIL_INITIAL_LOOKBACK
    assert first_search["filters"] == ["binance.example"]

    mailbox.add(make_email(code="CURSOR02"))
    fetches_before = mailbox.fetches
    summary = await c.sync_service.sync_all(SyncMode.MANUAL)
    assert mailbox.last_search["after_uid"] == 1
    assert mailbox.fetches - fetches_before == 1  # only the new message is downloaded
    assert summary.payments_imported == 1


async def test_reprocessing_same_email_creates_no_duplicates(
    build: Callable[..., Any],
    mailbox: FakeMailbox,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    c = build()
    raw = make_email(code="DUP00001")
    mailbox.add(raw)
    await c.sync_service.sync_all(SyncMode.MANUAL)
    # Same message delivered again (same Message-ID, new UID) + mailbox UIDVALIDITY reset.
    mailbox.add(raw)
    mailbox.uidvalidity = 2
    summary = await c.sync_service.sync_all(SyncMode.MANUAL)
    assert summary.duplicates >= 1
    assert await _count(sessionmaker, Payment) == 1


async def test_duplicate_payment_code_in_different_emails(
    build: Callable[..., Any],
    mailbox: FakeMailbox,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    c = build()
    mailbox.add(make_email(code="SAMECODE1", message_id="<a1@binance.example>"))
    mailbox.add(make_email(code="SAMECODE1", message_id="<a2@binance.example>"))
    summary = await c.sync_service.sync_all(SyncMode.MANUAL)
    assert summary.payments_imported == 1 and summary.duplicates == 1
    async with sessionmaker() as s:
        payments = list(await s.scalars(select(Payment)))
    assert len(payments) == 1 and not payments[0].ambiguous


async def test_contradicting_evidence_marks_payment_ambiguous(
    build: Callable[..., Any],
    mailbox: FakeMailbox,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    c = build()
    mailbox.add(make_email(code="CONFLICT1", amount="10", message_id="<c1@binance.example>"))
    mailbox.add(make_email(code="CONFLICT1", amount="99", message_id="<c2@binance.example>"))
    await c.sync_service.sync_all(SyncMode.MANUAL)
    async with sessionmaker() as s:
        payment = await s.scalar(select(Payment).where(Payment.trusted))
    assert payment is not None and payment.ambiguous


async def test_pending_then_completed_upgrades_status(
    build: Callable[..., Any],
    mailbox: FakeMailbox,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    c = build()
    mailbox.add(make_email(code="UPGRADE1", status="Pending", message_id="<u1@binance.example>"))
    mailbox.add(make_email(code="UPGRADE1", status="Completed", message_id="<u2@binance.example>"))
    await c.sync_service.sync_all(SyncMode.MANUAL)
    async with sessionmaker() as s:
        payment = await s.scalar(select(Payment))
    assert payment is not None and payment.payment_status == "PAID" and not payment.ambiguous


async def test_untrusted_and_foreign_emails(
    build: Callable[..., Any],
    mailbox: FakeMailbox,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    c = build()
    mailbox.add(fixture_bytes("spoofed_dkim_fail.eml"))
    # Same Message-ID as the fixture above would be deduplicated: use a distinct one.
    mailbox.add(
        make_email(
            sender="binance-support@lookalike-binance.test",
            message_id="<lookalike-1@lookalike-binance.test>",
        )
    )
    summary = await c.sync_service.sync_all(SyncMode.MANUAL)
    assert summary.binance_messages == 1  # the lookalike sender is not even a candidate
    assert summary.untrusted_messages == 1
    async with sessionmaker() as s:
        payments = list(await s.scalars(select(Payment)))
        statuses = sorted(m.parse_status for m in await s.scalars(select(EmailMessage)))
    assert len(payments) == 1 and payments[0].trusted is False
    assert statuses == [MessageParseStatus.NOT_BINANCE_SENDER, MessageParseStatus.PARSED]


async def test_raw_email_storage_is_opt_in_and_encrypted(
    build: Callable[..., Any],
    mailbox: FakeMailbox,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    c = build(store_raw_emails=True)
    raw = make_email(code="RAWSTORE1")
    mailbox.add(raw)
    await c.sync_service.sync_all(SyncMode.MANUAL)
    async with sessionmaker() as s:
        message = await s.scalar(select(EmailMessage))
    assert message is not None and message.raw_encrypted is not None
    assert "RAWSTORE1" not in message.raw_encrypted
    cipher = CredentialCipher(ENCRYPTION_KEY)
    assert cipher.decrypt(message.raw_encrypted, f"email:{message.mail_account_id}:1:1") == raw


async def test_concurrent_syncs_from_two_instances_do_not_duplicate(
    build: Callable[..., Any],
    mailbox: FakeMailbox,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Two independent containers simulate two replicas sharing PostgreSQL and the mailbox.
    a, b = build(), build()
    await a.sync_service.ensure_env_account()
    for i in range(20):
        mailbox.add(make_email(code=f"CONC{i:04d}"))
    mailbox.connect_delay = 0.05
    results = await asyncio.gather(
        a.sync_service.sync_all(SyncMode.MANUAL), b.sync_service.sync_all(SyncMode.MANUAL)
    )
    assert sum(r.payments_imported for r in results) == 20
    assert await _count(sessionmaker, Payment) == 20
    assert await _count(sessionmaker, EmailMessage) == 20


async def test_periodic_sync_skips_when_lock_is_held(
    build: Callable[..., Any], mailbox: FakeMailbox
) -> None:
    a = build()
    await a.sync_service.ensure_env_account()
    (account_id,) = await a.sync_service.enabled_account_ids()
    async with a.sync_service._account_lock(account_id, 0) as acquired:
        assert acquired
        summary = await build().sync_service.sync_account(account_id, SyncMode.PERIODIC)
    assert summary.accounts_busy == 1


async def test_imap_down_is_reported_not_raised(
    build: Callable[..., Any],
    mailbox: FakeMailbox,
    failing_errors: dict[str, Exception],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    c = build()
    mailbox.fail = failing_errors["down"]
    summary = await c.sync_service.sync_all(SyncMode.MANUAL)
    assert summary.accounts_failed == 1
    assert summary.errors == ["Mail provider unavailable"]
    async with sessionmaker() as s:
        account = await s.scalar(select(MailAccount))
    assert account is not None and account.last_sync_error == "Mail provider unavailable"


async def test_old_emails_outside_window_are_still_stored_but_dated(
    build: Callable[..., Any],
    mailbox: FakeMailbox,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    c = build()
    old = datetime.now(UTC).replace(microsecond=0) - timedelta(days=3)
    mailbox.add(make_email(code="OLDMAIL1", when=old))
    await c.sync_service.sync_all(SyncMode.MANUAL)
    async with sessionmaker() as s:
        payment = await s.scalar(select(Payment))
    assert payment is not None and payment.received_at == old


async def test_periodic_worker_loop_syncs_and_stops(
    build: Callable[..., Any],
    mailbox: FakeMailbox,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from app.workers.mail_sync_worker import run_sync_loop

    c = build(mail_sync_interval_seconds=5)
    mailbox.add(make_email(code="WORKER001"))
    stop = asyncio.Event()
    task = asyncio.create_task(run_sync_loop(c, stop))
    for _ in range(50):
        if await _count(sessionmaker, Payment):
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert await _count(sessionmaker, Payment) == 1


async def test_worker_survives_provider_failures(
    build: Callable[..., Any], mailbox: FakeMailbox, failing_errors: dict[str, Exception]
) -> None:
    from app.workers.mail_sync_worker import run_sync_loop

    c = build(mail_sync_interval_seconds=5)
    mailbox.fail = failing_errors["timeout"]
    stop = asyncio.Event()
    task = asyncio.create_task(run_sync_loop(c, stop))
    await asyncio.sleep(0.3)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=5)


async def test_transaction_pooler_mode_lock_and_no_duplicates(
    build: Callable[..., Any],
    mailbox: FakeMailbox,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Supabase Supavisor transaction mode: transaction-scoped advisory locks."""
    a = build(database_pooler_mode="transaction")
    b = build(database_pooler_mode="transaction")
    await a.sync_service.ensure_env_account()
    (account_id,) = await a.sync_service.enabled_account_ids()

    async with a.sync_service._account_lock(account_id, 0) as acquired:
        assert acquired
        busy = await b.sync_service.sync_account(account_id, SyncMode.PERIODIC)
    assert busy.accounts_busy == 1  # lock really held across connections
    async with b.sync_service._account_lock(account_id, 0) as acquired:
        assert acquired  # and released afterwards

    for i in range(10):
        mailbox.add(make_email(code=f"TXPOOL{i:03d}"))
    mailbox.connect_delay = 0.05
    await asyncio.gather(
        a.sync_service.sync_all(SyncMode.MANUAL), b.sync_service.sync_all(SyncMode.MANUAL)
    )
    assert await _count(sessionmaker, Payment) == 10
