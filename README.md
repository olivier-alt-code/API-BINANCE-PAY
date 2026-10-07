# Binance Pay Verifier: web de suscripciones

Web sencilla para **vender acceso mensual** a la API de verificación de pagos de Binance Pay.
El cliente paga **5 USDT por Binance Pay** (la misma pasarela que vende la API), pega el
**ID de orden** y recibe al instante su **token privado** de la API.

- Sin pasarelas ni comisiones: los pagos llegan a tu cuenta de Binance y se verifican con
  tu propia API.
- Cada pago se usa una sola vez: lo garantiza el *claim* atómico de la API.
- Renovación desde el panel del cliente. Si renueva antes de que venza, los días se suman.
- Al vencer, el acceso se **pausa**: el cliente de la API se desactiva y su token responde
  401. Al renovar se reactiva **con el mismo token**.
- Panel del cliente: estado y vencimiento, renovar, conectar su API key de Binance y
  cambiar el token.
- Recuperación de token perdido con el enlace de pago y el ID de orden.

## Cómo funciona

```
Comprador                     Esta web                          API de verificación
─────────                     ────────                          ───────────────────
email ───────────────► suscripción (pendiente)
                       + página de pago /pago/<clave secreta>
paga 5 USDT por Binance Pay a tu cuenta
pega el ID de orden ─► POST /v1/payments/verify  ─────────────► (tu token de la web)
                         orderReference = saas-<id del pedido>    VERIFIED
                       POST /v1/admin/clients  ────────────────► (clave admin)
                         cliente + token nuevo                     201
◄── token (se muestra UNA vez; aquí solo se guarda su SHA-256)
                       vence en 30 días ── barrido cada 5 min ──► PATCH enabled=false
renueva y paga ──────► verify + PATCH enabled=true ──────────────► mismo token
```

Detalles que lo hacen seguro:

* **Un pago, un pedido.** La API reserva el pago para `saas-<id del pedido>`. Si alguien
  reutiliza ese ID de orden en otro pedido, la API responde `ALREADY_CLAIMED`.
* **Un pedido, un pago.** Cuando un pedido ya tiene un pago asociado, no acepta otro. Así
  nadie paga dos veces por error si la activación falló a medias. Reintentar con el mismo
  ID de orden completa el pedido.
* **Sin dobles altas.** El pedido se bloquea en la base de datos (`SELECT … FOR UPDATE`)
  mientras se activa: cinco envíos simultáneos crean **un** cliente. Está probado contra
  PostgreSQL.
* **Recuperable ante caídas.** Si la web cae después de crear el cliente en la API pero
  antes de guardarlo, el reintento lo encuentra por su nombre, emite un token nuevo y
  revoca el huérfano.
* **El token nunca se guarda.** Solo su hash, que sirve para iniciar sesión en el panel.
* Formularios con CSRF, cookies de sesión firmadas (`HttpOnly`, `Secure`, `SameSite=Lax`),
  límites por IP, CSP estricta, `no-store` en páginas con token y `no-referrer`.

## Requisitos en la API

1. **La API debe aceptar el ID de orden** (`orderId`) como `paymentCode`, que es lo que ve
   quien paga en Binance.
2. **Clave admin:** usa una de las `ADMIN_API_KEYS` de la API.
3. **Token propio para la web**, del cliente cuya cuenta de Binance **recibe** los pagos
   (tu cliente, con tu API key de solo lectura ya registrada). Usa un token **dedicado**
   para que el límite de peticiones de la web no afecte a tus otras apps:

   ```powershell
   $API   = "https://<tu-api>.azurewebsites.net"
   $ADMIN = @{ Authorization = "Bearer TU_CLAVE_MAESTRA" }
   Invoke-RestMethod -Uri "$API/v1/admin/clients" -Headers $ADMIN | Format-Table   # busca tu id
   $t = Invoke-RestMethod -Method Post -Uri "$API/v1/admin/clients/<TU_ID>/tokens" -Headers $ADMIN `
          -ContentType "application/json" -Body '{"name": "web-suscripciones"}'
   $t.token   # -> VERIFIER_TOKEN
   ```
4. **Antigüedad de los pagos:** si un comprador puede tardar en pegar el ID de orden, la
   antigüedad aceptada la marca `DEFAULT_PAYMENT_MAX_AGE_MINUTES` de la API, o
   `PAYMENT_MAX_AGE_MINUTES` de esta web.

Los clientes de la web aparecen en la API con nombres del tipo `saas-<id> <email>`.

## Variables de entorno

| Variable | Obligatoria | Descripción |
|---|---|---|
| `DATABASE_URL` | sí | PostgreSQL. Con Supabase usa el *Transaction pooler* (puerto 6543); se detecta solo |
| `DB_SCHEMA` | no | Esquema de las tablas de la web (por defecto `saas`, separado de las tablas de la API) |
| `DB_POOL_MAX` | no | Conexiones máximas sin el pooler de Supabase (por defecto 3) |
| `VERIFIER_API_URL` | sí | URL de tu API de verificación |
| `VERIFIER_ADMIN_KEY` | sí | Una de las `ADMIN_API_KEYS` de la API |
| `VERIFIER_TOKEN` | sí | Token `bpv_…` dedicado, del cliente que recibe los pagos |
| `PAY_TO_ID` | sí | Tu Binance Pay ID (se muestra al comprador) |
| `PAY_TO_NAME` | no | Tu alias en Binance, para que el comprador lo compruebe |
| `PAY_QR_URL` | no | URL `https://` de una imagen con tu QR de Binance Pay |
| `PRICE_USDT` | no | Precio por periodo (por defecto `5`) |
| `PERIOD_DAYS` | no | Días por pago (por defecto `30`) |
| `SESSION_SECRET` | sí | ≥ 32 caracteres aleatorios. Si lo cambias, se cierran las sesiones |
| `SITE_NAME` | no | Nombre que aparece en la web |
| `SUPPORT_CONTACT` | no | Email o Telegram de soporte |
| `PUBLIC_API_URL` | no | URL de la API que se muestra en la documentación |
| `PAYMENT_MAX_AGE_MINUTES` | no | `maxAgeMinutes` en cada verificación (≤ `MAX_PAYMENT_AGE_MINUTES` de la API) |
| `COOKIE_SECURE` | no | `true` en producción; `false` solo en local por http |
| `SWEEP_INTERVAL_SECONDS` | no | Cada cuánto se pausan las suscripciones vencidas (por defecto 300) |
| `APP_ENV` | no | `production` (por defecto) exige las variables obligatorias |

Genera el secreto de sesión con:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

**Base de datos:** puede ser el **mismo proyecto de Supabase** que la API. Las tablas
(`saas_subscriptions`, `saas_checkouts`) se crean solas al arrancar, dentro del esquema
`saas`. Ese esquema no lo expone la API REST de Supabase y no interfiere con las
migraciones de la API.

## Ejecutar en local

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env      # y rellénalo; para probar sin Postgres:
                            # DATABASE_URL=sqlite+aiosqlite:///./saas.db  COOKIE_SECURE=false
uvicorn --factory saas.main:create_app --reload
```

Abre <http://localhost:8000>.

Pruebas:

```powershell
ruff check . ; ruff format --check . ; mypy saas ; pytest
# también contra PostgreSQL:
$env:TEST_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/saas_test"; pytest
```

## Desplegar en Azure

Es igual que la API: Web App for Containers, una imagen en GHCR y el perfil de publicación.
Puede ir en el **mismo plan B1** que la API sin coste extra, porque un plan de App Service
admite varias Web Apps.

1. **Crea otra Web App** en Azure: Publish = Container, Linux, tu plan B1.
   - Configuration → General settings: **Always On = On** (el barrido de vencimientos
     corre dentro de la app), **HTTPS Only = On** y **Basic Auth Publishing Credentials =
     On**.
   - Health check: `/health`.
2. **Application settings:**
   - `WEBSITES_PORT=8000`
   - `FORWARDED_ALLOW_IPS=*`
   - todas las variables obligatorias de la tabla anterior
3. **Secretos del repositorio** en GitHub (Settings → Secrets and variables → Actions):
   - `AZURE_WEBAPP_NAME`: el nombre de **esta** Web App.
   - `AZURE_WEBAPP_PUBLISH_PROFILE`: el contenido de su `.PublishSettings` (Overview →
     Download publish profile).

   Crea también el environment `production` en Settings → Environments.
4. **Despliega:** push a `main`. La imagen se publica como `ghcr.io/<usuario>/<repo>`.
   Hazla pública en GitHub → Packages, o configura `DOCKER_REGISTRY_SERVER_*` en la Web App.
5. **Comprueba:** abre la web y haz una compra real de prueba con tu propio Binance.

Con una instancia basta. Si escalas a varias, el barrido y las activaciones siguen siendo
seguros; solo los límites por IP se cuentan en memoria, en cada instancia por separado.

## Operación

Consultas útiles en Supabase (SQL Editor):

```sql
-- Suscriptores y vencimientos
select email, status, paid_until, token_prefix, api_client_name
from saas.saas_subscriptions order by paid_until desc nulls last;

-- Pagos recibidos
select c.fulfilled_at, s.email, c.kind, c.amount, c.payment_code
from saas.saas_checkouts c join saas.saas_subscriptions s on s.id = c.subscription_id
where c.status = 'fulfilled' order by c.fulfilled_at desc;

-- Regalar días a alguien (el barrido no lo pausará; si ya estaba vencido, reactívalo
-- también en la API con PATCH /v1/admin/clients/{id} {"enabled": true})
update saas.saas_subscriptions
set paid_until = paid_until + interval '7 days', status = 'active'
where email = 'cliente@ejemplo.com';
```

**Token perdido:** el cliente abre el enlace de pago de su compra (la página
`/pago/<clave>`) y pega el mismo ID de orden. Recibe un token nuevo y el anterior queda
revocado. Si perdió también el enlace, búscalo por su email y envíaselo por un canal
privado; seguirá necesitando su ID de orden:

```sql
select 'https://<tu-web>/pago/' || c.access_key as enlace, c.payment_code, c.fulfilled_at
from saas.saas_checkouts c join saas.saas_subscriptions s on s.id = c.subscription_id
where s.email = 'cliente@ejemplo.com' and c.status = 'fulfilled';
```

## Estructura

```
saas/
  config.py     variables de entorno
  db.py         tablas y motor de base de datos (Postgres/Supabase o SQLite)
  verifier.py   cliente HTTP de la API (verify + endpoints admin)
  billing.py    suscripciones: alta, pago, activación, renovación, vencimiento, tokens
  web.py        páginas y formularios
  security.py   CSRF, límites por IP, cabeceras de seguridad
  main.py       app FastAPI y barrido periódico de vencimientos
  templates/    HTML (Jinja2)   static/  CSS y JS mínimo (todo funciona sin JS)
tests/          flujo completo con una API simulada + cliente HTTP real con MockTransport
```
