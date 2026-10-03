# Despliegue en Azure App Service (plan B2)

La API se despliega como **Web App for Containers**: GitHub Actions construye la imagen
Docker, la publica en GitHub Container Registry (`ghcr.io`) y le dice a la Web App que la
use, autenticándose con el **perfil de publicación** descargado de Azure.

Se usa contenedor (y no el runtime Python integrado de App Service) porque el proyecto
requiere Python 3.14.

```
push a main ──► GitHub Actions ──► ghcr.io/<usuario>/api-binance-pay:<sha>
                                        │
                     publish profile ──►│ Azure Web App (B2) ──► Supabase (6543)
```

Con un B2 basta **una sola app**: la API ejecuta también el worker de sincronización
(`RUN_SYNC_WORKER_IN_API=true`) y aplica las migraciones al arrancar
(`RUN_MIGRATIONS_ON_START=true`).

---

## 1. Crear la Web App

Portal de Azure → **Create a resource** → **Web App**:

| Campo | Valor |
|---|---|
| Publish | **Container** |
| Operating System | **Linux** |
| Region | La más cercana a tu región de Supabase |
| App Service Plan | Tu plan **B2** |
| Container / Image source | Cualquiera de ejemplo (el workflow la sustituirá) |

Una vez creada:

1. **Configuration → General settings**
   * **Always On: On** (el worker necesita que la app no se duerma).
   * **HTTPS Only: On**.
   * **Basic Auth Publishing Credentials: On** — sin esto no se puede descargar ni usar el
     perfil de publicación.
2. **Health check** → ruta `/health`.

## 2. Hacer accesible la imagen

El workflow publica en `ghcr.io/<tu-usuario>/api-binance-pay`. Dos opciones:

* **Paquete público (más fácil):** tras el primer despliegue, en GitHub → tu perfil →
  **Packages → api-binance-pay → Package settings → Change visibility → Public**. La imagen
  no contiene secretos.
* **Paquete privado:** crea un *Personal access token (classic)* con solo `read:packages`
  y añade en la Web App (Configuration → Application settings):

  | Nombre | Valor |
  |---|---|
  | `DOCKER_REGISTRY_SERVER_URL` | `https://ghcr.io` |
  | `DOCKER_REGISTRY_SERVER_USERNAME` | tu usuario de GitHub |
  | `DOCKER_REGISTRY_SERVER_PASSWORD` | el token |

## 3. Variables de la Web App

**Configuration → Application settings** (se inyectan como variables de entorno; Azure las
guarda cifradas). Pulsa **Save** al terminar.

| Nombre | Valor |
|---|---|
| `WEBSITES_PORT` | `8000` |
| `APP_ENV` | `production` |
| `DATABASE_URL` | `postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres` |
| `DB_POOL_MAX` | `5` |
| `ADMIN_API_KEYS` | tu clave maestra (`python -m app.cli generate-admin-key`) |
| `CREDENTIALS_ENCRYPTION_KEY` | `python -m app.cli generate-encryption-key` — **no la cambies nunca** |
| `RUN_SYNC_WORKER_IN_API` | `true` |
| `RUN_MIGRATIONS_ON_START` | `true` |
| `FORWARDED_ALLOW_IPS` | `*` (el balanceador de Azure es el único que llega al contenedor) |
| `TRUST_FORWARDED_HEADERS` | `true` (rate limit por IP real del cliente) |
| `PAYMENT_CODE_CASE_INSENSITIVE` | `true` (opcional) |

Notas:

* `DATABASE_URL` es la URL del **Transaction pooler** de Supabase (puerto **6543**):
  Supabase → Project Settings → Database → Connection string → *Transaction pooler*. Si la
  contraseña tiene caracteres especiales, la API los tolera; aun así, una contraseña
  alfanumérica larga evita sorpresas.
* Las claves se generan **una vez** en tu PC (PowerShell, dentro del proyecto):
  ```powershell
  python -m app.cli generate-admin-key
  python -m app.cli generate-encryption-key
  ```
  Guárdalas también en un gestor de contraseñas.
* Las API keys de Binance **no** van aquí: cada cliente registra la suya con
  `PUT /v1/me/binance-credentials`.
* Mantén la app en **1 instancia** (Scale out = 1) mientras `RUN_MIGRATIONS_ON_START` y
  `RUN_SYNC_WORKER_IN_API` estén en `true`.

## 4. Secretos del repositorio en GitHub

1. Azure → tu Web App → **Overview → Download publish profile**. Se descarga un archivo
   `.PublishSettings` (XML).
2. GitHub → el repositorio → **Settings → Secrets and variables → Actions → New repository
   secret**:

   | Secreto | Valor |
   |---|---|
   | `AZURE_WEBAPP_PUBLISH_PROFILE` | el **contenido completo** del `.PublishSettings` (ábrelo con el Bloc de notas y pégalo tal cual) |
   | `AZURE_WEBAPP_NAME` | el nombre de la Web App (sin `.azurewebsites.net`) |

3. **Settings → Environments → New environment** → `production` (el workflow lo usa; puedes
   añadir *required reviewers* si quieres aprobar cada despliegue).

> El perfil de publicación es una credencial: no lo subas al repositorio y bórralo de tu
> carpeta de descargas después de copiarlo. Si se filtra, en Azure usa **Reset publish
> profile** y actualiza el secreto.

`GITHUB_TOKEN` lo pone GitHub automáticamente; no hay que crearlo.

## 5. Desplegar

El workflow [`.github/workflows/deploy-azure.yml`](../.github/workflows/deploy-azure.yml) se
ejecuta en cada push a `main`, o a mano desde **Actions → Deploy to Azure → Run workflow**.
Pasos:

1. Construye la imagen con el `Dockerfile`.
2. La publica como `ghcr.io/<usuario>/api-binance-pay:<sha del commit>` y `:latest`.
3. `azure/webapps-deploy` configura la Web App con esa imagen y la reinicia.

Al arrancar, el contenedor aplica las migraciones (`alembic upgrade head`) y levanta la API
con el worker.

## 6. Verificar

```powershell
$API = "https://<tu-app>.azurewebsites.net"
Invoke-RestMethod "$API/health"
Invoke-RestMethod "$API/ready"
# status = ready, checks: database, admin_key_configured, encryption_key = True
```

Si algo falla: Web App → **Log stream** (activa antes **App Service logs → Application
logging: File system**). Errores típicos:

| Síntoma | Causa |
|---|---|
| *Container didn't respond to HTTP pings on port* | Falta `WEBSITES_PORT=8000` |
| *ImagePullFailure / unauthorized* | Paquete privado sin `DOCKER_REGISTRY_SERVER_*` |
| `/ready` con `database: false` | `DATABASE_URL` incorrecta o contraseña mal copiada |
| El workflow falla en *webapps-deploy* con 401 | Basic Auth Publishing desactivado o perfil reseteado |
| `encryption_key: false` | Falta `CREDENTIALS_ENCRYPTION_KEY` |

## 7. Primer cliente

Con la API en marcha, créate tu propio cliente y token
(detalles en la [guía de administración](admin-api.md)):

```powershell
$ADMIN = @{ Authorization = "Bearer TU_CLAVE_MAESTRA" }
$r = Invoke-RestMethod -Method Post -Uri "$API/v1/admin/clients" -Headers $ADMIN `
       -ContentType "application/json" -Body '{"name": "Olivier"}'
$r.token.token   # cópialo ahora
```

Después registra tu API key de Binance de solo lectura con ese token
(`PUT /v1/me/binance-credentials`) y prueba `POST /v1/payments/verify`.

## 8. Mantenimiento

* **Actualizar:** merge a `main` → despliegue automático.
* **Volver a una versión anterior:** Actions → la ejecución buena → **Re-run all jobs**
  (vuelve a desplegar esa imagen por su sha).
* **Rotar la clave maestra:** `ADMIN_API_KEYS` acepta varias separadas por comas.
* **Escalar a varias instancias:** pon `RUN_MIGRATIONS_ON_START=false` y
  `RUN_SYNC_WORKER_IN_API=false`, y ejecuta el worker (`python -m app.workers.sync_worker`)
  y las migraciones aparte.

## Sobre el puerto 5432 del CI

`.github/workflows/ci.yml` levanta un PostgreSQL **desechable** solo para los tests de
GitHub Actions; no es Supabase. Ahora se publica en el puerto 6543 para seguir la misma
convención, pero la base de datos real de producción es únicamente la de `DATABASE_URL` en
la Web App.
