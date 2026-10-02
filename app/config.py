"""Application configuration.

All configuration comes from environment variables (optionally a ``.env`` file in
development). Secrets are typed as :class:`pydantic.SecretStr` so they are never
rendered by ``repr()``/``str()`` and therefore never leak into logs or tracebacks.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

if TYPE_CHECKING:
    from app.db.connection import DatabaseConnection

_APP_PASSWORD_RE = re.compile(r"^[a-z]{16}$")


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class GmailAuthMethod(StrEnum):
    APP_PASSWORD = "app_password"  # noqa: S105 - enum label, not a secret
    OAUTH2 = "oauth2"
    # Normal Google account password. Implemented but DISABLED unless
    # GMAIL_ACCOUNT_PASSWORD_AUTH_ENABLED=true. Never recommended.
    ACCOUNT_PASSWORD = "account_password"  # noqa: S105 - enum label, not a secret


def _split_csv(value: object) -> object:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # `VAR=` (empty) means "not set" rather than an empty string.
        env_parse_none_str="",
    )

    # --- Application -----------------------------------------------------------------
    app_env: Environment = Environment.DEVELOPMENT
    app_name: str = "binance-pay-verifier"
    log_level: str = "INFO"
    log_json: bool = True
    enable_docs: bool | None = None  # default: enabled outside production

    # --- Database --------------------------------------------------------------------
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://postgres:postgres@localhost:5432/binance_pay"
    )
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    database_pool_timeout_seconds: float = Field(default=10, gt=0)
    database_statement_timeout_ms: int = Field(default=15_000, ge=1_000)
    # Supabase: TLS is required automatically; pooler mode is auto-detected (port 6543 =
    # transaction pooler). Override only if needed.
    database_ssl_mode: str | None = None
    database_pooler_mode: Literal["session", "transaction"] | None = None

    # --- API security ----------------------------------------------------------------
    api_keys: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)
    admin_api_keys: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)
    cors_allowed_origins: CsvList = Field(default_factory=list)
    allowed_hosts: CsvList = Field(default_factory=list)
    max_request_body_bytes: int = Field(default=16 * 1024, ge=1024)
    rate_limit_verify_per_minute: int = Field(default=60, ge=1)
    rate_limit_admin_per_minute: int = Field(default=10, ge=1)
    trust_forwarded_headers: bool = False

    # --- Encryption ------------------------------------------------------------------
    credentials_encryption_key: SecretStr | None = None
    credentials_encryption_previous_keys: Annotated[list[SecretStr], NoDecode] = Field(
        default_factory=list
    )

    # --- Gmail -----------------------------------------------------------------------
    gmail_email: str | None = None
    gmail_auth_method: GmailAuthMethod = GmailAuthMethod.APP_PASSWORD
    gmail_app_password: SecretStr | None = None
    gmail_account_password_auth_enabled: bool = False
    gmail_account_password: SecretStr | None = None
    gmail_oauth_refresh_token: SecretStr | None = None
    gmail_imap_host: str = "imap.gmail.com"
    gmail_imap_port: int = 993
    gmail_mailbox: str = "INBOX"

    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_redirect_uri: str | None = None
    google_oauth_state_ttl_seconds: int = Field(default=600, ge=60, le=3600)

    # --- Mail sync -------------------------------------------------------------------
    mail_initial_lookback_hours: int = Field(default=48, ge=1, le=24 * 30)
    mail_sync_interval_seconds: int = Field(default=15, ge=5)
    mail_sync_max_messages_per_run: int = Field(default=200, ge=1, le=5000)
    mail_imap_timeout_seconds: float = Field(default=15, gt=0, le=120)
    mail_imap_max_retries: int = Field(default=2, ge=0, le=5)
    mail_imap_retry_backoff_seconds: float = Field(default=1.0, ge=0)
    mail_on_demand_sync_enabled: bool = True
    mail_on_demand_min_interval_seconds: float = Field(default=3, ge=0)
    mail_sync_lock_wait_seconds: float = Field(default=10, ge=0, le=60)
    store_raw_emails: bool = False
    # Run the periodic sync loop inside the API process (otherwise run the worker process).
    run_sync_worker_in_api: bool = False

    # --- Binance email identification & trust ----------------------------------------
    binance_allowed_senders: CsvList = Field(default_factory=list)
    binance_allowed_domains: CsvList = Field(default_factory=list)
    binance_allow_simulated_templates: bool | None = None  # default: not in production
    binance_enabled_templates: CsvList = Field(default_factory=list)  # empty = all
    email_trusted_authserv_ids: CsvList = Field(default_factory=lambda: ["mx.google.com"])
    email_require_authentication_results: bool = True
    email_require_dkim: bool = True
    email_require_spf: bool = True
    email_require_dmarc: bool = True
    email_require_dkim_alignment: bool = True

    # --- Payment verification --------------------------------------------------------
    default_asset: str = "USDT"
    default_payment_max_age_minutes: int = Field(default=60, ge=1)
    max_payment_age_minutes: int = Field(default=1440, ge=1)
    payment_clock_skew_seconds: int = Field(default=300, ge=0, le=3600)
    payment_code_case_insensitive: bool = False
    # Explicit, opt-in absolute tolerance. 0 means exact match (the default).
    payment_amount_tolerance: str = "0"

    # ---------------------------------------------------------------------------------
    @field_validator(
        "cors_allowed_origins",
        "allowed_hosts",
        "binance_allowed_senders",
        "binance_allowed_domains",
        "binance_enabled_templates",
        "email_trusted_authserv_ids",
        mode="before",
    )
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator(
        "api_keys", "admin_api_keys", "credentials_encryption_previous_keys", mode="before"
    )
    @classmethod
    def _csv_secrets(cls, value: object) -> object:
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        return _split_csv(value)

    @field_validator("binance_allowed_senders", "binance_allowed_domains")
    @classmethod
    def _lower(cls, value: list[str]) -> list[str]:
        return [v.strip().lower() for v in value]

    @field_validator("default_asset")
    @classmethod
    def _upper_asset(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("gmail_email")
    @classmethod
    def _normalize_email(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value.strip().lower()

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for key in [*self.api_keys, *self.admin_api_keys]:
            if len(key.get_secret_value()) < 32:
                raise ValueError("API keys must be at least 32 characters long")

        if self.max_payment_age_minutes < self.default_payment_max_age_minutes:
            raise ValueError("MAX_PAYMENT_AGE_MINUTES must be >= DEFAULT_PAYMENT_MAX_AGE_MINUTES")

        if (
            self.gmail_auth_method is GmailAuthMethod.ACCOUNT_PASSWORD
            and self.gmail_email
            and not self.gmail_account_password_auth_enabled
        ):
            raise ValueError(
                "GMAIL_AUTH_METHOD=account_password is disabled. Use an App Password "
                "or OAuth2. Never provide your normal Google password."
            )

        if self.gmail_app_password is not None:
            normalized = self.gmail_app_password.get_secret_value().replace(" ", "")
            if not _APP_PASSWORD_RE.fullmatch(normalized):
                # Google App Passwords are always 16 lowercase letters. Anything else is
                # most likely the normal account password, which we refuse to accept.
                raise ValueError(
                    "GMAIL_APP_PASSWORD does not look like a Google App Password "
                    "(16 letters). Do not use your normal Google password."
                )
            self.gmail_app_password = SecretStr(normalized)

        try:
            tolerance = Decimal(self.payment_amount_tolerance)
        except InvalidOperation as exc:
            raise ValueError("PAYMENT_AMOUNT_TOLERANCE must be a decimal string") from exc
        if not tolerance.is_finite() or tolerance < 0:
            raise ValueError("PAYMENT_AMOUNT_TOLERANCE must be >= 0")
        return self

    # --- Derived values --------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env is Environment.PRODUCTION

    @property
    def docs_enabled(self) -> bool:
        return self.enable_docs if self.enable_docs is not None else not self.is_production

    @property
    def simulated_templates_allowed(self) -> bool:
        if self.binance_allow_simulated_templates is not None:
            return self.binance_allow_simulated_templates
        return not self.is_production

    @property
    def binance_senders_configured(self) -> bool:
        return bool(self.binance_allowed_senders or self.binance_allowed_domains)

    @property
    def database(self) -> DatabaseConnection:
        from app.db.connection import resolve_database  # noqa: PLC0415 - avoid import cycle

        return resolve_database(
            self.database_url.get_secret_value(),
            ssl_mode=self.database_ssl_mode,
            pooler_mode=self.database_pooler_mode,
            statement_timeout_ms=self.database_statement_timeout_ms,
            application_name=self.app_name,
        )

    @property
    def amount_tolerance(self) -> Decimal:
        return Decimal(self.payment_amount_tolerance)

    def secret_values(self) -> list[str]:
        """Every configured secret, used by the log redaction filter."""
        candidates: list[SecretStr | None] = [
            self.database_url,
            self.credentials_encryption_key,
            self.gmail_app_password,
            self.gmail_account_password,
            self.gmail_oauth_refresh_token,
            self.google_client_secret,
            *self.api_keys,
            *self.admin_api_keys,
            *self.credentials_encryption_previous_keys,
        ]
        values = [c.get_secret_value() for c in candidates if c is not None]
        # Also redact the DB password alone if present in the URL.
        url = self.database_url.get_secret_value()
        match = re.search(r"://[^:/@]+:([^@]+)@", url)
        if match:
            values.append(match.group(1))
        return [v for v in values if len(v) >= 6]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
