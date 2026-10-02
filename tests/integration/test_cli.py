from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import cli
from app.services.tenants import TenantService


def test_generate_keys(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["generate-admin-key"]) == 0
    admin = capsys.readouterr().out.strip()
    assert len(admin) >= 32
    assert cli.main(["generate-encryption-key"]) == 0
    from app.core.encryption import CredentialCipher

    CredentialCipher(capsys.readouterr().out.strip())  # valid 32-byte key


async def test_cli_service_flow_create_list_revoke(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    # Run the CLI coroutine directly against the test database.
    import argparse

    monkeypatch.setattr(cli, "get_sessionmaker", lambda: sessionmaker)

    class _Engine:
        async def dispose(self) -> None:
            return None

    monkeypatch.setattr(cli, "get_engine", _Engine)
    ns = argparse.Namespace(
        command="create-client", name="Ana", token_name="default", expires_in_days=None
    )
    assert await cli._run(ns) == 0
    out = capsys.readouterr().out
    token = out.strip().splitlines()[-1]
    assert token.startswith("bpv_")
    context = await TenantService(sessionmaker).authenticate(token)
    assert context is not None and context.tenant_name == "Ana"

    assert await cli._run(argparse.Namespace(command="list-clients")) == 0
    assert "Ana" in capsys.readouterr().out
    assert (
        await cli._run(argparse.Namespace(command="revoke-token", token_id=context.token_id)) == 0
    )
    assert await TenantService(sessionmaker).authenticate(token) is None
    assert await cli._run(argparse.Namespace(command="revoke-token", token_id=999)) == 1
