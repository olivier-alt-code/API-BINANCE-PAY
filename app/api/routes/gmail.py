"""Protected admin endpoints for the Gmail integration. Never lists or returns messages."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_container, rate_limit
from app.container import Container
from app.core.exceptions import AppError, MailProviderError, OAuthError
from app.core.logging import mask_email
from app.core.security import require_admin_key
from app.db.models import MailAuthType
from app.db.repositories.mail_accounts import MailAccountRepository
from app.integrations.mail.gmail_oauth import refresh_token_aad
from app.schemas.gmail import (
    MailSyncResponse,
    MailTestRequest,
    MailTestResponse,
    OAuthAuthorizeResponse,
    OAuthCallbackResponse,
)
from app.services.mail_sync import SyncMode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/mail", tags=["admin"])

_admin_rl = Depends(rate_limit("admin", "rate_limit_admin_per_minute", require_admin_key))


@router.post(
    "/test",
    response_model=MailTestResponse,
    summary="Test the Gmail IMAP connection",
    dependencies=[_admin_rl],
)
async def test_mail(
    body: MailTestRequest | None = None,
    _: str = Depends(require_admin_key),
    container: Container = Depends(get_container),
) -> MailTestResponse:
    await container.sync_service.ensure_env_account()
    async with container.sessionmaker() as session:
        repo = MailAccountRepository(session)
        if body is not None and body.account:
            account = await repo.get_by_email(body.account)
        else:
            enabled = await repo.list_enabled()
            account = enabled[0] if enabled else None
    if account is None:
        return MailTestResponse(
            connected=False, provider="gmail", account=None, error="No mail account configured"
        )
    try:
        await container.sync_service.test_connection(account.id)
    except (MailProviderError, AppError) as exc:
        return MailTestResponse(
            connected=False,
            provider="gmail",
            account=mask_email(account.email),
            auth_type=account.auth_type,
            error=exc.public_message,
        )
    return MailTestResponse(
        connected=True,
        provider="gmail",
        account=mask_email(account.email),
        auth_type=account.auth_type,
    )


@router.post(
    "/sync",
    response_model=MailSyncResponse,
    summary="Run a mailbox synchronization now",
    dependencies=[_admin_rl],
)
async def sync_mail(
    _: str = Depends(require_admin_key),
    container: Container = Depends(get_container),
) -> MailSyncResponse:
    summary = await container.sync_service.sync_all(SyncMode.MANUAL)
    return MailSyncResponse(
        messages_scanned=summary.messages_scanned,
        binance_messages=summary.binance_messages,
        payments_imported=summary.payments_imported,
        duplicates=summary.duplicates,
        untrusted_messages=summary.untrusted_messages,
        unparsed_messages=summary.unparsed_messages,
        accounts_synced=summary.accounts_synced,
        accounts_failed=summary.accounts_failed,
        accounts_busy=summary.accounts_busy,
        errors=sorted(set(summary.errors)),
    )


@router.get(
    "/oauth/authorize",
    response_model=OAuthAuthorizeResponse,
    summary="Start the Google OAuth2 consent flow (returns the URL to open)",
    dependencies=[_admin_rl],
)
async def oauth_authorize(
    login_hint: str | None = Query(default=None, max_length=320),
    _: str = Depends(require_admin_key),
    container: Container = Depends(get_container),
) -> OAuthAuthorizeResponse:
    if container.oauth_state is None or not container.oauth_client.configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OAuth2 is not configured (GOOGLE_* and CREDENTIALS_ENCRYPTION_KEY)",
        )
    state, challenge = container.oauth_state.issue()
    url = container.oauth_client.authorization_url(state, challenge, login_hint)
    return OAuthAuthorizeResponse(authorization_url=url)


@router.get(
    "/oauth/callback",
    response_model=OAuthCallbackResponse,
    summary="Google OAuth2 redirect target (authenticated by the encrypted state)",
)
async def oauth_callback(
    code: str = Query(min_length=1, max_length=2048),
    state: str = Query(min_length=1, max_length=4096),
    container: Container = Depends(get_container),
) -> OAuthCallbackResponse:
    if container.oauth_state is None or container.cipher is None:
        raise HTTPException(status_code=503, detail="OAuth2 is not configured")
    try:
        parsed_state = container.oauth_state.verify(state)
        tokens = await container.oauth_client.exchange_code(code, parsed_state.code_verifier)
        if tokens.refresh_token is None:
            raise OAuthError("Google returned no refresh token (re-consent required)")
        email = await container.oauth_client.fetch_email(tokens.access_token)
    except OAuthError as exc:
        logger.warning("oauth_callback_failed", extra={"error_type": type(exc).__name__})
        raise HTTPException(status_code=400, detail=exc.public_message) from None

    encrypted = container.cipher.encrypt_json(
        {"refresh_token": tokens.refresh_token.get_secret_value()}, refresh_token_aad(email)
    )
    async with container.sessionmaker() as session, session.begin():
        await MailAccountRepository(session).upsert(
            email=email,
            auth_type=MailAuthType.OAUTH2,
            mailbox=container.settings.gmail_mailbox,
            encrypted_credentials=encrypted,
            overwrite_credentials=True,
        )
    logger.info("oauth_account_connected", extra={"account": mask_email(email)})
    return OAuthCallbackResponse(connected=True, provider="gmail", account=mask_email(email))
