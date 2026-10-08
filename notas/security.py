"""Single-user login, CSRF protection and the vault key."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from functools import wraps
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from flask import abort, current_app, flash, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from notas.db import get_db, get_setting, now, set_setting

# Vault keys live only in server memory, never in the cookie. Restarting the app (or
# logging out, or the idle timeout) locks the vault: you log in again.
_KEYS: dict[str, tuple[bytes, float]] = {}
_FAILED: dict[str, list[float]] = {}


def is_configured() -> bool:
    return get_setting("password_hash") is not None


def _derive(password: str, salt: bytes) -> bytes:
    raw = hashlib.scrypt(
        password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 2**20, dklen=32
    )
    return base64.urlsafe_b64encode(raw)


def set_master_password(password: str) -> bytes:
    salt = secrets.token_bytes(16)
    set_setting("password_hash", generate_password_hash(password))
    set_setting("kdf_salt", base64.b64encode(salt).decode())
    return _derive(password, salt)


def check_master_password(password: str) -> bytes | None:
    stored = get_setting("password_hash")
    if stored is None or not check_password_hash(stored, password):
        return None
    salt = base64.b64decode(get_setting("kdf_salt") or "")
    return _derive(password, salt)


def change_master_password(old: str, new: str) -> bool:
    """Re-encrypts the whole vault with the new key, in one transaction."""
    old_key = check_master_password(old)
    if old_key is None:
        return False
    db = get_db()
    rows = db.execute("SELECT id, secret FROM vault").fetchall()
    plain = {r["id"]: Fernet(old_key).decrypt(r["secret"].encode()) for r in rows}
    new_key = set_master_password(new)
    for row_id, data in plain.items():
        db.execute(
            "UPDATE vault SET secret = ?, updated_at = ? WHERE id = ?",
            (Fernet(new_key).encrypt(data).decode(), now(), row_id),
        )
    db.commit()
    start_session(new_key)
    return True


def too_many_attempts() -> bool:
    ip = request.remote_addr or "?"
    recent = [t for t in _FAILED.get(ip, []) if t > time.time() - 600]
    _FAILED[ip] = recent
    return len(recent) >= 10


def record_failure() -> None:
    _FAILED.setdefault(request.remote_addr or "?", []).append(time.time())


def start_session(key: bytes) -> None:
    old = session.get("sid")
    if old:
        _KEYS.pop(old, None)
    session.clear()
    session.permanent = True
    sid = secrets.token_urlsafe(24)
    session["sid"] = sid
    session["csrf"] = secrets.token_urlsafe(24)
    _KEYS[sid] = (key, time.time())


def end_session() -> None:
    sid = session.get("sid")
    if sid:
        _KEYS.pop(sid, None)
    session.clear()


def is_logged_in() -> bool:
    sid = session.get("sid")
    if not sid or sid not in _KEYS:
        return False
    key, last = _KEYS[sid]
    if time.time() - last > current_app.config["IDLE_TIMEOUT_SECONDS"]:
        _KEYS.pop(sid, None)
        return False
    _KEYS[sid] = (key, time.time())
    return True


def login_required(view: Any) -> Any:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not is_configured():
            return redirect(url_for("auth.setup"))
        if not is_logged_in():
            session.pop("sid", None)
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def check_csrf() -> None:
    if request.method == "POST":
        token = session.get("csrf")
        sent = request.form.get("csrf", "")
        if not token or not secrets.compare_digest(token, sent):
            abort(400, "Formulario caducado: recarga la página.")


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return str(session["csrf"])


# --- vault encryption -----------------------------------------------------------------
def _fernet() -> Fernet:
    sid = session.get("sid")
    if not sid or sid not in _KEYS:
        abort(401)
    return Fernet(_KEYS[sid][0])


def encrypt_secret(password: str, notes: str) -> str:
    data = json.dumps({"password": password, "notes": notes}).encode()
    return _fernet().encrypt(data).decode()


def decrypt_secret(token: str) -> dict[str, str]:
    try:
        return dict(json.loads(_fernet().decrypt(token.encode())))
    except InvalidToken:
        flash("No se pudo descifrar una entrada.", "error")
        return {"password": "", "notes": ""}
