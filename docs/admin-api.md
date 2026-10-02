# Guía de administración: clientes y tokens

Esta guía es para **ti, el dueño** de la API. Explica cómo dar y quitar acceso a otras
personas (y a ti mismo) mediante los endpoints `/v1/admin/*`, con ejemplos para
**PowerShell** (Windows) y **curl** (Linux/macOS/Git Bash).

---

## 1. Conceptos

| Término | Qué es |
|---|---|
| **Clave maestra** | Tu credencial de dueño, en `ADMIN_API_KEYS`. Solo sirve para `/v1/admin/*`. |
| **Cliente** | Una persona o sistema al que das acceso (tú también eres uno). Tiene su propia cuenta de Binance y solo ve sus pagos. |
| **Token** | Credencial privada de un cliente: `bpv_` + 43 caracteres. Un cliente puede tener varios. |

Reglas importantes:

* El token se muestra **una sola vez**, al crearlo. En la base de datos solo queda su hash:
  si se pierde, no se puede recuperar; se emite otro y se revoca el viejo.
* La clave maestra **no** sirve como token de cliente, y un token de cliente **no** sirve
  para `/v1/admin/*`.
* Revocar un token o desactivar un cliente tiene efecto **inmediato** en todas las réplicas.

## 2. Preparación (una sola vez)

### 2.1 Generar las dos claves

Las claves se generan **una vez** y se guardan como secretos (en el `.env` o en el panel de
variables de tu hosting). **No** se generan en cada arranque: si cambiara
`CREDENTIALS_ENCRYPTION_KEY`, las API keys de Binance guardadas quedarían ilegibles.

Con el proyecto instalado:

```bash
python -m app.cli generate-admin-key        # -> ADMIN_API_KEYS
python -m app.cli generate-encryption-key   # -> CREDENTIALS_ENCRYPTION_KEY
```

Sin instalar el proyecto (solo Python):

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Con Docker:

```bash
docker compose run --rm --no-deps api python -m app.cli generate-admin-key
docker compose run --rm --no-deps api python -m app.cli generate-encryption-key
```

Guárdalas también en un gestor de contraseñas.

### 2.2 Arrancar

```bash
docker compose up -d --build
```

El servicio `migrate` aplica las migraciones automáticamente antes de arrancar la API y el
worker. Comprueba que todo está bien:

```bash
curl https://tu-api.example.com/ready
# {"status":"ready","checks":{"database":true,"admin_key_configured":true,"encryption_key":true}}
```

## 3. Variables para los ejemplos

**PowerShell:**

```powershell
$API   = "https://tu-api.example.com"            # o http://localhost:8000
$ADMIN = @{ Authorization = "Bearer TU_CLAVE_MAESTRA" }
```

**curl:**

```bash
API="https://tu-api.example.com"
ADMIN="Authorization: Bearer TU_CLAVE_MAESTRA"
```

> En PowerShell, `curl` es un alias de `Invoke-WebRequest`. Usa `Invoke-RestMethod` (como en
> los ejemplos) o `curl.exe` explícitamente.

## 4. Endpoints

Todos requieren `Authorization: Bearer <clave maestra>`. Las fechas son ISO 8601 en UTC.

| Método | Ruta | Para qué |
|---|---|---|
| `POST` | `/v1/admin/clients` | Crear un cliente y su primer token |
| `GET` | `/v1/admin/clients` | Listar clientes |
| `GET` | `/v1/admin/clients/{id}` | Ver un cliente |
| `PATCH` | `/v1/admin/clients/{id}` | Activar / desactivar un cliente |
| `POST` | `/v1/admin/clients/{id}/tokens` | Emitir otro token para un cliente |
| `GET` | `/v1/admin/clients/{id}/tokens` | Listar sus tokens (sin su valor) |
| `DELETE` | `/v1/admin/tokens/{token_id}` | Revocar un token |

### 4.1 Crear un cliente — `POST /v1/admin/clients`

Cuerpo:

| Campo | Tipo | Obligatorio | Descripción |
|---|---|---|---|
| `name` | string (1–100) | sí | Nombre único. Letras, números, espacios y `. @ + - _` |
| `tokenName` | string | no | Nombre del primer token (por defecto `default`) |
| `expiresInDays` | int (1–3650) | no | Caducidad del primer token. Sin él, no caduca |

```powershell
$r = Invoke-RestMethod -Method Post -Uri "$API/v1/admin/clients" -Headers $ADMIN `
       -ContentType "application/json" -Body '{"name": "Ana"}'
$r.token.token      # <- el token de Ana: cópialo ahora, no se vuelve a mostrar
```

```bash
curl -s -X POST "$API/v1/admin/clients" -H "$ADMIN" \
     -H "Content-Type: application/json" -d '{"name": "Ana"}'
```

Respuesta `201`:

```json
{
  "client": {
    "id": 1, "name": "Ana", "enabled": true,
    "createdAt": "2026-10-02T21:04:51.035204Z",
    "activeTokens": 1, "binanceConfigured": false
  },
  "token": {
    "id": 1, "name": "default", "prefix": "bpv_tFFbshEX",
    "token": "bpv_tFFbshEXwxj6rezAIDqLI0pxxQAyR25pGMwJeSysCaI",
    "expiresAt": null
  }
}
```

Errores: `409` nombre ya usado · `422` nombre inválido.

**Para darte acceso a ti mismo**, haz lo mismo con tu nombre (`{"name": "Olivier"}`) y usa
ese token para tus verificaciones.

### 4.2 Listar clientes — `GET /v1/admin/clients`

```powershell
Invoke-RestMethod -Uri "$API/v1/admin/clients" -Headers $ADMIN | Format-Table
```

```bash
curl -s "$API/v1/admin/clients" -H "$ADMIN"
```

Respuesta `200`:

```json
[
  {"id": 1, "name": "Ana", "enabled": true, "createdAt": "2026-10-02T21:04:51Z",
   "activeTokens": 2, "binanceConfigured": true}
]
```

* `activeTokens`: tokens no revocados y no caducados.
* `binanceConfigured`: si ya registró su API key de Binance.

### 4.3 Ver un cliente — `GET /v1/admin/clients/{id}`

Misma forma que un elemento de la lista. `404` si no existe.

### 4.4 Activar o desactivar — `PATCH /v1/admin/clients/{id}`

Cuerpo: `{"enabled": false}` o `{"enabled": true}`.

Desactivar **corta al instante todos sus tokens** (responden `401`) sin borrar nada: sus
pagos, claims y su API key se conservan. Reactivarlo devuelve el acceso a sus tokens que no
estén revocados ni caducados.

```powershell
Invoke-RestMethod -Method Patch -Uri "$API/v1/admin/clients/1" -Headers $ADMIN `
  -ContentType "application/json" -Body '{"enabled": false}'
```

```bash
curl -s -X PATCH "$API/v1/admin/clients/1" -H "$ADMIN" \
     -H "Content-Type: application/json" -d '{"enabled": false}'
```

Respuesta `200`: el cliente actualizado. `404` si no existe.

### 4.5 Otro token — `POST /v1/admin/clients/{id}/tokens`

Útil para dar un token distinto a cada sistema del cliente (tienda, bot, servidor de
pruebas…): si uno se filtra, revocas solo ese.

| Campo | Tipo | Obligatorio | Descripción |
|---|---|---|---|
| `name` | string (1–100) | sí | Para reconocerlo después (p. ej. `tienda`) |
| `expiresInDays` | int (1–3650) | no | Caducidad. Sin él, no caduca |

```powershell
$t = Invoke-RestMethod -Method Post -Uri "$API/v1/admin/clients/1/tokens" -Headers $ADMIN `
       -ContentType "application/json" -Body '{"name": "tienda", "expiresInDays": 90}'
$t.token            # <- se muestra solo ahora
```

```bash
curl -s -X POST "$API/v1/admin/clients/1/tokens" -H "$ADMIN" \
     -H "Content-Type: application/json" -d '{"name": "tienda", "expiresInDays": 90}'
```

Respuesta `201`:

```json
{"id": 2, "name": "tienda", "prefix": "bpv_BTIqmONl",
 "token": "bpv_BTIqmONl56037RWcTFPw7hXWRQptniSjTDz1HdP6Of0",
 "expiresAt": "2026-12-31T21:04:51.112568Z"}
```

`404` si el cliente no existe.

### 4.6 Listar tokens — `GET /v1/admin/clients/{id}/tokens`

Nunca devuelve el valor del token, solo su prefijo para reconocerlo.

```json
[
  {"id": 1, "name": "default", "prefix": "bpv_tFFbshEX",
   "createdAt": "2026-10-02T21:04:51Z", "expiresAt": null,
   "revokedAt": null, "lastUsedAt": "2026-10-03T10:12:00Z", "active": true}
]
```

`lastUsedAt` se actualiza como mucho una vez por minuto (`TOKEN_LAST_USED_UPDATE_SECONDS`).

### 4.7 Revocar un token — `DELETE /v1/admin/tokens/{token_id}`

```powershell
Invoke-RestMethod -Method Delete -Uri "$API/v1/admin/tokens/2" -Headers $ADMIN
```

```bash
curl -s -X DELETE "$API/v1/admin/tokens/2" -H "$ADMIN"
```

Respuesta `200` `{"revoked": true}`. `404` si no existe o ya estaba revocado. Es
irreversible: para devolver el acceso, emite un token nuevo.

## 5. Errores comunes

| Código | Significado |
|---|---|
| `401` | Falta la clave maestra, es incorrecta o se usó un token de cliente |
| `404` | Cliente o token inexistente |
| `409` | Ya existe un cliente con ese nombre |
| `422` | Datos inválidos (la respuesta nunca repite los valores enviados) |
| `429` | Demasiadas peticiones admin (`RATE_LIMIT_ADMIN_PER_MINUTE`, 30/min) |

## 6. Alternativas a HTTP

**Swagger UI:** con `ENABLE_DOCS=true`, abre `https://tu-api/docs`, pulsa **Authorize**,
pega la clave maestra y usa los endpoints desde el navegador. En producción `/docs` está
desactivado por defecto; actívalo solo si lo necesitas.

**CLI**, con el servidor funcionando (se conecta directamente a la misma base de datos):

```bash
# desde la carpeta del proyecto, con el mismo .env
python -m app.cli create-client "Ana"
python -m app.cli create-token 1 --name tienda --expires-in-days 90
python -m app.cli list-clients
python -m app.cli list-tokens 1
python -m app.cli revoke-token 2
python -m app.cli set-client-enabled 1 false

# o dentro del contenedor que ya está corriendo
docker compose exec api python -m app.cli create-client "Ana"
```

## 7. Qué hace el cliente después

Le envías su token por un canal privado (no por un canal público ni en un repositorio). Con
él:

1. Registra su API key de Binance **de solo lectura**:
   `PUT /v1/me/binance-credentials` con `{"apiKey": "...", "apiSecret": "..."}`. Se rechaza
   (422) si la key permite operar, retirar o transferir.
2. Verifica pagos: `POST /v1/payments/verify`.
3. Puede comprobar su estado con `GET /v1/me` y `GET /v1/me/binance-credentials`.

Detalles en el [README](../README.md#registrar-la-api-key-de-binance-de-un-cliente).

## 8. Buenas prácticas

* Un token por sistema; caducidad (`expiresInDays`) para accesos temporales.
* Si un token se filtra: revócalo y emite otro. Si sospechas de todo un cliente:
  desactívalo.
* **Rotar la clave maestra** sin cortes: `ADMIN_API_KEYS` admite varias separadas por comas.
  Añade la nueva, despliega, empieza a usarla y después quita la vieja.
* Nunca compartas la clave maestra ni la publiques: con ella se pueden crear tokens.
