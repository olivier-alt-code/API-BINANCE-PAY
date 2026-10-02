from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.core.exceptions import ConfigurationError
from app.db.connection import resolve_database
from tests.conftest import make_settings

REF = "abcdefghijklmnopqrst"
DIRECT = f"postgresql://postgres:s3cr3t-pass@db.{REF}.supabase.co:5432/postgres"
SESSION = (
    f"postgresql://postgres.{REF}:s3cr3t-pass@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
)
TRANSACTION = SESSION.replace(":5432/", ":6543/")


@pytest.mark.parametrize("raw", [DIRECT, DIRECT.replace("postgresql://", "postgres://")])
def test_supabase_direct_connection(raw: str) -> None:
    db = resolve_database(raw)
    assert db.url.drivername == "postgresql+psycopg"
    assert db.is_supabase and not db.transaction_pooler
    assert db.url.query["sslmode"] == "require"
    assert "statement_timeout" in db.connect_args["options"]


def test_supabase_session_pooler() -> None:
    db = resolve_database(SESSION)
    assert db.is_supabase and not db.transaction_pooler
    assert db.url.username == f"postgres.{REF}"


def test_supabase_transaction_pooler_disables_prepared_statements() -> None:
    db = resolve_database(TRANSACTION)
    assert db.transaction_pooler
    assert db.connect_args["prepare_threshold"] is None
    assert "options" not in db.connect_args  # startup options unsupported by Supavisor


def test_explicit_sslmode_and_pooler_mode_win() -> None:
    db = resolve_database(DIRECT + "?sslmode=verify-full", pooler_mode="transaction")
    assert db.url.query["sslmode"] == "verify-full" and db.transaction_pooler


def test_local_postgres_unchanged() -> None:
    db = resolve_database("postgresql+psycopg://u:p@localhost:5432/db")
    assert not db.is_supabase and not db.transaction_pooler
    assert "sslmode" not in db.url.query


@pytest.mark.parametrize("raw", ["mysql://u:p@h/db", "not a url"])
def test_invalid_urls_rejected_without_leaking(raw: str) -> None:
    with pytest.raises(ConfigurationError) as info:
        resolve_database(raw)
    assert "p@" not in str(info.value)


def test_settings_expose_resolved_database_and_redact_password() -> None:
    s = make_settings(database_url=SecretStr(TRANSACTION))
    assert s.database.transaction_pooler
    assert "s3cr3t-pass" in s.secret_values()
