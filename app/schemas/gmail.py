from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class MailTestRequest(_Camel):
    account: str | None = Field(
        default=None,
        max_length=320,
        description="Email of the account to test. Defaults to the first enabled account.",
    )


class MailTestResponse(_Camel):
    connected: bool
    provider: str
    account: str | None = Field(description="Masked email address")
    auth_type: str | None = None
    error: str | None = None


class MailSyncResponse(_Camel):
    messages_scanned: int
    binance_messages: int
    payments_imported: int
    duplicates: int
    untrusted_messages: int
    unparsed_messages: int
    accounts_synced: int
    accounts_failed: int
    accounts_busy: int
    errors: list[str]

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "messagesScanned": 12,
                    "binanceMessages": 2,
                    "paymentsImported": 1,
                    "duplicates": 1,
                    "untrustedMessages": 0,
                    "unparsedMessages": 0,
                    "accountsSynced": 1,
                    "accountsFailed": 0,
                    "accountsBusy": 0,
                    "errors": [],
                }
            ]
        }
    )


class OAuthAuthorizeResponse(_Camel):
    authorization_url: str


class OAuthCallbackResponse(_Camel):
    connected: bool
    provider: str
    account: str | None
