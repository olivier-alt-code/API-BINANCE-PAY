from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.encryption import generate_key
from app.core.exceptions import MailProviderError, MailTimeoutError
from app.integrations.mail.base import FetchedMessage, MailAccountConfig, MailboxInfo

FIXTURES = Path(__file__).parent / "fixtures" / "binance"
API_KEY = "test-api-key-0123456789-abcdefghijklmnop"
ADMIN_KEY = "test-admin-key-0123456789-abcdefghijklmn"
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/binance_pay_test"
)
ENCRYPTION_KEY = generate_key()


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": SecretStr(TEST_DATABASE_URL),
        "api_keys": [SecretStr(API_KEY)],
        "admin_api_keys": [SecretStr(ADMIN_KEY)],
        "credentials_encryption_key": SecretStr(ENCRYPTION_KEY),
        "gmail_email": "merchant@gmail.com",
        "gmail_auth_method": "app_password",
        "gmail_app_password": SecretStr("abcd efgh ijkl mnop"),
        "binance_allowed_domains": ["binance.example"],
        "binance_allow_simulated_templates": True,
        "mail_on_demand_min_interval_seconds": 0,
        "mail_sync_lock_wait_seconds": 2,
        "log_json": False,
        "rate_limit_verify_per_minute": 1000,
        "rate_limit_admin_per_minute": 1000,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


# --- Fake mailbox -----------------------------------------------------------------------


class FakeMailbox:
    """In-memory IMAP mailbox shared by every FakeMailProvider (like a real server)."""

    def __init__(self) -> None:
        self.uidvalidity = 1
        self.messages: dict[int, bytes] = {}
        self.next_uid = 1
        self.fail: Exception | None = None
        self.connect_delay = 0.0
        self.connections = 0
        self.fetches = 0
        self.last_search: dict[str, Any] = {}

    def add(self, raw: bytes) -> int:
        uid = self.next_uid
        self.messages[uid] = raw
        self.next_uid += 1
        return uid


class FakeMailProvider:
    def __init__(self, mailbox: FakeMailbox, account: MailAccountConfig) -> None:
        self._box = mailbox
        self._account = account
        self.connected = False

    @property
    def provider_name(self) -> str:
        return "gmail"

    @property
    def account_email(self) -> str:
        return self._account.email

    async def connect(self) -> MailboxInfo:
        self._box.connections += 1
        if self._box.connect_delay:
            await asyncio.sleep(self._box.connect_delay)
        if self._box.fail is not None:
            raise self._box.fail
        self.connected = True
        return MailboxInfo(self._account.mailbox, self._box.uidvalidity, self._box.next_uid)

    async def search_messages(
        self, *, after_uid: int | None, since: datetime | None, from_filters: Sequence[str]
    ) -> list[int]:
        self._box.last_search = {"after_uid": after_uid, "since": since, "filters": from_filters}
        return sorted(u for u in self._box.messages if after_uid is None or u > after_uid)

    async def get_message(self, uid: int) -> FetchedMessage | None:
        self._box.fetches += 1
        raw = self._box.messages.get(uid)
        return FetchedMessage(uid, raw) if raw is not None else None

    async def close(self) -> None:
        self.connected = False


class FakeProviderFactory:
    def __init__(self, mailbox: FakeMailbox) -> None:
        self.mailbox = mailbox

    def build(self, account: MailAccountConfig) -> FakeMailProvider:
        return FakeMailProvider(self.mailbox, account)


@pytest.fixture
def mailbox() -> FakeMailbox:
    return FakeMailbox()


@pytest.fixture
def failing_errors() -> dict[str, Exception]:
    return {
        "down": MailProviderError("IMAP connection failed"),
        "timeout": MailTimeoutError("IMAP operation timed out"),
    }


# --- Database ---------------------------------------------------------------------------


def _run_migrations(url: str) -> None:
    from alembic.config import Config

    from alembic import command

    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
    cfg.cmd_opts = type("Opts", (), {"x": [f"url={url}"]})()  # type: ignore[assignment]
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    from app.db.session import create_engine

    settings = make_settings()
    eng = create_engine(settings)
    try:
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        await eng.dispose()
        pytest.skip(f"PostgreSQL not available for integration tests: {type(exc).__name__}")
    # Simulate Supabase's public API roles so the RLS/REVOKE migration is exercised.
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') "
                "THEN CREATE ROLE anon NOLOGIN; END IF; "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') "
                "THEN CREATE ROLE authenticated NOLOGIN; END IF; END $$"
            )
        )
    await asyncio.to_thread(_run_migrations, TEST_DATABASE_URL)
    yield eng
    await eng.dispose()


@pytest.fixture
async def sessionmaker(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE payment_claims, payments, email_messages, mail_accounts, "
                "rate_limit_counters RESTART IDENTITY CASCADE"
            )
        )
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
def build(
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    mailbox: FakeMailbox,
) -> Callable[..., Any]:
    """Build a fully wired container against the test DB and the fake mailbox."""
    from app.container import build_container

    def _build(**overrides: Any) -> Any:
        return build_container(
            make_settings(**overrides),
            engine,
            sessionmaker,
            provider_factory=FakeProviderFactory(mailbox),
        )

    return _build
