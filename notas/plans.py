"""Plans: anything with ordered steps (a trip, a move, a study plan...)."""

from __future__ import annotations

import sqlite3

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from notas.db import get_db, now
from notas.helpers import back, checked, clean_date, text
from notas.security import login_required

bp = Blueprint("plans", __name__)

PLAN_LIST_SQL = (
    "SELECT p.*, "
    "(SELECT COUNT(*) FROM plan_steps s WHERE s.plan_id = p.id) AS steps, "
    "(SELECT COUNT(*) FROM plan_steps s WHERE s.plan_id = p.id AND s.done = 1) AS done_steps, "
    "(SELECT text FROM plan_steps s WHERE s.plan_id = p.id AND s.done = 0 "
    " ORDER BY position LIMIT 1) AS next_step "
    "FROM plans p "
)


def recent_plans(limit: int) -> list[sqlite3.Row]:
    return (
        get_db()
        .execute(
            PLAN_LIST_SQL + "WHERE p.archived = 0 ORDER BY p.updated_at DESC LIMIT ?", (limit,)
        )
        .fetchall()
    )


def _plan_or_404(plan_id: int) -> sqlite3.Row:
    row = get_db().execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def _touch(plan_id: int) -> None:
    get_db().execute("UPDATE plans SET updated_at = ? WHERE id = ?", (now(), plan_id))


@bp.get("/planes")
@login_required
def index() -> str:
    archived = request.args.get("archivados") == "1"
    plans = (
        get_db()
        .execute(
            PLAN_LIST_SQL + "WHERE p.archived = ? ORDER BY p.updated_at DESC", (int(archived),)
        )
        .fetchall()
    )
    return render_template("plans.html", plans=plans, archived=archived)


@bp.post("/planes")
@login_required
def create() -> Response:
    title = text("title", 150)
    if not title:
        flash("El plan necesita un título.", "error")
        return redirect(url_for("plans.index"))
    stamp = now()
    cur = get_db().execute(
        "INSERT INTO plans(title, description, target_date, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            title,
            text("description", 5000),
            clean_date(request.form.get("target_date")),
            stamp,
            stamp,
        ),
    )
    plan_id = cur.lastrowid
    # Optional initial steps, one per line.
    lines = [ln.strip() for ln in text("steps", 10000).splitlines() if ln.strip()]
    for pos, line in enumerate(lines[:200]):
        get_db().execute(
            "INSERT INTO plan_steps(plan_id, text, position) VALUES (?, ?, ?)",
            (plan_id, line[:300], pos),
        )
    get_db().commit()
    return redirect(url_for("plans.detail", plan_id=plan_id))


@bp.get("/planes/<int:plan_id>")
@login_required
def detail(plan_id: int) -> str:
    plan = _plan_or_404(plan_id)
    steps = (
        get_db()
        .execute("SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY position, id", (plan_id,))
        .fetchall()
    )
    done = sum(1 for s in steps if s["done"])
    return render_template("plan_detail.html", plan=plan, steps=steps, done=done)


@bp.post("/planes/<int:plan_id>")
@login_required
def update(plan_id: int) -> Response:
    _plan_or_404(plan_id)
    title = text("title", 150)
    if not title:
        flash("El plan necesita un título.", "error")
    else:
        get_db().execute(
            "UPDATE plans SET title = ?, description = ?, target_date = ?, archived = ?, "
            "updated_at = ? WHERE id = ?",
            (
                title,
                text("description", 5000),
                clean_date(request.form.get("target_date")),
                checked("archived"),
                now(),
                plan_id,
            ),
        )
        get_db().commit()
        flash("Plan guardado.", "ok")
    return redirect(url_for("plans.detail", plan_id=plan_id))


@bp.post("/planes/<int:plan_id>/borrar")
@login_required
def delete(plan_id: int) -> Response:
    _plan_or_404(plan_id)
    get_db().execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    get_db().commit()
    flash("Plan borrado.", "ok")
    return redirect(url_for("plans.index"))


@bp.post("/planes/<int:plan_id>/pasos")
@login_required
def add_step(plan_id: int) -> Response:
    _plan_or_404(plan_id)
    lines = [ln.strip() for ln in text("text", 10000).splitlines() if ln.strip()]
    if lines:
        db = get_db()
        last = db.execute(
            "SELECT COALESCE(MAX(position), -1) FROM plan_steps WHERE plan_id = ?", (plan_id,)
        ).fetchone()[0]
        due = clean_date(request.form.get("due_date"))
        for i, line in enumerate(lines[:100], start=1):
            db.execute(
                "INSERT INTO plan_steps(plan_id, text, due_date, position) VALUES (?, ?, ?, ?)",
                (plan_id, line[:300], due, last + i),
            )
        _touch(plan_id)
        db.commit()
    return redirect(url_for("plans.detail", plan_id=plan_id) + "#pasos")


def _step_or_404(step_id: int) -> sqlite3.Row:
    row = get_db().execute("SELECT * FROM plan_steps WHERE id = ?", (step_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@bp.post("/pasos/<int:step_id>/hecho")
@login_required
def toggle_step(step_id: int) -> Response:
    step = _step_or_404(step_id)
    get_db().execute("UPDATE plan_steps SET done = 1 - done WHERE id = ?", (step_id,))
    _touch(step["plan_id"])
    get_db().commit()
    return back(url_for("plans.detail", plan_id=step["plan_id"]))


@bp.post("/pasos/<int:step_id>/borrar")
@login_required
def delete_step(step_id: int) -> Response:
    step = _step_or_404(step_id)
    get_db().execute("DELETE FROM plan_steps WHERE id = ?", (step_id,))
    _touch(step["plan_id"])
    get_db().commit()
    return redirect(url_for("plans.detail", plan_id=step["plan_id"]) + "#pasos")


@bp.post("/pasos/<int:step_id>/mover")
@login_required
def move_step(step_id: int) -> Response:
    step = _step_or_404(step_id)
    db = get_db()
    ids = [
        r["id"]
        for r in db.execute(
            "SELECT id FROM plan_steps WHERE plan_id = ? ORDER BY position, id", (step["plan_id"],)
        )
    ]
    i = ids.index(step_id)
    j = i - 1 if request.form.get("dir") == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        for pos, sid in enumerate(ids):
            db.execute("UPDATE plan_steps SET position = ? WHERE id = ?", (pos, sid))
        db.commit()
    return redirect(url_for("plans.detail", plan_id=step["plan_id"]) + "#pasos")
