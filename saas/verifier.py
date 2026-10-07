"""HTTP client for the Binance Pay verification API that this site sells.

Two credentials are used:

* the **admin key** (``/v1/admin/*``) to create a client per buyer, issue/revoke its tokens
  and enable/disable it when the subscription expires or is renewed;
* the **site's own client token** to verify the buyers' subscription payments, which are
  received in the owner's Binance account.

Secrets are never logged or included in exception messages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)


class VerifierError(Exception):
    """The API answered something unexpected (or could not be reached)."""


class ClientNameTakenError(VerifierError):
    pass


@dataclass(frozen=True)
class IssuedToken:
    client_id: int
    token_id: int
    token: str  # plaintext: shown to the buyer once, never stored
    prefix: str


@dataclass(frozen=True)
class Verification:
    status: str
    verified: bool
    retryable: bool
    idempotent: bool
    received_amount: str | None
    received_asset: str | None


@dataclass(frozen=True)
class CredentialsResult:
    ok: bool
    status_code: int
    detail: str | None
    problems: list[str]


class Verifier(Protocol):
    async def verify(
        self, payment_code: str, amount: str, order_reference: str, max_age: int | None
    ) -> Verification: ...

    async def create_client(self, name: str) -> IssuedToken: ...

    async def find_client_id(self, name: str) -> int | None: ...

    async def issue_token(self, client_id: int) -> IssuedToken: ...

    async def active_token_ids(self, client_id: int) -> list[int]: ...

    async def revoke_token(self, token_id: int) -> None: ...

    async def set_enabled(self, client_id: int, enabled: bool) -> None: ...

    async def put_binance_credentials(
        self, token: str, api_key: str, api_secret: str
    ) -> CredentialsResult: ...

    async def aclose(self) -> None: ...


TOKEN_NAME = "saas"  # noqa: S105 - token label, not a secret


class HttpVerifier:
    def __init__(
        self,
        base_url: str,
        admin_key: str,
        site_token: str,
        *,
        timeout: float = 90,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._admin = {"Authorization": f"Bearer {admin_key}"}
        self._site = {"Authorization": f"Bearer {site_token}"}
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=timeout, transport=transport, follow_redirects=False
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(
        self, method: str, path: str, headers: dict[str, str], json: Any = None
    ) -> httpx.Response:
        try:
            return await self._http.request(method, path, headers=headers, json=json)
        except httpx.HTTPError as exc:
            logger.warning(
                "verifier_unreachable", extra={"path": path, "error": type(exc).__name__}
            )
            raise VerifierError("Verification API unreachable") from None

    @staticmethod
    def _unexpected(response: httpx.Response, what: str) -> VerifierError:
        logger.error(
            "verifier_unexpected_status", extra={"what": what, "status": response.status_code}
        )
        return VerifierError(f"{what}: HTTP {response.status_code}")

    # --- payments (site token) ------------------------------------------------------------
    async def verify(
        self, payment_code: str, amount: str, order_reference: str, max_age: int | None
    ) -> Verification:
        body: dict[str, Any] = {
            "paymentCode": payment_code,
            "expectedAmount": amount,
            "asset": "USDT",
            "orderReference": order_reference,
        }
        if max_age is not None:
            body["maxAgeMinutes"] = max_age
        r = await self._call("POST", "/v1/payments/verify", self._site, body)
        if r.status_code != 200:
            raise self._unexpected(r, "verify")
        data = r.json()
        return Verification(
            status=str(data["status"]),
            verified=bool(data["verified"]),
            retryable=bool(data.get("retryable", False)),
            idempotent=bool(data.get("idempotent") or False),
            received_amount=data.get("receivedAmount"),
            received_asset=data.get("receivedAsset"),
        )

    # --- clients and tokens (admin key) ---------------------------------------------------
    async def create_client(self, name: str) -> IssuedToken:
        r = await self._call(
            "POST", "/v1/admin/clients", self._admin, {"name": name, "tokenName": TOKEN_NAME}
        )
        if r.status_code == 409:
            raise ClientNameTakenError(name)
        if r.status_code != 201:
            raise self._unexpected(r, "create_client")
        data = r.json()
        token = data["token"]
        return IssuedToken(
            client_id=int(data["client"]["id"]),
            token_id=int(token["id"]),
            token=str(token["token"]),
            prefix=str(token["prefix"]),
        )

    async def find_client_id(self, name: str) -> int | None:
        r = await self._call("GET", "/v1/admin/clients", self._admin)
        if r.status_code != 200:
            raise self._unexpected(r, "list_clients")
        for client in r.json():
            if client["name"] == name:
                return int(client["id"])
        return None

    async def issue_token(self, client_id: int) -> IssuedToken:
        r = await self._call(
            "POST", f"/v1/admin/clients/{client_id}/tokens", self._admin, {"name": TOKEN_NAME}
        )
        if r.status_code != 201:
            raise self._unexpected(r, "issue_token")
        data = r.json()
        return IssuedToken(
            client_id=client_id,
            token_id=int(data["id"]),
            token=str(data["token"]),
            prefix=str(data["prefix"]),
        )

    async def active_token_ids(self, client_id: int) -> list[int]:
        r = await self._call("GET", f"/v1/admin/clients/{client_id}/tokens", self._admin)
        if r.status_code != 200:
            raise self._unexpected(r, "list_tokens")
        return [int(t["id"]) for t in r.json() if t.get("active")]

    async def revoke_token(self, token_id: int) -> None:
        r = await self._call("DELETE", f"/v1/admin/tokens/{token_id}", self._admin)
        if r.status_code not in (200, 404):  # 404: already revoked
            raise self._unexpected(r, "revoke_token")

    async def set_enabled(self, client_id: int, enabled: bool) -> None:
        r = await self._call(
            "PATCH", f"/v1/admin/clients/{client_id}", self._admin, {"enabled": enabled}
        )
        if r.status_code != 200:
            raise self._unexpected(r, "set_enabled")

    # --- on behalf of a customer (their own token) ----------------------------------------
    async def put_binance_credentials(
        self, token: str, api_key: str, api_secret: str
    ) -> CredentialsResult:
        r = await self._call(
            "PUT",
            "/v1/me/binance-credentials",
            {"Authorization": f"Bearer {token}"},
            {"apiKey": api_key, "apiSecret": api_secret},
        )
        if r.status_code == 200:
            return CredentialsResult(True, 200, None, [])
        try:
            data = r.json()
        except ValueError:
            data = {}
        detail = data.get("detail") if isinstance(data, dict) else None
        problems = data.get("problems") if isinstance(data, dict) else None
        return CredentialsResult(
            False,
            r.status_code,
            detail if isinstance(detail, str) else None,
            [str(p) for p in problems] if isinstance(problems, list) else [],
        )
