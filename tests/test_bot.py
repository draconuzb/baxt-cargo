"""
test_bot.py — kartochka tugmalari (TZ 5.1).

Telegramga chiqish to'xtatilgan: `bot.api` o'rniga yig'uvchi qo'yiladi,
shuning uchun testlar tarmoqsiz ishlaydi.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

import bot
import config
import db
import notifier
import pipeline


class Calls(list):
    """Telegramga ketgan chaqiruvlar ro'yxati."""

    def texts(self) -> list[str]:
        return [c.get("text", "") for c in self if c["method"] == "sendMessage"]

    def answers(self) -> list[str]:
        return [c.get("text", "") for c in self
                if c["method"] == "answerCallbackQuery"]


@pytest.fixture
def tg(monkeypatch):
    """Telegram chaqiruvlarini yig'ib boradi (tarmoqqa chiqilmaydi)."""
    calls = Calls()

    def fake_api(method, http_timeout=15, **params):
        calls.append({"method": method, **params})
        return {"ok": True, "result": {}}

    monkeypatch.setattr(bot, "api", fake_api)
    monkeypatch.setattr(config, "BOT_TOKEN", "test-token")
    return calls


@pytest.fixture
def scene(clean_db, truck_tent, truck_ref, monkeypatch):
    """Bazada ikki mashina va bitta yuk + mosliklar."""
    monkeypatch.setattr(config, "BOT_TOKEN", "test-token")
    for t in (truck_tent, truck_ref):
        clean_db.upsert_truck(t)
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)
    ids = pipeline.handle_message(
        "Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09", source="grp1")
    cargo_id = ids[0]
    with clean_db.connect() as conn:
        matches = conn.execute(
            "SELECT * FROM matches WHERE cargo_id=? ORDER BY score DESC",
            (cargo_id,)).fetchall()
    return {"cargo_id": cargo_id, "matches": matches,
            "best": matches[0], "other": matches[1]}


def callback(data: str, chat_id=777, message_id=5) -> dict:
    return {"id": "cb1", "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat_id}}}


# ---------------------------------------------------------------- ✅ Беру

def test_take_assigns_truck(scene, tg):
    m = scene["best"]
    bot.handle_callback(callback(f"take:{m['id']}"))

    assert db.get_match(m["id"])["decision"] == "taken"
    assert db.get_cargo(scene["cargo_id"])["status"] == "taken"


def test_take_moves_truck_to_destination(scene, tg):
    """Mashina yangi shaharda va yangi bo'shash sanasi bilan qoladi."""
    m = scene["best"]
    bot.handle_callback(callback(f"take:{m['id']}"))

    truck = db.get_truck(m["truck_id"])
    assert truck["current_city"] == "Moskva"
    assert truck["pos_source"] == "trip"
    # yuklash 22.09 + reys kunlari
    assert date.fromisoformat(truck["free_date"]) > date(2026, 9, 22)


def test_take_cancels_other_trucks(scene, tg):
    """Yuk olingandan keyin boshqa mashinalarga taklif qolmaydi."""
    bot.handle_callback(callback(f"take:{scene['best']['id']}"))
    assert db.get_match(scene["other"]["id"])["decision"] == "cancelled"


def test_cargo_cannot_be_taken_twice(scene, tg):
    """Ikkinchi bosishda — "уже взято", holat o'zgarmaydi."""
    bot.handle_callback(callback(f"take:{scene['best']['id']}"))
    truck_before = dict(db.get_truck(scene["other"]["truck_id"]))

    bot.handle_callback(callback(f"take:{scene['other']['id']}"))

    assert "уже взят" in " ".join(tg.answers()).lower()
    assert db.get_match(scene["other"]["id"])["decision"] == "cancelled"
    assert dict(db.get_truck(scene["other"]["truck_id"])) == truck_before


def test_take_answers_callback(scene, tg):
    """TZ mezoni: tugma bosilganda javob keladi."""
    bot.handle_callback(callback(f"take:{scene['best']['id']}"))
    assert tg.answers(), "answerCallbackQuery chaqirilmadi"


def test_take_confirms_and_offers_roundtrip(scene, tg):
    bot.handle_callback(callback(f"take:{scene['best']['id']}"))
    texts = " ".join(tg.texts())
    assert "Машина №" in texts
    assert "Обратный груз" in texts


def test_take_finds_real_return_cargo(scene, tg, monkeypatch):
    """Moskvadan qaytish yuki bo'lsa — taklifda ko'rinadi."""
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)
    pipeline.handle_message(
        "Есть груз Москва → Ташкент, 20т тент, 3800$, 05.10", source="grp1")

    bot.handle_callback(callback(f"take:{scene['best']['id']}"))
    texts = " ".join(tg.texts())
    assert "Moskva → Toshkent" in texts


def test_take_buttons_are_replaced(scene, tg):
    bot.handle_callback(callback(f"take:{scene['best']['id']}"))
    edits = [c for c in tg if c["method"] == "editMessageReplyMarkup"]
    assert edits
    label = edits[-1]["reply_markup"]["inline_keyboard"][0][0]["text"]
    assert "Взято" in label


# ---------------------------------------------------------------- ⏭ Пропустить

def test_skip_records_decision(scene, tg):
    m = scene["best"]
    bot.handle_callback(callback(f"skip:{m['id']}"))

    assert db.get_match(m["id"])["decision"] == "skipped"
    assert db.get_cargo(scene["cargo_id"])["status"] == "new"   # yuk ochiq qoladi
    edits = [c for c in tg if c["method"] == "editMessageReplyMarkup"]
    assert "Пропущено" in edits[-1]["reply_markup"]["inline_keyboard"][0][0]["text"]


def test_skip_is_kept_for_statistics(scene, tg):
    """`skipped` — dispetcherning ongli qarori, statistikaning asosiy manbai."""
    bot.handle_callback(callback(f"skip:{scene['best']['id']}"))
    assert db.get_match(scene["best"]["id"])["decided_at"] is not None


def test_skip_twice_is_harmless(scene, tg):
    bot.handle_callback(callback(f"skip:{scene['best']['id']}"))
    bot.handle_callback(callback(f"skip:{scene['best']['id']}"))
    assert db.get_match(scene["best"]["id"])["decision"] == "skipped"


# ---------------------------------------------------------------- 📋 va 📞

def test_info_sends_original_text(scene, tg):
    bot.handle_callback(callback(f"info:{scene['cargo_id']}"))
    text = tg.texts()[0]
    assert "Есть груз Ташкент" in text          # asl e'lon matni
    assert "расходы" in text                     # to'liq hisob


def test_call_sends_phone_separately(scene, tg, monkeypatch):
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)
    ids = pipeline.handle_message(
        "Есть груз Бухара → Казань, 20т тент, 4000$, 22.09, тел +998901234567")
    bot.handle_callback(callback(f"call:{ids[0]}"))
    assert "+998901234567" in tg.texts()[0]


def test_unknown_cargo_does_not_crash(scene, tg):
    bot.handle_callback(callback("info:99999"))
    assert "не найден" in " ".join(tg.answers()).lower()


def test_broken_callback_data(scene, tg):
    bot.handle_callback(callback("take:abc"))
    assert tg.answers()


# ---------------------------------------------------------------- ruxsat

def test_foreign_chat_is_ignored(scene, tg, monkeypatch):
    """Begona odam mashinani band qila olmaydi."""
    monkeypatch.setattr(config, "DISPATCHER_CHAT_ID", "777")
    bot.handle_callback(callback(f"take:{scene['best']['id']}", chat_id=999))

    assert db.get_match(scene["best"]["id"])["decision"] is None
    assert "доступа" in " ".join(tg.answers()).lower()


# ---------------------------------------------------------------- buyruqlar

def message(text: str, chat_id=777) -> dict:
    return {"message_id": 1, "chat": {"id": chat_id}, "text": text}


def test_help_command(scene, tg):
    bot.handle_message(message("/help"))
    assert "BAXT TRANSPORT" in tg.texts()[0]


def test_list_command(scene, tg):
    bot.handle_message(message("/list"))
    assert "Toshkent → Moskva" in tg.texts()[0]


def test_trucks_command(scene, tg):
    bot.handle_message(message("/trucks"))
    assert "№01" in tg.texts()[0]


def test_pos_command_updates_position(scene, tg):
    bot.handle_message(message("/pos 01 Казань 25.09"))
    truck = db.get_truck("01")
    assert truck["current_city"] == "Qozon"          # kanonik nom
    assert truck["free_date"] == "2026-09-25"
    assert truck["pos_source"] == "manual"


def test_pos_command_with_two_word_city(scene, tg):
    bot.handle_message(message("/pos 01 Нижний Новгород"))
    assert db.get_truck("01")["current_city"] == "Nijniy Novgorod"


def test_pos_unknown_truck(scene, tg):
    bot.handle_message(message("/pos 99 Казань"))
    assert "не найдена" in tg.texts()[0]


def test_pos_unknown_city(scene, tg):
    bot.handle_message(message("/pos 01 Кукуево"))
    assert "Не понял город" in tg.texts()[0]
    assert db.get_truck("01")["current_city"] == "Toshkent"   # o'zgarmadi


def test_command_with_bot_name(scene, tg):
    """Guruhda buyruq /list@baxt_bot ko'rinishida keladi."""
    bot.handle_message(message("/list@baxt_bot"))
    assert tg.texts()


def test_done_command_saves_actual_margin(scene, tg):
    match_id = scene["best"]["id"]
    bot.handle_message(message(f"/done {match_id} 1850"))
    assert db.get_match(match_id)["actual_margin_usd"] == 1850.0
    assert "факт" in tg.texts()[0]


def test_expire_command(scene, tg, clean_db):
    with clean_db.connect() as conn:
        conn.execute("UPDATE cargos SET created_at='2020-01-01 00:00:00'")
    bot.handle_message(message("/expire"))
    assert db.get_cargo(scene["cargo_id"])["status"] == "expired"


def test_unknown_command(scene, tg):
    bot.handle_message(message("/foo"))
    assert "Не знаю команду" in tg.texts()[0]


def test_plain_text_is_ignored(scene, tg):
    bot.handle_message(message("просто сообщение"))
    assert tg.texts() == []


def test_details_json_survives_round_trip(scene, tg):
    """Kartochkadagi hisob JSON'dan qayta o'qilishi kerak."""
    details = json.loads(scene["best"]["details"])
    assert details["truck_id"] == scene["best"]["truck_id"]


# ---------------------------------------------------------------- haydovchi GPS

def driver_message(text: str = "", user_id=555, chat_id=555, **extra) -> dict:
    msg = {"message_id": 1, "chat": {"id": chat_id}, "from": {"id": user_id}}
    if text:
        msg["text"] = text
    msg.update(extra)
    return msg


def test_link_requires_plate(scene, tg):
    bot.handle_message(driver_message("/link 01"))
    assert "госномер" in tg.texts()[0]
    assert db.get_truck("01")["tg_user_id"] is None


def test_link_with_wrong_plate_is_refused(scene, tg):
    """Begona odam mashinani o'ziga bog'lay olmaydi."""
    bot.handle_message(driver_message("/link 01 XX999XX"))
    assert "не совпадают" in tg.texts()[0]
    assert db.get_truck("01")["tg_user_id"] is None


def test_link_with_correct_plate(scene, tg):
    bot.handle_message(driver_message("/link 01 01A111AA"))     # probelsiz ham
    assert db.get_truck("01")["tg_user_id"] == 555
    assert "привязана" in tg.texts()[0]


def test_driver_location_is_saved(scene, tg):
    bot.handle_message(driver_message("/link 01 01A111AA"))
    bot.handle_message(driver_message(
        location={"latitude": 55.7887, "longitude": 49.1221}))

    positions = db.latest_gps_positions()
    assert positions["01"]["lat"] == pytest.approx(55.7887)
    assert "Qozon" in tg.texts()[-1]


def test_location_without_link_is_rejected(scene, tg):
    bot.handle_message(driver_message(
        location={"latitude": 55.7887, "longitude": 49.1221}))
    assert db.latest_gps_positions() == {}
    assert "привяжите" in tg.texts()[0]


def test_live_location_update_is_quiet(scene, tg):
    """Live Location har necha daqiqada yangilanadi — har safar javob
    yozilsa dispetcher chatini spam bosadi."""
    bot.handle_message(driver_message("/link 01 01A111AA"))
    before = len(tg.texts())

    bot.handle_update({"edited_message": driver_message(
        location={"latitude": 55.7887, "longitude": 49.1221})})

    assert db.latest_gps_positions()["01"]["lat"] == pytest.approx(55.7887)
    assert len(tg.texts()) == before        # yangi xabar yo'q


def test_gps_command_syncs(scene, tg):
    bot.handle_message(driver_message("/link 01 01A111AA"))
    bot.handle_message(driver_message(
        location={"latitude": 55.7887, "longitude": 49.1221}))
    bot.handle_message(message("/gps"))

    assert db.get_truck("01")["current_city"] == "Qozon"
    assert "Машина №01" in tg.texts()[-1]


# ---------------------------------------------------------------- qidiruv (buyurtmachi talabi)

def test_plain_text_is_a_search(scene, tg):
    """Dispetcher buyruq yozmaydi — "Тошкент Москва" deb yozadi."""
    bot.handle_message(message("Тошкент Москва"))
    text = tg.texts()[0]
    assert "Toshkent → Moskva" in text
    assert "маржа" in text and "4 000" in text          # narx ko'rinadi
    assert "/день" not in text                           # buyurtmachi: kunlik marja kerak emas


def test_search_command(scene, tg):
    bot.handle_message(message("/find Ташкент Москва"))
    assert "Toshkent → Moskva" in tg.texts()[0]


def test_search_answers_with_our_truck(scene, tg):
    bot.handle_message(message("менга ташкент москва юк топиб бер"))
    assert "🚛 №" in tg.texts()[0]


def test_search_with_body_type(scene, tg, monkeypatch):
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)
    pipeline.handle_message("Есть груз Ташкент → Москва, 20т реф +2, 4300$, 23.09")
    bot.handle_message(message("Ташкент Москва реф"))
    assert "Реф" in tg.texts()[0]


def test_fleet_question(scene, tg):
    """"Бизда нечта мошина бор?" — parkni ko'rsatadi."""
    bot.handle_message(message("бизда нечта мошина бор"))
    assert "Наш парк" in tg.texts()[0]
    assert "№01" in tg.texts()[0]


def test_chat_talk_is_ignored(scene, tg):
    """Dispetcherlar chatida oddiy gaplar ham bo'ladi — bot aralashmaydi."""
    bot.handle_message(message("салом жигар, эртага кораман"))
    assert tg.texts() == []


def test_nothing_found_offers_to_watch(scene, tg):
    bot.handle_message(message("Бухара Казань"))
    text = tg.texts()[0]
    assert "ничего нет" in text.lower()
    keyboard = [c for c in tg if c["method"] == "sendMessage"][-1].get("reply_markup")
    assert keyboard["inline_keyboard"][0][0]["callback_data"] == "watch:Buxoro|Qozon|"


def test_watch_button_creates_watch(scene, tg):
    bot.handle_callback(callback("watch:Buxoro|Qozon|"))
    watches = db.active_watches()
    assert len(watches) == 1
    assert (watches[0]["from_city"], watches[0]["to_city"]) == ("Buxoro", "Qozon")
    assert "Сообщу" in tg.texts()[-1]


def test_watch_is_not_duplicated(scene, tg):
    bot.handle_callback(callback("watch:Buxoro|Qozon|"))
    bot.handle_callback(callback("watch:Buxoro|Qozon|"))
    assert len(db.active_watches()) == 1


def test_watch_command_and_list(scene, tg):
    bot.handle_message(message("/watch Бухара Казань реф"))
    bot.handle_message(message("/watches"))
    assert "Buxoro → Qozon" in tg.texts()[-1]


def test_unwatch(scene, tg):
    bot.handle_message(message("/watch Бухара Казань"))
    watch_id = db.active_watches()[0]["id"]
    bot.handle_message(message(f"/unwatch_{watch_id}"))
    assert db.active_watches() == []


def test_watched_cargo_arrives(scene, tg, monkeypatch):
    """So'ralgan yuk chiqqanda dispetcher darhol xabar oladi."""
    sent = []
    monkeypatch.setattr("notifier.send", lambda text, *a, **kw: sent.append(text))
    monkeypatch.setattr(pipeline, "NOTIFY_THRESHOLD", 99.9)
    bot.handle_message(message("/watch Бухара Казань"))

    pipeline.handle_message("Есть груз Бухара → Казань, 20т тент, 4100$, 23.09")
    assert any("По вашему запросу" in t for t in sent)


# ---------------------------------------------------------------- qulaylik (menyu, /start, panel)

def test_start_registers_dispatcher_chat(clean_db, tg, monkeypatch):
    """Birinchi /start — chatni bildirishnoma ro'yxatiga qo'shadi
    (chat id ni qo'lda qidirish shart emas)."""
    monkeypatch.setattr(config, "DISPATCHER_CHAT_ID", "")
    bot.handle_message({"message_id": 1, "chat": {"id": 555}, "text": "/start"})
    assert "555" in db.dispatcher_chats()
    assert any("BAXT TRANSPORT" in t for t in tg.texts())


def test_start_shows_main_keyboard(clean_db, tg, monkeypatch):
    monkeypatch.setattr(config, "DISPATCHER_CHAT_ID", "")
    bot.handle_message({"message_id": 1, "chat": {"id": 555}, "text": "/start"})
    kb = [c for c in tg if c["method"] == "sendMessage" and c.get("reply_markup")]
    assert kb, "doimiy tugmalar paneli yo'q"
    keys = str(kb[0]["reply_markup"])
    assert "Qidiruv" in keys and "Park" in keys


def test_menu_buttons_route(scene, tg):
    """Pastdagi tugma matni to'g'ri buyruqqa yo'naltiriladi."""
    bot.handle_message(message(bot.KB_FLEET))
    assert "Наш парк" in tg.texts()[-1]
    bot.handle_message(message(bot.KB_LIST))
    assert any("предложени" in t.lower() or "Toshkent" in t for t in tg.texts())


def test_search_button_prompts(scene, tg):
    bot.handle_message(message(bot.KB_SEARCH))
    assert "направление" in tg.texts()[-1].lower()


def test_panel_link_is_signed(scene, tg, monkeypatch):
    monkeypatch.setenv("PANEL_URL", "https://trans.bizdaoson.uz")
    monkeypatch.setenv("WEB_PASSWORD", "p")
    bot.handle_message(message("/panel"))
    btns = [c for c in tg if c["method"] == "sendMessage" and c.get("reply_markup")]
    row = btns[-1]["reply_markup"]["inline_keyboard"][0]
    url = next(b["url"] for b in row if "url" in b)
    assert url.startswith("https://trans.bizdaoson.uz/enter?t=")
    # shaxsiy chatda — Telegram ichida ochiladigan Web App tugmasi ham bor
    assert any("web_app" in b for b in row)


def test_panel_web_app_only_in_private_chat(scene, monkeypatch):
    """Guruhda Web App tugmasi xato beradi — faqat havola."""
    monkeypatch.setenv("PANEL_URL", "https://x.uz")
    monkeypatch.setenv("WEB_PASSWORD", "p")
    row = bot.panel_button(-100123)["inline_keyboard"][0]
    assert all("web_app" not in b for b in row)


def test_panel_link_absent_without_url(scene, tg, monkeypatch):
    monkeypatch.delenv("PANEL_URL", raising=False)
    assert bot.panel_link() is None


def test_dispatcher_chat_restricts_access(clean_db, tg, monkeypatch):
    """Ro'yxat to'lgach, begona chatga javob berilmaydi."""
    monkeypatch.setattr(config, "DISPATCHER_CHAT_ID", "")
    clean_db.add_dispatcher_chat(777)
    assert bot.allowed(777) is True
    assert bot.allowed(999) is False


def test_notifier_broadcasts_to_registered_chats(clean_db, monkeypatch):
    """Bildirishnoma ro'yxatdagi barcha chatlarga boradi."""
    monkeypatch.setattr(config, "DISPATCHER_CHAT_ID", "")
    monkeypatch.setattr(config, "BOT_TOKEN", "t")
    clean_db.add_dispatcher_chat(111)
    clean_db.add_dispatcher_chat(222)
    sent = []
    monkeypatch.setattr(notifier, "_send_to",
                        lambda chat, text, kb, token: sent.append(chat) or {"ok": True})
    notifier.send("тест")
    assert sent == ["111", "222"]
