import sqlite3
from decimal import Decimal

from notas.money import fmt, parse_amount, to_units
from tests.conftest import post


def test_parse_amount_formats():
    assert parse_amount("1.234,56") == Decimal("1234.56")
    assert parse_amount("1,234.56") == Decimal("1234.56")
    assert parse_amount("25,5") == Decimal("25.5")
    assert parse_amount("1.000.000") == Decimal("1000000")
    assert parse_amount("12") == Decimal("12")
    assert parse_amount("1.500") == Decimal("1500")
    assert parse_amount("12.5") == Decimal("12.5")
    assert parse_amount("0.15") == Decimal("0.15")
    assert parse_amount("Bs 3.500,00") == Decimal("3500.00")
    assert parse_amount("0") is None and parse_amount("abc") is None
    assert fmt(to_units(Decimal("1234567.891")), "VES") == "1.234.567,89 Bs"
    assert fmt(-to_units(Decimal("5")), "USDT", signed=True) == "-5,00 USDT"


def test_money_flow(logged):
    post(logged, "/dinero/cuentas/sugeridas", {}, "/dinero")
    db = sqlite3.connect(logged.application.config["DATABASE"])
    ids = dict(db.execute("SELECT name, id FROM accounts").fetchall())
    post(logged, "/dinero/tasa", {"rate": "150,00"}, "/dinero")
    post(
        logged,
        "/dinero/movimientos",
        {"kind": "ingreso", "amount": "100", "account_id": ids["Binance"], "category": "Trabajo"},
        "/dinero",
    )
    # Sell 20 USDT for 3.100 Bs (rate 155) and update the reference rate.
    post(
        logged,
        "/dinero/cambios",
        {
            "from_id": ids["Binance"],
            "to_id": ids["Banco Bs"],
            "amount_out": "20",
            "amount_in": "3.100,00",
            "update_rate": "1",
        },
        "/dinero",
    )
    post(
        logged,
        "/dinero/movimientos",
        {
            "kind": "gasto",
            "amount": "1.550,00",
            "account_id": ids["Banco Bs"],
            "category": "Comida",
            "description": "Mercado",
        },
        "/dinero",
    )

    page = logged.get("/dinero").get_data(as_text=True)
    assert "80,00 USDT" in page  # Binance: 100 - 20
    assert "1.550,00 Bs" in page  # bank: 3100 - 1550
    assert "155,00" in page  # rate updated by the exchange
    assert "90,00 USDT" in page  # total: 80 + 1550/155
    assert "Mercado" in page and "Comida" in page
    assert "Dinero" in logged.get("/").get_data(as_text=True)

    # Deleting one leg of an exchange deletes both.
    leg = db.execute("SELECT id FROM movements WHERE kind = 'cambio' LIMIT 1").fetchone()[0]
    post(logged, f"/dinero/movimientos/{leg}/borrar", {}, "/dinero")
    db = sqlite3.connect(logged.application.config["DATABASE"])
    assert db.execute("SELECT COUNT(*) FROM movements WHERE kind = 'cambio'").fetchone()[0] == 0


def test_offline_endpoints(client, logged):
    sw = logged.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.content_type
    assert "outbox" in sw.get_data(as_text=True)
    assert logged.get("/manifest.webmanifest").json["start_url"] == "/"
    assert "csrf" in logged.get("/api/csrf").json
    post(logged, "/salir", {})
    assert logged.get("/api/csrf").status_code == 401
