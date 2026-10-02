"""Lock tables down for Supabase: enable RLS and revoke public API roles.

Supabase exposes the ``public`` schema through its Data API (PostgREST/GraphQL) to the
``anon`` and ``authenticated`` roles. These tables hold payments, claims and encrypted
credentials and must only be reachable by this backend, so:

* Row Level Security is enabled with NO policies (deny-all for API roles). The backend
  connects as the table owner (``postgres``), which bypasses RLS, so it is unaffected.
* All privileges are revoked from ``anon`` and ``authenticated`` when those roles exist
  (they only exist on Supabase; on plain PostgreSQL this part is a no-op).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "mail_accounts",
    "email_messages",
    "payments",
    "payment_claims",
    "rate_limit_counters",
)
# Only this app's sequences: the Supabase project may host other tables.
SEQUENCES = tuple(f"{t}_id_seq" for t in TABLES if t != "rate_limit_counters")
API_ROLES = ("anon", "authenticated")


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    for role in API_ROLES:
        tables = ", ".join(TABLES)
        sequences = ", ".join(SEQUENCES)
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    EXECUTE 'REVOKE ALL ON {tables} FROM {role}';
                    EXECUTE 'REVOKE ALL ON SEQUENCE {sequences} FROM {role}';
                END IF;
            END $$;
            """
        )


def downgrade() -> None:
    # Privileges are intentionally not re-granted to the public API roles.
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
