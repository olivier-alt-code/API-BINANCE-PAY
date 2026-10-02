"""HTTP API: owner admin, client tokens, Binance credentials and API hardening."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.encryption import CredentialCipher
from app.db.models import ApiToken, BinanceCredential
from app.main import create_app
from app.services.binance_credentials import credentials_aad
from tests.conftest import (
    ADMIN_KEY,
    ENCRYPTION_KEY,
    READ_ONLY_KEY,
    READ_ONLY_SECRET,
    FakeBinance,
    make_settings,
)

ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}
TX = "P_A99TESTPAYX71116"


@pytest.fixture
def make_http(build: Callable[..., Any]) -> Callable[..., Any]:
    def _make(**overrides: Any) -> httpx.AsyncClient:
        app = create_app(make_settings(**overrides), container=build(**overrides))
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        )

    return _make


@pytest.fixture
async def http(make_http: Callable[..., Any]) -> AsyncIterator[httpx.AsyncClient]:
    async with make_http() as c:
        yield c


async def create_client(http: httpx.AsyncClient, name: str = "olivier") -> dict[str, Any]:
    r = await http.post("/v1/admin/clients", json={"name": name}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def register_key(
    http: httpx.AsyncClient, token: str, key: str = READ_ONLY_KEY, secret: str = READ_ONLY_SECRET
) -> httpx.Response:
    return await http.put(
        "/v1/me/binance-credentials",
        json={"apiKey": key, "apiSecret": secret},
        headers=bearer(token),
    )


def verify_body(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "paymentCode": TX,
        "expectedAmount": "98.814",
        "asset": "USDT",
        "orderReference": "ORDER-123",
    }
    data.update(kw)
    return data


# --- Owner: clients and tokens -------------------------------------------------------------


async def test_owner_creates_client_and_token_is_shown_once(
    http: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await create_client(http, "ana")
    token = created["token"]["token"]
    assert token.startswith("bpv_") and len(token) >= 40
    assert created["client"]["name"] == "ana"
    # Only the hash is stored; listings never include the token.
    async with sessionmaker() as s:
        row = await s.scalar(select(ApiToken))
    assert row is not None and token not in (row.token_hash, row.prefix)
    listing = await http.get(f"/v1/admin/clients/{created['client']['id']}/tokens", headers=ADMIN)
    assert token not in listing.text and listing.json()[0]["active"] is True

    me = await http.get("/v1/me", headers=bearer(token))
    assert me.json()["clientName"] == "ana"


async def test_duplicate_client_name_conflicts(http: httpx.AsyncClient) -> None:
    await create_client(http, "ana")
    r = await http.post("/v1/admin/clients", json={"name": "ana"}, headers=ADMIN)
    assert r.status_code == 409


async def test_extra_tokens_revocation_and_disable(http: httpx.AsyncClient) -> None:
    created = await create_client(http, "ana")
    client_id = created["client"]["id"]
    first = created["token"]["token"]
    r = await http.post(
        f"/v1/admin/clients/{client_id}/tokens", json={"name": "tienda"}, headers=ADMIN
    )
    second = r.json()
    assert (await http.get("/v1/me", headers=bearer(second["token"]))).status_code == 200

    revoke = await http.delete(f"/v1/admin/tokens/{second['id']}", headers=ADMIN)
    assert revoke.json() == {"revoked": True}
    assert (await http.get("/v1/me", headers=bearer(second["token"]))).status_code == 401
    assert (await http.get("/v1/me", headers=bearer(first))).status_code == 200

    disabled = await http.patch(
        f"/v1/admin/clients/{client_id}", json={"enabled": False}, headers=ADMIN
    )
    assert disabled.json()["enabled"] is False
    assert (await http.get("/v1/me", headers=bearer(first))).status_code == 401

    clients = (await http.get("/v1/admin/clients", headers=ADMIN)).json()
    assert clients[0]["enabled"] is False and clients[0]["activeTokens"] == 1


async def test_expired_token_is_rejected(
    http: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await create_client(http)
    r = await http.post(
        f"/v1/admin/clients/{created['client']['id']}/tokens",
        json={"name": "temporal", "expiresInDays": 1},
        headers=ADMIN,
    )
    token = r.json()["token"]
    from datetime import UTC, datetime, timedelta

    async with sessionmaker() as s, s.begin():
        row = await s.get(ApiToken, r.json()["id"])
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (await http.get("/v1/me", headers=bearer(token))).status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer bpv_doesnotexist"},
        {"Authorization": "Bearer not-even-a-token"},
        {"Authorization": f"Bearer {ADMIN_KEY}"},  # admin key is not a client token
    ],
)
async def test_client_endpoints_require_a_valid_token(
    http: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    assert (
        await http.post("/v1/payments/verify", json=verify_body(), headers=headers)
    ).status_code == 401
    assert (await http.get("/v1/me/binance-credentials", headers=headers)).status_code == 401


async def test_admin_endpoints_reject_client_tokens(http: httpx.AsyncClient) -> None:
    token = (await create_client(http))["token"]["token"]
    assert (await http.get("/v1/admin/clients", headers=bearer(token))).status_code == 401
    assert (await http.get("/v1/admin/clients")).status_code == 401


# --- Client: Binance credentials --------------------------------------------------------


async def test_register_read_only_key_stored_encrypted_never_returned(
    http: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await create_client(http)
    token = created["token"]["token"]
    r = await register_key(http, token)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True and body["apiKeyHint"] == "…" + READ_ONLY_KEY[-4:]
    for secret in (READ_ONLY_KEY, READ_ONLY_SECRET):
        assert secret not in r.text

    status = await http.get("/v1/me/binance-credentials", headers=bearer(token))
    assert status.json()["configured"] is True
    assert READ_ONLY_SECRET not in status.text and READ_ONLY_KEY not in status.text

    async with sessionmaker() as s:
        row = await s.get(BinanceCredential, created["client"]["id"])
    assert row is not None
    assert READ_ONLY_SECRET not in row.encrypted and READ_ONLY_KEY not in row.encrypted
    decrypted = CredentialCipher(ENCRYPTION_KEY).decrypt_json(
        row.encrypted, credentials_aad(created["client"]["id"])
    )
    assert decrypted == {"api_key": READ_ONLY_KEY, "api_secret": READ_ONLY_SECRET}

    assert (
        await http.delete("/v1/me/binance-credentials", headers=bearer(token))
    ).status_code == 204
    assert (await http.get("/v1/me/binance-credentials", headers=bearer(token))).json() == {
        "configured": False,
        "apiKeyHint": None,
        "ipRestricted": None,
        "verifiedAt": None,
        "permissions": None,
    }


@pytest.mark.parametrize(
    "permission",
    [
        "enableWithdrawals",
        "enableSpotAndMarginTrading",
        "enableInternalTransfer",
        "permitsUniversalTransfer",
        "enableFutures",
        "enableMargin",
    ],
)
async def test_keys_with_dangerous_permissions_are_rejected_and_not_stored(
    http: httpx.AsyncClient,
    binance: FakeBinance,
    sessionmaker: async_sessionmaker[AsyncSession],
    permission: str,
) -> None:
    token = (await create_client(http))["token"]["token"]
    binance.main.permissions[permission] = True
    r = await register_key(http, token)
    assert r.status_code == 422
    assert f"{permission} must be OFF" in r.json()["problems"]
    async with sessionmaker() as s:
        assert await s.scalar(select(BinanceCredential)) is None


async def test_key_without_reading_permission_rejected(
    http: httpx.AsyncClient, binance: FakeBinance
) -> None:
    token = (await create_client(http))["token"]["token"]
    binance.main.permissions["enableReading"] = False
    r = await register_key(http, token)
    assert r.status_code == 422 and "enableReading must be ON" in r.json()["problems"]


async def test_invalid_key_rejected_without_echoing_secret(http: httpx.AsyncClient) -> None:
    token = (await create_client(http))["token"]["token"]
    secret = "S" * 64
    r = await register_key(http, token, key="K" * 64, secret=secret)
    assert r.status_code == 422 and secret not in r.text


async def test_validation_errors_never_echo_submitted_secrets(http: httpx.AsyncClient) -> None:
    token = (await create_client(http))["token"]["token"]
    secret = "short-secret-xyz"  # too short -> 422 from validation
    r = await http.put(
        "/v1/me/binance-credentials",
        json={"apiKey": READ_ONLY_KEY, "apiSecret": secret, "extra": "x"},
        headers=bearer(token),
    )
    assert r.status_code == 422
    assert secret not in r.text and READ_ONLY_KEY not in r.text


async def test_manual_sync_and_verify_over_http(
    http: httpx.AsyncClient, binance: FakeBinance
) -> None:
    token = (await create_client(http))["token"]["token"]
    not_configured = await http.post(
        "/v1/payments/verify", json=verify_body(), headers=bearer(token)
    )
    assert not_configured.json()["status"] == "BINANCE_NOT_CONFIGURED"

    await register_key(http, token)
    binance.main.add(TX, "98.814")
    sync = await http.post("/v1/me/binance/sync", headers=bearer(token))
    assert sync.json()["paymentsImported"] == 1
    r = await http.post("/v1/payments/verify", json=verify_body(), headers=bearer(token))
    data = r.json()
    assert data["verified"] is True and data["status"] == "VERIFIED"
    assert data["receivedAmount"] == "98.814" and data["orderReference"] == "ORDER-123"
    again = await http.post("/v1/payments/verify", json=verify_body(), headers=bearer(token))
    assert again.json()["idempotent"] is True
    replay = await http.post(
        "/v1/payments/verify", json=verify_body(orderReference="ORDER-999"), headers=bearer(token)
    )
    assert replay.json()["status"] == "ALREADY_CLAIMED"


async def test_amount_mismatch_formatting(http: httpx.AsyncClient, binance: FakeBinance) -> None:
    token = (await create_client(http))["token"]["token"]
    await register_key(http, token)
    binance.main.add(TX, "20")
    r = await http.post(
        "/v1/payments/verify", json=verify_body(expectedAmount="25.50"), headers=bearer(token)
    )
    data = r.json()
    assert data["status"] == "AMOUNT_MISMATCH"
    assert data["receivedAmount"] == "20.00" and data["expectedAmount"] == "25.50"


# --- Hardening -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        verify_body(expectedAmount=25.5),  # float -> rejected
        verify_body(expectedAmount="25,50"),
        verify_body(expectedAmount="-1"),
        verify_body(expectedAmount="0"),
        verify_body(expectedAmount="1e3"),
        verify_body(expectedAmount="NaN"),
        verify_body(paymentCode="x" * 129),
        verify_body(paymentCode=""),
        verify_body(orderReference="x" * 129),
        verify_body(orderReference="ORDER 1; DROP TABLE"),
        verify_body(asset="US DT"),
        verify_body(maxAgeMinutes=0),
        verify_body(maxAgeMinutes=100_000),
        verify_body(unexpected="field"),
    ],
)
async def test_request_validation(http: httpx.AsyncClient, payload: dict[str, Any]) -> None:
    token = (await create_client(http))["token"]["token"]
    r = await http.post("/v1/payments/verify", json=payload, headers=bearer(token))
    assert r.status_code == 422, r.text


async def test_body_size_limit(http: httpx.AsyncClient) -> None:
    token = (await create_client(http))["token"]["token"]
    r = await http.post(
        "/v1/payments/verify",
        content=json.dumps(verify_body(orderReference="x" * 50_000)),
        headers={**bearer(token), "Content-Type": "application/json"},
    )
    assert r.status_code == 413


async def test_rate_limit_per_token(make_http: Callable[..., Any]) -> None:
    async with make_http(rate_limit_verify_per_minute=2) as http:
        token = (await create_client(http))["token"]["token"]
        other = (await create_client(http, "otro"))["token"]["token"]
        codes = [
            (
                await http.post(
                    "/v1/payments/verify",
                    json=verify_body(paymentCode="AB$"),
                    headers=bearer(token),
                )
            ).status_code
            for _ in range(3)
        ]
        other_code = (
            await http.post(
                "/v1/payments/verify", json=verify_body(paymentCode="AB$"), headers=bearer(other)
            )
        ).status_code
    assert codes == [200, 200, 429]
    assert other_code == 200  # quotas are per token


async def test_security_headers_and_no_cors_by_default(http: httpx.AsyncClient) -> None:
    r = await http.get("/health", headers={"Origin": "https://evil.example"})
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["Cache-Control"] == "no-store"
    assert "access-control-allow-origin" not in r.headers
    assert "X-Request-ID" in r.headers


async def test_hsts_and_docs_in_production(make_http: Callable[..., Any]) -> None:
    async with make_http(app_env="production") as http:
        r = await http.get("/health")
        assert "max-age" in r.headers["Strict-Transport-Security"]
        assert (await http.get("/openapi.json")).status_code == 404


async def test_health_and_ready(http: httpx.AsyncClient) -> None:
    assert (await http.get("/health")).json() == {"status": "ok"}
    r = await http.get("/ready")
    assert r.status_code == 200, r.text
    assert r.json()["checks"] == {
        "database": True,
        "admin_key_configured": True,
        "encryption_key": True,
    }
    assert ENCRYPTION_KEY not in r.text and ADMIN_KEY not in r.text


async def test_ready_fails_without_encryption_key(make_http: Callable[..., Any]) -> None:
    async with make_http(credentials_encryption_key=None) as http:
        r = await http.get("/ready")
    assert r.status_code == 503 and r.json()["checks"]["encryption_key"] is False


async def test_openapi_documents_statuses(http: httpx.AsyncClient) -> None:
    spec = (await http.get("/openapi.json")).json()
    op = spec["paths"]["/v1/payments/verify"]["post"]
    assert "ALREADY_CLAIMED" in op["description"] and "BINANCE_NOT_CONFIGURED" in op["description"]
    assert "/v1/admin/clients" in spec["paths"] and "/v1/me/binance-credentials" in spec["paths"]
