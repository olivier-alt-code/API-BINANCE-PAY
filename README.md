# Mis notas

App web **personal** (Flask + SQLite) para organizarte. Tiene cuatro secciones:

| Sección | Para qué |
|---|---|
| **Rutina y pendientes** | Hábitos, horarios, recados y tareas. Notas y pendientes en el mismo sitio, con pestañas (*Pendientes / Notas / Hechas / Todo*), etiquetas, búsqueda y fechas que se marcan como *hoy*, *mañana* o *vencido*. |
| **Proyectos** | Apuntes de tus proyectos de ingeniería: cálculos, decisiones, ideas, pendientes. Cada proyecto tiene color, contador de pendientes, filtros y etiquetas propias. También puedes ver las últimas notas de todos los proyectos juntas. |
| **Planificación** | Planes con pasos ordenados (un viaje, una mudanza, un estudio…), barra de progreso, fecha objetivo y "siguiente paso". |
| **Contraseñas** | Gestor rápido: buscar, copiar usuario y clave con un clic, ver u ocultar la clave y generar claves seguras. Todo se guarda **cifrado**. |

La **pantalla de inicio** reúne:
- captura rápida;
- los próximos pendientes, de todas las secciones;
- las notas recientes de rutina;
- las notas recientes de proyectos;
- los planes activos.

## Uso rápido

- **Captura sin pensar:** escribe en el cuadro superior. La **primera línea es el título**
  y el resto el contenido. `Ctrl+Enter` guarda.
- **Pendientes:** marca *Pendiente*, o simplemente pon una fecha; con fecha se convierte en
  pendiente automáticamente.
- **Etiquetas:** separadas por comas (`casa, gym`). Pulsa una etiqueta para filtrar por ella.
- **Fijar:** la ★ deja una nota siempre arriba.
- **Buscar:** el buscador lateral (atajo `/`) busca en todas las notas y planes.
- **Mover una nota:** al editarla puedes cambiarla de sección, de rutina a un proyecto o
  al revés.

## Instalar y arrancar (Windows / PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

Abre <http://127.0.0.1:5000>. La primera vez te pide crear la **contraseña maestra**.

## Seguridad

- **Una sola contraseña maestra** protege el acceso. Se guarda solo su hash.
- **Contraseñas guardadas:** se cifran (Fernet, AES-128 + HMAC) con una clave derivada de la
  maestra mediante scrypt. La clave solo vive en la memoria de la app mientras tienes la
  sesión abierta: al pulsar **Bloquear**, tras 30 min sin uso (`NOTAS_IDLE_MINUTES`) o al
  reiniciar la app, hay que volver a entrar. Con el archivo de la base de datos solo no se
  pueden leer.
- **Cambiar la contraseña maestra** (en Ajustes) vuelve a cifrar todas las claves.
  **Si la olvidas, las contraseñas guardadas no se pueden recuperar**; las notas sí, porque
  no están cifradas.
- **Protecciones de la web:** formularios con protección CSRF, cookies `HttpOnly` y
  `SameSite`, CSP estricta y límite de intentos de entrada.
- **Acceso desde la red:** por defecto solo escucha en tu propio PC (`127.0.0.1`). Para
  abrirla desde el móvil o la red, ponla detrás de HTTPS y usa `NOTAS_HOST=0.0.0.0` y
  `NOTAS_HTTPS=1`.

## Datos y copias de seguridad

Todo vive en `instance/notas.db`, junto con `instance/secret_key`, que es la clave de las
cookies. Para hacer una copia, cierra la app y copia la carpeta `instance`. Para guardar los
datos en otra carpeta, por ejemplo una sincronizada, usa `NOTAS_DATA_DIR=C:\ruta\a\carpeta`.

| Variable | Por defecto | |
|---|---|---|
| `NOTAS_DATA_DIR` | `./instance` | Carpeta de la base de datos |
| `NOTAS_HOST` / `NOTAS_PORT` | `127.0.0.1` / `5000` | Dónde escucha |
| `NOTAS_IDLE_MINUTES` | `30` | Bloqueo por inactividad |
| `NOTAS_HTTPS` | `0` | `1` si la sirves por HTTPS (cookie `Secure`) |

## Desarrollo

```powershell
pip install -e ".[dev]"
pytest -q
ruff check . ; ruff format --check .
```
