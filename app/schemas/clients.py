from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic.alias_generators import to_camel


class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


# --- admin: clients and tokens -----------------------------------------------------------


class CreateClientRequest(_Camel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[\w .@+-]+$", examples=["Olivier"])
    token_name: str = Field(default="default", min_length=1, max_length=100)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class CreateTokenRequest(_Camel):
    name: str = Field(min_length=1, max_length=100, examples=["servidor-tienda"])
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class UpdateClientRequest(_Camel):
    enabled: bool


class ClientOut(_Camel):
    id: int
    name: str
    enabled: bool
    created_at: datetime
    active_tokens: int | None = None
    binance_configured: bool | None = None


class IssuedTokenOut(_Camel):
    id: int
    name: str
    prefix: str
    token: str = Field(
        description="The private token. It is shown ONLY NOW: store it safely and share it "
        "only with this client. Only its hash is kept."
    )
    expires_at: datetime | None


class CreateClientResponse(_Camel):
    client: ClientOut
    token: IssuedTokenOut


class TokenOut(_Camel):
    id: int
    name: str
    prefix: str
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    active: bool


class RevokeTokenResponse(_Camel):
    revoked: bool


# --- client self-service -----------------------------------------------------------------


class MeOut(_Camel):
    client_id: int
    client_name: str
    token_id: int
    token_name: str


class BinanceCredentialsRequest(_Camel):
    api_key: SecretStr = Field(min_length=16, max_length=128, description="Read-only API key")
    api_secret: SecretStr = Field(min_length=16, max_length=128, description="Its secret key")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"apiKey": "<your read-only API key>", "apiSecret": "<its secret>"}]
        }
    )


class BinanceCredentialsOut(_Camel):
    configured: bool
    api_key_hint: str | None = Field(default=None, description="Last 4 characters only")
    ip_restricted: bool | None = None
    verified_at: datetime | None = None
    permissions: dict[str, bool] | None = None


class CredentialsRejectedOut(_Camel):
    detail: str
    problems: list[str]


class BinanceSyncOut(_Camel):
    synced: bool
    fetched: int
    incoming: int
    payments_imported: int
    duplicates: int
    busy: bool
    skipped_recent: bool
    errors: list[str]
