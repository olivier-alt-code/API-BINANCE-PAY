from datetime import datetime
from zoneinfo import ZoneInfo

from notas import db, home


def test_greeting_uses_venezuela_time(logged, monkeypatch):
    # 21:00 UTC is 17:00 in Caracas (UTC-4): afternoon, not night.
    utc = datetime(2026, 10, 8, 21, 0, tzinfo=ZoneInfo("UTC"))
    monkeypatch.setattr(home, "local_now", lambda: utc.astimezone(db.TZ))
    page = logged.get("/").get_data(as_text=True)
    assert "Buenas tardes" in page and "jueves 8 de octubre" in page.lower()


def test_dates_are_local():
    assert str(db.TZ) == "America/Caracas"
    assert db.local_now().utcoffset().total_seconds() == -4 * 3600
