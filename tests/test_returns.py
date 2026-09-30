"""
test_returns.py — qaytish yukini kuzatish: yo'ldagi fura bo'sh qaytmasin.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

import actions
import config
import db
import notifier
import pipeline
import returns
import settings


@pytest.fixture
def on_trip(clean_db, truck_tent, monkeypatch):
    """01 fura Toshkent → Moskva reysini oldi — endi Moskvada bo'shaydi."""
    monkeypatch.setattr(config, "BOT_TOKEN", "t")
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)
    clean_db.upsert_truck(truck_tent)
    ids = pipeline.handle_message("Груз Ташкент → Москва, 20т тент, 4000$", source="g")
    res = actions.take_match(db.find_match(ids[0], "01")["id"])
    assert res.ok and db.get_truck("01")["current_city"] == "Moskva"
    return res


def test_new_cargo_from_destination_is_marked_return(on_trip, monkeypatch):
    seen = []
    monkeypatch.setattr(notifier, "notify_match",
                        lambda cargo, r, mid=None, reason=None, insight=None:
                        seen.append(reason) or True)
    settings.save({"notify_threshold": "99"})         # oddiy chegara juda baland
    pipeline.handle_message("Груз Москва → Ташкент, 20т тент, 3800$", source="g2")
    assert seen and "Обратный груз для №01" in seen[0]   # past chegara bilan baribir keldi


def test_free_truck_gets_no_return_label(clean_db, truck_tent):
    clean_db.upsert_truck(truck_tent)
    assert returns.match_reason("01") is None


def test_reminder_day_before_once(on_trip):
    pipeline.handle_message("Груз Москва → Ташкент, 20т тент, 3800$", source="g2")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    db.set_truck_position("01", free_date=tomorrow, source="trip")
    sent = []
    assert returns.remind_due(send=lambda text, kb=None: sent.append((text, kb))) == 1
    text, kb = sent[0]
    assert "завтра освобождается в Moskva" in text and "Moskva → Toshkent" in text
    assert kb["inline_keyboard"][0][0]["callback_data"].startswith("take:")
    assert returns.remind_due(send=lambda *a: sent.append(a)) == 0      # takrorlanmaydi


def test_reminder_not_early_and_waits_when_empty(on_trip):
    far = (date.today() + timedelta(days=5)).isoformat()
    db.set_truck_position("01", free_date=far, source="trip")
    sent = []
    assert returns.remind_due(send=lambda *a: sent.append(a)) == 0      # hali erta
    db.set_truck_position("01", free_date=date.today().isoformat(), source="trip")
    assert returns.remind_due(send=lambda text, kb=None: sent.append(text)) == 1
    assert "пока нет" in sent[0]
