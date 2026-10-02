# Binance Payment Verifier

API backend (FastAPI + **Supabase**/PostgreSQL) que **verifica pagos recibidos en Binance**
y **reclama cada pago de forma atómica** para que nunca pueda usarse para pagar dos órdenes.

Fuente de evidencias (`PAYMENT_EVIDENCE_SOURCE`):

* **`binance_api` (por defecto, recomendada):** la API oficial de historial de Binance Pay
  de tu cuenta (`GET /sapi/v1/pay/transactions`, API key **solo lectura**). Devuelve el
  **`transactionId`** de cada transferencia (p. ej. `P_A99TESTPAYX71116`), que es el
  `paymentCode` que se verifica. Ver [API de Binance Pay](#fuente-recomendada-api-de-historial-de-binance-pay).
* **`email`:** notificaciones de Binance en Gmail. Los emails reales de "Payment Receive
  Successful" **no incluyen el ID de la transacción** (solo monto, hora y nickname del
  pagador), así que por sí solos no permiten saber qué orden se pagó.

> ## ⚠️ Nunca proporciones la contraseña normal de tu cuenta de Google
>
> Esta aplicación se conecta a Gmail **solo** con una **App Password** (contraseña de
> aplicación de 16 letras) o con **OAuth2**. La configuración rechaza cualquier valor de
> `GMAIL_APP_PASSWORD` que no tenga el formato de una App Password. La autenticación con
> la contraseña normal está implementada pero **deshabilitada** y no debe activarse.

> ## ⚠️ Fuente `email`: plantillas SIMULADAS
>
> Ya se analizó un email real (remitente `donotreply@directmail.binance.com`, asunto
> `[Binance] Payment Receive Successful`), y **no contiene el ID de la transacción**. Por
> eso la fuente por defecto es la API. Las plantillas del parser y las fixtures `.eml`
> siguen siendo **simuladas** y están marcadas como tal.
> En producción (`APP_ENV=production`) las plantillas simuladas están **desactivadas**:
> `/ready` devolverá `not_ready` hasta que añadas la plantilla real
> (ver [Introducir un email Binance real](#cómo-introducir-un-email-binance-real-como-fixture)).
> Toda esa incertidumbre está aislada en un único archivo:
> `app/integrations/binance/templates.py`.

---

## Índice

1. [API de historial de Binance Pay (fuente recomendada)](#fuente-recomendada-api-de-historial-de-binance-pay)
2. [Arquitectura](#arquitectura)
3. [Instalación](#instalación)
4. [Supabase (almacenamiento)](#supabase-almacenamiento)
5. [Variables de entorno](#variables-de-entorno)
6. [Gmail: App Password](#gmail-método-a--app-password)
7. [Gmail: OAuth2](#gmail-método-b--oauth2--xoauth2)
8. [Iniciar la API y el worker](#iniciar-la-api-y-el-worker)
9. [Migraciones](#migraciones)
10. [Tests, lint y type checking](#tests-lint-y-type-checking)
11. [Introducir un email Binance real como fixture](#cómo-introducir-un-email-binance-real-como-fixture)
12. [Configurar remitentes permitidos](#configurar-remitentes-permitidos)
13. [Probar `/v1/payments/verify`](#probar-v1paymentsverify)
14. [Anti-replay, idempotencia y concurrencia](#anti-replay-idempotencia-y-concurrencia)
15. [Seguridad](#seguridad)
16. [Binance Pay Merchant API (futuro)](#binance-pay-merchant-api-futuro)
17. [Riesgos y limitaciones](#riesgos-y-limitaciones-de-verificar-pagos-mediante-email)

---

## Fuente recomendada: API de historial de Binance Pay

Endpoint oficial de tu cuenta normal de Binance (no requiere cuenta de comerciante):
`GET /sapi/v1/pay/transactions` ("Get Pay Trade History"). Por cada transferencia devuelve
`transactionId`, `orderType` (`C2C` para transferencias entre usuarios), `amount`
(positivo = ingreso), `currency`, `transactionTime` y `payerInfo`.

Verificado con una cuenta real: una transferencia notificada por email (98.814 USDT a una
hora concreta, de un nickname `User-xxxxxxxx`) aparece en la API con la misma hora, monto
y pagador, y con su ID (formato `P_` + 16 caracteres en mayúsculas). Ejemplo (anonimizado):
`C2C P_A99TESTPAYX71116 98.814 USDT 2026-09-19 01:16:42 User-0000aaaa`.

**Configuración**

1. Binance → *Gestión de API* → crea una API key con **solo "Enable Reading"** (nada de
   trading ni retiros) y, si puedes, restringida a la IP de tu servidor.
2. `.env`: `PAYMENT_EVIDENCE_SOURCE=binance_api`, `BINANCE_API_KEY`, `BINANCE_API_SECRET`
   y `PAYMENT_CODE_CASE_INSENSITIVE=true` (los IDs observados son siempre mayúsculas).
3. Prueba: `POST /v1/admin/binance/test` (debe dar `connected: true`) y
   `POST /v1/admin/binance/sync` (importa las transferencias recientes).

**Cómo funciona**

* El worker consulta la API cada `BINANCE_API_SYNC_INTERVAL_SECONDS` (15 s) y guarda las
  transferencias **entrantes** (`amount > 0`, `orderType` en `BINANCE_PAY_ORDER_TYPES`,
  por defecto `C2C`) en `payments` (`source=BINANCE_PAY_HISTORY`,
  `payment_code=transactionId`). Las salientes y otros tipos se ignoran.
* `POST /v1/payments/verify` busca el `transactionId` exacto en PostgreSQL; si no está,
  consulta Binance en ese momento y vuelve a buscar. Después aplica las mismas reglas:
  monto `Decimal` exacto, asset, antigüedad, claim atómico e idempotencia.
* Sincronización incremental con cursor y ventana de solape
  (`BINANCE_API_OVERLAP_SECONDS`) en la tabla `evidence_sync_state`.
* **Límite de peso:** cada llamada pesa 3000 (UID). Un advisory lock y
  `BINANCE_API_MIN_INTERVAL_SECONDS` (guardado en PostgreSQL) garantizan que, aunque
  lleguen muchas verificaciones o haya varias réplicas, no se llama a Binance más de una
  vez por intervalo.
* Firma HMAC-SHA256, montos como `Decimal`, reintentos limitados ante 5xx/429/red,
  corrección automática del desfase de reloj (`-1021`), y ni la key, ni el secret, ni la
  firma aparecen en logs, excepciones o respuestas.

**Flujo para tu cliente:** paga por Binance Pay a tu cuenta y te envía el **ID de la
transacción** de su comprobante. Tu sistema llama a `/v1/payments/verify` con ese ID, el
monto esperado y tu `orderReference`.

> Confirma con un pago real que el ID que el **pagador** ve en el detalle de su
> transferencia en la app de Binance es el mismo `transactionId` que devuelve la API.

## Arquitectura

```
POST /v1/payments/verify
        │
        ▼
PaymentVerifier ──────────────► PaymentEvidenceProvider (Protocol)
  │  formato del código              │
  │  confianza / asset / monto       ├── BinanceEmailPaymentProvider  (hoy)
  │  estado / ventana temporal       │      1. busca en PostgreSQL (igualdad exacta)
  │                                  │      2. si no está → MailSyncService (on-demand)
  │                                  │      3. vuelve a buscar
  │                                  └── BinancePayApiProvider       (preparado, no activo)
  ▼
PaymentClaimService ── transacción PostgreSQL: SELECT … FOR UPDATE + UNIQUE(payment_id)
        │
        ▼
     VERIFIED / ALREADY_CLAIMED / …

MailSyncService (worker periódico + on-demand)
  MailProvider (Protocol) ── GmailImapProvider (IMAP SSL, App Password o XOAUTH2)
        │ UIDs > último UID procesado (cursor + UIDVALIDITY), BODY.PEEK[]
        ▼
  EmailTrustValidator  → remitente exacto, Message-ID, Authentication-Results (DKIM/SPF/DMARC)
  BinanceEmailParser   → plantillas centralizadas → ParsedBinancePayment
        ▼
  PostgreSQL: email_messages, payments (dedupe por UID, Message-ID y payment_code)
```

Separación de responsabilidades:

| Responsabilidad | Módulo |
|---|---|
| Conexión al buzón | `app/integrations/mail/` (`base.py`, `gmail_imap.py`, `gmail_oauth.py`, `factory.py`) |
| Sincronización | `app/services/mail_sync.py`, `app/workers/mail_sync_worker.py` |
| Autenticidad del email | `app/integrations/binance/email_validator.py` |
| Parsing MIME / HTML | `app/integrations/binance/email_parser.py` |
| Formato Binance (regex) | `app/integrations/binance/templates.py` (**único lugar**) |
| Evidencia neutral | `app/integrations/binance/evidence.py` (`PaymentEvidence`, `PaymentEvidenceProvider`) |
| Almacenamiento | `app/db/models`, `app/db/repositories/` |
| Verificación | `app/services/payment_verifier.py` |
| Claim / idempotencia | `app/services/payment_claim.py` |
| HTTP | `app/api/routes/` (`payments.py`, `gmail.py`, `health.py`) |
| Composición | `app/container.py` |

```
app/
  main.py  config.py  container.py
  api/        deps.py, routes/{health,gmail,payments}.py
  core/       security.py, encryption.py, logging.py, exceptions.py
  db/         base.py, session.py, models/, repositories/
  integrations/
    mail/     base.py, gmail_imap.py, gmail_oauth.py, factory.py
    binance/  email_parser.py, email_validator.py, templates.py, evidence.py, pay_api.py
  services/   mail_sync.py, email_evidence.py, payment_verifier.py, payment_claim.py, rate_limiter.py
  schemas/    gmail.py, payments.py
  workers/    mail_sync_worker.py
alembic/      env.py, versions/
tests/        unit/, integration/, fixtures/binance/*.eml, emails.py
```

**Stateless, sin almacenamiento interno:** nada necesario para operar se guarda en el
filesystem ni en los contenedores. La base de datos es **Supabase** (PostgreSQL gestionado),
fuente de verdad de mensajes sincronizados, pagos, claims, cursores IMAP, cuentas,
rate limiting y locks (advisory locks). La imagen Docker funciona con rootfs read-only.

**Redis no se usa:** PostgreSQL ya cubre rate limiting compartido (contadores de ventana
fija), locks distribuidos (advisory locks) e idempotencia. Añadir Redis no aportaba
garantías adicionales para el volumen esperado.

## Instalación

Requisitos: Python **3.14+** y un proyecto de **Supabase** (o cualquier PostgreSQL 14+).

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # y rellena los valores
```

Con Docker Compose (API + worker + migraciones automáticas contra Supabase):

```bash
cp .env.example .env          # DATABASE_URL de Supabase, API_KEYS, GMAIL_*, BINANCE_ALLOWED_*…
docker compose up --build
curl http://127.0.0.1:8000/health
```

Para desarrollo sin conexión existe un PostgreSQL local opcional:
`docker compose --profile local-db up --build` (y en `.env`
`DATABASE_URL=postgresql://binance:binance@postgres:5432/binance_pay`).

## Supabase (almacenamiento)

Supabase es PostgreSQL gestionado, así que **toda** la persistencia vive allí: mensajes
sincronizados, pagos, claims (anti-replay), idempotencia, cursores IMAP, cuentas de correo
con credenciales cifradas, rate limiting y locks entre réplicas. La API y el worker no
guardan nada localmente y pueden ejecutarse en cualquier sitio (Docker, Render, Fly.io,
Railway, Cloud Run…) apuntando al mismo proyecto.

1. Crea un proyecto en <https://supabase.com/dashboard> y guarda la contraseña de la base
   de datos.
2. Pulsa **Connect** y copia una cadena de conexión. Pégala tal cual en `DATABASE_URL`
   (se aceptan `postgres://` y `postgresql://`; el driver psycopg se elige solo):

   | Modo | URL | Uso |
   |---|---|---|
   | **Session pooler** (recomendado) | `postgresql://postgres.<ref>:<pass>@aws-0-<region>.pooler.supabase.com:5432/postgres` | API, worker y migraciones. Funciona con IPv4 |
   | Direct connection | `postgresql://postgres:<pass>@db.<ref>.supabase.co:5432/postgres` | Igual que session; solo IPv6 salvo add-on IPv4 |
   | Transaction pooler | `…pooler.supabase.com:6543/postgres` | Serverless. Soportado; no lo uses para migraciones |

3. Aplica las migraciones: `alembic upgrade head` (en Docker Compose lo hace el servicio
   `migrate`).
4. Arranca la API y el worker. `GET /ready` confirma la conexión y que el esquema existe.

Qué hace la aplicación automáticamente con Supabase:

* **TLS obligatorio** (`sslmode=require`) para hosts `*.supabase.co`/`*.supabase.com`.
  Para verificar también el certificado: descarga el certificado CA desde el dashboard
  (*Database Settings → SSL*) y usa `DATABASE_SSL_MODE=verify-full` añadiendo
  `?sslrootcert=/ruta/ca.crt` a la URL.
* **Pooler en modo transacción** (puerto 6543, detectado solo o con
  `DATABASE_POOLER_MODE=transaction`): desactiva los prepared statements y las
  opciones de arranque (no soportadas por Supavisor) y cambia el lock de sincronización a
  `pg_try_advisory_xact_lock` dentro de una transacción. Las garantías anti-replay no
  cambian (`SELECT … FOR UPDATE` + `UNIQUE` dentro de una transacción funcionan igual).
* **Tablas cerradas a la Data API de Supabase:** la migración `0002` activa **Row Level
  Security** sin políticas en todas las tablas y revoca los privilegios de los roles
  `anon` y `authenticated`. Ni con la *anon key* ni con un usuario autenticado se pueden
  leer pagos, claims o credenciales cifradas vía REST/GraphQL. El backend conecta como
  `postgres` (propietario de las tablas), por lo que no le afecta.
* No uses la *service_role key* ni el cliente HTTP de Supabase: el backend habla
  PostgreSQL directamente, que es lo que permite transacciones y locks.

Tamaño del pool: `DATABASE_POOL_SIZE` + `DATABASE_MAX_OVERFLOW` por réplica (API y worker)
debe caber en el límite de conexiones de tu plan de Supabase. En modo transacción el
pooler multiplexa conexiones y el límite pesa menos.

Copias de seguridad, point-in-time recovery y monitorización quedan a cargo de Supabase
según tu plan.

## Variables de entorno

Todas están documentadas en [`.env.example`](.env.example). Las esenciales:

| Variable | Descripción |
|---|---|
| `DATABASE_URL` | Cadena de conexión de Supabase (o cualquier PostgreSQL), pegada tal cual |
| `DATABASE_SSL_MODE` / `DATABASE_POOLER_MODE` | Opcionales: por defecto `require` en Supabase y modo de pooler autodetectado |
| `API_KEYS` | Claves (≥ 32 caracteres, separadas por coma) para `/v1/payments/verify` |
| `ADMIN_API_KEYS` | Claves para `/v1/admin/*` (distintas de las anteriores) |
| `CREDENTIALS_ENCRYPTION_KEY` | Master key AES-256-GCM (32 bytes base64). Obligatoria en producción |
| `GMAIL_EMAIL`, `GMAIL_AUTH_METHOD` | Cuenta y método (`app_password` u `oauth2`) |
| `GMAIL_APP_PASSWORD` | App Password de 16 letras (solo método A) |
| `GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI` | OAuth2 (método B) |
| `BINANCE_ALLOWED_SENDERS` / `BINANCE_ALLOWED_DOMAINS` | Remitentes observados en emails reales |
| `MAIL_INITIAL_LOOKBACK_HOURS` | Ventana del primer bootstrap (por defecto 48 h) |
| `MAIL_SYNC_INTERVAL_SECONDS` | Intervalo del worker (por defecto 15 s) |
| `MAX_PAYMENT_AGE_MINUTES` | Máximo permitido para `maxAgeMinutes` (por defecto 1440) |
| `STORE_RAW_EMAILS` | `false` por defecto. Si se activa, el raw se guarda **cifrado** |
| `EMAIL_REQUIRE_DKIM/SPF/DMARC` | Política de confianza (todo `true` por defecto) |
| `PAYMENT_AMOUNT_TOLERANCE` | `0` = igualdad exacta (por defecto). Otra cosa es una decisión explícita |

Generar claves:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"                 # API keys
python -c "from app.core.encryption import generate_key; print(generate_key())" # cifrado
```

`VAR=` vacío significa "no configurado".

## Gmail método A — App Password

1. Activa la **verificación en dos pasos** en la cuenta de Google
   (Cuenta de Google → Seguridad → Verificación en dos pasos). Las App Passwords solo
   existen con 2FA activo.
2. Ve a <https://myaccount.google.com/apppasswords>, escribe un nombre (p. ej.
   `binance-verifier`) y pulsa **Crear**.
3. Google muestra **16 letras** (en 4 grupos). Cópialas en `GMAIL_APP_PASSWORD`
   (con o sin espacios). Esa es la única "contraseña" que acepta esta aplicación.
4. Comprueba que IMAP está disponible (Gmail → Configuración → Ver toda la configuración
   → Reenvío y correo POP/IMAP; en cuentas recientes IMAP está siempre activo).
5. Configura `GMAIL_EMAIL=tu-cuenta@gmail.com` y `GMAIL_AUTH_METHOD=app_password`.
6. Prueba la conexión: `POST /v1/admin/mail/test` (ver más abajo).

Notas:

* La App Password **nunca se persiste**: se lee solo de la variable de entorno; no se
  imprime en logs ni se devuelve por ningún endpoint.
* Si no aparece la opción de App Passwords: la cuenta no tiene 2FA, usa Protección
  Avanzada, o es Google Workspace con la opción deshabilitada por el administrador. En ese
  caso usa OAuth2.
* Revoca la App Password en la misma página si sospechas de una filtración.

## Gmail método B — OAuth2 / XOAUTH2

1. En <https://console.cloud.google.com/> crea (o elige) un proyecto.
2. **Pantalla de consentimiento OAuth**: tipo *Internal* si es una cuenta Google Workspace
   de tu organización; si es *External*, añade tu cuenta como *test user*.
   El scope necesario para IMAP es `https://mail.google.com/` (scope restringido).
3. **Credenciales → Crear ID de cliente OAuth → Aplicación web**. Añade como URI de
   redirección autorizada exactamente `GOOGLE_REDIRECT_URI`, p. ej.
   `https://tu-api.example.com/v1/admin/mail/oauth/callback`.
4. Configura `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`,
   `CREDENTIALS_ENCRYPTION_KEY` y `GMAIL_AUTH_METHOD=oauth2`.
5. Obtén la URL de consentimiento:

   ```bash
   curl -s -H "Authorization: Bearer $ADMIN_KEY" \
     "https://tu-api.example.com/v1/admin/mail/oauth/authorize?login_hint=tu-cuenta@gmail.com"
   ```

6. Abre `authorizationUrl` en el navegador y acepta. Google redirige al callback, que:
   valida el `state` (token **cifrado** con el verificador PKCE y caducidad, sin estado en
   servidor), intercambia el código, lee el email verificado de la cuenta y guarda el
   **refresh token cifrado (AES-256-GCM)** en `mail_accounts`.
7. El access token se obtiene en cada réplica a partir del refresh token y solo se cachea
   en memoria (es una caché, no estado).

Alternativa headless: define `GMAIL_OAUTH_REFRESH_TOKEN` (se guarda cifrado al arrancar la
sincronización).

> Con la app OAuth en estado *Testing* (External), Google caduca los refresh tokens a los
> 7 días. Para uso continuado usa *Internal* (Workspace) o publica/verifica la app.

## Iniciar la API y el worker

```bash
alembic upgrade head
uvicorn --factory app.main:app_factory --host 0.0.0.0 --port 8000   # API
python -m app.workers.mail_sync_worker                              # sync periódica
```

* El worker puede ejecutarse en varias réplicas: un advisory lock de PostgreSQL por cuenta
  evita trabajo duplicado y las constraints UNIQUE hacen idempotente el reprocesado.
* Alternativa para despliegues pequeños: `RUN_SYNC_WORKER_IN_API=true` ejecuta el bucle
  dentro del proceso de la API.
* Aunque el worker no esté corriendo, `/v1/payments/verify` sincroniza on-demand cuando un
  código no está todavía en la base de datos.
* HTTPS: en producción termina TLS en un reverse proxy / load balancer. Con
  `APP_ENV=production` se envía HSTS y la documentación OpenAPI se desactiva
  (reactivable con `ENABLE_DOCS=true`).
* Documentación interactiva (fuera de producción): <http://127.0.0.1:8000/docs>.

Health checks: `GET /health` (proceso vivo) y `GET /ready` (PostgreSQL + esquema migrado
+ configuración esencial: API keys, remitentes Binance, plantillas activas, clave de
cifrado válida, cuenta de correo). Ninguno expone secretos.

## Migraciones

```bash
alembic upgrade head          # aplica
alembic downgrade -1          # revierte la última
alembic revision --autogenerate -m "describe change"   # nueva migración
alembic check                 # verifica que modelos y migraciones coinciden
```

La URL sale de `DATABASE_URL` (nunca de `alembic.ini`). Con Supabase usa la URL del
*session pooler* o la conexión directa para migrar. En Docker Compose el servicio `migrate`
ejecuta `alembic upgrade head` antes de arrancar `api` y `worker`.

## Tests, lint y type checking

Los tests de integración necesitan PostgreSQL (se omiten si no está disponible). Usan una
base de datos dedicada que **se borra y recrea**: usa un PostgreSQL local o de CI, **nunca
tu proyecto de Supabase de producción**. Los tests crean los roles `anon` y
`authenticated` para comprobar el bloqueo de la Data API de Supabase.

```bash
createdb binance_pay_test
export TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/binance_pay_test
pytest                       # unit + integración
pytest tests/unit            # solo unitarios (sin base de datos)
ruff check . && ruff format --check .
mypy app                     # modo strict
```

Cobertura de los tests (entre otros): email válido, remitente falso (display name y
lookalike), DKIM/SPF/DMARC fallidos, `Authentication-Results` falsificado, código correcto e
incorrecto (prefijos/sufijos/caso), monto correcto e incorrecto, Decimal con distintas
escalas, asset incorrecto, ventana temporal, email duplicado, payment code duplicado,
evidencia contradictoria, claim, doble claim, **10 requests concurrentes para el mismo
pago (exactamente 1 `VERIFIED`)**, misma `orderReference` repetida, dos instancias
sincronizando a la vez, IMAP caído, timeout IMAP, HTML, texto plano y multipart.

## Cómo introducir un email Binance real como fixture

1. Realiza (o recibe) un pago real de prueba en Binance hacia la cuenta asociada al Gmail.
2. En Gmail abre la notificación → menú ⋮ → **Mostrar original** → **Descargar original**.
   Obtienes un `.eml` con cabeceras completas.
3. En "Mostrar original" anota:
   * el remitente exacto (`From:`, la dirección entre `< >`, no el nombre visible);
   * el dominio DKIM (`dkim=pass header.i=@…`), y que SPF y DMARC sean `PASS`;
   * cómo aparecen el identificador del pago, el monto, el asset, el estado y la fecha.
4. **Anonimiza** el archivo sin cambiar su estructura: sustituye nombres, emails
   personales, IDs de usuario, IPs y enlaces de seguimiento. No cambies `From`, las
   etiquetas de los campos (p. ej. "Amount") ni el HTML que las rodea. Si alteras el
   cuerpo, la firma DKIM ya no verificará, pero eso no afecta a los tests (el validador
   lee el resultado que Gmail dejó en `Authentication-Results`).
5. Guárdalo como `tests/fixtures/binance/real_<tipo>_<aaaamm>.eml`.
6. Añade en `app/integrations/binance/templates.py`, dentro de `REAL_TEMPLATES`, una
   `BinanceEmailTemplate` con `simulated=False`: patrón del asunto, marcadores
   obligatorios, regex del código (`CODE`), del monto (`AMOUNT` + `ASSET`), del estado
   (con su `status_map`) y de la fecha. Reutiliza los bloques `CODE`, `AMOUNT`, `ASSET`.
   Las regex se aplican al texto normalizado (HTML sin tags, entidades decodificadas,
   espacios colapsados). Usa `python -c "from app.integrations.binance.email_parser import *; m=parse_mime(open('X.eml','rb').read()); print(body_texts(m))"` para verlo.
7. Añade un test en `tests/unit/test_binance_email_parser.py` que parsee la fixture y
   compruebe código, monto (`Decimal`), asset, estado y fecha.
8. Configura `BINANCE_ALLOWED_SENDERS`/`BINANCE_ALLOWED_DOMAINS` con lo observado y, en
   producción, deja `BINANCE_ALLOW_SIMULATED_TEMPLATES` vacío/`false`.
9. Si el identificador de Binance no distingue mayúsculas/minúsculas (confírmalo),
   activa `PAYMENT_CODE_CASE_INSENSITIVE=true` **antes** de importar pagos.

Nada más en la aplicación necesita cambiar: el resto del código solo consume
`ParsedBinancePayment` / `PaymentEvidence`.

## Configurar remitentes permitidos

No se incluye ninguna dirección de Binance hardcodeada. Usa **solo** lo observado en
notificaciones reales (paso 3 anterior):

```bash
BINANCE_ALLOWED_SENDERS=direccion-exacta-observada@dominio-observado
BINANCE_ALLOWED_DOMAINS=dominio-observado            # coincidencia exacta del dominio
# BINANCE_ALLOWED_DOMAINS=*.dominio-observado        # permite también subdominios
```

* Se compara la **dirección** del `From` (ASCII, parseo estricto), nunca el nombre visible.
  Un `From` con varias direcciones o malformado se rechaza.
* Si Binance usa varias direcciones para distintos tipos de notificación, añádelas
  separadas por comas.
* Estas listas también se usan como pre-filtro IMAP (`SEARCH FROM`), pero la autenticidad
  se valida siempre en el cliente: remitente permitido + `Message-ID` válido + la cabecera
  `Authentication-Results` **superior** emitida por `mx.google.com` (las inferiores pueden
  estar falsificadas por el emisor) con DKIM `pass` alineado con el dominio del `From`,
  SPF `pass` y DMARC `pass`. Cada requisito es configurable (`EMAIL_REQUIRE_*`).
* Si no hay remitentes configurados, ningún email es de confianza (fail-closed).

## Probar `/v1/payments/verify`

```bash
curl -s https://tu-api.example.com/v1/payments/verify \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "paymentCode": "P_A99TESTPAYX71116",
        "expectedAmount": "98.814",
        "asset": "USDT",
        "orderReference": "SUBSCRIPTION-9321",
        "maxAgeMinutes": 60
      }'
```

```json
{
  "verified": true,
  "status": "VERIFIED",
  "paymentCode": "P_A99TESTPAYX71116",
  "expectedAmount": "98.814",
  "receivedAmount": "98.814",
  "asset": "USDT",
  "receivedAt": "2026-09-19T01:16:42Z",
  "orderReference": "SUBSCRIPTION-9321",
  "retryable": false
}
```

Repetir con la misma `orderReference` devuelve `VERIFIED` con `"idempotent": true`.
Otra orden (`SUBSCRIPTION-9999`) recibe `ALREADY_CLAIMED`.

Reglas de la petición: `expectedAmount` debe ser un **string decimal** (`"25.50"`); los
floats JSON se rechazan (422). `asset` por defecto `USDT`. `maxAgeMinutes` por defecto
`DEFAULT_PAYMENT_MAX_AGE_MINUTES` y nunca mayor que `MAX_PAYMENT_AGE_MINUTES`. Longitudes
máximas: código y `orderReference` 128 caracteres; cuerpo 16 KB.

Todas las verificaciones procesadas responden **HTTP 200** con `verified`, `status` y
`retryable`:

| status | Significado | retryable |
|---|---|---|
| `VERIFIED` | Pago de confianza, código/monto/asset exactos, reciente, reclamado para esta orden | – |
| `NOT_FOUND` | No existe un pago con exactamente ese código (tras sincronizar on-demand) | sí |
| `PENDING_SYNC` | Otra réplica está sincronizando el buzón | sí |
| `AMOUNT_MISMATCH` | El monto recibido no es igual (comparación `Decimal`) | no |
| `ASSET_MISMATCH` | El asset no coincide (p. ej. USDC vs USDT) | no |
| `UNTRUSTED_EMAIL` | Solo hay evidencia que no pasó remitente/DKIM/SPF/DMARC | no |
| `EXPIRED_PAYMENT` | Más antiguo que `maxAgeMinutes` (o fecha en el futuro) | no |
| `ALREADY_CLAIMED` | El pago ya fue usado por otra orden | no |
| `INVALID_PAYMENT_CODE` | Formato de código inválido | no |
| `MAIL_PROVIDER_UNAVAILABLE` | Gmail/IMAP caído, timeout o error de autenticación (fuente `email`) | sí |
| `BINANCE_API_UNAVAILABLE` | API de Binance caída, límite de peso o API key rechazada (fuente `binance_api`) | sí |
| `PAYMENT_NOT_COMPLETED` | *(extensión)* Notificación con estado pendiente/fallido/reembolsado | sí |
| `AMBIGUOUS_PAYMENT` | *(extensión)* Evidencias de confianza contradictorias para el mismo código → revisión manual | no |

Otros códigos HTTP: `401` API key ausente/incorrecta, `413` cuerpo demasiado grande,
`422` validación, `429` rate limit (con `Retry-After`).

Endpoints administrativos (requieren `ADMIN_API_KEYS`):

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN_KEY" https://…/v1/admin/mail/test
# {"connected": true, "provider": "gmail", "account": "tu*******@gmail.com", "authType": "app_password", "error": null}

curl -s -X POST -H "Authorization: Bearer $ADMIN_KEY" https://…/v1/admin/mail/sync
# {"messagesScanned": 12, "binanceMessages": 2, "paymentsImported": 1, "duplicates": 1, …}
```

Ninguno lista mensajes ni devuelve contenido de emails o credenciales.

## Anti-replay, idempotencia y concurrencia

* **Claim transaccional:** en una transacción se bloquea la fila del pago
  (`SELECT … FOR UPDATE`), se comprueba si ya existe claim y se inserta.
  `payment_claims.payment_id` es **UNIQUE**: aunque el lock se saltara, el segundo INSERT
  falla y se responde `ALREADY_CLAIMED`. Resultado garantizado: `A → VERIFIED`,
  `B → ALREADY_CLAIMED`, nunca dos `VERIFIED` (test con 10 réplicas concurrentes).
* **Idempotencia:** la misma `orderReference` + mismo pago devuelve `VERIFIED`
  (`idempotent: true`) incluso si el pago ya superó `maxAgeMinutes`. Sin
  `orderReference` no se puede reconocer al llamante original, así que una segunda
  petición recibe `ALREADY_CLAIMED`.
* **Deduplicación de evidencia:** `UNIQUE(mail_account_id, uidvalidity, imap_uid)`,
  `UNIQUE(mail_account_id, message_id)`, `UNIQUE(source, external_id)` en pagos y un índice
  único parcial que permite **un solo pago de confianza por `payment_code`**. Un segundo
  email de confianza con el mismo código y mismos datos es un duplicado; con datos
  distintos (o `PAID` seguido de `REFUNDED`) el pago se marca `ambiguous` y nunca se
  verifica automáticamente. `PENDING` → `PAID` actualiza el estado.
* **Múltiples instancias:** sin locks en memoria ni variables globales de negocio. La
  sincronización usa un advisory lock de PostgreSQL por cuenta; el cursor IMAP avanza en la
  misma transacción que guarda el mensaje.

## Seguridad

* **Credenciales cifradas** con AES-256-GCM (`cryptography`), nonce aleatorio, *associated
  data* que liga cada ciphertext a su cuenta (no se puede copiar a otra fila) e
  identificador de clave para rotación (`CREDENTIALS_ENCRYPTION_PREVIOUS_KEYS`). La master
  key solo viene de `CREDENTIALS_ENCRYPTION_KEY`.
* **Sin secretos en logs, excepciones o respuestas:** `SecretStr` en configuración, filtro
  de logging que redacta todos los secretos configurados, claves sensibles, tokens Bearer y
  cadenas XOAUTH2 (también en tracebacks); errores IMAP genéricos y sin encadenar la
  respuesta del servidor; SQL sin eco y con `hide_parameters`. Si integras Sentry/tracing,
  aplica el mismo filtro (`app.core.logging.get_redaction_filter`) en `before_send`.
* **API:** API key Bearer (comparación en tiempo constante; claves admin separadas),
  rate limiting compartido en PostgreSQL por API key, validación estricta con Pydantic
  (campos extra prohibidos, longitudes máximas, sin floats para dinero), límite de tamaño
  de cuerpo, CORS cerrado por defecto, `TrustedHost` opcional, cabeceras de seguridad
  (CSP, `nosniff`, `X-Frame-Options`, `Referrer-Policy`, `no-store`, HSTS en producción).
* **IMAP:** TLS ≥ 1.2 con verificación de certificado, timeouts de socket + guarda asyncio,
  reintentos limitados con backoff solo para errores transitorios (nunca de autenticación),
  buzón en solo lectura y `BODY.PEEK[]` (no marca como leído), filtros de búsqueda
  validados contra inyección IMAP.
* **Parser:** solo tokeniza HTML con la stdlib; nunca ejecuta JavaScript ni carga contenido
  externo; el contenido de `<script>`/`<style>` se descarta. Ambigüedad = rechazo.
* **Datos mínimos:** no se guarda el cuerpo del email (solo hash SHA-256 y metadatos).
  `STORE_RAW_EMAILS=true` guarda el raw **cifrado**, y solo de remitentes Binance.

## Binance Pay Merchant API (futuro)

`PaymentVerifier` depende de `PaymentEvidenceProvider`, no del email. En
`app/integrations/binance/pay_api.py` están preparados `BinancePayClient` (firma
HMAC-SHA512 de la API Merchant, consulta de orden), el DTO `BinancePayOrder` (`status`,
`transactionId`, `merchantTradeNo`, `prepayId`, `currency`, `totalFee`, `transactTime`) y
`BinancePayApiProvider`, que produce el mismo `PaymentEvidence`
(`source=BINANCE_PAY_API`). Para activarlo: obtener credenciales Merchant, confirmar el
contrato contra la documentación oficial, implementar la persistencia en `payments`
(mismo claim transaccional) y seleccionar el provider en `app/container.py`.

## Riesgos y limitaciones de verificar pagos mediante email

* **El email no es la fuente de verdad de Binance.** Es una notificación; puede retrasarse,
  no llegar, llegar a spam o a otra etiqueta (configura `GMAIL_MAILBOX` si usas filtros),
  o cambiar de formato sin aviso (el parser rechazará el email: los pagos quedarán
  `NOT_FOUND`, nunca verificados por error). Para volumen o importes altos, migra a la API
  oficial de Binance Pay.
* **Autenticidad delegada en Gmail:** confiamos en el resultado DKIM/SPF/DMARC que Gmail
  escribe en `Authentication-Results`. Si alguien obtiene acceso a la cuenta Gmail puede
  insertar mensajes en el buzón (p. ej. con IMAP APPEND) con cabeceras arbitrarias.
  Protege la cuenta (2FA, cuenta dedicada solo para esto, sin reenvíos) y revisa los
  `UNTRUSTED_EMAIL`/`AMBIGUOUS_PAYMENT`.
* **Reenvíos y listas** rompen SPF/DKIM: no reenvíes las notificaciones; deben llegar
  directamente de Binance a la cuenta monitorizada.
* **Código de pago:** su significado (ID de orden, de transacción, nota del pagador…)
  depende del formato real de Binance; confírmalo con un email real. Si el pagador puede
  elegir el valor (p. ej. una nota), dos pagos podrían declarar el mismo código: el sistema
  lo marca como ambiguo, pero diseña tus códigos para ser únicos e impredecibles.
* **Tiempo:** la antigüedad se calcula con la fecha del email (UTC); un reloj de servidor
  desajustado afecta a `EXPIRED_PAYMENT` (`PAYMENT_CLOCK_SKEW_SECONDS`).
* **Límites de Gmail:** IMAP tiene cuotas de ancho de banda y conexiones; la sincronización
  incremental y el intervalo mínimo on-demand (`MAIL_ON_DEMAND_MIN_INTERVAL_SECONDS`)
  lo mitigan, pero un abuso de códigos inexistentes provoca conexiones extra (de ahí el
  rate limiting por API key).
* **Privacidad:** se accede a un buzón real. Usa una cuenta dedicada, `GMAIL_MAILBOX` con
  una etiqueta exclusiva para Binance y mantén `STORE_RAW_EMAILS=false`.
* **OAuth en modo Testing** caduca refresh tokens a los 7 días (ver sección OAuth2).
