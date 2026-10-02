"""Binance Pay trade history: payer columns and the API sync cursor table.

The new table gets the same Supabase lock-down as migration 0002 (RLS, no policies, and no
privileges for the anon/authenticated Data API roles).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02 16:55:16.014732
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('evidence_sync_state',
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('cursor_time', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.String(length=255), nullable=True),
    sa.PrimaryKeyConstraint('source', name=op.f('pk_evidence_sync_state'))
    )
    op.add_column('payments', sa.Column('payer_name', sa.String(length=128), nullable=True))
    op.add_column('payments', sa.Column('payer_binance_id', sa.String(length=64), nullable=True))
    op.execute("ALTER TABLE evidence_sync_state ENABLE ROW LEVEL SECURITY")
    for role in ("anon", "authenticated"):
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    EXECUTE 'REVOKE ALL ON evidence_sync_state FROM {role}';
                END IF;
            END $$;
            """
        )


def downgrade() -> None:
    op.drop_column('payments', 'payer_binance_id')
    op.drop_column('payments', 'payer_name')
    op.drop_table('evidence_sync_state')
