"""Multi-client access: clients, private tokens, per-client Binance credentials.

* New tables: ``tenants``, ``api_tokens`` (only SHA-256 hashes), ``binance_credentials``
  (AES-GCM encrypted).
* ``payments`` and ``evidence_sync_state`` become per client (``tenant_id``). Existing
  payments, if any, are assigned to a client named ``legacy``.
* The Gmail/IMAP evidence source is removed: ``email_messages`` and ``mail_accounts`` are
  dropped, and so are ``payments.email_message_id`` / ``payments.template``.
* New tables get the same Supabase lock-down as 0002 (RLS, no grants for anon/authenticated).

This migration is not reversible (dropped email tables); restore a backup to go back.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TABLES = ("tenants", "api_tokens", "binance_credentials")
NEW_SEQUENCES = ("tenants_id_seq", "api_tokens_id_seq")


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("name", name=op.f("uq_tenants_name")),
    )
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_api_tokens_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_api_tokens_token_hash")),
    )
    op.create_index("ix_api_tokens_tenant_id", "api_tokens", ["tenant_id"], unique=False)
    op.create_table(
        "binance_credentials",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("encrypted", sa.Text(), nullable=False),
        sa.Column("api_key_hint", sa.String(length=16), nullable=False),
        sa.Column("ip_restricted", sa.Boolean(), nullable=False),
        sa.Column("permissions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_binance_credentials_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_binance_credentials")),
    )

    # --- payments: per client, no email columns -------------------------------------------
    op.drop_index("ix_payments_email_message_id", table_name="payments")
    op.drop_constraint(
        "fk_payments_email_message_id_email_messages", "payments", type_="foreignkey"
    )
    op.drop_column("payments", "email_message_id")
    op.drop_column("payments", "template")
    op.add_column("payments", sa.Column("tenant_id", sa.BigInteger(), nullable=True))
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM payments) THEN
                INSERT INTO tenants (name, enabled) VALUES ('legacy', true);
                UPDATE payments SET tenant_id = (SELECT id FROM tenants WHERE name = 'legacy');
            END IF;
        END $$;
        """
    )
    op.alter_column("payments", "tenant_id", nullable=False)
    op.create_foreign_key(
        op.f("fk_payments_tenant_id_tenants"),
        "payments",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint("uq_payments_source_external_id", "payments", type_="unique")
    op.drop_index(
        "uq_payments_trusted_payment_code",
        table_name="payments",
        postgresql_where=sa.text("trusted"),
    )
    op.drop_index("ix_payments_payment_code", table_name="payments")
    op.create_unique_constraint(
        op.f("uq_payments_tenant_id_source_external_id"),
        "payments",
        ["tenant_id", "source", "external_id"],
    )
    op.create_index(
        "uq_payments_tenant_trusted_payment_code",
        "payments",
        ["tenant_id", "payment_code"],
        unique=True,
        postgresql_where=sa.text("trusted"),
    )
    op.create_index(
        "ix_payments_tenant_id_payment_code", "payments", ["tenant_id", "payment_code"]
    )

    # --- sync cursor: per client (cursor data is disposable) -------------------------------
    op.drop_table("evidence_sync_state")
    op.create_table(
        "evidence_sync_state",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("cursor_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_evidence_sync_state_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "source", name=op.f("pk_evidence_sync_state")),
    )

    # --- drop the Gmail/IMAP source -------------------------------------------------------
    op.drop_table("email_messages")
    op.drop_table("mail_accounts")

    # --- Supabase lock-down for the new tables ---------------------------------------------
    for table in (*NEW_TABLES, "evidence_sync_state"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    tables = ", ".join((*NEW_TABLES, "evidence_sync_state"))
    sequences = ", ".join(NEW_SEQUENCES)
    for role in ("anon", "authenticated"):
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
    raise NotImplementedError(
        "0004 is irreversible (email tables were dropped). Restore a database backup."
    )
