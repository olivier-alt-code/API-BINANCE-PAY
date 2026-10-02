"""Composition root: wires settings, DB, mail integration and services together.

One container per process. It holds no business state: everything that matters lives in
PostgreSQL, so any number of replicas can run side by side.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.encryption import CredentialCipher, build_cipher
from app.core.exceptions import ConfigurationError
from app.integrations.binance.email_parser import BinanceEmailParser
from app.integrations.binance.email_validator import EmailTrustValidator, TrustPolicy
from app.integrations.binance.evidence import PaymentEvidenceProvider
from app.integrations.binance.templates import select_templates
from app.integrations.mail.base import MailProviderFactory
from app.integrations.mail.factory import GmailProviderFactory
from app.integrations.mail.gmail_oauth import GoogleOAuthClient, OAuthStateCodec
from app.services.email_evidence import BinanceEmailPaymentProvider
from app.services.mail_sync import MailSyncService
from app.services.payment_claim import PaymentClaimService
from app.services.payment_verifier import PaymentVerifier
from app.services.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    cipher: CredentialCipher | None
    oauth_client: GoogleOAuthClient
    oauth_state: OAuthStateCodec | None
    parser: BinanceEmailParser
    validator: EmailTrustValidator
    sync_service: MailSyncService
    evidence_provider: PaymentEvidenceProvider
    claim_service: PaymentClaimService
    verifier: PaymentVerifier
    rate_limiter: RateLimiter


def build_container(
    settings: Settings,
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    provider_factory: MailProviderFactory | None = None,
) -> Container:
    cipher: CredentialCipher | None = None
    if settings.credentials_encryption_key is not None:
        cipher = build_cipher(
            settings.credentials_encryption_key.get_secret_value(),
            [k.get_secret_value() for k in settings.credentials_encryption_previous_keys],
        )
    elif settings.is_production:
        raise ConfigurationError("CREDENTIALS_ENCRYPTION_KEY is required in production")

    oauth_client = GoogleOAuthClient(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        redirect_uri=settings.google_redirect_uri,
        timeout_seconds=settings.mail_imap_timeout_seconds,
    )
    oauth_state = (
        OAuthStateCodec(cipher, settings.google_oauth_state_ttl_seconds) if cipher else None
    )
    templates = select_templates(
        allow_simulated=settings.simulated_templates_allowed,
        enabled_names=settings.binance_enabled_templates,
    )
    if not templates:
        logger.warning("no_binance_templates_enabled")
    parser = BinanceEmailParser(
        templates, case_insensitive_code=settings.payment_code_case_insensitive
    )
    validator = EmailTrustValidator(TrustPolicy.from_settings(settings))
    factory = provider_factory or GmailProviderFactory(
        settings, cipher=cipher, oauth_client=oauth_client
    )
    sync_service = MailSyncService(
        settings=settings,
        engine=engine,
        sessionmaker=sessionmaker,
        provider_factory=factory,
        validator=validator,
        parser=parser,
        cipher=cipher,
    )
    evidence_provider = BinanceEmailPaymentProvider(
        settings=settings, sessionmaker=sessionmaker, sync_service=sync_service
    )
    claim_service = PaymentClaimService(sessionmaker)
    verifier = PaymentVerifier(
        evidence_provider=evidence_provider,
        claim_service=claim_service,
        case_insensitive_code=settings.payment_code_case_insensitive,
        amount_tolerance=settings.amount_tolerance,
        clock_skew=timedelta(seconds=settings.payment_clock_skew_seconds),
    )
    return Container(
        settings=settings,
        engine=engine,
        sessionmaker=sessionmaker,
        cipher=cipher,
        oauth_client=oauth_client,
        oauth_state=oauth_state,
        parser=parser,
        validator=validator,
        sync_service=sync_service,
        evidence_provider=evidence_provider,
        claim_service=claim_service,
        verifier=verifier,
        rate_limiter=RateLimiter(sessionmaker),
    )
