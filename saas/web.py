"""Pages and form handlers (server-rendered, no JavaScript required)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from saas.billing import (
    BillingService,
    Outcome,
    PaymentResult,
    days_left,
    format_usdt,
    normalize_email,
)
from saas.config import Settings
from saas.db import SubscriptionStatus, aware, utcnow
from saas.security import RateLimiter, check_csrf, client_ip, csrf_token
from saas.verifier import IssuedToken, VerifierError

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["usdt"] = format_usdt
templates.env.filters["date"] = lambda d: aware(d).strftime("%d/%m/%Y %H:%M UTC") if d else "—"

router = APIRouter()

SESSION_SUB = "sub"
SESSION_FLASH = "flash"

STATUS_MESSAGES = {
    "NOT_FOUND": (
        "Todavía no vemos ese pago. Si acabas de pagar, espera unos segundos y vuelve a "
        "intentarlo. Comprueba que pegaste el «ID de orden» que muestra Binance."
    ),
    "PENDING_SYNC": "Estamos consultando Binance. Vuelve a intentarlo en unos segundos.",
    "BINANCE_API_UNAVAILABLE": "Binance no responde ahora mismo. Inténtalo en unos minutos.",
    "PAYMENT_NOT_COMPLETED": "El pago aún no figura como completado en Binance.",
    "ASSET_MISMATCH": "El pago debe hacerse en USDT.",
    "ALREADY_CLAIMED": "Ese pago ya se usó para otra suscripción.",
    "EXPIRED_PAYMENT": "Ese pago es demasiado antiguo para esta compra. Haz un pago nuevo.",
    "INVALID_PAYMENT_CODE": "El ID de orden no tiene un formato válido.",
    "CHECKOUT_HAS_OTHER_PAYMENT": (
        "Este pedido ya tiene otro pago asociado. Usa ese mismo ID de orden para completarlo."
    ),
    "CHECKOUT_NOT_FOUND": "Este enlace de pago no existe.",
}
GENERIC_ERROR = (
    "No pudimos completar la verificación en este momento. Inténtalo de nuevo en unos "
    "minutos; si ya pagaste, tu pago está a salvo y podrás usar el mismo ID de orden."
)


def _deps(request: Request) -> tuple[Settings, BillingService, RateLimiter]:
    state = request.app.state
    return state.settings, state.billing, state.rate_limiter


def _render(
    request: Request, template: str, status_code: int = 200, **context: Any
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    flash = request.session.pop(SESSION_FLASH, None)
    return templates.TemplateResponse(
        request,
        template,
        {
            "settings": settings,
            "csrf": csrf_token(request),
            "flash": flash,
            "logged_in": SESSION_SUB in request.session,
            **context,
        },
        status_code=status_code,
    )


def _limit(request: Request, bucket: str, limit: int, window: int) -> None:
    _, _, limiter = _deps(request)
    if not limiter.allow(bucket, client_ip(request), limit, window):
        raise HTTPException(status_code=429, detail="Demasiados intentos. Espera unos minutos.")


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def payment_message(result: PaymentResult, settings: Settings) -> str:
    if result.status == "AMOUNT_MISMATCH":
        received = format_usdt(result.received_amount) if result.received_amount else "otro importe"
        return (
            f"El pago fue de {received} USDT y la suscripción cuesta "
            f"{format_usdt(settings.price_usdt)} USDT."
        )
    if result.outcome is Outcome.ERROR or result.status is None:
        return GENERIC_ERROR
    return STATUS_MESSAGES.get(result.status, GENERIC_ERROR)


# --- public pages -------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _render(request, "index.html")


@router.get("/documentacion", response_class=HTMLResponse)
async def docs(request: Request) -> HTMLResponse:
    return _render(request, "docs.html")


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/suscribirse")
async def subscribe(
    request: Request, email: str = Form(""), accept: str = Form(""), csrf: str = Form("")
) -> Response:
    check_csrf(request, csrf)
    _limit(request, "subscribe", 5, 600)
    normalized = normalize_email(email)
    if normalized is None or accept != "1":
        return _render(
            request,
            "index.html",
            status_code=422,
            error="Escribe un email válido y acepta las condiciones.",
            email=email[:254],
        )
    _, billing, _ = _deps(request)
    checkout = await billing.start_subscription(normalized)
    return _redirect(f"/pago/{checkout.access_key}")


# --- payment ------------------------------------------------------------------------------
async def _checkout_or_404(request: Request, key: str) -> tuple[Any, Any]:
    _, billing, _ = _deps(request)
    found = await billing.get_checkout(key)
    if found is None:
        raise HTTPException(status_code=404, detail="Este enlace de pago no existe.")
    return found


@router.get("/pago/{key}", response_class=HTMLResponse)
async def payment_page(request: Request, key: str) -> HTMLResponse:
    checkout, sub = await _checkout_or_404(request, key)
    return _render(request, "pay.html", checkout=checkout, sub=sub)


@router.post("/pago/{key}")
async def submit_payment(
    request: Request, key: str, order_id: str = Form(""), csrf: str = Form("")
) -> Response:
    check_csrf(request, csrf)
    _limit(request, "pay", 20, 600)
    settings, billing, _ = _deps(request)
    result = await billing.submit_payment(key, order_id)
    checkout, sub = await _checkout_or_404(request, key)

    if result.outcome is Outcome.ACTIVATED and result.token is not None:
        request.session[SESSION_SUB] = sub.id
        return _token_page(request, result.token, sub_email=sub.email, paid_until=result.paid_until)
    if result.outcome is Outcome.RENEWED:
        request.session[SESSION_SUB] = sub.id
        request.session[SESSION_FLASH] = (
            f"¡Pago recibido! Tu suscripción está activa hasta el "
            f"{aware(result.paid_until).strftime('%d/%m/%Y')}."
            if result.paid_until
            else "¡Pago recibido!"
        )
        return _redirect("/cuenta")
    if result.outcome is Outcome.ALREADY_DONE:
        return _render(request, "pay.html", checkout=checkout, sub=sub)
    return _render(
        request,
        "pay.html",
        status_code=200 if result.outcome is Outcome.WAIT else 422,
        checkout=checkout,
        sub=sub,
        order_id=order_id.strip()[:64],
        error=payment_message(result, settings),
        waiting=result.outcome is Outcome.WAIT,
    )


@router.post("/pago/{key}/recuperar")
async def recover_token(
    request: Request, key: str, order_id: str = Form(""), csrf: str = Form("")
) -> Response:
    check_csrf(request, csrf)
    _limit(request, "recover", 5, 600)
    _, billing, _ = _deps(request)
    checkout, sub = await _checkout_or_404(request, key)
    try:
        token = await billing.recover_token(key, order_id)
    except VerifierError:
        token = None
        error = GENERIC_ERROR
    else:
        error = "Ese no es el ID de orden con el que se pagó este pedido."
    if token is None:
        return _render(
            request, "pay.html", status_code=422, checkout=checkout, sub=sub, recover_error=error
        )
    request.session[SESSION_SUB] = sub.id
    return _token_page(request, token, sub_email=sub.email, paid_until=sub.paid_until)


def _token_page(
    request: Request, token: IssuedToken, *, sub_email: str, paid_until: Any
) -> HTMLResponse:
    return _render(request, "token.html", token=token, email=sub_email, paid_until=paid_until)


# --- customer area ------------------------------------------------------------------------
@router.get("/entrar", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if SESSION_SUB in request.session:
        return _redirect("/cuenta")
    return _render(request, "login.html")


@router.post("/entrar")
async def login(request: Request, token: str = Form(""), csrf: str = Form("")) -> Response:
    check_csrf(request, csrf)
    _limit(request, "login", 10, 600)
    _, billing, _ = _deps(request)
    sub = await billing.find_by_token(token)
    if sub is None:
        return _render(
            request, "login.html", status_code=401, error="Token no reconocido. Revísalo."
        )
    request.session.clear()
    request.session[SESSION_SUB] = sub.id
    return _redirect("/cuenta")


@router.post("/salir")
async def logout(request: Request, csrf: str = Form("")) -> Response:
    check_csrf(request, csrf)
    request.session.clear()
    return _redirect("/")


async def _current(request: Request) -> Any:
    _, billing, _ = _deps(request)
    sub_id = request.session.get(SESSION_SUB)
    sub = await billing.get_subscription(sub_id) if isinstance(sub_id, str) else None
    if sub is None:
        request.session.pop(SESSION_SUB, None)
    return sub


@router.get("/cuenta", response_class=HTMLResponse)
async def account(request: Request) -> Response:
    sub = await _current(request)
    if sub is None:
        return _redirect("/entrar")
    return _account_page(request, sub)


def _account_page(request: Request, sub: Any, status_code: int = 200, **extra: Any) -> HTMLResponse:
    now = utcnow()
    return _render(
        request,
        "account.html",
        status_code=status_code,
        sub=sub,
        days_left=days_left(sub, now),
        active=sub.status == SubscriptionStatus.ACTIVE,
        expired=sub.status == SubscriptionStatus.EXPIRED,
        **extra,
    )


@router.post("/cuenta/renovar")
async def renew(request: Request, csrf: str = Form("")) -> Response:
    check_csrf(request, csrf)
    sub = await _current(request)
    if sub is None:
        return _redirect("/entrar")
    _, billing, _ = _deps(request)
    try:
        checkout = await billing.start_renewal(sub.id)
    except LookupError:
        return _redirect("/cuenta")
    return _redirect(f"/pago/{checkout.access_key}")


@router.post("/cuenta/token")
async def new_token(request: Request, csrf: str = Form("")) -> Response:
    check_csrf(request, csrf)
    _limit(request, "rotate", 5, 600)
    sub = await _current(request)
    if sub is None:
        return _redirect("/entrar")
    _, billing, _ = _deps(request)
    try:
        token = await billing.rotate_token(sub.id)
    except (LookupError, VerifierError):
        return _account_page(request, sub, status_code=503, error=GENERIC_ERROR)
    return _token_page(request, token, sub_email=sub.email, paid_until=sub.paid_until)


@router.post("/cuenta/binance")
async def binance_credentials(
    request: Request,
    token: str = Form(""),
    api_key: str = Form(""),
    api_secret: str = Form(""),
    csrf: str = Form(""),
) -> Response:
    check_csrf(request, csrf)
    _limit(request, "binance", 10, 600)
    sub = await _current(request)
    if sub is None:
        return _redirect("/entrar")
    _, billing, _ = _deps(request)
    if not await billing.owns_token(sub.id, token):
        return _account_page(
            request, sub, status_code=422, binance_error="Ese no es el token de esta cuenta."
        )
    api_key, api_secret = api_key.strip(), api_secret.strip()
    if not (16 <= len(api_key) <= 128 and 16 <= len(api_secret) <= 128):
        return _account_page(
            request, sub, status_code=422, binance_error="La API key o el secret están incompletos."
        )
    verifier = request.app.state.verifier
    try:
        result = await verifier.put_binance_credentials(token.strip(), api_key, api_secret)
    except VerifierError:
        return _account_page(request, sub, status_code=503, binance_error=GENERIC_ERROR)
    if result.ok:
        return _account_page(
            request, sub, binance_ok="Tu API key de Binance quedó registrada (solo lectura)."
        )
    if result.status_code == 401:
        message = "Tu suscripción no está activa: renuévala para registrar la key."
    elif result.status_code == 422:
        message = "Binance rechazó la key. " + " ".join(result.problems or [result.detail or ""])
    else:
        message = GENERIC_ERROR
    return _account_page(request, sub, status_code=422, binance_error=message.strip())
