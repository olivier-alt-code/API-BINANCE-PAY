from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from saas.config import Settings
from saas.db import create_engine
from saas.main import create_app
from saas.verifier import (
    ClientNameTakenError,
    CredentialsResult,
    IssuedToken,
    Verification,
    VerifierError,
)

RETRYABLE = {"NOT_FOUND", "PENDING_SYNC", "BINANCE_API_UNAVAILABLE", "PAYMENT_NOT_COMPLETED"}


@dataclass
class FakePayment:
    amount: str
    asset: str = "USDT"
    claimed_by: str | None = None


@dataclass
class FakeClient:
    id: int
    name: str
    enabled: bool = True


@dataclass
class FakeVerifier:
    """In-memory stand-in for the verification API (same rules that matter here)."""

    payments: dict[str, FakePayment] = field(default_factory=dict)
    clients: dict[int, FakeClient] = field(default_factory=dict)
    # token id -> (client id, token, active)
    tokens: dict[int, tuple[int, str, bool]] = field(default_factory=dict)
    credentials: dict[str, tuple[str, str]] = field(default_factory=dict)
    fail_create_after_success: bool = False
    down: bool = False
    provision_down: bool = False  # verify works, client creation fails
    verify_calls: int = 0
    _next: int = 1

    def pay(self, code: str, amount: str = "5") -> None:
        self.payments[code] = FakePayment(amount)

    def _id(self) -> int:
        self._next += 1
        return self._next

    def _new_token(self, client_id: int) -> IssuedToken:
        token_id = self._id()
        token = f"bpv_test{token_id:04d}" + "x" * 30
        self.tokens[token_id] = (client_id, token, True)
        return IssuedToken(client_id, token_id, token, token[:12])

    def active_tokens(self, client_id: int) -> list[str]:
        return [t for c, t, a in self.tokens.values() if c == client_id and a]

    async def verify(
        self, payment_code: str, amount: str, order_reference: str, max_age: int | None
    ) -> Verification:
        self.verify_calls += 1
        if self.down:
            raise VerifierError("down")
        p = self.payments.get(payment_code)

        def res(status: str, **kw: object) -> Verification:
            return Verification(
                status=status,
                verified=status == "VERIFIED",
                retryable=status in RETRYABLE,
                idempotent=bool(kw.get("idempotent", False)),
                received_amount=p.amount if p else None,
                received_asset=p.asset if p else None,
            )

        if p is None:
            return res("NOT_FOUND")
        if p.asset != "USDT":
            return res("ASSET_MISMATCH")
        if Decimal(p.amount) != Decimal(amount):
            return res("AMOUNT_MISMATCH")
        if p.claimed_by is None:
            p.claimed_by = order_reference
            return res("VERIFIED")
        if p.claimed_by == order_reference:
            return res("VERIFIED", idempotent=True)
        return res("ALREADY_CLAIMED")

    async def create_client(self, name: str) -> IssuedToken:
        if self.down or self.provision_down:
            raise VerifierError("down")
        if any(c.name == name for c in self.clients.values()):
            raise ClientNameTakenError(name)
        client = FakeClient(self._id(), name)
        self.clients[client.id] = client
        token = self._new_token(client.id)
        if self.fail_create_after_success:
            self.fail_create_after_success = False
            raise VerifierError("crashed after creating")
        return token

    async def find_client_id(self, name: str) -> int | None:
        return next((c.id for c in self.clients.values() if c.name == name), None)

    async def issue_token(self, client_id: int) -> IssuedToken:
        if self.down:
            raise VerifierError("down")
        return self._new_token(client_id)

    async def active_token_ids(self, client_id: int) -> list[int]:
        return [i for i, (c, _, a) in self.tokens.items() if c == client_id and a]

    async def revoke_token(self, token_id: int) -> None:
        c, t, _ = self.tokens[token_id]
        self.tokens[token_id] = (c, t, False)

    async def set_enabled(self, client_id: int, enabled: bool) -> None:
        if self.down:
            raise VerifierError("down")
        self.clients[client_id].enabled = enabled

    async def put_binance_credentials(
        self, token: str, api_key: str, api_secret: str
    ) -> CredentialsResult:
        owner = next((c for c, t, a in self.tokens.values() if t == token and a), None)
        if owner is None or not self.clients[owner].enabled:
            return CredentialsResult(False, 401, "Unauthorized", [])
        if api_key.startswith("TRADE"):
            return CredentialsResult(
                False, 422, "Key is not read-only", ["enableSpotAndMarginTrading"]
            )
        self.credentials[token] = (api_key, api_secret)
        return CredentialsResult(True, 200, None, [])

    async def aclose(self) -> None:
        return None


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
async def settings(tmp_path: Path) -> AsyncIterator[Settings]:
    # TEST_DATABASE_URL=postgresql://... runs the suite on Postgres, each test in its own
    # schema (dropped afterwards). Default: a throwaway SQLite file.
    pg_url = os.environ.get("TEST_DATABASE_URL")
    schema = f"t_{uuid.uuid4().hex[:12]}"
    settings = Settings(
        app_env="test",
        database_url=pg_url or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        db_schema=schema,
        session_secret="s" * 40,
        cookie_secure=False,
        pay_to_id="123456789",
        pay_to_name="Lea_tmi",
        price_usdt="5",
        period_days=30,
    )
    yield settings
    if pg_url:
        engine = create_engine(settings)
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


@pytest.fixture
def fake() -> FakeVerifier:
    return FakeVerifier()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def app(settings: Settings, fake: FakeVerifier, clock: Clock):
    application = create_app(settings, verifier=fake, clock=clock, run_sweeper=False)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')
TOKEN_RE = re.compile(r'<code id="token">([^<]+)</code>')


async def csrf(client: httpx.AsyncClient, path: str = "/") -> str:
    r = await client.get(path)
    match = CSRF_RE.search(r.text)
    assert match, r.text
    return match.group(1)


async def subscribe(client: httpx.AsyncClient, email: str = "ana@example.com") -> str:
    """Returns the payment page path."""
    token = await csrf(client)
    r = await client.post("/suscribirse", data={"email": email, "accept": "1", "csrf": token})
    assert r.status_code == 303, r.text
    return r.headers["location"]


async def pay(client: httpx.AsyncClient, page: str, order_id: str) -> httpx.Response:
    token = await csrf(client, page)
    return await client.post(page, data={"order_id": order_id, "csrf": token})
