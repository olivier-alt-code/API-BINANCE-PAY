"""Personal notes, projects, plans and a small password vault (Flask + SQLite)."""

from __future__ import annotations

import os
import secrets
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, render_template

from notas import db
from notas.security import check_csrf, csrf_token, is_logged_in


def _secret_key(instance: Path) -> str:
    path = instance / "secret_key"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48))
        path.chmod(0o600)
    return path.read_text().strip()


def due_label(value: str | None) -> tuple[str, str] | None:
    """Human label and CSS class for a due date."""
    if not value:
        return None
    try:
        due = date.fromisoformat(value)
    except ValueError:
        return None
    today = date.today()
    delta = (due - today).days
    if delta < 0:
        return (f"vencido {due:%d/%m}", "overdue")
    if delta == 0:
        return ("hoy", "today")
    if delta == 1:
        return ("mañana", "soon")
    if delta < 7:
        names = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
        return (f"{names[due.weekday()]} {due:%d/%m}", "soon")
    return (f"{due:%d/%m/%Y}", "later")


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(
        __name__,
        instance_path=os.environ.get("NOTAS_DATA_DIR") or None,
        instance_relative_config=True,
    )
    instance = Path(app.instance_path)
    instance.mkdir(parents=True, exist_ok=True)
    app.config.update(
        DATABASE=str(instance / "notas.db"),
        SECRET_KEY=_secret_key(instance),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("NOTAS_HTTPS", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        IDLE_TIMEOUT_SECONDS=int(os.environ.get("NOTAS_IDLE_MINUTES", "30")) * 60,
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    db.init_db(app.config["DATABASE"])
    app.teardown_appcontext(db.close_db)
    app.before_request(check_csrf)

    from notas import auth, home, notes, plans, vault

    for module in (auth, home, notes, plans, vault):
        app.register_blueprint(module.bp)

    def color_class(value: str | None) -> str:
        colors = notes.PROJECT_COLORS
        return f"c{colors.index(value)}" if value in colors else "c0"

    app.jinja_env.globals.update(
        csrf_token=csrf_token, due_label=due_label, color_class=color_class
    )

    @app.context_processor
    def _ctx() -> dict[str, Any]:
        return {"logged_in": is_logged_in()}

    @app.after_request
    def _headers(response):  # type: ignore[no-untyped-def]
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(400)
    @app.errorhandler(404)
    def _error(exc):  # type: ignore[no-untyped-def]
        return render_template("error.html", error=exc), exc.code

    return app
