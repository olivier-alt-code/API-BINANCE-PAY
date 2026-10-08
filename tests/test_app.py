import sqlite3

from tests.conftest import PASSWORD, post, token


def test_setup_then_login_required(client):
    assert client.get("/").headers["Location"].endswith("/configurar")
    r = client.post(
        "/configurar",
        data={"password": "corta", "confirm": "corta", "csrf": token(client, "/configurar")},
    )
    assert "al menos 8" in r.get_data(as_text=True)
    client.post(
        "/configurar",
        data={"password": PASSWORD, "confirm": PASSWORD, "csrf": token(client, "/configurar")},
    )
    assert client.get("/").status_code == 200
    post(client, "/salir", {})
    assert "/entrar" in client.get("/").headers["Location"]
    r = client.post("/entrar", data={"password": "mal", "csrf": token(client)})
    assert "incorrecta" in r.get_data(as_text=True)
    r = client.post("/entrar", data={"password": PASSWORD, "csrf": token(client)})
    assert r.status_code == 302 and client.get("/").status_code == 200


def test_csrf_required(logged):
    assert logged.post("/notas", data={"body": "x"}).status_code == 400


def test_quick_capture_tasks_and_home(logged):
    post(logged, "/notas", {"body": "Comprar leche\nsemidesnatada", "tags": "Casa, #recados casa"})
    post(logged, "/notas", {"body": "Pagar luz", "due_date": "2000-01-01"})  # date => task
    post(logged, "/notas", {"body": "Meditar 10 min", "is_task": "1"})

    page = logged.get("/rutina?vista=pendientes").get_data(as_text=True)
    assert "Pagar luz" in page and "vencido" in page and "Meditar" in page
    assert "Comprar leche" not in page  # a note, not a task
    notas = logged.get("/rutina?vista=notas").get_data(as_text=True)
    assert "Comprar leche" in notas and "semidesnatada" in notas and "#recados" in notas
    assert "Comprar leche" in logged.get("/rutina?vista=todo&tag=casa").get_data(as_text=True)

    home = logged.get("/").get_data(as_text=True)
    assert "Pagar luz" in home and "1 vencido" in home and "Comprar leche" in home

    # Toggle done: it leaves "pendientes" and appears in "hechas".
    db = sqlite3.connect(logged.application.config["DATABASE"])
    note_id = db.execute("SELECT id FROM notes WHERE title = 'Pagar luz'").fetchone()[0]
    post(logged, f"/notas/{note_id}/hecho", {"next": "/rutina"})
    assert "Pagar luz" not in logged.get("/rutina?vista=pendientes").get_data(as_text=True)
    assert "Pagar luz" in logged.get("/rutina?vista=hechas").get_data(as_text=True)


def test_projects_filter_and_move_note(logged):
    post(logged, "/proyectos", {"name": "Puente grúa", "color": "#16a37f"}, "/proyectos")
    post(logged, "/proyectos", {"name": "Robot", "color": "#4f7cff"}, "/proyectos")
    db = sqlite3.connect(logged.application.config["DATABASE"])
    ids = dict(db.execute("SELECT name, id FROM projects").fetchall())
    post(
        logged,
        "/notas",
        {"body": "Viga IPE 300\nmomento máximo 120 kNm", "project_id": ids["Puente grúa"]},
    )
    post(
        logged,
        "/notas",
        {"body": "Elegir motor paso a paso", "project_id": ids["Robot"], "is_task": "1"},
    )

    grua = logged.get(f"/proyectos/{ids['Puente grúa']}").get_data(as_text=True)
    assert "Viga IPE 300" in grua and "motor paso a paso" not in grua
    listing = logged.get("/proyectos").get_data(as_text=True)
    assert "Puente grúa" in listing and "1 pendiente" in listing
    assert "motor" not in logged.get("/rutina?vista=todo").get_data(as_text=True)

    # Move the note to "rutina".
    note_id = db.execute("SELECT id FROM notes WHERE title LIKE 'Viga%'").fetchone()[0]
    post(logged, f"/notas/{note_id}", {"title": "Viga IPE 300", "body": "ok", "project_id": ""})
    assert "Viga IPE 300" in logged.get("/rutina?vista=todo").get_data(as_text=True)

    # Deleting a project requires typing its name and removes its notes.
    post(logged, f"/proyectos/{ids['Robot']}/borrar", {"confirm": "robot?"})
    assert db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 2
    post(logged, f"/proyectos/{ids['Robot']}/borrar", {"confirm": "Robot"})
    db = sqlite3.connect(logged.application.config["DATABASE"])
    assert db.execute("SELECT COUNT(*) FROM notes WHERE title LIKE 'Elegir%'").fetchone()[0] == 0


def test_plans_steps(logged):
    r = post(
        logged, "/planes", {"title": "Viaje a Mérida", "steps": "Reservar hotel\nComprar billetes"}
    )
    plan_url = r.headers["Location"]
    post(logged, plan_url + "/pasos", {"text": "Hacer maleta"})
    db = sqlite3.connect(logged.application.config["DATABASE"])
    steps = db.execute("SELECT id, text FROM plan_steps ORDER BY position").fetchall()
    assert [t for _, t in steps] == ["Reservar hotel", "Comprar billetes", "Hacer maleta"]
    post(logged, f"/pasos/{steps[0][0]}/hecho", {})
    post(logged, f"/pasos/{steps[2][0]}/mover", {"dir": "up"})
    order = [t for (t,) in db.execute("SELECT text FROM plan_steps ORDER BY position")]
    assert order == ["Reservar hotel", "Hacer maleta", "Comprar billetes"]
    page = logged.get(plan_url).get_data(as_text=True)
    assert "1/3 hechos" in page
    assert "Viaje a Mérida" in logged.get("/").get_data(as_text=True)
    assert "Mérida" in logged.get("/buscar?q=maleta").get_data(as_text=True)


def test_vault_is_encrypted_and_survives_password_change(logged):
    post(
        logged,
        "/claves",
        {"name": "Gmail", "username": "yo@gmail.com", "password": "S3cr3t!pw", "notes": "pin 1234"},
        "/claves",
    )
    db = sqlite3.connect(logged.application.config["DATABASE"])
    raw = db.execute("SELECT secret FROM vault").fetchone()[0]
    assert "S3cr3t" not in raw and "1234" not in raw
    page = logged.get("/claves").get_data(as_text=True)
    assert 'data-secret="S3cr3t!pw"' in page and "pin 1234" in page

    r = post(
        logged,
        "/ajustes",
        {"current": PASSWORD, "new": "otra-clave-456", "confirm": "otra-clave-456"},
        "/ajustes",
    )
    assert r.status_code == 302
    assert 'data-secret="S3cr3t!pw"' in logged.get("/claves").get_data(as_text=True)
    post(logged, "/salir", {})
    r = logged.post("/entrar", data={"password": PASSWORD, "csrf": token(logged)})
    assert "incorrecta" in r.get_data(as_text=True)
    logged.post("/entrar", data={"password": "otra-clave-456", "csrf": token(logged)})
    assert 'data-secret="S3cr3t!pw"' in logged.get("/claves").get_data(as_text=True)


def test_vault_locks_after_idle(logged):
    logged.application.config["IDLE_TIMEOUT_SECONDS"] = -1
    assert "/entrar" in logged.get("/claves").headers["Location"]


def test_open_redirect_is_blocked(logged):
    r = post(logged, "/notas", {"body": "x", "next": "//evil.com"})
    assert r.headers["Location"] == "/rutina"
