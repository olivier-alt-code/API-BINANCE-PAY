"""Domain exceptions.

Exception messages must NEVER contain secrets (passwords, tokens, API keys/secrets).
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


class EvidenceProviderUnavailableError(AppError):
    """The payment evidence source (Binance API...) is unavailable."""

    public_message = "Payment evidence provider unavailable"


class EvidenceSyncPendingError(AppError):
    """Another worker is synchronizing the evidence source; retry shortly."""

    public_message = "Payment evidence synchronization in progress"


class EvidenceNotConfiguredError(AppError):
    """The tenant has not configured its evidence source (e.g. no Binance API key)."""

    public_message = "Binance API key not configured for this client"
