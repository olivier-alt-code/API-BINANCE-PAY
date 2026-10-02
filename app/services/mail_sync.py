"""Incremental Gmail synchronization: mailbox -> trust validation -> parsing -> PostgreSQL.

* Incremental: only UIDs greater than ``mail_accounts.last_imap_uid`` are fetched (the
  cursor is reset if the mailbox ``UIDVALIDITY`` changes). The first sync looks back
  ``MAIL_INITIAL_LOOKBACK_HOURS`` only.
* Multi-replica safe: a PostgreSQL advisory lock per account serializes syncs across
  processes, and unique constraints (UID, Message-ID, payment external id, trusted code)
  make re-processing idempotent even if the lock were bypassed.
* Every message is stored with its cursor update in ONE transaction, so a crash never
  loses or duplicates a message.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage as MimeMessage
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import GmailAuthMethod, Settings
from app.core.encryption import CredentialCipher
from app.core.exceptions import AppError, BinanceEmailParseError, MailProviderError
from app.core.logging import mask_email
from app.db.models import MailAuthType, MessageParseStatus, PaymentSource
from app.db.repositories.email_messages import EmailMessageRepository
from app.db.repositories.mail_accounts import MailAccountRepository, to_config
from app.db.repositories.payments import NewPayment, PaymentRepository, StoreOutcome
from app.integrations.binance.email_parser import (
    BinanceEmailParser,
    ParsedBinancePayment,
    header_datetime,
    normalize_text,
    parse_mime,
    sender_address,
)
from app.integrations.binance.email_validator import EmailTrustResult, EmailTrustValidator
from app.integrations.mail.base import FetchedMessage, MailProvider, MailProviderFactory
from app.integrations.mail.gmail_oauth import refresh_token_aad

logger = logging.getLogger(__name__)

_LOCK_NAMESPACE = 727_001


def utcnow() -> datetime:
    return datetime.now(UTC)


class SyncMode(StrEnum):
    PERIODIC = "periodic"
    ON_DEMAND = "on_demand"
    MANUAL = "manual"


@dataclass
class SyncSummary:
    messages_scanned: int = 0
    binance_messages: int = 0
    payments_imported: int = 0
    duplicates: int = 0
    untrusted_messages: int = 0
    unparsed_messages: int = 0
    accounts_total: int = 0
    accounts_synced: int = 0
    accounts_failed: int = 0
    accounts_busy: int = 0  # another replica held the sync lock
    accounts_skipped_recent: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: SyncSummary) -> None:
        for key, value in asdict(other).items():
            if key == "errors":
                self.errors.extend(other.errors)
            else:
                setattr(self, key, getattr(self, key) + value)


class MailSyncService:
    def __init__(
        self,
        *,
        settings: Settings,
        engine: AsyncEngine,
        sessionmaker: async_sessionmaker[AsyncSession],
        provider_factory: MailProviderFactory,
        validator: EmailTrustValidator,
        parser: BinanceEmailParser,
        cipher: CredentialCipher | None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._sessionmaker = sessionmaker
        self._factory = provider_factory
        self._validator = validator
        self._parser = parser
        self._cipher = cipher
        self._clock = clock

    # --- accounts ---------------------------------------------------------------------
    async def ensure_env_account(self) -> None:
        """Create/refresh the mail account configured through environment variables."""
        s = self._settings
        if not s.gmail_email:
            return
        encrypted: str | None = None
        overwrite = False
        match s.gmail_auth_method:
            case GmailAuthMethod.APP_PASSWORD:
                if s.gmail_app_password is None:
                    return
                auth_type = MailAuthType.APP_PASSWORD  # secret stays in the environment
            case GmailAuthMethod.OAUTH2:
                if s.gmail_oauth_refresh_token is None or self._cipher is None:
                    return  # account is created by the OAuth callback instead
                auth_type = MailAuthType.OAUTH2
                encrypted = self._cipher.encrypt_json(
                    {"refresh_token": s.gmail_oauth_refresh_token.get_secret_value()},
                    refresh_token_aad(s.gmail_email),
                )
                overwrite = True
            case GmailAuthMethod.ACCOUNT_PASSWORD:
                if not s.gmail_account_password_auth_enabled or s.gmail_account_password is None:
                    return
                auth_type = MailAuthType.ACCOUNT_PASSWORD
        async with self._sessionmaker() as session, session.begin():
            existing = await MailAccountRepository(session).get_by_email(s.gmail_email)
            if (
                existing is not None
                and existing.auth_type == auth_type
                and existing.mailbox == s.gmail_mailbox
                and not overwrite
            ):
                return
            await MailAccountRepository(session).upsert(
                email=s.gmail_email,
                auth_type=auth_type,
                mailbox=s.gmail_mailbox,
                encrypted_credentials=encrypted,
                overwrite_credentials=overwrite or existing is None,
            )

    async def enabled_account_ids(self) -> list[int]:
        async with self._sessionmaker() as session:
            return [a.id for a in await MailAccountRepository(session).list_enabled()]

    # --- locking ----------------------------------------------------------------------
    @asynccontextmanager
    async def _account_lock(self, account_id: int, wait_seconds: float) -> AsyncIterator[bool]:
        """Session-level advisory lock on a dedicated connection (released on disconnect)."""
        async with self._engine.connect() as conn:
            deadline = time.monotonic() + wait_seconds
            acquired = False
            while True:
                acquired = bool(
                    (
                        await conn.execute(
                            text("SELECT pg_try_advisory_lock(:ns, CAST(:id AS integer))"),
                            {"ns": _LOCK_NAMESPACE, "id": account_id},
                        )
                    ).scalar()
                )
                await conn.commit()
                if acquired or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.2)
            try:
                yield acquired
            finally:
                if acquired:
                    await conn.execute(
                        text("SELECT pg_advisory_unlock(:ns, CAST(:id AS integer))"),
                        {"ns": _LOCK_NAMESPACE, "id": account_id},
                    )
                    await conn.commit()

    # --- sync -------------------------------------------------------------------------
    async def sync_all(self, mode: SyncMode) -> SyncSummary:
        await self.ensure_env_account()
        summary = SyncSummary()
        for account_id in await self.enabled_account_ids():
            summary.accounts_total += 1
            summary.merge(await self.sync_account(account_id, mode))
        return summary

    async def sync_account(self, account_id: int, mode: SyncMode) -> SyncSummary:
        summary = SyncSummary()
        wait = 0.0 if mode is SyncMode.PERIODIC else self._settings.mail_sync_lock_wait_seconds
        async with self._account_lock(account_id, wait) as acquired:
            if not acquired:
                summary.accounts_busy = 1
                return summary
            async with self._sessionmaker() as session:
                account = await MailAccountRepository(session).get(account_id)
            if account is None or not account.enabled:
                return summary

            now = self._clock()
            if (
                mode is SyncMode.ON_DEMAND
                and account.last_synced_at is not None
                and (now - account.last_synced_at).total_seconds()
                < self._settings.mail_on_demand_min_interval_seconds
            ):
                # Synced moments ago (possibly by the replica we waited for).
                summary.accounts_skipped_recent = 1
                return summary

            log_ctx = {"account": mask_email(account.email), "mode": mode.value}
            provider: MailProvider | None = None
            error: str | None = None
            try:
                provider = self._factory.build(to_config(account))
                await self._sync_with_provider(
                    provider, account.id, account.imap_uidvalidity, account.last_imap_uid, summary
                )
                summary.accounts_synced = 1
            except (MailProviderError, AppError) as exc:
                error = exc.public_message
                summary.accounts_failed = 1
                summary.errors.append(error)
                logger.warning(
                    "mail_sync_failed", extra={**log_ctx, "error_type": type(exc).__name__}
                )
            finally:
                if provider is not None:
                    await provider.close()
                async with self._sessionmaker() as session, session.begin():
                    await MailAccountRepository(session).mark_synced(
                        account_id, self._clock(), error
                    )
            logger.info(
                "mail_sync_done",
                extra={
                    **log_ctx,
                    "scanned": summary.messages_scanned,
                    "binance": summary.binance_messages,
                    "imported": summary.payments_imported,
                    "duplicates": summary.duplicates,
                },
            )
            return summary

    async def _sync_with_provider(
        self,
        provider: MailProvider,
        account_id: int,
        stored_uidvalidity: int | None,
        last_uid: int | None,
        summary: SyncSummary,
    ) -> None:
        info = await provider.connect()
        if stored_uidvalidity != info.uidvalidity:
            # New mailbox or UIDs were renumbered by the server: restart from the lookback.
            async with self._sessionmaker() as session, session.begin():
                await MailAccountRepository(session).reset_cursor(account_id, info.uidvalidity)
            last_uid = None

        since = None
        if last_uid is None:
            since = self._clock() - timedelta(hours=self._settings.mail_initial_lookback_hours)
        filters = [*self._settings.binance_allowed_senders, *self._settings.binance_allowed_domains]
        if not filters:
            raise MailProviderError("BINANCE_ALLOWED_SENDERS/DOMAINS not configured")
        uids = await provider.search_messages(after_uid=last_uid, since=since, from_filters=filters)
        for uid in uids[: self._settings.mail_sync_max_messages_per_run]:
            summary.messages_scanned += 1
            async with self._sessionmaker() as session, session.begin():
                if await EmailMessageRepository(session).exists_uid(
                    account_id, info.uidvalidity, uid
                ):
                    summary.duplicates += 1
                    await MailAccountRepository(session).advance_cursor(account_id, uid)
                    continue
            fetched = await provider.get_message(uid)
            if fetched is None:
                async with self._sessionmaker() as session, session.begin():
                    await MailAccountRepository(session).advance_cursor(account_id, uid)
                continue
            await self._process_message(account_id, info.uidvalidity, fetched, summary)

    # --- per message ------------------------------------------------------------------
    async def _process_message(
        self, account_id: int, uidvalidity: int, fetched: FetchedMessage, summary: SyncSummary
    ) -> None:
        raw = fetched.raw
        body_hash = hashlib.sha256(raw).hexdigest()
        mime: MimeMessage | None
        try:
            mime = parse_mime(raw)
        except BinanceEmailParseError:
            mime = None

        sender = sender_address(mime) if mime is not None else None
        candidate = mime is not None and self._validator.policy.sender_allowed(sender)
        trust: EmailTrustResult | None = None
        parsed: ParsedBinancePayment | None = None
        parse_status = MessageParseStatus.NOT_BINANCE_SENDER
        subject: str | None = None
        message_id: str | None = None
        received_at: datetime | None = None

        if mime is None:
            parse_status = MessageParseStatus.PARSE_ERROR
        else:
            message_id = (str(mime.get("Message-ID", "")).strip() or None) if mime else None
            received_at = header_datetime(mime)
        if candidate and mime is not None:
            summary.binance_messages += 1
            trust = self._validator.validate(mime)
            subject = normalize_text(str(mime.get("Subject", "")))[:500] or None
            try:
                parsed = self._parser.parse(mime)
                parse_status = MessageParseStatus.PARSED
            except BinanceEmailParseError as exc:
                parse_status = MessageParseStatus.NO_TEMPLATE
                summary.unparsed_messages += 1
                logger.info("binance_email_unparsed", extra={"uid": fetched.uid, "why": str(exc)})
            if not trust.trusted:
                summary.untrusted_messages += 1
                logger.warning(
                    "binance_email_untrusted",
                    extra={"uid": fetched.uid, "reasons": list(trust.reasons)},
                )

        raw_encrypted = None
        if self._settings.store_raw_emails and candidate:
            if self._cipher is not None:
                raw_encrypted = self._cipher.encrypt(
                    raw, f"email:{account_id}:{uidvalidity}:{fetched.uid}"
                )
            else:
                logger.warning("store_raw_emails_without_encryption_key_skipped")

        trusted = bool(trust and trust.trusted)
        async with self._sessionmaker() as session, session.begin():
            message_pk = await EmailMessageRepository(session).insert(
                account_id=account_id,
                uidvalidity=uidvalidity,
                uid=fetched.uid,
                message_id=message_id[:998] if message_id else None,
                sender=sender[:320] if sender else None,
                subject=subject,
                received_at=received_at,
                trusted=trusted,
                trust_details=trust.as_dict() if trust else {},
                parse_status=parse_status,
                template=parsed.template if parsed else None,
                body_hash=body_hash,
                raw_encrypted=raw_encrypted,
            )
            if message_pk is None:
                summary.duplicates += 1  # same UID or same Message-ID already stored
            elif parsed is not None:
                outcome = await PaymentRepository(session).store(
                    NewPayment(
                        source=PaymentSource.BINANCE_EMAIL,
                        external_id=(parsed.message_id or f"sha256:{body_hash}")[:998],
                        email_message_id=message_pk,
                        payment_code=parsed.payment_code,
                        amount=parsed.amount,
                        asset=parsed.asset,
                        payment_status=parsed.payment_status,
                        received_at=parsed.received_at,
                        trusted=trusted,
                        template=parsed.template,
                    )
                )
                if outcome in (StoreOutcome.INSERTED, StoreOutcome.UPGRADED):
                    summary.payments_imported += 1
                else:
                    summary.duplicates += 1
                if outcome is StoreOutcome.CONFLICT:
                    logger.error(
                        "payment_evidence_conflict",
                        extra={"payment_code": parsed.payment_code, "uid": fetched.uid},
                    )
                logger.info(
                    "payment_evidence_stored",
                    extra={
                        "payment_code": parsed.payment_code,
                        "trusted": trusted,
                        "outcome": outcome.value,
                        "template": parsed.template,
                    },
                )
            await MailAccountRepository(session).advance_cursor(account_id, fetched.uid)

    async def test_connection(self, account_id: int) -> dict[str, Any]:
        async with self._sessionmaker() as session:
            account = await MailAccountRepository(session).get(account_id)
        if account is None:
            raise MailProviderError("Mail account not found")
        provider = self._factory.build(to_config(account))
        try:
            await provider.connect()
        finally:
            await provider.close()
        return {"provider": provider.provider_name, "account": account.email}
