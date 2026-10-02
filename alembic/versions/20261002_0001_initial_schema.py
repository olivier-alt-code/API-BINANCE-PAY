"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-02 03:57:28.695567
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    
    op.create_table('mail_accounts',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('auth_type', sa.String(length=32), nullable=False),
    sa.Column('encrypted_credentials', sa.Text(), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('mailbox', sa.String(length=255), nullable=False),
    sa.Column('imap_uidvalidity', sa.BigInteger(), nullable=True),
    sa.Column('last_imap_uid', sa.BigInteger(), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_sync_error', sa.String(length=255), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("auth_type IN ('app_password', 'oauth2', 'account_password')", name=op.f('ck_mail_accounts_auth_type')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_mail_accounts')),
    sa.UniqueConstraint('email', name=op.f('uq_mail_accounts_email'))
    )
    op.create_table('rate_limit_counters',
    sa.Column('key', sa.String(length=128), nullable=False),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('key', 'window_start', name=op.f('pk_rate_limit_counters'))
    )
    op.create_table('email_messages',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('mail_account_id', sa.BigInteger(), nullable=False),
    sa.Column('imap_uidvalidity', sa.BigInteger(), nullable=False),
    sa.Column('imap_uid', sa.BigInteger(), nullable=False),
    sa.Column('message_id', sa.String(length=998), nullable=True),
    sa.Column('sender', sa.String(length=320), nullable=True),
    sa.Column('subject', sa.String(length=500), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('trusted', sa.Boolean(), nullable=False),
    sa.Column('trust_details', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('parse_status', sa.String(length=32), nullable=False),
    sa.Column('template', sa.String(length=64), nullable=True),
    sa.Column('body_hash', sa.String(length=64), nullable=False),
    sa.Column('raw_encrypted', sa.Text(), nullable=True),
    sa.Column('processed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['mail_account_id'], ['mail_accounts.id'], name=op.f('fk_email_messages_mail_account_id_mail_accounts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_email_messages')),
    sa.UniqueConstraint('mail_account_id', 'imap_uidvalidity', 'imap_uid', name=op.f('uq_email_messages_mail_account_id_imap_uidvalidity_imap_uid')),
    sa.UniqueConstraint('mail_account_id', 'message_id', name=op.f('uq_email_messages_mail_account_id_message_id'))
    )
    op.create_index('ix_email_messages_received_at', 'email_messages', ['received_at'], unique=False)
    op.create_table('payments',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('external_id', sa.String(length=998), nullable=False),
    sa.Column('email_message_id', sa.BigInteger(), nullable=True),
    sa.Column('payment_code', sa.String(length=128), nullable=False),
    sa.Column('amount', sa.Numeric(precision=38, scale=18), nullable=False),
    sa.Column('asset', sa.String(length=16), nullable=False),
    sa.Column('payment_status', sa.String(length=32), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('trusted', sa.Boolean(), nullable=False),
    sa.Column('ambiguous', sa.Boolean(), nullable=False),
    sa.Column('template', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount > 0', name=op.f('ck_payments_amount_positive')),
    sa.ForeignKeyConstraint(['email_message_id'], ['email_messages.id'], name=op.f('fk_payments_email_message_id_email_messages'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payments')),
    sa.UniqueConstraint('source', 'external_id', name=op.f('uq_payments_source_external_id'))
    )
    op.create_index('ix_payments_email_message_id', 'payments', ['email_message_id'], unique=False)
    op.create_index('ix_payments_payment_code', 'payments', ['payment_code'], unique=False)
    op.create_index('ix_payments_received_at', 'payments', ['received_at'], unique=False)
    op.create_index('uq_payments_trusted_payment_code', 'payments', ['payment_code'], unique=True, postgresql_where=sa.text('trusted'))
    op.create_table('payment_claims',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('payment_id', sa.BigInteger(), nullable=False),
    sa.Column('order_reference', sa.String(length=128), nullable=True),
    sa.Column('expected_amount', sa.Numeric(precision=38, scale=18), nullable=False),
    sa.Column('asset', sa.String(length=16), nullable=False),
    sa.Column('client_id', sa.String(length=64), nullable=True),
    sa.Column('claimed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['payment_id'], ['payments.id'], name=op.f('fk_payment_claims_payment_id_payments'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payment_claims')),
    sa.UniqueConstraint('payment_id', name=op.f('uq_payment_claims_payment_id'))
    )
    op.create_index('ix_payment_claims_order_reference', 'payment_claims', ['order_reference'], unique=False)



def downgrade() -> None:
    
    op.drop_index('ix_payment_claims_order_reference', table_name='payment_claims')
    op.drop_table('payment_claims')
    op.drop_index('uq_payments_trusted_payment_code', table_name='payments', postgresql_where=sa.text('trusted'))
    op.drop_index('ix_payments_received_at', table_name='payments')
    op.drop_index('ix_payments_payment_code', table_name='payments')
    op.drop_index('ix_payments_email_message_id', table_name='payments')
    op.drop_table('payments')
    op.drop_index('ix_email_messages_received_at', table_name='email_messages')
    op.drop_table('email_messages')
    op.drop_table('rate_limit_counters')
    op.drop_table('mail_accounts')

