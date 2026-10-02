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

from app.db.models import Payment, PaymentClaim
from app.services.payment_verifier import VerificationRequest
from app.services.payment_verifier import VerificationStatus as S
from tests.conftest import FakeMailbox
from tests.emails import make_email


def req(
    code: str = "PAY-82919381",
    amount: str = "25",
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
def container(build: Callable[..., Any]) -> Any:
    return build()


async def test_end_to_end_flow_from_spec(container: Any, mailbox: FakeMailbox) -> None:
    # Payment arrives in Gmail; nothing synced yet -> on-demand sync finds it.
    mailbox.add(make_email(code="PAY-82919381", amount="25", asset="USDT"))
    first = await container.verifier.verify(req())
    assert first.status is S.VERIFIED and first.verified
    assert first.received_amount == Decimal(25) and not first.idempotent

    again = await container.verifier.verify(req())
    assert again.status is S.VERIFIED and again.idempotent

    other = await container.verifier.verify(req(order="SUBSCRIPTION-9999"))
    assert other.status is S.ALREADY_CLAIMED and not other.verified


async def test_correct_code_amount_with_different_decimal_scale(
    container: Any, mailbox: FakeMailbox
) -> None:
    mailbox.add(make_email(code="SCALE0001", amount="25.5"))
    result = await container.verifier.verify(req(code="SCALE0001", amount="25.500000"))
    assert result.status is S.VERIFIED


@pytest.mark.parametrize(
    "wrong", ["ABC12345", "ABC1234567", "ABC123457", "abc123456", " ABC12345 "]
)
async def test_wrong_codes_never_match(container: Any, mailbox: FakeMailbox, wrong: str) -> None:
    mailbox.add(make_email(code="ABC123456"))
    result = await container.verifier.verify(req(code=wrong, amount="25.50"))
    assert result.status is S.NOT_FOUND


async def test_code_is_trimmed(container: Any, mailbox: FakeMailbox) -> None:
    mailbox.add(make_email(code="ABC123456", amount="25.50"))
    assert (await container.verifier.verify(req(code="  ABC123456 ", amount="25.50"))).verified


async def test_case_insensitive_only_when_configured(
    build: Callable[..., Any], mailbox: FakeMailbox
) -> None:
    mailbox.add(make_email(code="AbC123456", amount="25.50"))
    c = build(payment_code_case_insensitive=True)
    assert (await c.verifier.verify(req(code="abc123456", amount="25.50"))).verified


@pytest.mark.parametrize("amount", ["25.49", "25.51", "20", "250", "2.55"])
async def test_amount_mismatch(container: Any, mailbox: FakeMailbox, amount: str) -> None:
    mailbox.add(make_email(code="AMOUNT001", amount="25.50"))
    result = await container.verifier.verify(req(code="AMOUNT001", amount=amount))
    assert result.status is S.AMOUNT_MISMATCH
    assert result.received_amount == Decimal("25.50")


async def test_explicit_tolerance_is_opt_in(
    build: Callable[..., Any], mailbox: FakeMailbox
) -> None:
    mailbox.add(make_email(code="TOLER0001", amount="25.50"))
    c = build(payment_amount_tolerance="0.01")
    assert (await c.verifier.verify(req(code="TOLER0001", amount="25.49"))).verified


async def test_asset_mismatch(container: Any, mailbox: FakeMailbox) -> None:
    mailbox.add(make_email(code="ASSET0001", amount="25", asset="USDT"))
    result = await container.verifier.verify(req(code="ASSET0001", asset="USDC"))
    assert result.status is S.ASSET_MISMATCH
    assert result.received_asset == "USDT"


async def test_untrusted_email(container: Any, mailbox: FakeMailbox) -> None:
    mailbox.add(make_email(code="SPOOF0001", amount="25", dkim="fail", dmarc="fail"))
    assert (await container.verifier.verify(req(code="SPOOF0001"))).status is S.UNTRUSTED_EMAIL


async def test_trusted_email_wins_over_earlier_spoof(container: Any, mailbox: FakeMailbox) -> None:
    mailbox.add(
        make_email(code="RACE00001", amount="25", dkim="fail", message_id="<spoof@x.example>")
    )
    assert (await container.verifier.verify(req(code="RACE00001"))).status is S.UNTRUSTED_EMAIL
    mailbox.add(make_email(code="RACE00001", amount="25", message_id="<real@binance.example>"))
    assert (await container.verifier.verify(req(code="RACE00001"))).status is S.VERIFIED


async def test_expired_payment(container: Any, mailbox: FakeMailbox) -> None:
    old = datetime.now(UTC) - timedelta(minutes=90)
    mailbox.add(make_email(code="EXPIRED01", amount="25", when=old))
    assert (await container.verifier.verify(req(code="EXPIRED01", max_age=60))).status is (
        S.EXPIRED_PAYMENT
    )
    assert (await container.verifier.verify(req(code="EXPIRED01", max_age=120))).verified


async def test_future_dated_payment_rejected(container: Any, mailbox: FakeMailbox) -> None:
    future = datetime.now(UTC) + timedelta(hours=2)
    mailbox.add(make_email(code="FUTURE001", amount="25", when=future))
    assert (await container.verifier.verify(req(code="FUTURE001"))).status is S.EXPIRED_PAYMENT


async def test_pending_payment_not_completed(container: Any, mailbox: FakeMailbox) -> None:
    mailbox.add(make_email(code="PENDING01", amount="25", status="Pending"))
    result = await container.verifier.verify(req(code="PENDING01"))
    assert result.status is S.PAYMENT_NOT_COMPLETED and result.retryable


async def test_ambiguous_payment(container: Any, mailbox: FakeMailbox) -> None:
    mailbox.add(make_email(code="AMBIG0001", amount="25", message_id="<m1@binance.example>"))
    mailbox.add(make_email(code="AMBIG0001", amount="30", message_id="<m2@binance.example>"))
    assert (await container.verifier.verify(req(code="AMBIG0001"))).status is S.AMBIGUOUS_PAYMENT


@pytest.mark.parametrize("bad", ["AB", "has space", "x" * 65, "ABC$123", "-ABC123"])
async def test_invalid_payment_code(container: Any, mailbox: FakeMailbox, bad: str) -> None:
    result = await container.verifier.verify(req(code=bad))
    assert result.status is S.INVALID_PAYMENT_CODE
    assert mailbox.connections == 0  # never hits Gmail


async def test_not_found_after_on_demand_sync(container: Any, mailbox: FakeMailbox) -> None:
    result = await container.verifier.verify(req(code="NOPE00001"))
    assert result.status is S.NOT_FOUND and result.retryable
    assert mailbox.connections == 1


async def test_payment_already_synced_does_not_hit_gmail(
    container: Any, mailbox: FakeMailbox
) -> None:
    mailbox.add(make_email(code="LOCAL0001", amount="25"))
    from app.services.mail_sync import SyncMode

    await container.sync_service.sync_all(SyncMode.PERIODIC)
    connections = mailbox.connections
    assert (await container.verifier.verify(req(code="LOCAL0001"))).verified
    assert mailbox.connections == connections


@pytest.mark.parametrize("kind", ["down", "timeout"])
async def test_mail_provider_unavailable(
    container: Any, mailbox: FakeMailbox, failing_errors: dict[str, Exception], kind: str
) -> None:
    mailbox.fail = failing_errors[kind]
    result = await container.verifier.verify(req(code="DOWN00001"))
    assert result.status is S.MAIL_PROVIDER_UNAVAILABLE and result.retryable


async def test_pending_sync_when_other_replica_holds_lock(
    build: Callable[..., Any], mailbox: FakeMailbox
) -> None:
    holder = build()
    await holder.sync_service.ensure_env_account()
    (account_id,) = await holder.sync_service.enabled_account_ids()
    c = build(mail_sync_lock_wait_seconds=0.3)
    async with holder.sync_service._account_lock(account_id, 0):
        result = await c.verifier.verify(req(code="LOCKED001"))
    assert result.status is S.PENDING_SYNC


async def test_no_mail_account_configured(build: Callable[..., Any]) -> None:
    c = build(gmail_email=None, gmail_app_password=None)
    assert (await c.verifier.verify(req(code="NOACC0001"))).status is S.MAIL_PROVIDER_UNAVAILABLE


# --- Anti-replay ------------------------------------------------------------------------


async def test_claim_without_order_reference_cannot_be_reused(
    container: Any, mailbox: FakeMailbox
) -> None:
    mailbox.add(make_email(code="NOORDER01", amount="25"))
    assert (await container.verifier.verify(req(code="NOORDER01", order=None))).verified
    second = await container.verifier.verify(req(code="NOORDER01", order=None))
    assert second.status is S.ALREADY_CLAIMED


async def test_idempotent_even_after_max_age(
    container: Any, mailbox: FakeMailbox, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    mailbox.add(make_email(code="IDEMP0001", amount="25"))
    assert (await container.verifier.verify(req(code="IDEMP0001"))).verified
    async with sessionmaker() as s, s.begin():
        payment = await s.scalar(select(Payment))
        assert payment is not None
        payment.received_at = datetime.now(UTC) - timedelta(days=2)
    again = await container.verifier.verify(req(code="IDEMP0001"))
    assert again.verified and again.idempotent


async def test_unique_constraint_prevents_double_claim(
    container: Any, mailbox: FakeMailbox, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    mailbox.add(make_email(code="UNIQUE001", amount="25"))
    assert (await container.verifier.verify(req(code="UNIQUE001"))).verified
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


async def test_concurrent_requests_same_payment_only_one_wins(
    build: Callable[..., Any],
    mailbox: FakeMailbox,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Many replicas race to claim one payment for different orders: exactly one wins."""
    mailbox.add(make_email(code="RACEPAY01", amount="25"))
    # Pre-sync so that every request goes straight to the claim.
    from app.services.mail_sync import SyncMode

    await build().sync_service.sync_all(SyncMode.MANUAL)

    replicas = [build() for _ in range(10)]
    results = await asyncio.gather(
        *(
            r.verifier.verify(req(code="RACEPAY01", order=f"ORDER-{i}"))
            for i, r in enumerate(replicas)
        )
    )
    statuses = [r.status for r in results]
    assert statuses.count(S.VERIFIED) == 1, statuses
    assert statuses.count(S.ALREADY_CLAIMED) == 9, statuses
    async with sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(PaymentClaim)) == 1


async def test_concurrent_requests_bypassing_lookup_race_directly_on_claim(
    container: Any, mailbox: FakeMailbox, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Hammer PaymentClaimService itself: A -> CLAIMED, B -> ALREADY_CLAIMED, never both."""
    mailbox.add(make_email(code="RACEPAY02", amount="25"))
    from app.services.mail_sync import SyncMode

    await container.sync_service.sync_all(SyncMode.MANUAL)
    async with sessionmaker() as s:
        payment_id = await s.scalar(select(Payment.id))
    for _round in range(5):
        async with sessionmaker() as s, s.begin():
            await s.execute(PaymentClaim.__table__.delete())
        results = await asyncio.gather(
            *(
                container.claim_service.claim(
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


async def test_concurrent_same_order_is_idempotent(
    build: Callable[..., Any], mailbox: FakeMailbox
) -> None:
    mailbox.add(make_email(code="RACEPAY03", amount="25"))
    from app.services.mail_sync import SyncMode

    await build().sync_service.sync_all(SyncMode.MANUAL)
    results = await asyncio.gather(
        *(build().verifier.verify(req(code="RACEPAY03", order="ORDER-SAME")) for _ in range(5))
    )
    assert all(r.verified for r in results)
    assert sum(1 for r in results if not r.idempotent) == 1
