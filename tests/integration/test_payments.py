"""Payment verification per client, backed by each client's Binance Pay trade history.

Transaction ids follow the real format (``P_`` + 16 uppercase chars); values are synthetic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Payment, PaymentClaim, PaymentSource
from app.integrations.binance.account_api import BinanceApiError, BinanceAuthError
from app.services.binance_api_sync import SyncMode
from app.services.payment_verifier import VerificationRequest
from app.services.payment_verifier import VerificationStatus as S
from tests.conftest import OTHER_KEY, OTHER_SECRET, ClientHandle, FakeBinance

TX = "P_A99TESTPAYX71116"


def req(  # noqa: PLR0917 - test helper
    client: ClientHandle,
    code: str = TX,
    amount: str = "98.814",
    asset: str = "USDT",
    order: str | None = "SUBSCRIPTION-9321",
    max_age: int = 60,
) -> VerificationRequest:
    return VerificationRequest(
        tenant_id=client.tenant_id,
        payment_code=code,
        expected_amount=Decimal(amount),
        asset=asset,
        order_reference=order,
        max_age_minutes=max_age,
    )


async def _count(sm: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with sm() as s:
        return int(await s.scalar(select(func.count()).select_from(model)) or 0)


async def test_full_flow_verified_idempotent_and_replay_blocked(
    build: Callable[..., Any],
    new_client: Callable[..., Any],
    binance: FakeBinance,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    me = await new_client()
    c = build()
    binance.main.add(TX, "98.814")
    first = await c.verifier.verify(req(me))
    assert first.status is S.VERIFIED and first.received_amount == Decimal("98.814")
    again = await c.verifier.verify(req(me))
    assert again.status is S.VERIFIED and again.idempotent
    replay = await c.verifier.verify(req(me, order="SUBSCRIPTION-9999"))
    assert replay.status is S.ALREADY_CLAIMED

    async with sessionmaker() as s:
        payment = await s.scalar(select(Payment))
    assert payment is not None
    assert payment.tenant_id == me.tenant_id
    assert payment.source == PaymentSource.BINANCE_PAY_HISTORY
    assert payment.payment_code == TX and payment.trusted
    assert payment.payer_name == "User-0000aaaa"


async def test_clients_are_isolated(
    build: Callable[..., Any], new_client: Callable[..., Any], binance: FakeBinance
) -> None:
    """A payment received by client A can never be verified or claimed by client B."""
    alice = await new_client("alice")
    bob = await new_client("bob", api_key=OTHER_KEY, api_secret=OTHER_SECRET)
    c = build()
    binance.main.add(TX, "98.814")  # Alice's Binance account
    binance.other.add("P_A99TESTBOBX71116", "5")  # Bob's account
    assert (await c.verifier.verify(req(bob))).status is S.NOT_FOUND
    assert (await c.verifier.verify(req(alice))).status is S.VERIFIED
    assert (await c.verifier.verify(req(alice, code="P_A99TESTBOBX71116", amount="5"))).status is (
        S.NOT_FOUND
    )
    assert (await c.verifier.verify(req(bob, code="P_A99TESTBOBX71116", amount="5"))).verified


async def test_same_transaction_id_on_two_accounts_does_not_collide(
    build: Callable[..., Any],
    new_client: Callable[..., Any],
    binance: FakeBinance,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    alice = await new_client("alice")
    bob = await new_client("bob", api_key=OTHER_KEY, api_secret=OTHER_SECRET)
    c = build()
    binance.main.add(TX, "10")
    binance.other.add(TX, "20")
    assert (await c.verifier.verify(req(alice, amount="10"))).verified
    assert (await c.verifier.verify(req(bob, amount="20"))).verified
    assert await _count(sessionmaker, Payment) == 2


async def test_amount_asset_and_age_checks(
    build: Callable[..., Any], new_client: Callable[..., Any], binance: FakeBinance
) -> None:
    me = await new_client()
    c = build()
    binance.main.add(TX, "98.814")
    binance.main.add(
        "P_A99TESTPAYY71115", "99.79136539", when=datetime.now(UTC) - timedelta(hours=3)
    )
    assert (await c.verifier.verify(req(me, amount="98.81"))).status is S.AMOUNT_MISMATCH
    assert (await c.verifier.verify(req(me, asset="USDC"))).status is S.ASSET_MISMATCH
    assert (await c.verifier.verify(req(me, amount="98.8140"))).verified  # same Decimal
    old = req(me, code="P_A99TESTPAYY71115", amount="99.79136539", order="O-3")
    assert (await c.verifier.verify(old)).status is S.EXPIRED_PAYMENT
    assert (
        await c.verifier.verify(
            req(me, code="P_A99TESTPAYY71115", amount="99.79136539", order="O-3", max_age=240)
        )
    ).verified


async def test_explicit_tolerance_is_opt_in(
    build: Callable[..., Any], new_client: Callable[..., Any], binance: FakeBinance
) -> None:
    me = await new_client()
    binance.main.add(TX, "25.50")
    assert (await build().verifier.verify(req(me, amount="25.49"))).status is S.AMOUNT_MISMATCH
    tolerant = build(payment_amount_tolerance="0.01")
    assert (await tolerant.verifier.verify(req(me, amount="25.49"))).verified


async def test_future_dated_payment_rejected(
    build: Callable[..., Any], new_client: Callable[..., Any], binance: FakeBinance
) -> None:
    from app.db.repositories.payments import NewPayment, PaymentRepository

    me = await new_client()
    c = build()

    async with c.sessionmaker() as s, s.begin():
        await PaymentRepository(s).store(
            NewPayment(
                tenant_id=me.tenant_id,
                source=PaymentSource.BINANCE_PAY_HISTORY,
                external_id="P_A99FUTUREXX71116",
                payment_code="P_A99FUTUREXX71116",
                amount=Decimal(1),
                asset="USDT",
                payment_status="PAID",
                received_at=datetime.now(UTC) + timedelta(hours=2),
                trusted=True,
            )
        )
    result = await c.verifier.verify(req(me, code="P_A99FUTUREXX71116", amount="1"))
    assert result.status is S.EXPIRED_PAYMENT


@pytest.mark.parametrize(
    "code", ["P_A99TESTPAYX7111", "P_A99TESTPAYX711166", "A99TESTPAYX71116", "P_A99TESTPAYX71117"]
)
async def test_codes_must_match_exactly(
    build: Callable[..., Any], new_client: Callable[..., Any], binance: FakeBinance, code: str
) -> None:
    me = await new_client()
    binance.main.add(TX, "98.814")
    assert (await build().verifier.verify(req(me, code=code))).status is S.NOT_FOUND


async def test_lowercase_code_with_case_insensitive_setting(
    build: Callable[..., Any], new_client: Callable[..., Any], binance: FakeBinance
) -> None:
    me = await new_client()
    binance.main.add(TX, "98.814")
    assert (await build().verifier.verify(req(me, code=TX.lower()))).status is S.NOT_FOUND
    ci = build(payment_code_case_insensitive=True)
    await asyncio.sleep(1.1)  # past the min interval so the new container re-imports
    assert (await ci.verifier.verify(req(me, code=TX.lower(), order="O-CI"))).verified


@pytest.mark.parametrize("bad", ["AB", "has space", "x" * 65, "ABC$123", "-ABC123"])
async def test_invalid_payment_code(
    build: Callable[..., Any], new_client: Callable[..., Any], binance: FakeBinance, bad: str
) -> None:
    me = await new_client()
    assert (await build().verifier.verify(req(me, code=bad))).status is S.INVALID_PAYMENT_CODE
    assert binance.main.calls == []  # never hits Binance


async def test_outgoing_and_other_order_types_are_never_evidence(
    build: Callable[..., Any], new_client: Callable[..., Any], binance: FakeBinance
) -> None:
    me = await new_client()
    c = build()
    binance.main.add("P_A99TESTOUTX71112", "-100", payer=None)
    binance.main.add("P_A99TESTBOXX71112", "5", order_type="CRYPTO_BOX")
    assert (await c.verifier.verify(req(me, code="P_A99TESTOUTX71112", amount="100"))).status is (
        S.NOT_FOUND
    )
    assert (await c.verifier.verify(req(me, code="P_A99TESTBOXX71112", amount="5"))).status is (
        S.NOT_FOUND
    )


async def test_already_stored_payment_does_not_call_binance(
    build: Callable[..., Any], new_client: Callable[..., Any], binance: FakeBinance
) -> None:
    me = await new_client()
    c = build()
    binance.main.add(TX, "98.814")
    await c.api_sync_service.sync(me.tenant_id, SyncMode.PERIODIC)
    calls = len(binance.main.calls)
    assert (await c.verifier.verify(req(me))).verified
    assert len(binance.main.calls) == calls


async def test_min_interval_protects_api_weight(
    build: Callable[..., Any], new_client: Callable[..., Any], binance: FakeBinance
) -> None:
    me = await new_client()
    c = build(binance_api_min_interval_seconds=60)
    for i in range(5):  # unknown codes: each would trigger an on-demand query
        result = await c.verifier.verify(req(me, code=f"P_UNKNOWN{i:07d}"))
        assert result.status is S.NOT_FOUND and result.retryable
    assert len(binance.main.calls) == 1


async def test_incremental_cursor_with_overlap_and_no_duplicates(
    build: Callable[..., Any],
    new_client: Callable[..., Any],
    binance: FakeBinance,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    me = await new_client()
    c = build(binance_api_initial_lookback_hours=48, binance_api_overlap_seconds=300)
    binance.main.add(TX, "98.814")
    first = await c.api_sync_service.sync(me.tenant_id, SyncMode.MANUAL)
    assert first.payments_imported == 1
    start, end = binance.main.calls[0]
    assert end - start == timedelta(hours=48)

    await asyncio.sleep(1.1)
    second = await c.api_sync_service.sync(me.tenant_id, SyncMode.MANUAL)
    start2, _ = binance.main.calls[1]
    assert start2 == end - timedelta(seconds=300)  # cursor minus overlap
    assert second.payments_imported == 0 and second.duplicates == 1
    assert await _count(sessionmaker, Payment) == 1


async def test_concurrent_requests_from_replicas_only_one_wins(
    build: Callable[..., Any],
    new_client: Callable[..., Any],
    binance: FakeBinance,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    me = await new_client()
    binance.main.add(TX, "98.814")
    binance.main.delay = 0.05
    replicas = [build() for _ in range(8)]
    results = await asyncio.gather(
        *(r.verifier.verify(req(me, order=f"ORDER-{i}")) for i, r in enumerate(replicas))
    )
    statuses = [r.status for r in results]
    assert statuses.count(S.VERIFIED) == 1, statuses
    assert set(statuses) <= {S.VERIFIED, S.ALREADY_CLAIMED, S.NOT_FOUND, S.PENDING_SYNC}
    assert await _count(sessionmaker, Payment) == 1
    assert await _count(sessionmaker, PaymentClaim) == 1


async def test_claim_service_race_a_verified_b_already_claimed(
    build: Callable[..., Any],
    new_client: Callable[..., Any],
    binance: FakeBinance,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    me = await new_client()
    c = build()
    binance.main.add(TX, "25")
    await c.api_sync_service.sync(me.tenant_id, SyncMode.MANUAL)
    async with sessionmaker() as s:
        payment_id = await s.scalar(select(Payment.id))
    for _round in range(5):
        async with sessionmaker() as s, s.begin():
            await s.execute(PaymentClaim.__table__.delete())
        results = await asyncio.gather(
            *(
                c.claim_service.claim(
                    payment_id=payment_id,
                    order_reference=order,
                    expected_amount=Decimal(25),
                    asset="USDT",
                    client_id=None,
                )
                for order in ("ORDER-A", "ORDER-B")
            )
        )
        assert sorted(r.status.value for r in results) == ["ALREADY_CLAIMED", "CLAIMED"]


async def test_unique_constraint_prevents_double_claim(
    build: Callable[..., Any],
    new_client: Callable[..., Any],
    binance: FakeBinance,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    me = await new_client()
    binance.main.add(TX, "25")
    assert (await build().verifier.verify(req(me, amount="25"))).verified
    async with sessionmaker() as s:
        payment_id = await s.scalar(select(Payment.id))
    with pytest.raises(IntegrityError):
        async with sessionmaker() as s, s.begin():
            s.add(
                PaymentClaim(
                    payment_id=payment_id,
                    order_reference="BYPASS",
                    expected_amount=Decimal(25),
                    asset="USDT",
                )
            )


async def test_claim_without_order_reference_cannot_be_reused(
    build: Callable[..., Any], new_client: Callable[..., Any], binance: FakeBinance
) -> None:
    me = await new_client()
    binance.main.add(TX, "25")
    c = build()
    assert (await c.verifier.verify(req(me, amount="25", order=None))).verified
    assert (await c.verifier.verify(req(me, amount="25", order=None))).status is (S.ALREADY_CLAIMED)


async def test_idempotent_even_after_max_age(
    build: Callable[..., Any],
    new_client: Callable[..., Any],
    binance: FakeBinance,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    me = await new_client()
    binance.main.add(TX, "25")
    c = build()
    assert (await c.verifier.verify(req(me, amount="25"))).verified
    async with sessionmaker() as s, s.begin():
        payment = await s.scalar(select(Payment))
        assert payment is not None
        payment.received_at = datetime.now(UTC) - timedelta(days=2)
    again = await c.verifier.verify(req(me, amount="25"))
    assert again.verified and again.idempotent


@pytest.mark.parametrize(
    "error", [BinanceApiError("down"), BinanceAuthError("key revoked", retryable=False)]
)
async def test_binance_unavailable(
    build: Callable[..., Any],
    new_client: Callable[..., Any],
    binance: FakeBinance,
    error: Exception,
) -> None:
    me = await new_client()
    binance.main.fail = error
    result = await build().verifier.verify(req(me))
    assert result.status is S.BINANCE_API_UNAVAILABLE and result.retryable


async def test_client_without_binance_key(
    build: Callable[..., Any], new_client: Callable[..., Any]
) -> None:
    me = await new_client(api_key=None)
    result = await build().verifier.verify(req(me))
    assert result.status is S.BINANCE_NOT_CONFIGURED and not result.retryable


async def test_pending_sync_when_other_replica_is_querying(
    build: Callable[..., Any], new_client: Callable[..., Any]
) -> None:
    from app.db.locks import BINANCE_API_SYNC_LOCK_NAMESPACE, advisory_lock

    me = await new_client()
    holder = build()
    c = build(sync_lock_wait_seconds=0.3)
    async with advisory_lock(
        holder.engine,
        BINANCE_API_SYNC_LOCK_NAMESPACE,
        me.tenant_id,
        wait_seconds=0,
        transaction_pooler=False,
    ):
        result = await c.verifier.verify(req(me))
    assert result.status is S.PENDING_SYNC


async def test_transaction_pooler_mode_lock(
    build: Callable[..., Any], new_client: Callable[..., Any], binance: FakeBinance
) -> None:
    """Supabase Supavisor transaction mode: transaction-scoped advisory locks."""
    me = await new_client()
    a = build(database_pooler_mode="transaction")
    b = build(database_pooler_mode="transaction")
    from app.db.locks import BINANCE_API_SYNC_LOCK_NAMESPACE, advisory_lock

    async with advisory_lock(
        a.engine,
        BINANCE_API_SYNC_LOCK_NAMESPACE,
        me.tenant_id,
        wait_seconds=0,
        transaction_pooler=True,
    ) as acquired:
        assert acquired
        busy = await b.api_sync_service.sync(me.tenant_id, SyncMode.PERIODIC)
    assert busy.busy
    binance.main.add(TX, "1")
    assert (await b.api_sync_service.sync(me.tenant_id, SyncMode.PERIODIC)).payments_imported == 1


async def test_periodic_worker_imports_for_every_client(
    build: Callable[..., Any],
    new_client: Callable[..., Any],
    binance: FakeBinance,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from app.workers.sync_worker import run_sync_loop

    await new_client("alice")
    await new_client("bob", api_key=OTHER_KEY, api_secret=OTHER_SECRET)
    await new_client("carol", api_key=None)  # no key: skipped
    binance.main.add(TX, "98.814")
    binance.other.add("P_A99TESTBOBX71116", "5")
    stop = asyncio.Event()
    task = asyncio.create_task(run_sync_loop(build(binance_api_sync_interval_seconds=5), stop))
    for _ in range(60):
        if await _count(sessionmaker, Payment) == 2:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert await _count(sessionmaker, Payment) == 2


async def test_worker_survives_binance_failures(
    build: Callable[..., Any], new_client: Callable[..., Any], binance: FakeBinance
) -> None:
    from app.workers.sync_worker import run_sync_loop

    await new_client()
    binance.main.fail = BinanceApiError("down")
    stop = asyncio.Event()
    task = asyncio.create_task(run_sync_loop(build(binance_api_sync_interval_seconds=5), stop))
    await asyncio.sleep(0.3)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=5)
