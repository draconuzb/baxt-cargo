"""
test_trips.py — reys hayoti: olish → yo'lda → tugadi, yoki xato bosilsa bekor.

Fura holati har doim aniq bo'lishi kerak: dispetcher kartochkaga qarab
"bu fura bo'shmi yoki yuk olib ketyaptimi" ni darhol ko'rsin.
"""
from __future__ import annotations

from datetime import date

import pytest

import actions
import config
import db
import pipeline


@pytest.fixture
def scene(clean_db, truck_tent, truck_ref, monkeypatch):
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)
    monkeypatch.setattr(config, "BOT_TOKEN", "t")
    for t in (truck_tent, truck_ref):
        clean_db.upsert_truck(t)
    ids = pipeline.handle_message("Груз Ташкент → Москва, 20т тент, 4000$, 22.09", source="g")
    ids += pipeline.handle_message("Груз Ташкент → Казань, 20т тент, 3500$, 22.09", source="g")
    return {"ids": ids}


def take(cargo_id, truck_id="01"):
    return actions.take_match(db.find_match(cargo_id, truck_id)["id"])


def test_taken_trip_is_active(scene):
    res = take(scene["ids"][0])
    trips = db.active_trips("01")
    assert [t["cargo_id"] for t in trips] == [scene["ids"][0]]
    assert trips[0]["prev_city"] == "Toshkent"        # olishdan oldingi joy saqlandi
    assert db.get_truck("01")["current_city"] == "Moskva"
    assert res.ok


def test_undo_restores_everything(scene):
    before = dict(db.get_truck("01"))
    res = take(scene["ids"][0])
    assert actions.undo_take(res.match["id"]) == "ok"
    after = db.get_truck("01")
    assert (after["current_city"], after["free_date"]) == (before["current_city"],
                                                          before["free_date"])
    assert db.get_cargo(scene["ids"][0])["status"] == "new"
    assert db.get_match(res.match["id"])["decision"] == "undone"
    assert db.active_trips("01") == []
    # yuk yana taklif qilinadi — boshqa furaga ham
    assert pipeline.rematch_cargo(scene["ids"][0]) >= 1
    assert db.find_match(scene["ids"][0], "02") is not None


def test_undo_not_counted_in_stats(scene):
    import analytics
    res = take(scene["ids"][0])
    actions.undo_take(res.match["id"])
    assert analytics.totals(30)["taken"] == 0


def test_undo_only_latest_trip(scene):
    first = take(scene["ids"][0])
    # ikkinchi reys (masalan qaytish yuki) — fura endi Moskvada; yangi taklif
    import scoring
    cargo = dict(db.get_cargo(scene["ids"][1]))
    r = scoring.evaluate(cargo, db.get_truck("01"), ignore_date=True)
    r["ok"] = True
    mid = db.save_match(cargo["id"], "01", r) or db.revive_match(cargo["id"], "01", r)
    assert actions.take_match(mid).ok
    assert actions.undo_take(first.match["id"]) == "not_latest"
    assert actions.undo_take(mid) == "ok"
    assert actions.undo_take(first.match["id"]) == "ok"


def test_finish_trip(scene):
    res = take(scene["ids"][0])
    assert actions.finish_trip(res.match["id"], 1850) == "ok"
    m = db.get_match(res.match["id"])
    assert m["finished_at"] and m["actual_margin_usd"] == 1850
    assert db.active_trips("01") == []
    assert [t["id"] for t in db.finished_trips()] == [res.match["id"]]
    assert actions.finish_trip(res.match["id"]) == "finished"
    assert actions.undo_take(res.match["id"]) == "finished"   # tugagan reys bekor bo'lmaydi


def test_finish_early_frees_truck_today(scene, monkeypatch):
    res = take(scene["ids"][0])
    db.set_truck_position("01", free_date="2099-01-01", source="trip")
    actions.finish_trip(res.match["id"])
    assert db.get_truck("01")["free_date"] == date.today().isoformat()


def test_actions_on_wrong_state(scene):
    m = db.find_match(scene["ids"][0], "01")
    assert actions.undo_take(m["id"]) == "not_taken"
    assert actions.finish_trip(m["id"]) == "not_taken"
    assert actions.undo_take(99999) == "not_found"


# ---------------------------------------------------------------- bot

@pytest.fixture
def tg(monkeypatch):
    import bot
    calls = []
    monkeypatch.setattr(bot, "api", lambda method, http_timeout=15, **p:
                        calls.append({"method": method, **p}) or {"ok": True, "result": {}})
    return calls


def cb(data):
    return {"id": "cb", "data": data, "message": {"message_id": 5, "chat": {"id": 777}}}


def test_bot_take_offers_undo_and_finish(scene, tg):
    import bot
    m = db.find_match(scene["ids"][0], "01")
    bot.handle_callback(cb(f"take:{m['id']}"))
    kb = [c for c in tg if c["method"] == "sendMessage"][0]["reply_markup"]
    datas = [b["callback_data"] for b in kb["inline_keyboard"][0]]
    assert datas == [f"undo:{m['id']}", f"finish:{m['id']}"]

    bot.handle_callback(cb(f"undo:{m['id']}"))
    assert db.get_cargo(scene["ids"][0])["status"] == "new"
    assert db.get_match(m["id"])["decision"] == "undone"


def test_bot_finish_and_fleet_shows_trip(scene, tg):
    import bot
    m = db.find_match(scene["ids"][0], "01")
    bot.handle_callback(cb(f"take:{m['id']}"))
    bot.handle_message({"message_id": 1, "chat": {"id": 777}, "text": bot.KB_FLEET})
    fleet = [c["text"] for c in tg if c["method"] == "sendMessage"][-1]
    assert "в рейсе: Toshkent → Moskva" in fleet
    bot.handle_callback(cb(f"finish:{m['id']}"))
    assert db.active_trips() == []


def test_bot_done_finishes_trip(scene, tg):
    import bot
    m = db.find_match(scene["ids"][0], "01")
    actions.take_match(m["id"])
    bot.handle_message({"message_id": 1, "chat": {"id": 777}, "text": f"/done {m['id']} 1850"})
    assert db.get_match(m["id"])["finished_at"] is not None


# ---------------------------------------------------------------- haydovchiga xabar

@pytest.fixture
def outbox(monkeypatch):
    """notifier.send ning haqiqiy yo'li: kimga nima ketdi (chat -> matnlar)."""
    import notifier
    sent = []

    def fake_send(text, reply_markup=None, chat_id=None):
        sent.append((str(chat_id or "777"), text))
        return {"ok": True}
    monkeypatch.setattr(notifier, "send", fake_send)     # scene "jim" send'ini almashtiramiz
    monkeypatch.setattr(config, "BOT_TOKEN", "t")
    monkeypatch.setattr(config, "DISPATCHER_CHAT_ID", "777")
    return sent


def test_linked_driver_gets_trip(scene, tg, outbox):
    import bot
    db.link_truck_tg_user("01", 5551)
    m = db.find_match(scene["ids"][0], "01")
    bot.handle_callback(cb(f"take:{m['id']}"))
    to_driver = [t for chat, t in outbox if chat == "5551"]
    assert to_driver and "Новый рейс" in to_driver[0] and "Toshkent → Moskva" in to_driver[0]
    dispatcher = [c["text"] for c in tg if c["method"] == "sendMessage"][0]
    assert "Водителю отправлено" in dispatcher

    bot.handle_callback(cb(f"undo:{m['id']}"))
    assert any("Рейс отменён" in t for chat, t in outbox if chat == "5551")


def test_unlinked_driver_hint(scene, tg, outbox):
    import bot
    db.update_truck("01", plate="01 A 111 AA")
    m = db.find_match(scene["ids"][0], "01")
    bot.handle_callback(cb(f"take:{m['id']}"))
    dispatcher = [c["text"] for c in tg if c["method"] == "sendMessage"][0]
    assert "не подключён" in dispatcher and "/link 01 01 A 111 AA" in dispatcher
    assert not [chat for chat, _ in outbox if chat not in ("777",)]


def test_link_requires_plate(scene, tg):
    import bot
    db.update_truck("02", plate="")
    bot.handle_message({"message_id": 1, "chat": {"id": 9}, "from": {"id": 9},
                        "text": "/link 2 X"})
    assert "госномер" in [c["text"] for c in tg if c["method"] == "sendMessage"][-1]
