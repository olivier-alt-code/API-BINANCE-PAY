"""Money: accounts in bolívares and USDT, income, expenses and currency exchanges."""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from notas.db import get_db, get_setting, now, set_setting, today
from notas.helpers import back, checked, clean_date, text
from notas.security import login_required

bp = Blueprint("money", __name__)

SCALE = 10_000  # amounts stored as integers of 1/10000
CURRENCIES = {"VES": "Bs", "USDT": "USDT"}
CATEGORIES = [
    "Comida",
    "Transporte",
    "Servicios",
    "Hogar",
    "Salud",
    "Ocio",
    "Educación",
    "Ropa",
    "Trabajo",
    "Regalos",
    "Otros",
]
SUGGESTED_ACCOUNTS = [("Efectivo Bs", "VES"), ("Banco Bs", "VES"), ("Binance", "USDT")]


# --- amounts ------------------------------------------------------------------------------
def parse_amount(raw: str | None) -> Decimal | None:
    """Accepts '1.234,56', '1,234.56', '1234,5', '1234.5', '12'. Returns a positive Decimal."""
    value = re.sub(r"[^\d.,]", "", (raw or "").strip())
    if not value:
        return None
    if "," in value and "." in value:
        decimal_sep = "," if value.rfind(",") > value.rfind(".") else "."
        thousands = "." if decimal_sep == "," else ","
        value = value.replace(thousands, "").replace(decimal_sep, ".")
    elif "," in value:
        value = (
            value.replace(".", "").replace(",", ".")
            if value.count(",") == 1
            else value.replace(",", "")
        )
    elif value.count(".") > 1:
        value = value.replace(".", "")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and 0 < amount < Decimal("1e12") else None


def to_units(amount: Decimal) -> int:
    return int((amount * SCALE).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_units(units: int | None) -> Decimal:
    return Decimal(units or 0) / SCALE


def fmt(units: int | None, currency: str | None = None, signed: bool = False) -> str:
    """12345678 -> '1.234,57' (Venezuelan style), optional currency suffix."""
    value = from_units(units)
    sign = "-" if value < 0 else ("+" if signed and value > 0 else "")
    q = abs(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, _, cents = f"{q:f}".partition(".")
    whole = f"{int(whole):,}".replace(",", ".")
    out = f"{sign}{whole},{cents or '00'}"
    return f"{out} {CURRENCIES.get(currency or '', currency or '')}".strip()


def current_rate() -> int | None:
    value = get_setting("rate_ves_usdt")
    return int(value) if value else None


def to_usdt(units: int, currency: str, rate: int | None) -> int | None:
    if currency == "USDT":
        return units
    if not rate:
        return None
    return round(units * SCALE / rate)


def to_ves(units: int, currency: str, rate: int | None) -> int | None:
    if currency == "VES":
        return units
    if not rate:
        return None
    return round(units * rate / SCALE)


# --- queries ------------------------------------------------------------------------------
def accounts(include_archived: bool = False) -> list[dict[str, Any]]:
    sql = (
        "SELECT a.*, a.initial + COALESCE((SELECT SUM(amount) FROM movements m "
        "WHERE m.account_id = a.id), 0) AS balance FROM accounts a "
    )
    if not include_archived:
        sql += "WHERE a.archived = 0 "
    rows = get_db().execute(sql + "ORDER BY a.archived, a.currency DESC, a.name").fetchall()
    return [dict(r) for r in rows]


def summary(month: str | None = None) -> dict[str, Any]:
    """Balances, totals and this month's income/expenses (for the page and the home)."""
    month = month or today()[:7]
    rate = current_rate()
    accts = accounts()
    totals = {c: sum(a["balance"] for a in accts if a["currency"] == c) for c in CURRENCIES}
    usdt_total = totals["USDT"] + (to_usdt(totals["VES"], "VES", rate) or 0)
    rows = (
        get_db()
        .execute(
            "SELECT m.kind, m.amount, m.category, m.rate, a.currency FROM movements m "
            "JOIN accounts a ON a.id = m.account_id "
            "WHERE m.kind != 'cambio' AND substr(m.date, 1, 7) = ?",
            (month,),
        )
        .fetchall()
    )
    spent = {c: 0 for c in CURRENCIES}
    earned = {c: 0 for c in CURRENCIES}
    by_category: dict[str, int] = {}
    spent_usdt = 0
    for r in rows:
        if r["kind"] == "gasto":
            spent[r["currency"]] += -r["amount"]
            eq = to_usdt(-r["amount"], r["currency"], r["rate"] or rate) or 0
            spent_usdt += eq
            by_category[r["category"] or "Otros"] = (
                by_category.get(r["category"] or "Otros", 0) + eq
            )
        else:
            earned[r["currency"]] += r["amount"]
    cats = sorted(by_category.items(), key=lambda kv: -kv[1])
    return {
        "month": month,
        "rate": rate,
        "rate_date": get_setting("rate_date"),
        "accounts": accts,
        "totals": totals,
        "usdt_total": usdt_total,
        "ves_total": to_ves(usdt_total, "USDT", rate) if rate else None,
        "spent": spent,
        "earned": earned,
        "spent_usdt": spent_usdt,
        "by_category": cats,
        "max_category": cats[0][1] if cats else 0,
    }


def _account_or_404(account_id: int | None) -> sqlite3.Row:
    row = (
        get_db().execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if account_id
        else None
    )
    if row is None:
        abort(404)
    return row


# --- pages --------------------------------------------------------------------------------
@bp.get("/dinero")
@login_required
def index() -> str:
    month = request.args.get("mes") or today()[:7]
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        month = today()[:7]
    account_id = request.args.get("cuenta", type=int)
    category = (request.args.get("cat") or "").strip()[:40]
    where = ["substr(m.date, 1, 7) = ?"]
    args: list[Any] = [month]
    if account_id:
        where.append("m.account_id = ?")
        args.append(account_id)
    if category:
        where.append("m.category = ?")
        args.append(category)
    movements = (
        get_db()
        .execute(
            "SELECT m.*, a.name AS account_name, a.currency FROM movements m "
            "JOIN accounts a ON a.id = m.account_id "
            f"WHERE {' AND '.join(where)} ORDER BY m.date DESC, m.id DESC LIMIT 500",
            args,
        )
        .fetchall()
    )
    months = [
        r[0]
        for r in get_db().execute(
            "SELECT DISTINCT substr(date, 1, 7) FROM movements ORDER BY 1 DESC LIMIT 36"
        )
    ]
    if today()[:7] not in months:
        months.insert(0, today()[:7])
    used = [
        r[0]
        for r in get_db().execute(
            "SELECT category FROM movements WHERE category != '' GROUP BY category "
            "ORDER BY COUNT(*) DESC LIMIT 30"
        )
    ]
    categories = used + [c for c in CATEGORIES if c not in used]
    return render_template(
        "money.html",
        s=summary(month),
        movements=movements,
        months=months,
        month=month,
        account_id=account_id,
        category=category,
        categories=categories,
        all_accounts=accounts(include_archived=True),
        suggested=SUGGESTED_ACCOUNTS,
        today=today(),
        fmt=fmt,
        to_usdt=to_usdt,
    )


@bp.post("/dinero/cuentas")
@login_required
def create_account() -> Response:
    name, currency = text("name", 60), request.form.get("currency", "")
    if not name or currency not in CURRENCIES:
        flash("La cuenta necesita un nombre y una moneda.", "error")
        return redirect(url_for("money.index"))
    initial = parse_amount(request.form.get("initial")) or Decimal(0)
    if request.form.get("initial_negative") == "1":
        initial = -initial
    try:
        get_db().execute(
            "INSERT INTO accounts(name, currency, initial, created_at) VALUES (?, ?, ?, ?)",
            (name, currency, to_units(initial), now()),
        )
        get_db().commit()
    except sqlite3.IntegrityError:
        flash("Ya existe una cuenta con ese nombre.", "error")
    return redirect(url_for("money.index"))


@bp.post("/dinero/cuentas/sugeridas")
@login_required
def create_suggested() -> Response:
    for name, currency in SUGGESTED_ACCOUNTS:
        get_db().execute(
            "INSERT OR IGNORE INTO accounts(name, currency, created_at) VALUES (?, ?, ?)",
            (name, currency, now()),
        )
    get_db().commit()
    flash("Cuentas creadas. Ajusta sus saldos iniciales si hace falta.", "ok")
    return redirect(url_for("money.index"))


@bp.post("/dinero/cuentas/<int:account_id>")
@login_required
def update_account(account_id: int) -> Response:
    _account_or_404(account_id)
    name = text("name", 60)
    initial = parse_amount(request.form.get("initial")) or Decimal(0)
    if request.form.get("initial_negative") == "1":
        initial = -initial
    try:
        get_db().execute(
            "UPDATE accounts SET name = COALESCE(NULLIF(?, ''), name), initial = ?, archived = ? "
            "WHERE id = ?",
            (name, to_units(initial), checked("archived"), account_id),
        )
        get_db().commit()
        flash("Cuenta actualizada.", "ok")
    except sqlite3.IntegrityError:
        flash("Ya existe una cuenta con ese nombre.", "error")
    return redirect(url_for("money.index"))


@bp.post("/dinero/tasa")
@login_required
def set_rate() -> Response:
    rate = parse_amount(request.form.get("rate"))
    if rate is None:
        flash("Escribe la tasa en Bs por 1 USDT, por ejemplo 150,25.", "error")
    else:
        set_setting("rate_ves_usdt", str(to_units(rate)))
        set_setting("rate_date", today())
        get_db().commit()
        flash(f"Tasa actualizada: 1 USDT = {fmt(to_units(rate), 'VES')}.", "ok")
    return redirect(url_for("money.index"))


@bp.post("/dinero/movimientos")
@login_required
def create_movement() -> Response:
    kind = request.form.get("kind", "gasto")
    if kind not in ("gasto", "ingreso"):
        abort(400)
    account = _account_or_404(request.form.get("account_id", type=int))
    amount = parse_amount(request.form.get("amount"))
    if amount is None:
        flash("Escribe un monto válido, por ejemplo 25,50.", "error")
        return redirect(url_for("money.index"))
    units = to_units(amount)
    get_db().execute(
        "INSERT INTO movements(account_id, kind, amount, category, description, date, rate, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            account["id"],
            kind,
            -units if kind == "gasto" else units,
            text("category", 40),
            text("description", 200),
            clean_date(request.form.get("date")) or today(),
            current_rate(),
            now(),
        ),
    )
    get_db().commit()
    flash(
        f"{'Gasto' if kind == 'gasto' else 'Ingreso'} de {fmt(units, account['currency'])} "
        "registrado.",
        "ok",
    )
    return redirect(url_for("money.index"))


@bp.post("/dinero/cambios")
@login_required
def create_exchange() -> Response:
    """Money moved between accounts: USDT -> Bs (selling), Bs -> USDT, or same currency."""
    source = _account_or_404(request.form.get("from_id", type=int))
    target = _account_or_404(request.form.get("to_id", type=int))
    out = parse_amount(request.form.get("amount_out"))
    got = parse_amount(request.form.get("amount_in"))
    if source["id"] == target["id"] or out is None:
        flash("Elige dos cuentas distintas y el monto que sale.", "error")
        return redirect(url_for("money.index"))
    if got is None:
        if source["currency"] != target["currency"]:
            flash("Para un cambio entre monedas indica también cuánto recibiste.", "error")
            return redirect(url_for("money.index"))
        got = out
    rate = current_rate()
    if source["currency"] != target["currency"]:
        ves, usdt = (out, got) if source["currency"] == "VES" else (got, out)
        rate = to_units(ves / usdt)
        if checked("update_rate"):
            set_setting("rate_ves_usdt", str(rate))
            set_setting("rate_date", today())
    exchange_id = uuid.uuid4().hex
    day = clean_date(request.form.get("date")) or today()
    note = text("description", 200)
    for account_id, units in ((source["id"], -to_units(out)), (target["id"], to_units(got))):
        get_db().execute(
            "INSERT INTO movements(account_id, kind, amount, category, description, date, rate, "
            "exchange_id, created_at) VALUES (?, 'cambio', ?, 'Cambio', ?, ?, ?, ?, ?)",
            (
                account_id,
                units,
                note or f"{source['name']} → {target['name']}",
                day,
                rate,
                exchange_id,
                now(),
            ),
        )
    get_db().commit()
    flash("Cambio registrado.", "ok")
    return redirect(url_for("money.index"))


@bp.post("/dinero/movimientos/<int:movement_id>/borrar")
@login_required
def delete_movement(movement_id: int) -> Response:
    row = get_db().execute("SELECT * FROM movements WHERE id = ?", (movement_id,)).fetchone()
    if row is not None:
        if row["exchange_id"]:  # both legs of an exchange go together
            get_db().execute("DELETE FROM movements WHERE exchange_id = ?", (row["exchange_id"],))
        else:
            get_db().execute("DELETE FROM movements WHERE id = ?", (movement_id,))
        get_db().commit()
        flash("Movimiento borrado.", "ok")
    return back(url_for("money.index"))


def month_name(value: str) -> str:
    names = [
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
    y, m = value.split("-")
    return f"{names[int(m) - 1]} {y}"


def days_ago(value: str | None) -> int | None:
    return (date.fromisoformat(today()) - date.fromisoformat(value)).days if value else None
