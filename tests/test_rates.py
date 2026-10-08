import sqlite3

from notas import money
from tests.conftest import post

API = [
    {"fuente": "oficial", "nombre": "Oficial", "compra": None, "venta": None, "promedio": 100.0},
    {"fuente": "paralelo", "nombre": "Paralelo", "compra": None, "venta": None, "promedio": 150.0},
]


def test_rates_from_api_and_bcv_reference(logged, monkeypatch):
    eur = [{"fuente": "oficial", "promedio": 110.0}, {"fuente": "paralelo", "promedio": 170.0}]
    monkeypatch.setattr(money, "_http_get_json", lambda url: eur if "euros" in url else API)
    post(logged, "/dinero/cuentas/sugeridas", {}, "/dinero")
    db = sqlite3.connect(logged.application.config["DATABASE"])
    ids = dict(db.execute("SELECT name, id FROM accounts").fetchall())
    post(
        logged,
        "/dinero/movimientos",
        {"kind": "ingreso", "amount": "1.500", "account_id": ids["Banco Bs"]},
        "/dinero",
    )
    post(
        logged,
        "/dinero/movimientos",
        {"kind": "ingreso", "amount": "10", "account_id": ids["Binance"]},
        "/dinero",
    )
    page = logged.get("/dinero").get_data(as_text=True)
    assert "150,00 Bs" in page and "100,00 Bs" in page and "50,0 %" in page  # rates + gap
    assert "20,00 USDT" in page  # 10 USDT + 1500/150
    assert "30,00 $" in page  # BCV: 1500/100 = 15 $ + 10*150/100 = 15 $
    assert "50,0 %" in page  # distribution 50/50
    assert 'data-eur-oficial="110,00"' in page and 'data-eur-paralelo="170,00"' in page


def test_rates_offline_keeps_last(logged, monkeypatch):
    def boom(url):
        raise OSError("offline")

    monkeypatch.setattr(money, "_http_get_json", boom)
    r = post(logged, "/dinero/tasas/actualizar", {}, "/dinero")
    assert r.status_code == 302
    assert "No se pudieron" in logged.get("/dinero").get_data(as_text=True)
