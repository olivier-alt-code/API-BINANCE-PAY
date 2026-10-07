from __future__ import annotations

import json

import httpx
import pytest

from saas.verifier import ClientNameTakenError, HttpVerifier, VerifierError


def make(handler) -> HttpVerifier:
    return HttpVerifier("https://api.test", "ADMIN", "SITE", transport=httpx.MockTransport(handler))


async def test_verify_uses_site_token_and_string_amount() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "verified": True,
                "status": "VERIFIED",
                "retryable": False,
                "idempotent": None,
                "receivedAmount": "5",
            },
        )

    v = make(handler)
    result = await v.verify("457", "5", "saas-abc", None)
    assert result.verified and result.received_amount == "5"
    assert seen["auth"] == "Bearer SITE"
    assert seen["body"] == {
        "paymentCode": "457",
        "expectedAmount": "5",
        "asset": "USDT",
        "orderReference": "saas-abc",
    }
    await v.aclose()


async def test_admin_calls_use_admin_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer ADMIN"
        if request.method == "POST" and request.url.path == "/v1/admin/clients":
            return httpx.Response(409, json={"detail": "taken"})
        if request.url.path == "/v1/admin/clients":
            return httpx.Response(200, json=[{"id": 7, "name": "saas-x"}])
        if request.url.path == "/v1/admin/clients/7/tokens" and request.method == "GET":
            return httpx.Response(200, json=[{"id": 1, "active": True}, {"id": 2, "active": False}])
        if request.url.path == "/v1/admin/tokens/1":
            return httpx.Response(404)
        return httpx.Response(500)

    v = make(handler)
    with pytest.raises(ClientNameTakenError):
        await v.create_client("saas-x")
    assert await v.find_client_id("saas-x") == 7
    assert await v.active_token_ids(7) == [1]
    await v.revoke_token(1)  # 404 = already revoked: fine
    with pytest.raises(VerifierError):
        await v.set_enabled(7, False)


async def test_network_errors_become_verifier_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(VerifierError):
        await make(handler).verify("457", "5", "saas-abc", None)
