"""Quick password manager. Secrets are encrypted with a key derived from the master
password (scrypt + Fernet/AES); the database alone never reveals them."""

from __future__ import annotations

import secrets
import sqlite3
import string

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from notas.db import get_db, now
from notas.helpers import text
from notas.security import decrypt_secret, encrypt_secret, login_required

bp = Blueprint("vault", __name__)


def generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%&*-_=+?"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)
            and any(not c.isalnum() for c in pw)
        ):
            return pw


def _entry_or_404(entry_id: int) -> sqlite3.Row:
    row = get_db().execute("SELECT * FROM vault WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@bp.get("/claves")
@login_required
def index() -> str:
    q = (request.args.get("q") or "").strip()[:100]
    sql = "SELECT * FROM vault"
    args: list[str] = []
    if q:
        sql += " WHERE name LIKE ? OR username LIKE ? OR url LIKE ?"
        args = [f"%{q}%"] * 3
    rows = get_db().execute(sql + " ORDER BY name COLLATE NOCASE", args).fetchall()
    entries = [{**dict(r), **decrypt_secret(r["secret"])} for r in rows]
    return render_template("vault.html", entries=entries, q=q, suggestion=generate_password())


def _fields() -> tuple[str, str, str, str, str] | None:
    name = text("name", 120)
    password = (request.form.get("password") or "")[:500]
    if not name or not password:
        flash("Hacen falta al menos un nombre y una contraseña.", "error")
        return None
    url = text("url", 500)
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return name, text("username", 200), url, password, text("notes", 2000)


@bp.post("/claves")
@login_required
def create() -> Response:
    fields = _fields()
    if fields:
        name, username, url, password, notes = fields
        stamp = now()
        get_db().execute(
            "INSERT INTO vault(name, username, url, secret, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, username, url, encrypt_secret(password, notes), stamp, stamp),
        )
        get_db().commit()
        flash(f"«{name}» guardada.", "ok")
    return redirect(url_for("vault.index"))


@bp.get("/claves/<int:entry_id>")
@login_required
def edit(entry_id: int) -> str:
    row = _entry_or_404(entry_id)
    entry = {**dict(row), **decrypt_secret(row["secret"])}
    return render_template("vault_edit.html", entry=entry, suggestion=generate_password())


@bp.post("/claves/<int:entry_id>")
@login_required
def update(entry_id: int) -> Response:
    _entry_or_404(entry_id)
    fields = _fields()
    if not fields:
        return redirect(url_for("vault.edit", entry_id=entry_id))
    name, username, url, password, notes = fields
    get_db().execute(
        "UPDATE vault SET name = ?, username = ?, url = ?, secret = ?, updated_at = ? WHERE id = ?",
        (name, username, url, encrypt_secret(password, notes), now(), entry_id),
    )
    get_db().commit()
    flash(f"«{name}» actualizada.", "ok")
    return redirect(url_for("vault.index"))


@bp.post("/claves/<int:entry_id>/borrar")
@login_required
def delete(entry_id: int) -> Response:
    row = _entry_or_404(entry_id)
    get_db().execute("DELETE FROM vault WHERE id = ?", (entry_id,))
    get_db().commit()
    flash(f"«{row['name']}» borrada.", "ok")
    return redirect(url_for("vault.index"))
