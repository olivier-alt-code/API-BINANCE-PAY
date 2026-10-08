"""SQLite storage: one file, created on first run."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    color       TEXT NOT NULL DEFAULT '#4f7cff',
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- Notes of the "rutina" section and of engineering projects. A note can be a task
-- (is_task=1) with an optional due date.
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY,
    section    TEXT NOT NULL CHECK (section IN ('rutina', 'proyecto')),
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    title      TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    tags       TEXT NOT NULL DEFAULT '',
    is_task    INTEGER NOT NULL DEFAULT 0,
    done       INTEGER NOT NULL DEFAULT 0,
    due_date   TEXT,
    pinned     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((section = 'proyecto') = (project_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_notes_section ON notes(section, updated_at);
CREATE INDEX IF NOT EXISTS ix_notes_project ON notes(project_id, updated_at);

CREATE TABLE IF NOT EXISTS plans (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    target_date TEXT,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_steps (
    id       INTEGER PRIMARY KEY,
    plan_id  INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    text     TEXT NOT NULL,
    done     INTEGER NOT NULL DEFAULT 0,
    due_date TEXT,
    position INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_steps_plan ON plan_steps(plan_id, position);

-- Money: accounts in bolívares (VES) or USDT. Amounts are integers in 1/10000 units
-- (exact sums in SQL), signed: + money in, - money out.
CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    currency   TEXT NOT NULL CHECK (currency IN ('VES', 'USDT')),
    initial    INTEGER NOT NULL DEFAULT 0,
    archived   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS movements (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('ingreso', 'gasto', 'cambio')),
    amount      INTEGER NOT NULL,
    category    TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    date        TEXT NOT NULL,
    rate        INTEGER,               -- Bs per USDT (x10000) when it was recorded
    exchange_id TEXT,                  -- both legs of a currency exchange share it
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_movements_date ON movements(date);
CREATE INDEX IF NOT EXISTS ix_movements_account ON movements(account_id, date);

-- Password vault: secrets are encrypted with a key derived from the master password.
CREATE TABLE IF NOT EXISTS vault (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    username   TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    secret     TEXT NOT NULL,          -- Fernet token of {"password": ..., "notes": ...}
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_: object = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# All dates and times are local to the user (Venezuela by default), not to the server,
# which may run in UTC (e.g. in the cloud). Change it with NOTAS_TZ=Region/City.
TZ = ZoneInfo(os.environ.get("NOTAS_TZ", "America/Caracas"))


def local_now() -> datetime:
    return datetime.now(TZ)


def now() -> str:
    return local_now().replace(tzinfo=None).isoformat(timespec="seconds")


def today() -> str:
    return local_now().date().isoformat()


def get_setting(key: str) -> str | None:
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    get_db().execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
