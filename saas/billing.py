"""Subscriptions: checkout, payment confirmation, provisioning, renewal and expiry.

Payment flow (same gateway as the API itself)::

    buyer: email ──► Subscription(pending) + Checkout(new) ──► payment page
    buyer pays PRICE USDT by Binance Pay and pastes the Order ID
    POST /v1/payments/verify (orderReference = "saas-<checkout id>")
        VERIFIED ──► API client + token created ──► token shown ONCE ──► active
                     paid_until = now + PERIOD_DAYS

* The API's atomic claim guarantees that a Binance payment is used by one checkout only.
* A checkout is fulfilled once: its row is locked (``SELECT ... FOR UPDATE``) while
  provisioning, so a double submit cannot create two clients or extend twice.
* When ``paid_until`` passes, the sweeper disables the API client (its tokens answer 401)
  without deleting anything; a renewal re-enables it. The token never changes.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saas.config import Settings
from saas.db import (
    Checkout,
    CheckoutKind,
    CheckoutStatus,
    Subscription,
    SubscriptionStatus,
    aware,
    utcnow,
)
from saas.verifier import ClientNameTakenError, IssuedToken, Verifier, VerifierError

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
# Same rule as the API: Binance Order ID (digits) or transactionId (P_...).
PAYMENT_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
TOKEN_RE = re.compile(r"^bpv_[A-Za-z0-9_-]{20,80}$")
CLIENT_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_ .@+-]")
ORDER_REFERENCE_PREFIX = "saas-"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email: str) -> str | None:
    email = email.strip().lower()
    return email if EMAIL_RE.fullmatch(email) else None


class Outcome(StrEnum):
    ACTIVATED = "activated"  # new subscription: token issued (show it once)
    RENEWED = "renewed"
    ALREADY_DONE = "already_done"  # checkout already fulfilled earlier
    WAIT = "wait"  # payment not visible yet / Binance busy: retry in a few seconds
    REJECTED = "rejected"  # payment exists but cannot be accepted (see status)
    INVALID = "invalid"  # malformed input
    ERROR = "error"  # API unreachable or misconfigured


@dataclass(frozen=True)
class PaymentResult:
    outcome: Outcome
    status: str | None = None  # verification status from the API
    token: IssuedToken | None = None
    paid_until: datetime | None = None
    received_amount: str | None = None


class BillingService:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        verifier: Verifier,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._settings = settings
        self._sessions = sessionmaker
        self._verifier = verifier
        self._clock = clock

    @property
    def period(self) -> timedelta:
        return timedelta(days=self._settings.period_days)

    # --- checkouts --------------------------------------------------------------------------
    async def start_subscription(self, email: str) -> Checkout:
        """New subscription (pending) and its first checkout."""
        now = self._clock()
        sub_id = uuid.uuid4().hex
        # Deterministic, unique, valid for the API (letters, digits, spaces, . @ + - _).
        client_name = f"saas-{sub_id[:12]} {CLIENT_NAME_UNSAFE.sub('_', email)}"[:100]
        async with self._sessions() as session, session.begin():
            session.add(
                Subscription(
                    id=sub_id,
                    email=email,
                    status=SubscriptionStatus.PENDING,
                    api_client_name=client_name,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            checkout = self._new_checkout(sub_id, CheckoutKind.NEW, now)
            session.add(checkout)
        return checkout

    async def start_renewal(self, subscription_id: str) -> Checkout:
        now = self._clock()
        async with self._sessions() as session, session.begin():
            sub = await session.get(Subscription, subscription_id)
            if sub is None or sub.api_client_id is None:
                raise LookupError("Subscription not found or never activated")
            # Reuse an open renewal checkout instead of piling up new ones.
            existing = await session.scalar(
                select(Checkout).where(
                    Checkout.subscription_id == subscription_id,
                    Checkout.kind == CheckoutKind.RENEWAL,
                    Checkout.status == CheckoutStatus.PENDING,
                    Checkout.payment_code.is_(None),
                )
            )
            if existing is not None:
                return existing
            checkout = self._new_checkout(subscription_id, CheckoutKind.RENEWAL, now)
            session.add(checkout)
        return checkout

    def _new_checkout(self, subscription_id: str, kind: str, now: datetime) -> Checkout:
        return Checkout(
            id=uuid.uuid4().hex,
            access_key=secrets.token_urlsafe(24),
            subscription_id=subscription_id,
            kind=kind,
            amount=self._settings.price_usdt,
            status=CheckoutStatus.PENDING,
            created_at=now,
        )

    async def get_checkout(self, access_key: str) -> tuple[Checkout, Subscription] | None:
        if not access_key or len(access_key) > 64:
            return None
        async with self._sessions() as session:
            checkout = await session.scalar(
                select(Checkout).where(Checkout.access_key == access_key)
            )
            if checkout is None:
                return None
            sub = await session.get(Subscription, checkout.subscription_id)
            return (checkout, sub) if sub is not None else None

    # --- payment confirmation -------------------------------------------------------------
    async def submit_payment(self, access_key: str, payment_code: str) -> PaymentResult:
        code = payment_code.strip()
        if not PAYMENT_CODE_RE.fullmatch(code):
            return PaymentResult(Outcome.INVALID, status="INVALID_PAYMENT_CODE")
        found = await self.get_checkout(access_key)
        if found is None:
            return PaymentResult(Outcome.INVALID, status="CHECKOUT_NOT_FOUND")
        checkout, _ = found
        if checkout.payment_code is not None and checkout.payment_code != code:
            # Never let a second payment be claimed by a checkout that already has one.
            return PaymentResult(Outcome.REJECTED, status="CHECKOUT_HAS_OTHER_PAYMENT")
        if checkout.status == CheckoutStatus.FULFILLED:
            return PaymentResult(Outcome.ALREADY_DONE)

        try:
            result = await self._verifier.verify(
                code,
                checkout.amount,
                ORDER_REFERENCE_PREFIX + checkout.id,
                self._settings.payment_max_age_minutes,
            )
        except VerifierError:
            return PaymentResult(Outcome.ERROR)
        if not result.verified:
            outcome = Outcome.WAIT if result.retryable else Outcome.REJECTED
            return PaymentResult(
                outcome, status=result.status, received_amount=result.received_amount
            )

        # The payment is now claimed for this checkout in the API: remember it first, so a
        # failed provisioning can be retried with the same Order ID (and only with it).
        if not await self._attach_payment(checkout.id, code):
            return PaymentResult(Outcome.REJECTED, status="CHECKOUT_HAS_OTHER_PAYMENT")
        try:
            return await self._fulfill(checkout.id)
        except VerifierError:
            return PaymentResult(Outcome.ERROR)

    async def _attach_payment(self, checkout_id: str, code: str) -> bool:
        try:
            async with self._sessions() as session, session.begin():
                checkout = await session.get(Checkout, checkout_id, with_for_update=True)
                if checkout is None:
                    return False
                if checkout.payment_code is None:
                    checkout.payment_code = code
                return checkout.payment_code == code
        except IntegrityError:
            # Same Order ID already attached to another checkout (the API would have said
            # ALREADY_CLAIMED; this is the local backstop).
            return False

    async def _fulfill(self, checkout_id: str) -> PaymentResult:
        now = self._clock()
        async with self._sessions() as session, session.begin():
            checkout = await session.get(Checkout, checkout_id, with_for_update=True)
            if checkout is not None and checkout.status == CheckoutStatus.FULFILLED:
                return PaymentResult(Outcome.ALREADY_DONE)
            sub = (
                await session.get(Subscription, checkout.subscription_id, with_for_update=True)
                if checkout is not None
                else None
            )
            if checkout is None or sub is None:
                return PaymentResult(Outcome.INVALID, status="CHECKOUT_NOT_FOUND")

            token: IssuedToken | None = None
            if sub.api_client_id is None:
                token = await self._provision(sub)
                sub.api_client_id = token.client_id
                self._store_token(sub, token)
            else:
                # Idempotent; also repairs a client left disabled by an interrupted sweep.
                await self._verifier.set_enabled(sub.api_client_id, True)

            current = aware(sub.paid_until) if sub.paid_until else now
            sub.paid_until = max(current, now) + self.period
            sub.status = SubscriptionStatus.ACTIVE
            sub.updated_at = now
            checkout.status = CheckoutStatus.FULFILLED
            checkout.fulfilled_at = now
            logger.info(
                "subscription_paid",
                extra={"subscription": sub.id, "kind": checkout.kind, "until": str(sub.paid_until)},
            )
            outcome = Outcome.ACTIVATED if token is not None else Outcome.RENEWED
            return PaymentResult(outcome, token=token, paid_until=sub.paid_until)

    async def _provision(self, sub: Subscription) -> IssuedToken:
        """Create the buyer's API client. Safe to retry after a crash halfway."""
        try:
            return await self._verifier.create_client(sub.api_client_name)
        except ClientNameTakenError:
            # Created by an earlier attempt whose token was never delivered: issue a fresh
            # token and revoke any other one.
            client_id = await self._verifier.find_client_id(sub.api_client_name)
            if client_id is None:
                raise VerifierError("Client name taken but not found") from None
            token = await self._verifier.issue_token(client_id)
            for token_id in await self._verifier.active_token_ids(client_id):
                if token_id != token.token_id:
                    await self._verifier.revoke_token(token_id)
            return token

    @staticmethod
    def _store_token(sub: Subscription, token: IssuedToken) -> None:
        sub.api_token_id = token.token_id
        sub.token_hash = hash_token(token.token)
        sub.token_prefix = token.prefix

    # --- tokens ---------------------------------------------------------------------------
    async def rotate_token(self, subscription_id: str) -> IssuedToken:
        """New token for the customer; the previous one stops working immediately."""
        async with self._sessions() as session, session.begin():
            sub = await session.get(Subscription, subscription_id, with_for_update=True)
            if sub is None or sub.api_client_id is None:
                raise LookupError("Subscription not active")
            token = await self._verifier.issue_token(sub.api_client_id)
            for token_id in await self._verifier.active_token_ids(sub.api_client_id):
                if token_id != token.token_id:
                    await self._verifier.revoke_token(token_id)
            self._store_token(sub, token)
            sub.updated_at = self._clock()
            return token

    async def recover_token(self, access_key: str, payment_code: str) -> IssuedToken | None:
        """Lost token: whoever has the payment page URL and its Order ID gets a new one."""
        found = await self.get_checkout(access_key)
        if found is None:
            return None
        checkout, sub = found
        if (
            checkout.status != CheckoutStatus.FULFILLED
            or checkout.payment_code is None
            or not secrets.compare_digest(checkout.payment_code, payment_code.strip())
        ):
            return None
        return await self.rotate_token(sub.id)

    async def find_by_token(self, token: str) -> Subscription | None:
        token = token.strip()
        if not TOKEN_RE.fullmatch(token):
            return None
        async with self._sessions() as session:
            return await session.scalar(
                select(Subscription).where(Subscription.token_hash == hash_token(token))
            )

    async def get_subscription(self, subscription_id: str) -> Subscription | None:
        async with self._sessions() as session:
            return await session.get(Subscription, subscription_id)

    async def owns_token(self, subscription_id: str, token: str) -> bool:
        sub = await self.find_by_token(token)
        return sub is not None and sub.id == subscription_id

    # --- expiry -----------------------------------------------------------------------------
    async def sweep_expired(self) -> int:
        """Disable the API client of every subscription whose period has ended."""
        now = self._clock()
        async with self._sessions() as session:
            candidates = list(
                await session.scalars(
                    select(Subscription.id).where(
                        Subscription.status == SubscriptionStatus.ACTIVE,
                        Subscription.paid_until < now,
                    )
                )
            )
        expired = 0
        for sub_id in candidates:
            try:
                async with self._sessions() as session, session.begin():
                    # Locked: a renewal of this same subscription waits for us (or we for it).
                    sub = await session.get(Subscription, sub_id, with_for_update=True)
                    if (
                        sub is None
                        or sub.status != SubscriptionStatus.ACTIVE
                        or sub.paid_until is None
                        or aware(sub.paid_until) >= now
                    ):
                        continue
                    if sub.api_client_id is not None:
                        await self._verifier.set_enabled(sub.api_client_id, False)
                    sub.status = SubscriptionStatus.EXPIRED
                    sub.updated_at = now
                    expired += 1
                    logger.info("subscription_expired", extra={"subscription": sub_id})
            except VerifierError:
                logger.warning("subscription_expire_failed", extra={"subscription": sub_id})
        return expired


def days_left(sub: Subscription, now: datetime) -> int:
    if sub.paid_until is None:
        return 0
    remaining = aware(sub.paid_until) - now
    return max(0, remaining.days + (1 if remaining.seconds > 0 else 0))


def format_usdt(value: str | Decimal) -> str:
    try:
        amount = Decimal(value).normalize()
    except InvalidOperation:
        return str(value)[:32]
    return f"{amount:f}"
