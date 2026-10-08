"""Home dashboard and global search."""

from __future__ import annotations

from datetime import datetime

from flask import Blueprint, render_template, request

from notas.db import get_db, today
from notas.money import summary as money_summary
from notas.notes import query_notes
from notas.plans import recent_plans
from notas.security import login_required

bp = Blueprint("home", __name__)


@bp.get("/")
@login_required
def index() -> str:
    db = get_db()
    upcoming = db.execute(
        "SELECT n.*, p.name AS project_name, p.color AS project_color FROM notes n "
        "LEFT JOIN projects p ON p.id = n.project_id "
        "WHERE n.is_task = 1 AND n.done = 0 AND (p.archived IS NULL OR p.archived = 0) "
        "ORDER BY n.due_date IS NULL, n.due_date, n.pinned DESC, n.updated_at DESC LIMIT 8"
    ).fetchall()
    overdue = db.execute(
        "SELECT COUNT(*) FROM notes WHERE is_task = 1 AND done = 0 AND due_date < ?", (today(),)
    ).fetchone()[0]
    now = datetime.now()
    greeting = (
        "Buenos días"
        if 5 <= now.hour < 12
        else ("Buenas tardes" if now.hour < 19 else "Buenas noches")
    )
    days = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    months = [
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    ]
    today_label = f"{days[now.weekday()]} {now.day} de {months[now.month - 1]}"
    return render_template(
        "home.html",
        greeting=greeting,
        today_label=today_label,
        upcoming=upcoming,
        overdue=overdue,
        rutina=query_notes(section="rutina", view="notas", limit=5),
        proyectos=query_notes(section="proyecto", limit=6),
        plans=recent_plans(4),
        money=money_summary(),
        vault_count=db.execute("SELECT COUNT(*) FROM vault").fetchone()[0],
    )


@bp.get("/buscar")
@login_required
def search() -> str:
    q = (request.args.get("q") or "").strip()[:100]
    notes, plans = [], []
    if q:
        like = f"%{q}%"
        notes = (
            get_db()
            .execute(
                "SELECT n.*, p.name AS project_name, p.color AS project_color FROM notes n "
                "LEFT JOIN projects p ON p.id = n.project_id "
                "WHERE n.title LIKE ? OR n.body LIKE ? OR n.tags LIKE ? "
                "ORDER BY n.updated_at DESC LIMIT 100",
                (like, like, like),
            )
            .fetchall()
        )
        plans = (
            get_db()
            .execute(
                "SELECT DISTINCT p.* FROM plans p LEFT JOIN plan_steps s ON s.plan_id = p.id "
                "WHERE p.title LIKE ? OR p.description LIKE ? OR s.text LIKE ? "
                "ORDER BY p.updated_at DESC LIMIT 50",
                (like, like, like),
            )
            .fetchall()
        )
    return render_template("search.html", q=q, notes=notes, plans=plans)
