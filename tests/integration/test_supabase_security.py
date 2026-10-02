"""Tables must not be reachable through Supabase's public Data API roles."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

TABLES = ("mail_accounts", "email_messages", "payments", "payment_claims", "rate_limit_counters")


async def test_rls_enabled_on_all_tables(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with engine.connect() as conn:
        rows = dict(
            (
                await conn.execute(
                    text("SELECT relname, relrowsecurity FROM pg_class WHERE relname = ANY(:t)"),
                    {"t": list(TABLES)},
                )
            ).all()
        )
    assert rows == dict.fromkeys(TABLES, True)


@pytest.mark.parametrize("role", ["anon", "authenticated"])
@pytest.mark.parametrize("table", TABLES)
async def test_public_api_roles_have_no_privileges(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession], role: str, table: str
) -> None:
    async with engine.connect() as conn:
        await conn.execute(text(f"SET ROLE {role}"))
        with pytest.raises(ProgrammingError, match="permission denied"):
            await conn.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
        await conn.rollback()


async def test_rls_denies_rows_even_if_privileges_are_regranted(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Supabase default grants could give anon SELECT again: RLS still returns nothing."""
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO mail_accounts (provider, email, auth_type, enabled, mailbox) "
                "VALUES ('gmail', 'rls@gmail.com', 'app_password', true, 'INBOX')"
            )
        )
        await conn.execute(text("GRANT SELECT ON mail_accounts TO anon"))
        await conn.execute(text("SET LOCAL ROLE anon"))
        count = (await conn.execute(text("SELECT count(*) FROM mail_accounts"))).scalar()
        await conn.rollback()  # undo the grant and the row
    assert count == 0
