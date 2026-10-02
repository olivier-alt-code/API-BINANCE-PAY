from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EmailMessage


class EmailMessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def exists_uid(self, account_id: int, uidvalidity: int, uid: int) -> bool:
        found = await self._s.scalar(
            select(EmailMessage.id).where(
                EmailMessage.mail_account_id == account_id,
                EmailMessage.imap_uidvalidity == uidvalidity,
                EmailMessage.imap_uid == uid,
            )
        )
        return found is not None

    async def insert(
        self,
        *,
        account_id: int,
        uidvalidity: int,
        uid: int,
        message_id: str | None,
        sender: str | None,
        subject: str | None,
        received_at: datetime | None,
        trusted: bool,
        trust_details: dict[str, Any],
        parse_status: str,
        template: str | None,
        body_hash: str,
        raw_encrypted: str | None,
    ) -> int | None:
        """Insert a message; ``None`` if it already exists (same UID or same Message-ID)."""
        stmt = (
            insert(EmailMessage)
            .values(
                mail_account_id=account_id,
                imap_uidvalidity=uidvalidity,
                imap_uid=uid,
                message_id=message_id,
                sender=sender,
                subject=subject,
                received_at=received_at,
                trusted=trusted,
                trust_details=trust_details,
                parse_status=parse_status,
                template=template,
                body_hash=body_hash,
                raw_encrypted=raw_encrypted,
            )
            .on_conflict_do_nothing()
            .returning(EmailMessage.id)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()
