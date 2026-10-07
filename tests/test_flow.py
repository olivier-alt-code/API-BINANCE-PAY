from __future__ import annotations

import re
from datetime import timedelta

import httpx

from saas.billing import hash_token
from saas.db import Subscription
from tests.conftest import TOKEN_RE, Clock, FakeVerifier, csrf, pay, subscribe


async def _subs(app) -> list[Subscription]:
    from sqlalchemy import select

    async with app.state.billing._sessions() as s:
        return list(await s.scalars(select(Subscription)))


async def test_new_subscription_issues_token_once(
    client: httpx.AsyncClient, app, fake: FakeVerifier, clock: Clock
) -> None:
    page = await subscribe(client)
    r = await client.get(page)
    assert "5 USDT" in r.text and "123456789" in r.text
    assert r.headers["cache-control"] == "no-store"

    fake.pay("457883830937640960", "5")
    r = await pay(client, page, "457883830937640960")
    assert r.status_code == 200
    token = TOKEN_RE.search(r.text).group(1)
    assert token.startswith("bpv_")

    [sub] = await _subs(app)
    assert sub.status == "active"
    assert sub.token_hash == hash_token(token)
    assert sub.paid_until.replace(tzinfo=None) == (clock.now + timedelta(days=30)).replace(
        tzinfo=None
    )
    assert len(fake.clients) == 1
    assert fake.payments["457883830937640960"].claimed_by.startswith("saas-")

    # Submitting again never shows the token again nor creates another client.
    r = await pay(client, page, "457883830937640960")
    assert "ya se procesó" in r.text and token not in r.text
    assert len(fake.clients) == 1

    # The buyer is logged in after paying.
    r = await client.get("/cuenta")
    assert "Activa" in r.text and sub.token_prefix in r.text


async def test_payment_not_visible_yet_then_ok(
    client: httpx.AsyncClient, fake: FakeVerifier
) -> None:
    page = await subscribe(client)
    r = await pay(client, page, "457000000000000001")
    assert r.status_code == 200 and "Todavía no vemos ese pago" in r.text
    fake.pay("457000000000000001")
    r = await pay(client, page, "457000000000000001")
    assert TOKEN_RE.search(r.text)


async def test_wrong_amount_is_rejected(client: httpx.AsyncClient, fake: FakeVerifier) -> None:
    page = await subscribe(client)
    fake.pay("457000000000000002", "4.99")
    r = await pay(client, page, "457000000000000002")
    assert r.status_code == 422
    assert "4.99 USDT" in r.text and "cuesta 5 USDT" in r.text
    assert not fake.clients


async def test_payment_cannot_serve_two_checkouts(
    client: httpx.AsyncClient, fake: FakeVerifier
) -> None:
    fake.pay("457000000000000003")
    first = await subscribe(client, "a@example.com")
    assert TOKEN_RE.search((await pay(client, first, "457000000000000003")).text)
    second = await subscribe(client, "b@example.com")
    r = await pay(client, second, "457000000000000003")
    assert r.status_code == 422 and "ya se usó" in r.text
    assert len(fake.clients) == 1


async def test_checkout_keeps_its_first_payment(
    client: httpx.AsyncClient, fake: FakeVerifier
) -> None:
    page = await subscribe(client)
    fake.pay("457000000000000004")
    fake.provision_down = True  # payment claimed, but provisioning fails
    r = await pay(client, page, "457000000000000004")
    assert "No pudimos completar" in r.text
    assert fake.payments["457000000000000004"].claimed_by is not None
    fake.provision_down = False
    # A different (second) payment is refused before reaching the API...
    fake.pay("457000000000000005")
    calls = fake.verify_calls
    r = await pay(client, page, "457000000000000005")
    assert "otro pago asociado" in r.text and fake.verify_calls == calls
    # ...and retrying with the first one completes the order.
    r = await pay(client, page, "457000000000000004")
    assert TOKEN_RE.search(r.text)


async def test_crash_after_client_creation_is_recovered(
    client: httpx.AsyncClient, fake: FakeVerifier
) -> None:
    page = await subscribe(client)
    fake.pay("457000000000000006")
    fake.fail_create_after_success = True
    r = await pay(client, page, "457000000000000006")
    assert "No pudimos completar" in r.text
    [client_obj] = fake.clients.values()
    assert len(fake.active_tokens(client_obj.id)) == 1  # orphan token from the crash

    r = await pay(client, page, "457000000000000006")
    token = TOKEN_RE.search(r.text).group(1)
    assert len(fake.clients) == 1
    assert fake.active_tokens(client_obj.id) == [token]  # orphan revoked


async def test_renewal_extends_and_expiry_disables(
    client: httpx.AsyncClient, app, fake: FakeVerifier, clock: Clock
) -> None:
    page = await subscribe(client)
    fake.pay("457000000000000007")
    await pay(client, page, "457000000000000007")
    [sub] = await _subs(app)
    first_until = sub.paid_until

    # Renew 10 days early: days are added on top of the remaining ones.
    clock.advance(days=20)
    token = await csrf(client, "/cuenta")
    r = await client.post("/cuenta/renovar", data={"csrf": token})
    renewal_page = r.headers["location"]
    assert "Renovar suscripción" in (await client.get(renewal_page)).text
    fake.pay("457000000000000008")
    r = await pay(client, renewal_page, "457000000000000008")
    assert r.status_code == 303 and r.headers["location"] == "/cuenta"
    [sub] = await _subs(app)
    assert sub.paid_until == first_until + timedelta(days=30)

    # Past the end: the sweeper pauses the API client.
    clock.now = sub.paid_until.replace(tzinfo=clock.now.tzinfo) + timedelta(minutes=1)
    assert await app.state.billing.sweep_expired() == 1
    [client_obj] = fake.clients.values()
    assert not client_obj.enabled
    assert "Vencida" in (await client.get("/cuenta")).text

    # Renewing after expiry counts from now and re-enables the same client/token.
    token = await csrf(client, "/cuenta")
    renewal_page = (await client.post("/cuenta/renovar", data={"csrf": token})).headers["location"]
    fake.pay("457000000000000009")
    await pay(client, renewal_page, "457000000000000009")
    [sub] = await _subs(app)
    assert sub.status == "active" and client_obj.enabled
    assert sub.paid_until.replace(tzinfo=None) == (clock.now + timedelta(days=30)).replace(
        tzinfo=None
    )
    assert await app.state.billing.sweep_expired() == 0


async def test_login_rotate_and_recover_token(
    client: httpx.AsyncClient, fake: FakeVerifier
) -> None:
    page = await subscribe(client)
    fake.pay("457000000000000010")
    token = TOKEN_RE.search((await pay(client, page, "457000000000000010")).text).group(1)

    # Fresh browser: log in with the token.
    async with httpx.AsyncClient(transport=client._transport, base_url="http://test") as other:
        assert (await other.get("/cuenta")).headers["location"] == "/entrar"
        c = await csrf(other, "/entrar")
        r = await other.post("/entrar", data={"token": "bpv_wrong" + "x" * 30, "csrf": c})
        assert r.status_code == 401
        r = await other.post("/entrar", data={"token": token, "csrf": c})
        assert r.status_code == 303
        c = await csrf(other, "/cuenta")
        r = await other.post("/cuenta/token", data={"csrf": c})
        new_token = TOKEN_RE.search(r.text).group(1)
    assert new_token != token
    [client_obj] = fake.clients.values()
    assert fake.active_tokens(client_obj.id) == [new_token]

    # Lost token: payment URL + Order ID gives a new one.
    c = await csrf(client, page)
    r = await client.post(page + "/recuperar", data={"order_id": "999", "csrf": c})
    assert r.status_code == 422
    r = await client.post(page + "/recuperar", data={"order_id": "457000000000000010", "csrf": c})
    recovered = TOKEN_RE.search(r.text).group(1)
    assert fake.active_tokens(client_obj.id) == [recovered]


async def test_register_binance_key_from_account(
    client: httpx.AsyncClient, fake: FakeVerifier
) -> None:
    page = await subscribe(client)
    fake.pay("457000000000000011")
    token = TOKEN_RE.search((await pay(client, page, "457000000000000011")).text).group(1)
    c = await csrf(client, "/cuenta")
    form = {"token": token, "api_key": "k" * 20, "api_secret": "s" * 20, "csrf": c}
    r = await client.post("/cuenta/binance", data=form)
    assert "quedó registrada" in r.text
    r = await client.post("/cuenta/binance", data=form | {"api_key": "TRADE" + "k" * 20})
    assert r.status_code == 422 and "enableSpotAndMarginTrading" in r.text
    r = await client.post("/cuenta/binance", data=form | {"token": "bpv_" + "z" * 40})
    assert "Ese no es el token" in r.text


async def test_forms_require_csrf(client: httpx.AsyncClient) -> None:
    r = await client.post("/suscribirse", data={"email": "a@example.com", "accept": "1"})
    assert r.status_code == 403


async def test_invalid_email_and_unknown_checkout(client: httpx.AsyncClient) -> None:
    c = await csrf(client)
    r = await client.post("/suscribirse", data={"email": "nope", "accept": "1", "csrf": c})
    assert r.status_code == 422 and "email válido" in r.text
    assert (await client.get("/pago/does-not-exist")).status_code == 404


async def test_rate_limit_on_payment_attempts(client: httpx.AsyncClient) -> None:
    page = await subscribe(client)
    c = await csrf(client, page)
    statuses = [
        (await client.post(page, data={"order_id": "4570000000000", "csrf": c})).status_code
        for _ in range(21)
    ]
    assert statuses[-1] == 429 and 429 not in statuses[:20]


async def test_static_pages(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).json() == {"status": "ok"}
    r = await client.get("/documentacion")
    assert "/v1/payments/verify" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


async def test_client_name_is_valid_for_the_api(
    client: httpx.AsyncClient, fake: FakeVerifier
) -> None:
    page = await subscribe(client, "ana%test+1@example.com")
    fake.pay("457000000000000012")
    assert TOKEN_RE.search((await pay(client, page, "457000000000000012")).text)
    [created] = fake.clients.values()
    assert re.fullmatch(r"saas-[0-9a-f]{12} [\w .@+-]+", created.name)
    assert "%" not in created.name
