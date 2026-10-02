"""Mailbox abstraction. Sync code depends on :class:`MailProvider`, not on Gmail/IMAP."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pydantic import SecretStr


@dataclass(frozen=True)
class MailboxInfo:
    mailbox: str
    uidvalidity: int
    uidnext: int | None = None


@dataclass(frozen=True)
class FetchedMessage:
    uid: int
    raw: bytes


@dataclass(frozen=True)
class PasswordCredentials:
    """IMAP LOGIN. Used for App Passwords (and, if explicitly enabled, account passwords)."""

    username: str
    password: SecretStr


@dataclass(frozen=True)
class XOAuth2Credentials:
    username: str
    access_token: SecretStr


ImapCredentials = PasswordCredentials | XOAuth2Credentials


class CredentialsProvider(Protocol):
    async def get_credentials(self) -> ImapCredentials: ...

    async def invalidate(self) -> None:
        """Forget cached credentials (e.g. after an auth failure with an expired token)."""
        ...


class MailProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def account_email(self) -> str: ...

    async def connect(self) -> MailboxInfo:
        """Connect, authenticate and select the mailbox (read-only)."""
        ...

    async def search_messages(
        self,
        *,
        after_uid: int | None,
        since: datetime | None,
        from_filters: Sequence[str],
    ) -> list[int]:
        """UIDs (ascending) of messages with UID > ``after_uid`` and/or date >= ``since``,
        whose From header contains any of ``from_filters`` (server-side pre-filter only;
        authenticity is ALWAYS validated client-side afterwards)."""
        ...

    async def get_message(self, uid: int) -> FetchedMessage | None:
        """Raw RFC 822 bytes of a message, without marking it as read."""
        ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class MailAccountConfig:
    """What a factory needs to build a provider for one mail account."""

    id: int
    email: str
    auth_type: str
    mailbox: str
    encrypted_credentials: str | None


class MailProviderFactory(Protocol):
    def build(self, account: MailAccountConfig) -> MailProvider: ...


class StaticCredentials:
    """Credentials that never change (App Password from the environment)."""

    def __init__(self, credentials: ImapCredentials) -> None:
        self._credentials = credentials

    async def get_credentials(self) -> ImapCredentials:
        return self._credentials

    async def invalidate(self) -> None:
        return None
