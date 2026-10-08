"""Notes and tasks: the "rutina" section and engineering projects."""

from __future__ import annotations

import sqlite3
from typing import Any

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from notas.db import get_db, now
from notas.helpers import back, checked, clean_date, clean_tags, text
from notas.security import login_required

bp = Blueprint("notes", __name__)

VIEWS = {
    "pendientes": "Pendientes",
    "notas": "Notas",
    "hechas": "Hechas",
    "todo": "Todo",
}
PROJECT_COLORS = ["#4f7cff", "#16a37f", "#e0882b", "#d64b6b", "#8a5cf6", "#0ea5c6", "#6b7280"]


def query_notes(
    *,
    section: str,
    project_id: int | None = None,
    view: str = "todo",
    q: str = "",
    tag: str = "",
    limit: int = 300,
) -> list[sqlite3.Row]:
    where = ["n.section = ?"]
    args: list[Any] = [section]
    if project_id is not None:
        where.append("n.project_id = ?")
        args.append(project_id)
    if view == "pendientes":
        where.append("n.is_task = 1 AND n.done = 0")
    elif view == "notas":
        where.append("n.is_task = 0")
    elif view == "hechas":
        where.append("n.is_task = 1 AND n.done = 1")
    if q:
        where.append("(n.title LIKE ? OR n.body LIKE ? OR n.tags LIKE ?)")
        like = f"%{q}%"
        args += [like, like, like]
    if tag:
        where.append("(',' || n.tags || ',') LIKE ?")
        args.append(f"%,{tag},%")
    if view == "pendientes":
        order = "n.due_date IS NULL, n.due_date, n.pinned DESC, n.updated_at DESC"
    else:
        order = "n.pinned DESC, n.updated_at DESC"
    sql = (
        "SELECT n.*, p.name AS project_name, p.color AS project_color "
        "FROM notes n LEFT JOIN projects p ON p.id = n.project_id "
        f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?"
    )
    return get_db().execute(sql, [*args, limit]).fetchall()


def tag_counts(section: str, project_id: int | None = None) -> list[tuple[str, int]]:
    sql = "SELECT tags FROM notes WHERE section = ? AND tags != ''"
    args: list[Any] = [section]
    if project_id is not None:
        sql += " AND project_id = ?"
        args.append(project_id)
    counts: dict[str, int] = {}
    for row in get_db().execute(sql, args):
        for tag in row["tags"].split(","):
            counts[tag] = counts.get(tag, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]


def _filters() -> dict[str, str]:
    view = request.args.get("vista", "pendientes")
    return {
        "view": view if view in VIEWS else "pendientes",
        "q": (request.args.get("q") or "").strip()[:100],
        "tag": (request.args.get("tag") or "").strip().lower()[:30],
    }


def _counts(section: str, project_id: int | None = None) -> dict[str, int]:
    sql = (
        "SELECT SUM(is_task = 1 AND done = 0) AS pendientes, SUM(is_task = 0) AS notas, "
        "SUM(is_task = 1 AND done = 1) AS hechas, COUNT(*) AS todo FROM notes WHERE section = ?"
    )
    args: list[Any] = [section]
    if project_id is not None:
        sql += " AND project_id = ?"
        args.append(project_id)
    row = get_db().execute(sql, args).fetchone()
    return {k: int(row[k] or 0) for k in VIEWS}


# --- rutina ---------------------------------------------------------------------------
@bp.get("/rutina")
@login_required
def rutina() -> str:
    f = _filters()
    return render_template(
        "notes.html",
        section="rutina",
        project=None,
        notes=query_notes(section="rutina", **f),
        tags=tag_counts("rutina"),
        counts=_counts("rutina"),
        views=VIEWS,
        **f,
    )


# --- projects -------------------------------------------------------------------------
def _projects(include_archived: bool = False) -> list[sqlite3.Row]:
    sql = (
        "SELECT p.*, "
        "(SELECT COUNT(*) FROM notes n WHERE n.project_id = p.id) AS total, "
        "(SELECT COUNT(*) FROM notes n WHERE n.project_id = p.id AND n.is_task = 1 "
        " AND n.done = 0) AS pending, "
        "(SELECT MAX(updated_at) FROM notes n WHERE n.project_id = p.id) AS last_note "
        "FROM projects p "
    )
    if not include_archived:
        sql += "WHERE p.archived = 0 "
    sql += "ORDER BY p.archived, COALESCE(last_note, p.created_at) DESC"
    return get_db().execute(sql).fetchall()


def _project_or_404(project_id: int) -> sqlite3.Row:
    row = get_db().execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@bp.get("/proyectos")
@login_required
def projects() -> str:
    show_archived = request.args.get("archivados") == "1"
    q = (request.args.get("q") or "").strip()[:100]
    recent = query_notes(section="proyecto", q=q, limit=30)
    return render_template(
        "projects.html",
        projects=_projects(include_archived=show_archived),
        recent=recent,
        q=q,
        show_archived=show_archived,
        colors=PROJECT_COLORS,
    )


@bp.post("/proyectos")
@login_required
def create_project() -> Response:
    name = text("name", 80)
    if not name:
        flash("El proyecto necesita un nombre.", "error")
        return redirect(url_for("notes.projects"))
    color = request.form.get("color", PROJECT_COLORS[0])
    try:
        cur = get_db().execute(
            "INSERT INTO projects(name, description, color, created_at) VALUES (?, ?, ?, ?)",
            (
                name,
                text("description", 500),
                color if color in PROJECT_COLORS else PROJECT_COLORS[0],
                now(),
            ),
        )
        get_db().commit()
    except sqlite3.IntegrityError:
        flash("Ya existe un proyecto con ese nombre.", "error")
        return redirect(url_for("notes.projects"))
    return redirect(url_for("notes.project", project_id=cur.lastrowid))


@bp.get("/proyectos/<int:project_id>")
@login_required
def project(project_id: int) -> str:
    proj = _project_or_404(project_id)
    f = _filters()
    if "vista" not in request.args:
        f["view"] = "todo"
    return render_template(
        "notes.html",
        section="proyecto",
        project=proj,
        notes=query_notes(section="proyecto", project_id=project_id, **f),
        tags=tag_counts("proyecto", project_id),
        counts=_counts("proyecto", project_id),
        views=VIEWS,
        colors=PROJECT_COLORS,
        **f,
    )


@bp.post("/proyectos/<int:project_id>")
@login_required
def update_project(project_id: int) -> Response:
    _project_or_404(project_id)
    name = text("name", 80)
    color = request.form.get("color", PROJECT_COLORS[0])
    if not name:
        flash("El proyecto necesita un nombre.", "error")
    else:
        try:
            get_db().execute(
                "UPDATE projects SET name = ?, description = ?, color = ?, archived = ? "
                "WHERE id = ?",
                (
                    name,
                    text("description", 500),
                    color if color in PROJECT_COLORS else PROJECT_COLORS[0],
                    checked("archived"),
                    project_id,
                ),
            )
            get_db().commit()
            flash("Proyecto actualizado.", "ok")
        except sqlite3.IntegrityError:
            flash("Ya existe un proyecto con ese nombre.", "error")
    return redirect(url_for("notes.project", project_id=project_id))


@bp.post("/proyectos/<int:project_id>/borrar")
@login_required
def delete_project(project_id: int) -> Response:
    proj = _project_or_404(project_id)
    if text("confirm", 80) != proj["name"]:
        flash("Para borrar, escribe el nombre exacto del proyecto.", "error")
        return redirect(url_for("notes.project", project_id=project_id))
    get_db().execute("DELETE FROM projects WHERE id = ?", (project_id,))
    get_db().commit()
    flash(f"Proyecto «{proj['name']}» y sus notas borrados.", "ok")
    return redirect(url_for("notes.projects"))


# --- notes (shared) -------------------------------------------------------------------
def _note_or_404(note_id: int) -> sqlite3.Row:
    row = (
        get_db()
        .execute(
            "SELECT n.*, p.name AS project_name FROM notes n "
            "LEFT JOIN projects p ON p.id = n.project_id WHERE n.id = ?",
            (note_id,),
        )
        .fetchone()
    )
    if row is None:
        abort(404)
    return row


def _note_fields(*, date_implies_task: bool = False) -> dict[str, Any]:
    title, body = text("title", 200), text("body", 20000)
    if not title and body:
        # Quick capture: the first line becomes the title.
        first, _, rest = body.partition("\n")
        if len(first) <= 120:
            title, body = first.strip(), rest.strip()
    due = clean_date(request.form.get("due_date"))
    is_task = 1 if checked("is_task") or (date_implies_task and due) else 0
    return {
        "title": title,
        "body": body,
        "tags": clean_tags(request.form.get("tags")),
        "is_task": is_task,
        "due_date": due if is_task else None,
        "pinned": checked("pinned"),
    }


@bp.post("/notas")
@login_required
def create_note() -> Response:
    fields = _note_fields(date_implies_task=True)
    project_id = request.form.get("project_id", type=int)
    section = "proyecto" if project_id else "rutina"
    if project_id:
        _project_or_404(project_id)
    default = (
        url_for("notes.project", project_id=project_id) if project_id else url_for("notes.rutina")
    )
    if not fields["title"] and not fields["body"]:
        flash("La nota está vacía.", "error")
        return back(default)
    stamp = now()
    get_db().execute(
        "INSERT INTO notes(section, project_id, title, body, tags, is_task, due_date, pinned, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            section,
            project_id,
            fields["title"],
            fields["body"],
            fields["tags"],
            fields["is_task"],
            fields["due_date"],
            fields["pinned"],
            stamp,
            stamp,
        ),
    )
    get_db().commit()
    flash("Pendiente añadido." if fields["is_task"] else "Nota guardada.", "ok")
    return back(default)


@bp.get("/notas/<int:note_id>")
@login_required
def edit_note(note_id: int) -> str:
    note = _note_or_404(note_id)
    projects = _projects(include_archived=True)
    return render_template("note_edit.html", note=note, projects=projects)


def _note_home(note: sqlite3.Row) -> str:
    if note["project_id"]:
        return url_for("notes.project", project_id=note["project_id"])
    return url_for("notes.rutina")


@bp.post("/notas/<int:note_id>")
@login_required
def update_note(note_id: int) -> Response:
    _note_or_404(note_id)
    fields = _note_fields()
    if not fields["title"] and not fields["body"]:
        flash("La nota no puede quedar vacía.", "error")
        return redirect(url_for("notes.edit_note", note_id=note_id))
    # Moving between "rutina" and a project.
    target = request.form.get("project_id", type=int)
    if target:
        _project_or_404(target)
    section = "proyecto" if target else "rutina"
    get_db().execute(
        "UPDATE notes SET section = ?, project_id = ?, title = ?, body = ?, tags = ?, "
        "is_task = ?, due_date = ?, pinned = ?, done = CASE WHEN ? = 1 THEN done ELSE 0 END, "
        "updated_at = ? WHERE id = ?",
        (
            section,
            target,
            fields["title"],
            fields["body"],
            fields["tags"],
            fields["is_task"],
            fields["due_date"],
            fields["pinned"],
            fields["is_task"],
            now(),
            note_id,
        ),
    )
    get_db().commit()
    flash("Nota guardada.", "ok")
    return back(url_for("notes.project", project_id=target) if target else url_for("notes.rutina"))


@bp.post("/notas/<int:note_id>/hecho")
@login_required
def toggle_done(note_id: int) -> Response:
    note = _note_or_404(note_id)
    get_db().execute(
        "UPDATE notes SET done = 1 - done, updated_at = ? WHERE id = ?", (now(), note_id)
    )
    get_db().commit()
    return back(_note_home(note))


@bp.post("/notas/<int:note_id>/fijar")
@login_required
def toggle_pin(note_id: int) -> Response:
    note = _note_or_404(note_id)
    get_db().execute("UPDATE notes SET pinned = 1 - pinned WHERE id = ?", (note_id,))
    get_db().commit()
    return back(_note_home(note))


@bp.post("/notas/<int:note_id>/borrar")
@login_required
def delete_note(note_id: int) -> Response:
    note = _note_or_404(note_id)
    get_db().execute("DELETE FROM notes WHERE id = ?", (note_id,))
    get_db().commit()
    flash("Nota borrada.", "ok")
    return back(_note_home(note))
