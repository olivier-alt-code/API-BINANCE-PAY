"""GmailImapProvider against a fake imaplib connection (no network)."""

from __future__ import annotations

import imaplib
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.exceptions import MailAuthenticationError, MailProviderError, MailTimeoutError
from app.integrations.binance.email_parser import parse_mime, sender_address
from app.integrations.mail.base import PasswordCredentials, StaticCredentials, XOAuth2Credentials
from app.integrations.mail.gmail_imap import (
    GmailImapProvider,
    build_from_criteria,
    build_xoauth2_string,
    imap_date,
)

APP_PASSWORD = "abcdefghijklmnop"


class FakeImap:
    def __init__(self, *, login_ok: bool = True, messages: dict[int, bytes] | None = None) -> None:
        self.login_ok = login_ok
        self.messages = messages or {}
        self.calls: list[tuple[Any, ...]] = []
        self.logged_out = False
        self.search_error: BaseException | None = None

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", user))
        if not self.login_ok:
            raise imaplib.IMAP4.error(f"[AUTHENTICATIONFAILED] Invalid credentials {password}")
        return "OK", [b"Logged in"]

    def authenticate(self, mech: str, cb: Any) -> tuple[str, list[bytes]]:
        self.calls.append(("authenticate", mech, cb(b"")))
        if not self.login_ok:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        return "OK", [b"ok"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"3"]

    def response(self, name: str) -> tuple[str, list[bytes | None]]:
        return name, [b"777"] if name == "UIDVALIDITY" else [b"10"]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            if self.search_error:
                raise self.search_error
            return "OK", [" ".join(str(u) for u in sorted(self.messages)).encode()]
        if command == "FETCH":
            uid = int(args[0])
            if uid not in self.messages:
                return "OK", [None]
            return "OK", [(f"{uid} (UID {uid} BODY[] {{10}}".encode(), self.messages[uid]), b")"]
        raise AssertionError(command)

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", [b""]


def provider(factory: Any, creds: Any = None, **kw: Any) -> GmailImapProvider:
    return GmailImapProvider(
        account_email="merchant@gmail.com",
        credentials=creds
        or StaticCredentials(PasswordCredentials("merchant@gmail.com", SecretStr(APP_PASSWORD))),
        imap_factory=factory,
        timeout_seconds=1,
        max_retries=kw.pop("max_retries", 2),
        retry_backoff_seconds=0,
        **kw,
    )


async def test_connect_login_and_readonly_select() -> None:
    fake = FakeImap()
    p = provider(lambda *_: fake)
    info = await p.connect()
    assert info.uidvalidity == 777
    assert ("select", '"INBOX"', True) in fake.calls
    await p.close()
    assert fake.logged_out


async def test_auth_failure_is_not_retried_and_hides_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts = 0

    def factory(*_: Any) -> FakeImap:
        nonlocal attempts
        attempts += 1
        return FakeImap(login_ok=False)

    with pytest.raises(MailAuthenticationError) as info:
        await provider(factory).connect()
    assert attempts == 1
    assert APP_PASSWORD not in str(info.value)
    assert info.value.__cause__ is None  # server message (may echo secrets) not chained
    assert APP_PASSWORD not in caplog.text


async def test_imap_down_retries_then_fails() -> None:
    attempts = 0

    def factory(*_: Any) -> FakeImap:
        nonlocal attempts
        attempts += 1
        raise ConnectionRefusedError("connection refused")

    with pytest.raises(MailProviderError):
        await provider(factory, max_retries=2).connect()
    assert attempts == 3  # first try + 2 limited retries


async def test_reconnects_after_transient_error() -> None:
    attempts = 0
    fake = FakeImap()

    def factory(*_: Any) -> FakeImap:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("network unreachable")
        return fake

    info = await provider(factory).connect()
    assert info.uidvalidity == 777 and attempts == 2


async def test_imap_timeout() -> None:
    def factory(*_: Any) -> FakeImap:
        raise TimeoutError("timed out")

    with pytest.raises(MailTimeoutError):
        await provider(factory).connect()


async def test_timeout_during_search_closes_connection() -> None:
    fake = FakeImap()
    fake.search_error = TimeoutError("read timed out")
    p = provider(lambda *_: fake)
    await p.connect()
    with pytest.raises(MailTimeoutError):
        await p.search_messages(after_uid=1, since=None, from_filters=["binance.example"])
    assert fake.logged_out


async def test_search_filters_uids_and_fetch_uses_peek() -> None:
    fake = FakeImap(messages={5: b"a", 9: b"raw-9"})
    p = provider(lambda *_: fake)
    await p.connect()
    # "UID 10:*" on IMAP returns the last message even if its UID < 10.
    assert await p.search_messages(after_uid=9, since=None, from_filters=["binance.example"]) == []
    assert await p.search_messages(after_uid=5, since=None, from_filters=["binance.example"]) == [9]
    fetched = await p.get_message(9)
    assert fetched is not None and fetched.raw == b"raw-9"
    assert ("uid", "FETCH", "9", "(BODY.PEEK[])") in fake.calls
    assert await p.get_message(42) is None


async def test_xoauth2_authentication_and_single_refresh_on_failure() -> None:
    class Creds:
        def __init__(self) -> None:
            self.invalidated = 0

        async def get_credentials(self) -> XOAuth2Credentials:
            return XOAuth2Credentials("merchant@gmail.com", SecretStr(f"tok{self.invalidated}"))

        async def invalidate(self) -> None:
            self.invalidated += 1

    fakes = [FakeImap(login_ok=False), FakeImap()]
    creds = Creds()
    p = provider(lambda *_: fakes.pop(0), creds=creds)
    await p.connect()
    assert creds.invalidated == 1


def test_xoauth2_string_format() -> None:
    assert (
        build_xoauth2_string("u@gmail.com", "tok") == b"user=u@gmail.com\x01auth=Bearer tok\x01\x01"
    )


def test_search_criteria_and_injection_guard() -> None:
    assert build_from_criteria(["binance.example"]) == ["FROM", '"binance.example"']
    assert build_from_criteria(["a@x.example", "*.y.example"]) == [
        "(",
        "OR",
        "FROM",
        '"a@x.example"',
        "FROM",
        '"y.example"',
        ")",
    ]
    with pytest.raises(MailProviderError):
        build_from_criteria(['x" OR ALL'])
    with pytest.raises(MailProviderError):
        build_from_criteria(["x\r\nA001 LOGOUT"])
    assert imap_date(datetime(2026, 3, 7, tzinfo=UTC)) == "07-Mar-2026"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("=?utf-8?q?Binance_Team?= <a@binance.example>", "a@binance.example"),
        ('"Binance, Inc" <a@binance.example>', "a@binance.example"),
        ("A@Binance.Example", "a@binance.example"),
        ("a@binance.example, b@x.test", None),
        ("Binance <a@binance.example> <evil@x.test>", None),
        ("Binance", None),
    ],
)
def test_strict_sender_parsing(header: str, expected: str | None) -> None:
    msg = parse_mime(f"From: {header}\nSubject: x\n\nbody".encode())
    assert sender_address(msg) == expected


def test_no_imap_debug_logging() -> None:
    assert logging.getLogger("imaplib").level in (logging.NOTSET, logging.WARNING)
