from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.encryption import CredentialCipher
from app.db.models import MailAccount
from app.integrations.mail.gmail_oauth import GoogleOAuthClient, refresh_token_aad
from app.main import create_app
from tests.conftest import ADMIN_KEY, API_KEY, ENCRYPTION_KEY, FakeMailbox, make_settings
from tests.emails import make_email

AUTH = {"Authorization": f"Bearer {API_KEY}"}
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}


@pytest.fixture
def make_client(build: Callable[..., Any]) -> Callable[..., Any]:
    def _make(**overrides: Any) -> httpx.AsyncClient:
        container = build(**overrides)
        app = create_app(make_settings(**overrides), container=container)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        )

    return _make


@pytest.fixture
async def client(make_client: Callable[..., Any]) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client() as c:
        yield c


def body(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "paymentCode": "ABC123456789",
        "expectedAmount": "25.50",
        "asset": "USDT",
        "orderReference": "ORDER-123",
    }
    data.update(kw)
    return data


async def test_verify_success_response_shape(
    client: httpx.AsyncClient, mailbox: FakeMailbox
) -> None:
    mailbox.add(make_email(code="ABC123456789", amount="25.50"))
    r = await client.post("/v1/payments/verify", json=body(), headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["verified"] is True and data["status"] == "VERIFIED"
    assert data["expectedAmount"] == "25.50" and data["receivedAmount"] == "25.50"
    assert data["asset"] == "USDT" and data["orderReference"] == "ORDER-123"
    assert data["receivedAt"].endswith("Z") or data["receivedAt"].endswith("+00:00")
    assert "idempotent" not in data
    # No email content, no credentials.
    text = r.text.lower()
    for forbidden in ("subject", "binance.example", "abcdefghijklmnop", "message"):
        assert forbidden not in text

    again = await client.post("/v1/payments/verify", json=body(), headers=AUTH)
    assert again.json()["idempotent"] is True
    other = await client.post(
        "/v1/payments/verify", json=body(orderReference="ORDER-999"), headers=AUTH
    )
    assert other.json()["status"] == "ALREADY_CLAIMED" and other.json()["verified"] is False


async def test_amount_mismatch_formatting(client: httpx.AsyncClient, mailbox: FakeMailbox) -> None:
    mailbox.add(make_email(code="ABC123456789", amount="20"))
    data = (await client.post("/v1/payments/verify", json=body(), headers=AUTH)).json()
    assert data["status"] == "AMOUNT_MISMATCH"
    assert data["receivedAmount"] == "20.00" and data["expectedAmount"] == "25.50"


async def test_default_asset_is_usdt(client: httpx.AsyncClient, mailbox: FakeMailbox) -> None:
    mailbox.add(make_email(code="ABC123456789", amount="25.50"))
    payload = body()
    del payload["asset"]
    assert (await client.post("/v1/payments/verify", json=payload, headers=AUTH)).json()["verified"]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-key-wrong-key-wrong-key-wrong"},
        ADMIN,
        {"Authorization": API_KEY},
    ],
)
async def test_api_key_required(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    r = await client.post("/v1/payments/verify", json=body(), headers=headers)
    assert r.status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        body(expectedAmount=25.5),  # float -> rejected
        body(expectedAmount="25,50"),
        body(expectedAmount="-1"),
        body(expectedAmount="0"),
        body(expectedAmount="1e3"),
        body(expectedAmount="NaN"),
        body(paymentCode="x" * 129),
        body(paymentCode=""),
        body(orderReference="x" * 129),
        body(orderReference="ORDER 1; DROP TABLE"),
        body(asset="US DT"),
        body(maxAgeMinutes=0),
        body(maxAgeMinutes=100_000),
        body(unexpected="field"),
    ],
)
async def test_request_validation(client: httpx.AsyncClient, payload: dict[str, Any]) -> None:
    r = await client.post("/v1/payments/verify", json=payload, headers=AUTH)
    assert r.status_code == 422, r.text


async def test_invalid_code_format_returns_status(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/payments/verify", json=body(paymentCode="AB$12345"), headers=AUTH)
    assert r.status_code == 200
    assert r.json()["status"] == "INVALID_PAYMENT_CODE"


async def test_body_size_limit(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/v1/payments/verify",
        content=json.dumps(body(orderReference="x" * 50_000)),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert r.status_code == 413


async def test_rate_limit(make_client: Callable[..., Any]) -> None:
    async with make_client(rate_limit_verify_per_minute=2) as c:
        codes = [
            (await c.post("/v1/payments/verify", json=body(paymentCode="AB$"), headers=AUTH))
            for _ in range(3)
        ]
    assert [r.status_code for r in codes] == [200, 200, 429]
    assert "Retry-After" in codes[2].headers


async def test_security_headers_and_no_cors_by_default(client: httpx.AsyncClient) -> None:
    r = await client.get("/health", headers={"Origin": "https://evil.example"})
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["Cache-Control"] == "no-store"
    assert "access-control-allow-origin" not in r.headers
    assert "X-Request-ID" in r.headers


async def test_hsts_in_production(make_client: Callable[..., Any]) -> None:
    async with make_client(app_env="production") as c:
        r = await c.get("/health")
    assert "max-age" in r.headers["Strict-Transport-Security"]


async def test_health_and_ready(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).json() == {"status": "ok"}
    r = await client.get("/ready")
    assert r.status_code == 200, r.text
    assert r.json()["checks"]["database"] is True
    assert "abcdefghijklmnop" not in r.text and ENCRYPTION_KEY not in r.text


async def test_ready_fails_without_essential_config(make_client: Callable[..., Any]) -> None:
    async with make_client(binance_allowed_domains=[], api_keys=[]) as c:
        r = await c.get("/ready")
    assert r.status_code == 503
    checks = r.json()["checks"]
    assert checks["binance_senders_configured"] is False and checks["api_keys_configured"] is False


async def test_openapi_documents_examples(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    op = spec["paths"]["/v1/payments/verify"]["post"]
    assert "ALREADY_CLAIMED" in op["description"]
    assert "verified" in op["responses"]["200"]["content"]["application/json"]["examples"]


async def test_docs_disabled_in_production(make_client: Callable[..., Any]) -> None:
    async with make_client(app_env="production") as c:
        assert (await c.get("/openapi.json")).status_code == 404


# --- Admin ------------------------------------------------------------------------------


async def test_admin_requires_admin_key(client: httpx.AsyncClient) -> None:
    assert (await client.post("/v1/admin/mail/test", headers=AUTH)).status_code == 401
    assert (await client.post("/v1/admin/mail/sync")).status_code == 401


async def test_admin_mail_test_masks_account(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/admin/mail/test", headers=ADMIN)
    assert r.status_code == 200
    assert r.json() == {
        "connected": True,
        "provider": "gmail",
        "account": "me******@gmail.com",
        "authType": "app_password",
        "error": None,
    }
    assert "abcdefghijklmnop" not in r.text


async def test_admin_mail_test_reports_failure(
    client: httpx.AsyncClient, mailbox: FakeMailbox, failing_errors: dict[str, Exception]
) -> None:
    mailbox.fail = failing_errors["timeout"]
    data = (await client.post("/v1/admin/mail/test", headers=ADMIN)).json()
    assert data["connected"] is False and data["error"] == "Mail provider timeout"


async def test_admin_manual_sync(client: httpx.AsyncClient, mailbox: FakeMailbox) -> None:
    mailbox.add(make_email(code="ADMINSYNC1", message_id="<x1@binance.example>"))
    mailbox.add(make_email(code="ADMINSYNC1", message_id="<x2@binance.example>"))
    data = (await client.post("/v1/admin/mail/sync", headers=ADMIN)).json()
    assert data["messagesScanned"] == 2
    assert data["binanceMessages"] == 2
    assert data["paymentsImported"] == 1
    assert data["duplicates"] == 1


async def test_oauth_flow_stores_encrypted_refresh_token(
    make_client: Callable[..., Any],
    build: Callable[..., Any],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    def google(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["authorization_code"]
            assert form["code_verifier"][0]
            return httpx.Response(
                200,
                json={
                    "access_token": "ya29.access",
                    "expires_in": 3599,
                    "refresh_token": "1//refresh-secret",
                },
            )
        return httpx.Response(200, json={"email": "Owner@Gmail.com", "email_verified": True})

    overrides = {
        "google_client_id": "cid.apps.googleusercontent.com",
        "google_client_secret": SecretStr("client-secret-value"),
        "google_redirect_uri": "https://api.example.com/v1/admin/mail/oauth/callback",
    }
    container = build(**overrides)
    container.oauth_client = GoogleOAuthClient(
        client_id=overrides["google_client_id"],
        client_secret=overrides["google_client_secret"],
        redirect_uri=overrides["google_redirect_uri"],
        transport=httpx.MockTransport(google),
    )
    app = create_app(make_settings(**overrides), container=container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        r = await c.get("/v1/admin/mail/oauth/authorize", headers=ADMIN)
        url = urlparse(r.json()["authorizationUrl"])
        params = parse_qs(url.query)
        assert params["scope"] == ["https://mail.google.com/ openid email"]
        assert params["access_type"] == ["offline"]
        assert params["code_challenge_method"] == ["S256"]

        bad = await c.get("/v1/admin/mail/oauth/callback", params={"code": "c", "state": "bogus"})
        assert bad.status_code == 400

        cb = await c.get(
            "/v1/admin/mail/oauth/callback", params={"code": "c", "state": params["state"][0]}
        )
    assert cb.status_code == 200, cb.text
    assert cb.json() == {"connected": True, "provider": "gmail", "account": "ow***@gmail.com"}
    async with sessionmaker() as s:
        account = await s.scalar(select(MailAccount).where(MailAccount.email == "owner@gmail.com"))
    assert account is not None and account.auth_type == "oauth2"
    assert account.encrypted_credentials is not None
    assert "refresh-secret" not in account.encrypted_credentials
    decrypted = CredentialCipher(ENCRYPTION_KEY).decrypt_json(
        account.encrypted_credentials, refresh_token_aad("owner@gmail.com")
    )
    assert decrypted == {"refresh_token": "1//refresh-secret"}
