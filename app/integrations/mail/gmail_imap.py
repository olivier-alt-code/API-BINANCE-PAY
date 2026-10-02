"""Gmail over IMAP4 + SSL (``imap.gmail.com:993``).

Uses the stdlib :mod:`imaplib` in a worker thread (``asyncio.to_thread``), with:

* TLS certificate verification (default SSL context);
* a socket timeout on every operation plus an asyncio timeout guard;
* limited retries with exponential backoff for transient network errors only
  (authentication failures are never retried);
* read-only mailbox selection and ``BODY.PEEK[]`` so messages are not marked as read.

Credentials come from a :class:`CredentialsProvider` (App Password or OAuth2/XOAUTH2), so
switching the authentication method never touches the sync or verification logic.
"""

from __future__ import annotations

import asyncio
import contextlib
import imaplib
import logging
import re
import socket
import ssl
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, TypeVar

from app.core.exceptions import MailAuthenticationError, MailProviderError, MailTimeoutError
from app.integrations.mail.base import (
    CredentialsProvider,
    FetchedMessage,
    ImapCredentials,
    MailboxInfo,
    PasswordCredentials,
    XOAuth2Credentials,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

ImapFactory = Callable[[str, int, float], imaplib.IMAP4]

# Values interpolated in IMAP SEARCH must be safe atoms: no quotes, spaces, parentheses or
# CRLF (prevents IMAP command injection through configuration).
_SAFE_FILTER_RE = re.compile(r"^[A-Za-z0-9.@_+\-]{1,254}$")
_UIDVALIDITY_RE = re.compile(rb"UIDVALIDITY (\d+)")
_UIDNEXT_RE = re.compile(rb"UIDNEXT (\d+)")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_TRANSIENT = (OSError, socket.timeout, ssl.SSLError, imaplib.IMAP4.abort, EOFError)


def default_imap_factory(host: str, port: int, timeout: float) -> imaplib.IMAP4:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=timeout)


def build_xoauth2_string(username: str, access_token: str) -> bytes:
    return f"user={username}\x01auth=Bearer {access_token}\x01\x01".encode()


def imap_date(value: datetime) -> str:
    return f"{value.day:02d}-{_MONTHS[value.month - 1]}-{value.year}"


def build_from_criteria(filters: Sequence[str]) -> list[str]:
    safe: list[str] = []
    for item in filters:
        value = item.removeprefix("*.").strip()
        if not _SAFE_FILTER_RE.fullmatch(value):
            raise MailProviderError("Invalid sender filter in configuration")
        safe.append(value)
    if not safe:
        return []
    criteria = ["FROM", f'"{safe[-1]}"']
    for value in reversed(safe[:-1]):
        criteria = ["OR", "FROM", f'"{value}"', *criteria]
    return ["(", *criteria, ")"] if len(safe) > 1 else criteria


class GmailImapProvider:
    def __init__(
        self,
        *,
        account_email: str,
        credentials: CredentialsProvider,
        host: str = "imap.gmail.com",
        port: int = 993,
        mailbox: str = "INBOX",
        timeout_seconds: float = 15.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
        imap_factory: ImapFactory | None = None,
    ) -> None:
        self._email = account_email
        self._credentials = credentials
        self._host = host
        self._port = port
        self._mailbox = mailbox
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff = retry_backoff_seconds
        self._factory = imap_factory or default_imap_factory
        self._conn: imaplib.IMAP4 | None = None

    @property
    def provider_name(self) -> str:
        return "gmail"

    @property
    def account_email(self) -> str:
        return self._email

    # --- helpers ----------------------------------------------------------------------
    async def _run(self, func: Callable[..., T], *args: Any) -> T:
        try:
            # Socket timeout bounds each blocking call; the asyncio guard covers the rest.
            return await asyncio.wait_for(asyncio.to_thread(func, *args), self._timeout * 2)
        except TimeoutError:  # asyncio guard or socket timeout (socket.timeout is an alias)
            raise MailTimeoutError("IMAP operation timed out") from None

    @staticmethod
    def _check(typ: str, what: str) -> None:
        if typ != "OK":
            raise MailProviderError(f"IMAP {what} failed")

    # --- connection -------------------------------------------------------------------
    def _connect_sync(self, creds: ImapCredentials) -> tuple[imaplib.IMAP4, MailboxInfo]:
        conn = self._factory(self._host, self._port, self._timeout)
        try:
            try:
                if isinstance(creds, PasswordCredentials):
                    conn.login(creds.username, creds.password.get_secret_value())
                else:
                    payload = build_xoauth2_string(
                        creds.username, creds.access_token.get_secret_value()
                    )
                    conn.authenticate("XOAUTH2", lambda _challenge: payload)
            except imaplib.IMAP4.abort:
                raise  # connection-level problem: transient, may be retried
            except imaplib.IMAP4.error:
                # Generic message and no chaining: server responses may echo credentials.
                raise MailAuthenticationError("IMAP authentication failed") from None
            typ, data = conn.select(f'"{self._mailbox}"', readonly=True)
            self._check(typ, "SELECT")
            uidvalidity = self._read_status_value(conn, "UIDVALIDITY", data, _UIDVALIDITY_RE)
            if uidvalidity is None:
                raise MailProviderError("IMAP server did not report UIDVALIDITY")
            uidnext = self._read_status_value(conn, "UIDNEXT", data, _UIDNEXT_RE)
            return conn, MailboxInfo(self._mailbox, uidvalidity, uidnext)
        except BaseException:
            self._safe_logout(conn)
            raise

    @staticmethod
    def _read_status_value(
        conn: imaplib.IMAP4, name: str, data: Sequence[Any], pattern: re.Pattern[bytes]
    ) -> int | None:
        _typ, values = conn.response(name)
        for value in values or []:
            if isinstance(value, bytes) and value.strip().isdigit():
                return int(value)
        for item in data or []:
            if isinstance(item, bytes) and (m := pattern.search(item)):
                return int(m.group(1))
        return None

    @staticmethod
    def _safe_logout(conn: imaplib.IMAP4) -> None:
        with contextlib.suppress(Exception):  # best effort cleanup
            conn.logout()

    async def connect(self) -> MailboxInfo:
        await self.close()
        attempt = 0
        refreshed = False
        while True:
            creds = await self._credentials.get_credentials()
            try:
                conn, info = await self._run(self._connect_sync, creds)
            except MailAuthenticationError:
                if isinstance(creds, XOAuth2Credentials) and not refreshed:
                    # Access token may have been revoked/expired early: refresh once.
                    refreshed = True
                    await self._credentials.invalidate()
                    continue
                raise
            except MailProviderError:
                raise
            except _TRANSIENT as exc:
                attempt += 1
                if attempt > self._max_retries:
                    if isinstance(exc, TimeoutError):
                        raise MailTimeoutError("IMAP connection timed out") from None
                    raise MailProviderError("IMAP connection failed") from None
                delay = self._backoff * (2 ** (attempt - 1))
                logger.warning(
                    "imap_connect_retry",
                    extra={"attempt": attempt, "delay_s": delay, "error_type": type(exc).__name__},
                )
                await asyncio.sleep(delay)
                continue
            except imaplib.IMAP4.error:
                raise MailProviderError("IMAP command failed") from None
            self._conn = conn
            return info

    def _require(self) -> imaplib.IMAP4:
        if self._conn is None:
            raise MailProviderError("IMAP connection is not open")
        return self._conn

    # --- operations -------------------------------------------------------------------
    async def search_messages(
        self,
        *,
        after_uid: int | None,
        since: datetime | None,
        from_filters: Sequence[str],
    ) -> list[int]:
        conn = self._require()
        criteria: list[str] = []
        if after_uid is not None:
            criteria += ["UID", f"{after_uid + 1}:*"]
        if since is not None:
            criteria += ["SINCE", imap_date(since)]
        criteria += build_from_criteria(from_filters)
        if not criteria:
            criteria = ["ALL"]

        def _search() -> list[int]:
            typ, data = conn.uid("SEARCH", *criteria)
            self._check(typ, "SEARCH")
            raw = b" ".join(d for d in data if isinstance(d, bytes))
            return sorted({int(x) for x in raw.split() if x.isdigit()})

        uids = await self._guard(_search)
        # "UID n:*" always returns the highest UID even if it is < n: filter client-side.
        if after_uid is not None:
            uids = [u for u in uids if u > after_uid]
        return uids

    async def get_message(self, uid: int) -> FetchedMessage | None:
        conn = self._require()

        def _fetch() -> FetchedMessage | None:
            typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            self._check(typ, "FETCH")
            for item in data or []:
                if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                    return FetchedMessage(uid=uid, raw=item[1])
            return None  # message deleted between SEARCH and FETCH

        return await self._guard(_fetch)

    async def _guard(self, func: Callable[[], T]) -> T:
        try:
            return await self._run(func)
        except MailProviderError:
            await self.close()
            raise
        except _TRANSIENT as exc:
            await self.close()
            if isinstance(exc, TimeoutError):
                raise MailTimeoutError("IMAP operation timed out") from None
            raise MailProviderError("IMAP connection lost") from None
        except imaplib.IMAP4.error:
            await self.close()
            raise MailProviderError("IMAP command failed") from None

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await asyncio.wait_for(asyncio.to_thread(self._safe_logout, conn), self._timeout)
            except TimeoutError:
                logger.warning("imap_logout_timeout")
