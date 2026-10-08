from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from notas.db import get_db
from notas.helpers import back
from notas.security import (
    change_master_password,
    check_master_password,
    end_session,
    is_configured,
    login_required,
    record_failure,
    set_master_password,
    start_session,
    too_many_attempts,
)

bp = Blueprint("auth", __name__)

MIN_PASSWORD = 8


@bp.route("/configurar", methods=["GET", "POST"])
def setup() -> str | Response:
    if is_configured():
        return redirect(url_for("auth.login"))
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) < MIN_PASSWORD:
            error = f"Usa al menos {MIN_PASSWORD} caracteres."
        elif password != request.form.get("confirm", ""):
            error = "Las contraseñas no coinciden."
        else:
            key = set_master_password(password)
            get_db().commit()
            start_session(key)
            flash(
                "Listo. Esta contraseña protege la app y tus claves: no la olvides, "
                "no se puede recuperar.",
                "ok",
            )
            return redirect(url_for("home.index"))
    return render_template("setup.html", error=error)


@bp.route("/entrar", methods=["GET", "POST"])
def login() -> str | Response:
    if not is_configured():
        return redirect(url_for("auth.setup"))
    error = None
    if request.method == "POST":
        if too_many_attempts():
            error = "Demasiados intentos. Espera unos minutos."
        else:
            key = check_master_password(request.form.get("password", ""))
            if key is None:
                record_failure()
                error = "Contraseña incorrecta."
            else:
                start_session(key)
                return back(url_for("home.index"))
    return render_template("login.html", error=error, next=request.args.get("next", ""))


@bp.post("/salir")
def logout() -> Response:
    end_session()
    return redirect(url_for("auth.login"))


@bp.route("/ajustes", methods=["GET", "POST"])
@login_required
def settings() -> str | Response:
    error = None
    if request.method == "POST":
        new = request.form.get("new", "")
        if len(new) < MIN_PASSWORD:
            error = f"La nueva contraseña necesita al menos {MIN_PASSWORD} caracteres."
        elif new != request.form.get("confirm", ""):
            error = "Las contraseñas nuevas no coinciden."
        elif not change_master_password(request.form.get("current", ""), new):
            error = "La contraseña actual no es correcta."
        else:
            flash("Contraseña maestra cambiada y claves re-cifradas.", "ok")
            return redirect(url_for("auth.settings"))
    return render_template("settings.html", error=error)
