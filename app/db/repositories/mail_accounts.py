from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MailAccount
from app.integrations.mail.base import MailAccountConfig


def to_config(account: MailAccount) -> MailAccountConfig:
    return MailAccountConfig(
        id=account.id,
        email=account.email,
        auth_type=account.auth_type,
        mailbox=account.mailbox,
        encrypted_credentials=account.encrypted_credentials,
    )


class MailAccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def list_enabled(self) -> list[MailAccount]:
        result = await self._s.scalars(
            select(MailAccount).where(MailAccount.enabled).order_by(MailAccount.id)
        )
        return list(result)

    async def get(self, account_id: int) -> MailAccount | None:
        return await self._s.get(MailAccount, account_id, populate_existing=True)

    async def get_by_email(self, email: str) -> MailAccount | None:
        return await self._s.scalar(select(MailAccount).where(MailAccount.email == email.lower()))

    async def upsert(
        self,
        *,
        email: str,
        auth_type: str,
        mailbox: str,
        encrypted_credentials: str | None,
        overwrite_credentials: bool,
    ) -> None:
        """Insert or update an account. Safe under concurrency (ON CONFLICT)."""
        stmt = insert(MailAccount).values(
            provider="gmail",
            email=email.lower(),
            auth_type=auth_type,
            mailbox=mailbox,
            encrypted_credentials=encrypted_credentials,
            enabled=True,
        )
        set_: dict[str, object] = {"auth_type": auth_type, "mailbox": mailbox}
        if overwrite_credentials:
            set_["encrypted_credentials"] = encrypted_credentials
        stmt = stmt.on_conflict_do_update(index_elements=[MailAccount.email], set_=set_)
        await self._s.execute(stmt)

    async def reset_cursor(self, account_id: int, uidvalidity: int) -> None:
        await self._s.execute(
            update(MailAccount)
            .where(MailAccount.id == account_id)
            .values(imap_uidvalidity=uidvalidity, last_imap_uid=None)
        )

    async def advance_cursor(self, account_id: int, uid: int) -> None:
        # GREATEST: never move the cursor backwards, even with concurrent writers.
        await self._s.execute(
            update(MailAccount)
            .where(MailAccount.id == account_id)
            .values(last_imap_uid=func.greatest(func.coalesce(MailAccount.last_imap_uid, 0), uid))
        )

    async def mark_synced(self, account_id: int, at: datetime, error: str | None) -> None:
        await self._s.execute(
            update(MailAccount)
            .where(MailAccount.id == account_id)
            .values(last_synced_at=at, last_sync_error=error)
        )
