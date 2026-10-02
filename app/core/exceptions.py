"""Domain exceptions.

Exception messages must NEVER contain secrets (passwords, tokens, keys, raw emails).
They are written to logs and may be forwarded to error trackers.
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for all application errors."""

    public_message = "Internal error"


class ConfigurationError(AppError):
    public_message = "Service is not configured correctly"


class EncryptionError(AppError):
    public_message = "Credential encryption error"


class MailProviderError(AppError):
    """The mailbox could not be reached or the IMAP conversation failed."""

    public_message = "Mail provider unavailable"


class MailAuthenticationError(MailProviderError):
    public_message = "Mail provider authentication failed"


class MailTimeoutError(MailProviderError):
    public_message = "Mail provider timeout"


class OAuthError(AppError):
    public_message = "OAuth flow failed"


class EvidenceProviderUnavailableError(AppError):
    """The payment evidence source (Gmail, Binance Pay API...) is unavailable."""

    public_message = "Payment evidence provider unavailable"


class EvidenceSyncPendingError(AppError):
    """Another worker is synchronizing the evidence source; retry shortly."""

    public_message = "Payment evidence synchronization in progress"


class BinanceEmailParseError(AppError):
    """The email could not be parsed unambiguously as a Binance payment notification."""

    public_message = "Unrecognized Binance email"
