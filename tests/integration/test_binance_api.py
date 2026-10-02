"""End-to-end verification using the Binance Pay trade history API as evidence source.

Transaction ids follow the real format observed in the API (``P_`` + 16 chars); values
are synthetic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Payment, PaymentClaim, PaymentSource
from app.integrations.binance.account_api import BinanceApiError, BinanceAuthError
from app.main import create_app
from app.services.mail_sync import SyncMode
from app.services.payment_verifier import VerificationRequest
from app.services.payment_verifier import VerificationStatus as S
from tests.conftest import ADMIN_KEY, API_KEY, FakeMailbox, FakePayHistoryClient, make_settings

TX = "P_A99TESTPAYX71116"
API = {"payment_evidence_source": "binance_api", "binance_api_min_interval_seconds": 1}


def req(
    code: str = TX,
    amount: str = "98.814",
    asset: str = "USDT",
    order: str | None = "SUBSCRIPTION-9321",
    max_age: int = 60,
) -> VerificationRequest:
    return VerificationRequest(
        payment_code=code,
        expected_amount=Decimal(amount),
        asset=asset,
        order_reference=order,
        max_age_minutes=max_age,
    )


@pytest.fixture
def api_build(build: Callable[..., Any], pay_api: FakePayHistoryClient) -> Callable[..., Any]:
    def _build(**overrides: Any) -> Any:
        return build(pay_history_client=pay_api, **{**API, **overrides})

    return _build


async def _count(sm: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with sm() as s:
        return int(await s.scalar(select(func.count()).select_from(model)) or 0)


async def test_full_flow_verified_idempotent_and_replay_blocked(
    api_build: Callable[..., Any],
    pay_api: FakePayHistoryClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    c = api_build()
    pay_api.add(TX, "98.814")
    first = await c.verifier.verify(req())
    assert first.status is S.VERIFIED and first.received_amount == Decimal("98.814")
    again = await c.verifier.verify(req())
    assert again.status is S.VERIFIED and again.idempotent
    replay = await c.verifier.verify(req(order="SUBSCRIPTION-9999"))
    assert replay.status is S.ALREADY_CLAIMED

    async with sessionmaker() as s:
        payment = await s.scalar(select(Payment))
    assert payment is not None
    assert payment.source == PaymentSource.BINANCE_PAY_HISTORY
    assert payment.payment_code == TX and payment.trusted
    assert payment.payer_name == "User-0000aaaa"
    assert payment.email_message_id is None


async def test_amount_asset_and_age_checks(
    api_build: Callable[..., Any], pay_api: FakePayHistoryClient
) -> None:
    c = api_build()
    pay_api.add(TX, "98.814")
    pay_api.add("P_A99TESTPAYY71115", "99.79136539", when=datetime.now(UTC) - timedelta(hours=3))
    assert (await c.verifier.verify(req(amount="98.81"))).status is S.AMOUNT_MISMATCH
    assert (await c.verifier.verify(req(amount="98.8140"))).status is S.VERIFIED  # same Decimal
    assert (await c.verifier.verify(req(asset="USDC", order="O-2"))).status is S.ASSET_MISMATCH
    old = req(code="P_A99TESTPAYY71115", amount="99.79136539", order="O-3")
    assert (await c.verifier.verify(old)).status is S.EXPIRED_PAYMENT


@pytest.mark.parametrize(
    "code", ["P_A99TESTPAYX7111", "P_A99TESTPAYX711166", "A99TESTPAYX71116", "P_A99TESTPAYX71117"]
)
async def test_codes_must_match_exactly(
    api_build: Callable[..., Any], pay_api: FakePayHistoryClient, code: str
) -> None:
    pay_api.add(TX, "98.814")
    assert (await api_build().verifier.verify(req(code=code))).status is S.NOT_FOUND


async def test_lowercase_code_only_with_case_insensitive_setting(
    api_build: Callable[..., Any], pay_api: FakePayHistoryClient
) -> None:
    pay_api.add(TX, "98.814")
    assert (await api_build().verifier.verify(req(code=TX.lower()))).status is S.NOT_FOUND
    ci = api_build(payment_code_case_insensitive=True)
    assert (await ci.verifier.verify(req(code=TX.lower(), order="O-CI"))).verified


async def test_outgoing_and_other_order_types_are_never_evidence(
    api_build: Callable[..., Any], pay_api: FakePayHistoryClient
) -> None:
    c = api_build()
    pay_api.add("P_A99TESTOUTX71112", "-100", payer=None)
    pay_api.add("P_A99TESTBOXX71112", "5", order_type="CRYPTO_BOX")
    assert (await c.verifier.verify(req(code="P_A99TESTOUTX71112", amount="100"))).status is (
        S.NOT_FOUND
    )
    assert (await c.verifier.verify(req(code="P_A99TESTBOXX71112", amount="5"))).status is (
        S.NOT_FOUND
    )


async def test_already_stored_payment_does_not_call_binance(
    api_build: Callable[..., Any], pay_api: FakePayHistoryClient
) -> None:
    c = api_build()
    pay_api.add(TX, "98.814")
    await c.api_sync_service.sync(SyncMode.PERIODIC)
    calls = len(pay_api.calls)
    assert (await c.verifier.verify(req())).verified
    assert len(pay_api.calls) == calls


async def test_min_interval_protects_api_weight(
    api_build: Callable[..., Any], pay_api: FakePayHistoryClient
) -> None:
    c = api_build(binance_api_min_interval_seconds=60)
    for i in range(5):  # unknown codes: each would trigger an on-demand sync
        result = await c.verifier.verify(req(code=f"P_UNKNOWN{i:07d}"))
        assert result.status is S.NOT_FOUND and result.retryable
    assert len(pay_api.calls) == 1


async def test_incremental_cursor_with_overlap_and_no_duplicates(
    api_build: Callable[..., Any],
    pay_api: FakePayHistoryClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    c = api_build(binance_api_initial_lookback_hours=48, binance_api_overlap_seconds=300)
    pay_api.add(TX, "98.814")
    first = await c.api_sync_service.sync(SyncMode.MANUAL)
    assert first.payments_imported == 1
    start, end = pay_api.calls[0]
    assert end - start == timedelta(hours=48)

    await asyncio.sleep(1.1)
    second = await c.api_sync_service.sync(SyncMode.MANUAL)
    start2, _ = pay_api.calls[1]
    assert start2 == end - timedelta(seconds=300)  # cursor minus overlap
    assert second.payments_imported == 0 and second.duplicates == 1
    assert await _count(sessionmaker, Payment) == 1


async def test_concurrent_requests_from_replicas_only_one_wins(
    api_build: Callable[..., Any],
    pay_api: FakePayHistoryClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    pay_api.add(TX, "98.814")
    pay_api.delay = 0.05
    replicas = [api_build() for _ in range(8)]
    results = await asyncio.gather(
        *(r.verifier.verify(req(order=f"ORDER-{i}")) for i, r in enumerate(replicas))
    )
    statuses = [r.status for r in results]
    assert statuses.count(S.VERIFIED) == 1, statuses
    assert set(statuses) <= {S.VERIFIED, S.ALREADY_CLAIMED, S.NOT_FOUND, S.PENDING_SYNC}
    assert await _count(sessionmaker, Payment) == 1
    assert await _count(sessionmaker, PaymentClaim) == 1


@pytest.mark.parametrize(
    "error", [BinanceApiError("down"), BinanceAuthError("bad key", retryable=False)]
)
async def test_binance_unavailable(
    api_build: Callable[..., Any], pay_api: FakePayHistoryClient, error: Exception
) -> None:
    pay_api.fail = error
    result = await api_build().verifier.verify(req())
    assert result.status is S.BINANCE_API_UNAVAILABLE and result.retryable


async def test_api_key_not_configured(build: Callable[..., Any]) -> None:
    c = build(**API)  # no client and no BINANCE_API_KEY
    assert (await c.verifier.verify(req())).status is S.BINANCE_API_UNAVAILABLE


async def test_pending_sync_when_other_replica_is_syncing(
    api_build: Callable[..., Any], pay_api: FakePayHistoryClient
) -> None:
    from app.db.locks import BINANCE_API_SYNC_LOCK_NAMESPACE, advisory_lock

    holder = api_build()
    c = api_build(mail_sync_lock_wait_seconds=0.3)
    async with advisory_lock(
        holder.engine, BINANCE_API_SYNC_LOCK_NAMESPACE, 1, wait_seconds=0, transaction_pooler=False
    ):
        result = await c.verifier.verify(req())
    assert result.status is S.PENDING_SYNC


async def test_periodic_worker_imports_from_api(
    api_build: Callable[..., Any],
    pay_api: FakePayHistoryClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from app.workers.mail_sync_worker import run_sync_loop

    pay_api.add(TX, "98.814")
    stop = asyncio.Event()
    task = asyncio.create_task(run_sync_loop(api_build(binance_api_sync_interval_seconds=5), stop))
    for _ in range(50):
        if await _count(sessionmaker, Payment):
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert await _count(sessionmaker, Payment) == 1


# --- HTTP ---------------------------------------------------------------------------------


async def test_http_verify_admin_sync_and_ready(
    api_build: Callable[..., Any], pay_api: FakePayHistoryClient, mailbox: FakeMailbox
) -> None:
    container = api_build()
    app = create_app(make_settings(**API), container=container)
    pay_api.add(TX, "98.814")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        sync = await client.post(
            "/v1/admin/binance/sync", headers={"Authorization": f"Bearer {ADMIN_KEY}"}
        )
        assert sync.json()["paymentsImported"] == 1
        r = await client.post(
            "/v1/payments/verify",
            json={"paymentCode": TX, "expectedAmount": "98.814", "orderReference": "SUB-1"},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert r.json()["status"] == "VERIFIED" and r.json()["receivedAmount"] == "98.814"
        test = await client.post(
            "/v1/admin/binance/test", headers={"Authorization": f"Bearer {ADMIN_KEY}"}
        )
        assert test.json() == {
            "connected": False,
            "source": "binance_pay_history",
            "error": "Binance API key not configured",
        }
        ready = await client.get("/ready")
    assert ready.json()["checks"]["binance_api_configured"] is False  # fake client, no key
    assert "binance_senders_configured" not in ready.json()["checks"]
    assert mailbox.connections == 0  # Gmail is not used with the API source
