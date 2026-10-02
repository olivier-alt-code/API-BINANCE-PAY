"""Builds a :class:`MailProvider` for a mail account, resolving its credentials.

* ``app_password``: the App Password comes from ``GMAIL_APP_PASSWORD`` (environment only,
  never persisted) and must belong to ``GMAIL_EMAIL``.
* ``oauth2``: the refresh token is stored AES-GCM encrypted in ``mail_accounts``.
* ``account_password``: normal Google password. DISABLED unless
  ``GMAIL_ACCOUNT_PASSWORD_AUTH_ENABLED=true`` (Google rejects it for IMAP anyway).
"""

from __future__ import annotations

from pydantic import SecretStr

from app.config import Settings
from app.core.encryption import CredentialCipher
from app.core.exceptions import ConfigurationError
from app.db.models import MailAuthType
from app.integrations.mail.base import (
    CredentialsProvider,
    MailAccountConfig,
    MailProvider,
    PasswordCredentials,
    StaticCredentials,
)
from app.integrations.mail.gmail_imap import GmailImapProvider, ImapFactory
from app.integrations.mail.gmail_oauth import (
    GoogleOAuthClient,
    OAuth2CredentialsProvider,
    refresh_token_aad,
)


class GmailProviderFactory:
    def __init__(
        self,
        settings: Settings,
        *,
        cipher: CredentialCipher | None,
        oauth_client: GoogleOAuthClient,
        imap_factory: ImapFactory | None = None,
    ) -> None:
        self._settings = settings
        self._cipher = cipher
        self._oauth = oauth_client
        self._imap_factory = imap_factory

    def _credentials(self, account: MailAccountConfig) -> CredentialsProvider:
        s = self._settings
        match account.auth_type:
            case MailAuthType.APP_PASSWORD:
                if s.gmail_app_password is None or s.gmail_email != account.email:
                    raise ConfigurationError("No App Password configured for this mail account")
                return StaticCredentials(PasswordCredentials(account.email, s.gmail_app_password))
            case MailAuthType.OAUTH2:
                if self._cipher is None:
                    raise ConfigurationError("CREDENTIALS_ENCRYPTION_KEY is required for OAuth2")
                if not account.encrypted_credentials:
                    raise ConfigurationError("Mail account has no stored OAuth2 credentials")
                data = self._cipher.decrypt_json(
                    account.encrypted_credentials, refresh_token_aad(account.email)
                )
                return OAuth2CredentialsProvider(
                    email=account.email,
                    refresh_token=SecretStr(str(data["refresh_token"])),
                    client=self._oauth,
                )
            case MailAuthType.ACCOUNT_PASSWORD:
                if not s.gmail_account_password_auth_enabled:
                    raise ConfigurationError("Account-password authentication is disabled")
                if s.gmail_account_password is None or s.gmail_email != account.email:
                    raise ConfigurationError("No account password configured")
                return StaticCredentials(
                    PasswordCredentials(account.email, s.gmail_account_password)
                )
            case _:
                raise ConfigurationError("Unsupported mail auth type")

    def build(self, account: MailAccountConfig) -> MailProvider:
        s = self._settings
        return GmailImapProvider(
            account_email=account.email,
            credentials=self._credentials(account),
            host=s.gmail_imap_host,
            port=s.gmail_imap_port,
            mailbox=account.mailbox,
            timeout_seconds=s.mail_imap_timeout_seconds,
            max_retries=s.mail_imap_max_retries,
            retry_backoff_seconds=s.mail_imap_retry_backoff_seconds,
            imap_factory=self._imap_factory,
        )
