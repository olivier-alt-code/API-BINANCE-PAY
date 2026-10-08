"""Start the app:  python run.py   (then open http://127.0.0.1:5000)

Only listens on this computer by default. NOTAS_HOST=0.0.0.0 exposes it to your network;
do that only behind HTTPS (and set NOTAS_HTTPS=1).
"""

import os

from waitress import serve

from notas import create_app

if __name__ == "__main__":
    host = os.environ.get("NOTAS_HOST", "127.0.0.1")
    port = int(os.environ.get("NOTAS_PORT", "5000"))
    print(f"Mis notas en http://{host}:{port}  (Ctrl+C para salir)")
    # One process, several threads: vault keys live in this process's memory.
    serve(create_app(), host=host, port=port, threads=4)
