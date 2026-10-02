# Binance Payment Verifier

API backend privada (FastAPI + **Supabase**/PostgreSQL) que **verifica pagos recibidos en
Binance Pay** consultando la API oficial de historial de pagos de la cuenta de Binance de
cada cliente, y que **reclama cada pago de forma atómica** para que nunca pueda usarse para
pagar dos órdenes.

* **Privada y multi-cliente:** tú (el dueño) decides quién la usa. Cada persona a la que
  das acceso es un *cliente* con su propio **token privado** (`bpv_…`) y su propia cuenta
  de Binance. Ningún cliente puede ver ni reclamar pagos de otro.
* **Sin claves de Binance en el `.env`:** cada cliente registra **su** API key de Binance
  (**solo lectura**, se verifica) mediante un endpoint; se guarda **cifrada** en Supabase.
* **Sin estado local:** todo vive en Supabase (pagos, claims, tokens, credenciales cifradas,
  cursores, rate limiting y locks entre réplicas).

---

## Índice

1. [Cómo funciona el acceso](#cómo-funciona-el-acceso)
2. [Puesta en marcha](#puesta-en-marcha)
3. [Dar acceso a alguien (clientes y tokens)](#dar-acceso-a-alguien-clientes-y-tokens)
4. [Registrar la API key de Binance de un cliente](#registrar-la-api-key-de-binance-de-un-cliente)
5. [Verificar pagos](#verificar-pagos)
6. [Supabase (almacenamiento)](#supabase-almacenamiento)
7. [Arquitectura](#arquitectura)
8. [Variables de entorno](#variables-de-entorno)
9. [Tests, lint y type checking](#tests-lint-y-type-checking)
10. [Anti-replay, idempotencia y concurrencia](#anti-replay-idempotencia-y-concurrencia)
11. [Seguridad](#seguridad)
12. [Riesgos y limitaciones](#riesgos-y-limitaciones)

---

## Cómo funciona el acceso

Hay dos tipos de credencial:

| Quién | Credencial | Dónde vive | Para qué |
|---|---|---|---|
| **Tú (dueño)** | Clave maestra de admin | `ADMIN_API_KEYS` en el entorno del servidor | `/v1/admin/*`: crear clientes, emitir y revocar tokens |
| **Cada cliente** (tú incluido) | Token privado `bpv_…` | Solo el **hash** en Supabase | `/v1/me/*` y `/v1/payments/verify` |

Flujo completo:

```
Dueño ── POST /v1/admin/clients {"name": "Ana"} ──► token de Ana (se muestra UNA vez)
           │
           └── le pasas el token a Ana por un canal privado

Ana ──── PUT /v1/me/binance-credentials {apiKey, apiSecret} ──► verificada (solo lectura)
           │                                                     y guardada cifrada
           └── POST /v1/payments/verify {paymentCode, expectedAmount, …} ──► VERIFIED
```

Tú también eres un cliente: créate a ti mismo con `POST /v1/admin/clients` y usa tu token
para tus verificaciones. La clave maestra solo sirve para administrar.

## Puesta en marcha

Requisitos: Python **3.14+** y un proyecto de **Supabase** (o cualquier PostgreSQL 14+).

```bash
python3.14 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

1. **Base de datos:** pega en `DATABASE_URL` la URL del *Transaction pooler* de Supabase y
   ajusta `DB_POOL_MAX` (ver [Supabase](#supabase-almacenamiento)).
2. **Genera tus dos secretos** y ponlos en `.env`:

   ```bash
   python -m app.cli generate-admin-key        # -> ADMIN_API_KEYS
   python -m app.cli generate-encryption-key   # -> CREDENTIALS_ENCRYPTION_KEY
   ```

   Se generan **una sola vez** (no en cada arranque) y se guardan como secretos del entorno.
   Guárdalos también en un gestor de contraseñas. Si pierdes o cambias la clave de cifrado,
   las API keys de Binance guardadas no se podrán leer (los clientes tendrían que
   registrarlas de nuevo).
3. **Migraciones:** `alembic upgrade head` (con Docker Compose se aplican solas en cada
   arranque, mediante el servicio `migrate`).
4. **Arranca** la API y el worker:

   ```bash
   uvicorn --factory app.main:app_factory --host 0.0.0.0 --port 8000
   python -m app.workers.sync_worker
   ```

   O con Docker Compose (migraciones + API + worker contra Supabase):
   `docker compose up --build`.
5. Comprueba `GET /ready`: `database`, `admin_key_configured` y `encryption_key` en `true`.

En producción: `APP_ENV=production` (HSTS activado y `/docs` desactivado salvo
`ENABLE_DOCS=true`) y TLS terminado en un proxy o en la plataforma de hosting.

## Dar acceso a alguien (clientes y tokens)

> 📘 **Guía completa de los endpoints de administración** (todos los campos, respuestas,
> errores y ejemplos para PowerShell y curl): [`docs/admin-api.md`](docs/admin-api.md).

Puedes hacerlo por **HTTP** (con tu clave maestra) o con la **CLI** del servidor. Cada
token se muestra **una única vez**: cópialo y envíaselo a esa persona por un canal privado.

**Por HTTP**:

```bash
ADMIN="Authorization: Bearer $ADMIN_KEY"

# Crear un cliente -> devuelve su primer token
curl -s -X POST https://tu-api/v1/admin/clients -H "$ADMIN" \
     -H "Content-Type: application/json" -d '{"name": "Ana"}'
# {"client": {"id": 2, "name": "Ana", ...}, "token": {"id": 5, "token": "bpv_...", ...}}

# Otro token para el mismo cliente (p. ej. uno por servidor), opcionalmente con caducidad
curl -s -X POST https://tu-api/v1/admin/clients/2/tokens -H "$ADMIN" \
     -H "Content-Type: application/json" -d '{"name": "tienda", "expiresInDays": 90}'

curl -s https://tu-api/v1/admin/clients -H "$ADMIN"            # listar clientes
curl -s https://tu-api/v1/admin/clients/2/tokens -H "$ADMIN"   # tokens (sin su valor)
curl -s -X DELETE https://tu-api/v1/admin/tokens/5 -H "$ADMIN" # revocar un token
curl -s -X PATCH https://tu-api/v1/admin/clients/2 -H "$ADMIN" \
     -H "Content-Type: application/json" -d '{"enabled": false}' # cortar todo su acceso
```

Con `ENABLE_DOCS=true` puedes hacer lo mismo desde el navegador en `/docs` (botón
**Authorize** con tu clave maestra).

**Por CLI** (en el servidor, o `docker compose exec api python -m app.cli …`):

```bash
python -m app.cli create-client "Ana"                 # imprime el token una vez
python -m app.cli create-token 2 --name tienda --expires-in-days 90
python -m app.cli list-clients
python -m app.cli list-tokens 2
python -m app.cli revoke-token 5
python -m app.cli set-client-enabled 2 false
```

Buenas prácticas: un token por sistema que lo use (si uno se filtra, revocas solo ese),
caducidad para accesos temporales, y nunca compartir la clave maestra.

## Registrar la API key de Binance de un cliente

Cada cliente lo hace **una vez**, con su token:

1. En Binance → **Gestión de API** → *Crear API* (tipo "generada por el sistema").
2. Deja activado **solo "Enable Reading"**. Nada de *Spot & Margin Trading*, *Withdrawals*,
   *Internal Transfer*, *Universal Transfer*, *Futures* ni *Margin*.
3. Opcional pero recomendado: restringe la key a la **IP del servidor** de esta API.
4. Regístrala:

   ```bash
   curl -s -X PUT https://tu-api/v1/me/binance-credentials \
        -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        -d '{"apiKey": "...", "apiSecret": "..."}'
   # {"configured": true, "apiKeyHint": "…ab12", "ipRestricted": true, ...}
   ```

Antes de guardarla, la API consulta a Binance (`GET /sapi/v1/account/apiRestrictions`) y
**rechaza (422) cualquier key que no sea de solo lectura**, indicando qué permiso sobra. La
key nunca se guarda en ese caso. También comprueba que puede leer el historial de Binance
Pay. La key y el secret se guardan cifrados con AES-256-GCM y **no se devuelven nunca**
(solo los 4 últimos caracteres como referencia).

Otros endpoints del cliente: `GET /v1/me` (quién soy), `GET /v1/me/binance-credentials`
(estado), `DELETE /v1/me/binance-credentials` (borrarla), `POST /v1/me/binance/sync`
(importar ahora los pagos recientes).

## Verificar pagos

El pagador te envía por Binance Pay y te comparte el **ID de la transacción** de su
comprobante (formato `P_` + 16 caracteres). Tu sistema llama:

```bash
curl -s https://tu-api/v1/payments/verify \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
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

Repetir con la misma `orderReference` devuelve `VERIFIED` con `"idempotent": true`; otra
orden recibe `ALREADY_CLAIMED`. Solo se buscan pagos **de la cuenta de Binance del cliente
que llama**.

Cómo se obtiene la evidencia: el worker importa cada 15 s las transferencias **entrantes**
(`C2C`, monto positivo) de cada cliente desde `GET /sapi/v1/pay/transactions`, y si un ID
aún no está, `/verify` consulta Binance en ese momento. Un intervalo mínimo por cliente
(`BINANCE_API_MIN_INTERVAL_SECONDS`, guardado en la base de datos) evita superar el límite
de peso de Binance aunque haya muchas peticiones o réplicas.

Reglas de la petición: `expectedAmount` como **string decimal** (los floats JSON se
rechazan), `asset` por defecto `USDT`, `maxAgeMinutes` ≤ `MAX_PAYMENT_AGE_MINUTES`.

Todas las verificaciones procesadas responden **HTTP 200** con `verified`, `status` y
`retryable`:

| status | Significado | retryable |
|---|---|---|
| `VERIFIED` | Pago entrante, código/monto/asset exactos, reciente, reclamado para esta orden | – |
| `NOT_FOUND` | No hay un pago entrante con exactamente ese ID en tu cuenta (tras consultar Binance) | sí |
| `PENDING_SYNC` | Otra réplica está consultando Binance para tu cuenta | sí |
| `AMOUNT_MISMATCH` | El monto recibido no es igual (comparación `Decimal`) | no |
| `ASSET_MISMATCH` | El asset no coincide (p. ej. USDC vs USDT) | no |
| `EXPIRED_PAYMENT` | Más antiguo que `maxAgeMinutes` (o fecha en el futuro) | no |
| `ALREADY_CLAIMED` | El pago ya fue usado por otra orden | no |
| `INVALID_PAYMENT_CODE` | Formato de ID inválido | no |
| `BINANCE_API_UNAVAILABLE` | Binance caído, límite de peso o tu key fue revocada | sí |
| `BINANCE_NOT_CONFIGURED` | No has registrado tu API key de Binance | no |
| `AMBIGUOUS_PAYMENT` | Evidencias contradictorias para el mismo ID → revisión manual | no |
| `PAYMENT_NOT_COMPLETED` / `UNTRUSTED_EVIDENCE` | Reservados para fuentes futuras (Merchant API) | – |

Otros códigos HTTP: `401` token ausente, inválido, revocado, caducado o cliente desactivado;
`413` cuerpo demasiado grande; `422` validación (nunca devuelve los valores enviados);
`429` rate limit por token (con `Retry-After`).

`PAYMENT_CODE_CASE_INSENSITIVE=true` (recomendado): los IDs de Binance Pay son siempre
mayúsculas, así que se acepta el ID escrito en minúsculas. La comparación sigue siendo del
ID completo y exacto. Decide este valor antes de importar pagos y no lo cambies después.

## Supabase (almacenamiento)

Solo hacen falta **dos variables**:

```bash
# Supabase → Project Settings → Database → Connection string → "Transaction pooler"
# (puerto 6543). Usa el pooler: la conexión directa es solo IPv6.
DATABASE_URL="postgresql://postgres.<project-ref>:[YOUR-PASSWORD]@aws-0-<region>.pooler.supabase.com:6543/postgres"
DB_POOL_MAX=5
```

* Pega la URL tal cual y sustituye `[YOUR-PASSWORD]` por la contraseña de la base de datos
  (sin corchetes). Si se queda el placeholder, la app arranca con un error claro.
* La contraseña puede tener caracteres especiales (`@ # / ? :`) sin codificarlos. Si ya la
  tienes URL-codificada (`%40`…) también funciona; si contiene un `%` literal seguido de dos
  dígitos hexadecimales, escríbelo como `%25`.
* `DB_POOL_MAX`: conexiones máximas por proceso (la API y el worker cuentan por separado).
  Con el pooler de transacción, 5 es suficiente.
* Migraciones: `alembic upgrade head` con la misma `DATABASE_URL` (en Docker Compose lo hace
  el servicio `migrate`).

Lo que la app ajusta sola al ver el pooler de transacción de Supabase (puerto 6543):

* **TLS obligatorio** (`sslmode=require`).
* **Sin prepared statements ni opciones de arranque**, que Supavisor no soporta en este modo.
* **Locks entre réplicas ligados a la transacción** (`pg_try_advisory_xact_lock`), porque en
  este modo cada transacción puede ir por una conexión distinta. Las garantías anti-replay
  (`SELECT … FOR UPDATE` + `UNIQUE` dentro de una transacción) no cambian.
* **Tablas cerradas a la Data API de Supabase:** todas tienen **Row Level Security** sin
  políticas y sin privilegios para `anon`/`authenticated`. Ni con la *anon key* se pueden
  leer pagos, tokens o credenciales cifradas vía REST/GraphQL. El backend conecta como
  `postgres` (propietario), por lo que no le afecta.
* No uses la *service_role key* ni el cliente HTTP de Supabase: el backend habla PostgreSQL
  directamente (transacciones y locks).

Opcionales avanzados (normalmente no hacen falta): `DATABASE_SSL_MODE` (p. ej. `verify-full`
con `?sslrootcert=/ruta/ca.crt` en la URL) y `DATABASE_POOLER_MODE` (`session`/`transaction`,
autodetectado por el puerto). También funcionan el *Session pooler* (puerto 5432) y cualquier
PostgreSQL 14+.

## Arquitectura

```
POST /v1/payments/verify  (token bpv_… → cliente)
        │
        ▼
PaymentVerifier ──────────► PaymentEvidenceProvider (Protocol)
  │ formato / asset / monto       └── BinancePayHistoryProvider
  │ antigüedad / estado                 1. busca el ID en PostgreSQL (solo ese cliente)
  │                                     2. si no está → consulta Binance con SU key
  ▼                                     3. vuelve a buscar
PaymentClaimService ── transacción: SELECT … FOR UPDATE + UNIQUE(payment_id)

Worker ── por cada cliente con key: GET /sapi/v1/pay/transactions (cursor + solape)
          → payments (tenant_id, transactionId)
```

| Responsabilidad | Módulo |
|---|---|
| Clientes y tokens | `app/services/tenants.py`, `app/api/routes/admin.py`, `app/cli.py` |
| Credenciales Binance cifradas | `app/services/binance_credentials.py`, `app/api/routes/me.py` |
| Cliente API Binance (firma, permisos, historial) | `app/integrations/binance/account_api.py` |
| Sincronización + proveedor de evidencias | `app/services/binance_api_sync.py`, `app/workers/sync_worker.py` |
| Verificación / claim | `app/services/payment_verifier.py`, `app/services/payment_claim.py` |
| Cifrado / seguridad / logs | `app/core/` |
| Modelos / migraciones | `app/db/`, `alembic/versions/` |
| Binance Pay Merchant API (preparada, no activa) | `app/integrations/binance/pay_api.py` |

## Variables de entorno

Todas en [`.env.example`](.env.example). Las esenciales:

| Variable | Descripción |
|---|---|
| `DATABASE_URL` | Connection string del *Transaction pooler* de Supabase, pegada tal cual |
| `DB_POOL_MAX` | Conexiones máximas a la base de datos por proceso (5) |
| `ADMIN_API_KEYS` | Tu clave maestra (≥ 32 caracteres). `python -m app.cli generate-admin-key` |
| `CREDENTIALS_ENCRYPTION_KEY` | Clave AES-256 para las keys de Binance. `python -m app.cli generate-encryption-key` |
| `PAYMENT_CODE_CASE_INSENSITIVE` | `true` recomendado (IDs de Binance en mayúsculas) |
| `BINANCE_API_SYNC_INTERVAL_SECONDS` | Cada cuánto importa el worker (15 s) |
| `BINANCE_API_MIN_INTERVAL_SECONDS` | Separación mínima entre llamadas a Binance por cliente |
| `MAX_PAYMENT_AGE_MINUTES` | Máximo permitido para `maxAgeMinutes` |
| `RATE_LIMIT_*_PER_MINUTE` | Límites por token |

Ya no hay `API_KEYS`, claves de Binance ni configuración de Gmail en el entorno.

## Tests, lint y type checking

Los tests de integración necesitan un PostgreSQL **de pruebas** (su esquema se borra y
recrea en cada ejecución; **nunca** apuntes a tu proyecto de Supabase real):

```bash
createdb binance_pay_test
export TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/binance_pay_test
pytest
ruff check . && ruff format --check .
mypy app
```

Binance se simula con varias cuentas falsas: verificación completa, aislamiento entre
clientes, keys con permisos peligrosos rechazadas, tokens revocados/caducados, secretos que
nunca aparecen en respuestas, 8 réplicas concurrentes (exactamente 1 `VERIFIED`),
intervalo mínimo hacia Binance, worker multi-cliente, RLS de Supabase, etc.

## Anti-replay, idempotencia y concurrencia

* **Claim transaccional:** se bloquea la fila del pago (`SELECT … FOR UPDATE`) y
  `payment_claims.payment_id` es **UNIQUE**. Resultado garantizado: `A → VERIFIED`,
  `B → ALREADY_CLAIMED`, nunca dos `VERIFIED`.
* **Idempotencia:** misma `orderReference` + mismo pago → `VERIFIED` con
  `idempotent: true`, incluso pasado `maxAgeMinutes`. Sin `orderReference`, una segunda
  petición recibe `ALREADY_CLAIMED`.
* **Por cliente:** `UNIQUE(tenant_id, source, external_id)` y un único pago de confianza
  por `(tenant_id, payment_code)`. El mismo ID en dos cuentas distintas no colisiona.
* **Múltiples instancias:** sin estado en memoria; advisory lock de PostgreSQL por cliente
  y cursor de sincronización en la base de datos.

## Seguridad

* **Tokens:** 256 bits aleatorios con prefijo `bpv_`; solo se guarda su SHA-256; se
  muestran una vez; revocables, con caducidad opcional; desactivar un cliente corta todos
  sus tokens. La clave maestra se compara en tiempo constante y no sirve como token.
* **Keys de Binance:** solo lectura verificada contra Binance antes de guardar; cifradas
  con AES-256-GCM ligadas a cada cliente (un ciphertext copiado a otro cliente no se puede
  descifrar); nunca se devuelven ni se registran en logs; rotación de la clave de cifrado
  con `CREDENTIALS_ENCRYPTION_PREVIOUS_KEYS`.
* **Sin secretos en logs ni respuestas:** filtro de redacción, errores 422 que no repiten
  los valores enviados, SQL sin parámetros en logs.
* **API:** rate limiting por token en PostgreSQL, validación estricta (sin floats para
  dinero, longitudes máximas, campos extra prohibidos), límite de tamaño de cuerpo, CORS
  cerrado, cabeceras de seguridad, HSTS en producción.
* **Supabase:** RLS + sin privilegios para la Data API en todas las tablas.

## Riesgos y limitaciones

* Valida con un pago real que el ID que ve el **pagador** en su comprobante de Binance Pay
  es el mismo `transactionId` que devuelve la API.
* Se depende de `GET /sapi/v1/pay/transactions` (peso 3000, máximo 100 resultados por
  llamada, ventanas de 90 días, 18 meses de historial). Si Binance la cambia o limita, las
  verificaciones devolverán `BINANCE_API_UNAVAILABLE` (nunca un falso `VERIFIED`).
* Si un cliente restringe su key por IP y el servidor cambia de IP, sus verificaciones
  fallarán hasta que actualice la lista blanca en Binance.
* Quien tenga la clave maestra puede crear clientes y tokens: trátala como la contraseña
  más importante del sistema. Quien tenga `CREDENTIALS_ENCRYPTION_KEY` y acceso a la base
  de datos puede leer las keys de Binance (de solo lectura).
* Para volumen alto o integración comercial, la opción oficial es Binance Pay Merchant
  (estructura preparada en `app/integrations/binance/pay_api.py`).
