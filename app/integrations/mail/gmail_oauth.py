"""Google OAuth2 for Gmail IMAP (XOAUTH2).

Flow (no server-side session, no filesystem):

1. ``GET /v1/admin/mail/oauth/authorize`` builds the Google consent URL. The ``state``
   parameter is an AES-GCM *encrypted* token containing the PKCE verifier and an expiry,
   so the callback can be validated statelessly by any replica.
2. Google redirects to ``GOOGLE_REDIRECT_URI`` (``/v1/admin/mail/oauth/callback``) with a
   ``code``; we exchange it for tokens, read the account email from the OpenID userinfo
   endpoint and store the *refresh token encrypted* in ``mail_accounts``.
3. At sync time, :class:`OAuth2CredentialsProvider` exchanges the refresh token for a
   short-lived access token (cached in memory only) and IMAP authenticates with XOAUTH2.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from app.core.encryption import CredentialCipher
from app.core.exceptions import ConfigurationError, OAuthError
from app.integrations.mail.base import ImapCredentials, XOAuth2Credentials

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
# Full IMAP access requires the https://mail.google.com/ scope.
GMAIL_IMAP_SCOPES = ("https://mail.google.com/", "openid", "email")

_STATE_AAD = "google-oauth-state"


def refresh_token_aad(email: str) -> str:
    """Associated data binding an encrypted refresh token to its mail account."""
    return f"mail_account:{email.lower()}:oauth2"


@dataclass(frozen=True)
class OAuthTokens:
    access_token: SecretStr
    expires_in: int
    refresh_token: SecretStr | None


@dataclass(frozen=True)
class OAuthState:
    code_verifier: str
    expires_at: float


class OAuthStateCodec:
    def __init__(self, cipher: CredentialCipher, ttl_seconds: int) -> None:
        self._cipher = cipher
        self._ttl = ttl_seconds

    def issue(self) -> tuple[str, str]:
        """Return (state, code_challenge)."""
        verifier = base64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("=")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        state = self._cipher.encrypt_json(
            {"v": verifier, "exp": time.time() + self._ttl, "n": os.urandom(8).hex()}, _STATE_AAD
        )
        return state, challenge

    def verify(self, state: str) -> OAuthState:
        try:
            data = self._cipher.decrypt_json(state, _STATE_AAD)
        except Exception as exc:
            raise OAuthError("Invalid OAuth state") from exc
        if float(data.get("exp", 0)) < time.time():
            raise OAuthError("OAuth state expired")
        return OAuthState(code_verifier=str(data["v"]), expires_at=float(data["exp"]))


class GoogleOAuthClient:
    def __init__(
        self,
        *,
        client_id: str | None,
        client_secret: SecretStr | None,
        redirect_uri: str | None,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._timeout = timeout_seconds
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret and self._redirect_uri)

    def _require(self) -> tuple[str, str, str]:
        if not (self._client_id and self._client_secret and self._redirect_uri):
            raise ConfigurationError(
                "GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI are required"
            )
        return self._client_id, self._client_secret.get_secret_value(), self._redirect_uri

    def authorization_url(self, state: str, code_challenge: str, login_hint: str | None) -> str:
        client_id, _, redirect_uri = self._require()
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(GMAIL_IMAP_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if login_hint:
            params["login_hint"] = login_hint
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)

    async def _token_request(self, data: dict[str, str]) -> OAuthTokens:
        client_id, client_secret, _ = self._require()
        try:
            async with self._http() as client:
                response = await client.post(
                    GOOGLE_TOKEN_URL,
                    data={**data, "client_id": client_id, "client_secret": client_secret},
                )
        except httpx.HTTPError as exc:
            raise OAuthError("Google token endpoint unreachable") from exc
        if response.status_code != 200:
            # Do not include the response body: it may echo submitted values.
            raise OAuthError(f"Google token endpoint returned HTTP {response.status_code}")
        try:
            body = response.json()
            access = str(body["access_token"])
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise OAuthError("Unexpected token response") from exc
        refresh = body.get("refresh_token")
        return OAuthTokens(
            access_token=SecretStr(access),
            expires_in=int(body.get("expires_in", 3600)),
            refresh_token=SecretStr(str(refresh)) if refresh else None,
        )

    async def exchange_code(self, code: str, code_verifier: str) -> OAuthTokens:
        _, _, redirect_uri = self._require()
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            }
        )

    async def refresh(self, refresh_token: SecretStr) -> OAuthTokens:
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token.get_secret_value()}
        )

    async def fetch_email(self, access_token: SecretStr) -> str:
        try:
            async with self._http() as client:
                response = await client.get(
                    GOOGLE_USERINFO_URL,
                    headers={"Authorization": f"Bearer {access_token.get_secret_value()}"},
                )
        except httpx.HTTPError as exc:
            raise OAuthError("Google userinfo endpoint unreachable") from exc
        if response.status_code != 200:
            raise OAuthError(f"Google userinfo returned HTTP {response.status_code}")
        body = response.json()
        if not body.get("email") or not body.get("email_verified", False):
            raise OAuthError("Google account email is missing or not verified")
        return str(body["email"]).lower()


class OAuth2CredentialsProvider:
    """Produces XOAUTH2 credentials from an (encrypted-at-rest) refresh token.

    The access token is cached in process memory only; it is a performance cache, not
    state: any replica can mint its own from the refresh token stored in PostgreSQL.
    """

    _cache: dict[str, tuple[SecretStr, float]] = {}  # noqa: RUF012 - process-wide cache

    def __init__(self, *, email: str, refresh_token: SecretStr, client: GoogleOAuthClient) -> None:
        self._email = email
        self._refresh_token = refresh_token
        self._client = client

    def _cache_key(self) -> str:
        digest = hashlib.sha256(self._refresh_token.get_secret_value().encode()).hexdigest()
        return f"{self._email}:{digest[:16]}"

    async def get_credentials(self) -> ImapCredentials:
        cached = self._cache.get(self._cache_key())
        if cached and cached[1] - 60 > time.time():
            return XOAuth2Credentials(self._email, cached[0])
        tokens = await self._client.refresh(self._refresh_token)
        self._cache[self._cache_key()] = (tokens.access_token, time.time() + tokens.expires_in)
        return XOAuth2Credentials(self._email, tokens.access_token)

    async def invalidate(self) -> None:
        self._cache.pop(self._cache_key(), None)
