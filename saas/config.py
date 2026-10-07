"""Settings, read from environment variables (or a local ``.env`` file)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    app_env: str = "production"
    site_name: str = "Binance Pay Verifier"
    log_level: str = "INFO"

    # --- Storage (Postgres/Supabase in production, SQLite for local tests) -----------------
    database_url: SecretStr = SecretStr("sqlite+aiosqlite:///./saas.db")
    db_pool_max: int = Field(default=3, ge=1, le=50)
    # Postgres schema for this app's tables, so they never mix with the API's tables.
    db_schema: str = Field(default="saas", pattern=r"^[a-z_][a-z0-9_]{0,62}$")

    # --- The verification API this site sells ---------------------------------------------
    verifier_api_url: str = "http://localhost:8000"
    # Owner key (ADMIN_API_KEYS of the API): creates clients/tokens for buyers.
    verifier_admin_key: SecretStr = SecretStr("")
    # Client token of the account that RECEIVES the subscription payments (use a dedicated
    # token for this site so its rate limit is independent from your other apps).
    verifier_token: SecretStr = SecretStr("")
    verifier_timeout_seconds: float = Field(default=90, gt=0)
    # Optional maxAgeMinutes sent on each verification (must be <= MAX_PAYMENT_AGE_MINUTES
    # of the API). Empty = the API's DEFAULT_PAYMENT_MAX_AGE_MINUTES.
    payment_max_age_minutes: int | None = Field(default=None, ge=1)
    # Public URL of the API shown to customers in the docs (defaults to verifier_api_url).
    public_api_url: str | None = None

    # --- Plan -------------------------------------------------------------------------------
    price_usdt: str = "5"
    period_days: int = Field(default=30, ge=1, le=366)

    # --- Where buyers pay (shown on the payment page) ---------------------------------------
    pay_to_id: str = ""  # Binance Pay ID / Binance ID of the receiving account
    pay_to_name: str = ""  # its alias/nickname, so the buyer can double-check
    pay_qr_url: str | None = None  # optional image of your Binance Pay QR
    support_contact: str = ""  # e.g. an email or Telegram handle, shown to customers

    # --- Web --------------------------------------------------------------------------------
    session_secret: SecretStr = SecretStr("")
    cookie_secure: bool = True
    sweep_interval_seconds: int = Field(default=300, ge=10)

    @field_validator("price_usdt")
    @classmethod
    def _price(cls, value: str) -> str:
        try:
            price = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("PRICE_USDT must be a decimal string, e.g. 5") from exc
        if not price.is_finite() or price <= 0:
            raise ValueError("PRICE_USDT must be > 0")
        return value.strip()

    @field_validator("verifier_api_url", "public_api_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return value.strip().rstrip("/") if value else value

    @model_validator(mode="after")
    def _production_requirements(self) -> Settings:
        if self.app_env == "production":
            missing = [
                name
                for name, value in (
                    ("SESSION_SECRET", self.session_secret),
                    ("VERIFIER_ADMIN_KEY", self.verifier_admin_key),
                    ("VERIFIER_TOKEN", self.verifier_token),
                )
                if not value.get_secret_value()
            ]
            if missing:
                raise ValueError(f"Missing required settings: {', '.join(missing)}")
            if len(self.session_secret.get_secret_value()) < 32:
                raise ValueError("SESSION_SECRET must be at least 32 characters long")
            if not self.pay_to_id:
                raise ValueError("PAY_TO_ID is required: buyers need to know where to pay")
        return self

    @property
    def price(self) -> Decimal:
        return Decimal(self.price_usdt)

    @property
    def docs_api_url(self) -> str:
        return self.public_api_url or self.verifier_api_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
