"""
test_watchdog.py — nazoratchi faqat holat o'zgarganda yozadi.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import db
import watchdog


def test_alerts_once_then_recovery(clean_db):
    sent = []
    broken = lambda: {"service:baxt-listener": "baxt-listener ishlamayapti"}   # noqa: E731
    ok = lambda: {}                                                             # noqa: E731

    watchdog.run(checks=(broken,), send=sent.append)
    watchdog.run(checks=(broken,), send=sent.append)      # takror — jim
    assert len(sent) == 1 and "muammo" in sent[0] and "listener" in sent[0]

    watchdog.run(checks=(ok,), send=sent.append)
    assert len(sent) == 2 and "tuzaldi" in sent[1]
    watchdog.run(checks=(ok,), send=sent.append)          # hammasi joyida — jim
    assert len(sent) == 2


def test_broken_check_is_reported_not_crash(clean_db):
    sent = []

    def boom():
        raise RuntimeError("x")
    problems = watchdog.run(checks=(boom,), send=sent.append)
    assert "check:boom" in problems and sent


def test_fresh_cargo_quiet_at_day(clean_db, monkeypatch):
    old = (db.utc_now() - timedelta(hours=3)).isoformat(" ", "seconds")
    with db.connect() as conn:
        conn.execute("INSERT INTO cargos (fingerprint, from_city, to_city, source, raw_text,"
                     " created_at) VALUES ('f','Toshkent','Moskva','grp','x',?)", (old,))
    day = datetime(2026, 9, 30, 14, 0)
    night = datetime(2026, 9, 30, 3, 0)
    assert "fresh" in watchdog.check_fresh_cargo(day)
    assert watchdog.check_fresh_cargo(night) == {}            # tunda jimlik — normal


def test_fresh_cargo_ok_when_recent(clean_db):
    with db.connect() as conn:
        conn.execute("INSERT INTO cargos (fingerprint, from_city, to_city, source, raw_text)"
                     " VALUES ('f','Toshkent','Moskva','grp','x')")
    assert watchdog.check_fresh_cargo(datetime(2026, 9, 30, 14, 0)) == {}


def test_services_and_code_checks(monkeypatch):
    monkeypatch.setattr(watchdog, "_run", lambda cmd: (3, "inactive") if "baxt-bot" in cmd
                        else (0, "active"))
    assert list(watchdog.check_services()) == ["service:baxt-bot"]
    monkeypatch.setattr(watchdog, "_run", lambda cmd: (0, " M web.py\n M bot.py"))
    assert "web.py, bot.py" in watchdog.check_code()["code"]
    monkeypatch.setattr(watchdog, "_run", lambda cmd: (0, ""))
    assert watchdog.check_code() == {}
