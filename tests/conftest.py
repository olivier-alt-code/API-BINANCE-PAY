import re

import pytest

from notas import create_app

CSRF = re.compile(r'name="csrf" value="([^"]+)"')
PASSWORD = "clave-maestra-123"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTAS_DATA_DIR", str(tmp_path))
    app = create_app({"TESTING": True})
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def token(client, path="/entrar"):
    page = client.get(path, follow_redirects=True).get_data(as_text=True)
    match = CSRF.search(page)
    assert match, page
    return match.group(1)


def post(client, path, data, page="/"):
    return client.post(path, data={**data, "csrf": token(client, page)})


@pytest.fixture
def logged(client):
    client.post(
        "/configurar",
        data={"password": PASSWORD, "confirm": PASSWORD, "csrf": token(client, "/configurar")},
    )
    return client
