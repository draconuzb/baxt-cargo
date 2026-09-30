"""
test_ai_plus.py — AI kuchaytirishlari: narx maslahati, aqlli izoh, ovozli xabar.

Tashqi xizmatlar (Groq Whisper, Telegram fayllari) soxta — tarmoqsiz.
"""
from __future__ import annotations

import pytest

import ai_tools
import bot
import brain
import config
import insight
import notifier
import pipeline
import scoring


@pytest.fixture
def fleet(clean_db, truck_tent, truck_ref, monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", "test-token")
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)
    for t in (truck_tent, truck_ref):
        clean_db.upsert_truck(t)
    ids = []
    for text in ("Груз Ташкент → Москва, 20т тент, 4000$, 22.09",
                 "Груз Ташкент → Москва, 20т тент, 3600$, 23.09",
                 "Груз Москва → Ташкент, 18т тент, 3800$, 28.09"):
        ids += pipeline.handle_message(text, source="g")
    return {"ids": ids}


@pytest.fixture
def tg(monkeypatch):
    calls = []

    def fake_api(method, http_timeout=15, **params):
        calls.append({"method": method, **params})
        if method == "getFile":
            return {"ok": True, "result": {"file_path": "voice/1.oga", "file_size": 1000}}
        return {"ok": True, "result": {}}

    monkeypatch.setattr(bot, "api", fake_api)
    return calls


def sent(calls) -> list[str]:
    return [c.get("text", "") for c in calls if c["method"] == "sendMessage"]


# ---------------------------------------------------------------- narx

def test_price_advice_for_cargo(fleet):
    adv = ai_tools.price_advice(ai_tools.Ctx(), cargo_id=fleet["ids"][0])
    assert adv["break_even_usd"] < adv["floor_usd"] <= adv["ask_usd"]
    assert adv["target_usd"] <= adv["ask_usd"]
    assert adv["ask_usd"] % ai_tools.PRICE_STEP == 0
    assert adv["market"]["ads_30d"] >= 1          # shu yo'nalishdagi boshqa e'lon
    assert adv["offered_usd"] == 4000
    # yuk egasiga matn ruscha — shahar nomlari ham
    assert "Ташкент → Москва" in adv["message"] and f"${adv['ask_usd']}" in adv["message"]


def test_price_ask_not_below_target_even_if_market_low(fleet, costs):
    """Bozor arzon bo'lsa ham tavsiya o'z maqsadimizdan past bo'lmaydi — ogohlantiramiz."""
    adv = ai_tools.price_advice(ai_tools.Ctx(), from_city="Toshkent", to_city="Moskva",
                                body_type="tent", weight_t=20)
    assert adv["ask_usd"] >= adv["target_usd"]
    assert adv["cargo_id"] is None


def test_price_advice_errors(fleet):
    assert "error" in ai_tools.price_advice(ai_tools.Ctx())
    assert "error" in ai_tools.price_advice(ai_tools.Ctx(), cargo_id=99999)
    out = ai_tools.price_advice(ai_tools.Ctx(), from_city="Toshkent", to_city="Moskva",
                                body_type="tral")           # bizda tral yo'q
    assert out["error"] == "no truck can carry this cargo"


def test_price_button(fleet, tg):
    bot.handle_callback({"id": "cb", "data": f"price:{fleet['ids'][0]}",
                         "message": {"message_id": 1, "chat": {"id": 777}}})
    text = sent(tg)[-1]
    assert "Просите" in text and "<code>" in text


def test_card_has_price_button():
    kb = notifier._keyboard({"id": 5}, 9)
    assert {"text": "💰 Цена", "callback_data": "price:5"} in kb["inline_keyboard"][0]


def test_price_tool_registered():
    assert "price_advice" in ai_tools.REGISTRY
    assert any(s["function"]["name"] == "price_advice" for s in ai_tools.SCHEMAS)


# ---------------------------------------------------------------- aqlli izoh

def test_insight_explains_choice(fleet, truck_tent, truck_ref):
    import db
    cargo = dict(db.get_cargo(fleet["ids"][0]))
    results = scoring.best_trucks(cargo, [truck_tent, truck_ref], top=3)
    lines = insight.explain(cargo, results[0], results)
    text = "\n".join(lines)
    assert "Без пустого пробега" in text           # mashina Toshkentda turibdi
    assert "Обратно есть: Moskva → Toshkent" in text


def test_insight_never_breaks_card(fleet):
    # noma'lum fura, bo'sh natija — izoh bo'sh, lekin istisno yo'q
    assert insight.explain({"id": 1, "to_city": "Moskva"}, {"truck_id": "zz"}, None) == []
    assert insight.explain(None, None) == []       # buzuq kirish — bo'sh ro'yxat


def test_card_shows_insight():
    card = notifier.format_card(
        {"from_city": "A", "to_city": "B", "source": "g"},
        {"truck_id": "01", "empty_km": 0, "score": 90, "loaded_km": 100, "total_km": 100,
         "trip_days": 1, "fuel_l": 30, "fuel_cost": 30, "borders": 0, "border_cost": 0,
         "total_cost": 100, "revenue_usd": 500, "margin_usd": 400, "margin_per_day": 400},
        insight=["🏆 №01 — лучший <вариант>"])
    assert "Почему этот груз" in card and "&lt;вариант&gt;" in card


def test_pipeline_passes_insight(clean_db, truck_tent, monkeypatch):
    clean_db.upsert_truck(truck_tent)
    seen = {}

    def fake_notify(cargo, result, match_id=None, reason=None, insight=None):
        seen["insight"] = insight
        return True
    monkeypatch.setattr(notifier, "notify_match", fake_notify)
    pipeline.handle_message("Груз Ташкент → Москва, 20т тент, 4500$, 22.09", source="g")
    assert seen["insight"]


# ---------------------------------------------------------------- ovoz

def test_transcribe_sends_multipart(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    got = {}

    def fake_post(key, body, boundary):
        got["body"] = body
        return {"text": "  01 Moskvada, yuk top  "}
    monkeypatch.setattr(brain, "_post_audio", fake_post)
    assert brain.transcribe(b"OGGDATA") == "01 Moskvada, yuk top"
    assert b"whisper-large-v3-turbo" in got["body"] and b"OGGDATA" in got["body"]
    assert b"Toshkent" in got["body"]              # lug'at-prompt yuborildi


def test_transcribe_without_key_or_audio(monkeypatch):
    assert brain.transcribe(b"x") is None          # kalit yo'q
    monkeypatch.setenv("GROQ_API_KEY", "g")
    assert brain.transcribe(b"") is None


def test_voice_message_goes_to_ai(fleet, tg, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setattr(bot, "download_file", lambda file_id: b"OGG")
    monkeypatch.setattr(brain, "transcribe", lambda audio, name="v": "01 fura uchun yuk top")
    asked = []
    monkeypatch.setattr(bot, "_ai", lambda chat_id, text: asked.append(text) or True)
    bot.handle_message({"message_id": 1, "chat": {"id": 777, "type": "private"},
                        "voice": {"file_id": "F1", "duration": 4}})
    assert "01 fura uchun yuk top" in sent(tg)[-1]    # nima eshitilgani ko'rsatiladi
    assert asked == ["01 fura uchun yuk top"]


def test_voice_too_long_or_unheard(fleet, tg, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    bot.handle_message({"message_id": 1, "chat": {"id": 777, "type": "private"},
                        "voice": {"file_id": "F1", "duration": 600}})
    assert "до 2 минут" in sent(tg)[-1]
    monkeypatch.setattr(bot, "download_file", lambda file_id: b"OGG")
    monkeypatch.setattr(brain, "transcribe", lambda audio, name="v": None)
    bot.handle_message({"message_id": 1, "chat": {"id": 777, "type": "private"},
                        "voice": {"file_id": "F1", "duration": 5}})
    assert "Не расслышал" in sent(tg)[-1]


def test_voice_ignored_in_group(fleet, tg, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    bot.handle_message({"message_id": 1, "chat": {"id": -100, "type": "group"},
                        "voice": {"file_id": "F1", "duration": 5}})
    assert sent(tg) == []


# ---------------------------------------------------------------- sifat sinovi

def test_eval_helpers():
    import ai_eval
    assert ai_eval._script("Toshkent → Moskva, $459/kun") == "latn"
    assert ai_eval._script("Лучший груз: Ташкент → Москва, маржа $459") == "ru"
    assert ai_eval._script("Энг яхши юк: Тошкент → Москва, қайтиш юки ҳам бор") == "uz"
    ok, missing = ai_eval._grounded("Marja $1 251, kuniga $459", '{"m":1251.4,"d":459}')
    assert ok and missing == []
    ok, missing = ai_eval._grounded("Marja $9 999", '{"m":1251}')
    assert not ok and missing == [9999]
    assert ai_eval._BAD.search("chaqirdim find_cargo") and not ai_eval._BAD.search("Olaman")


def test_eval_run_on_copy(fleet, monkeypatch):
    import ai_eval
    import db
    import json as _json
    monkeypatch.setattr("notifier.send", lambda *a, **k: None)
    original = config.DB_PATH
    steps = iter([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call00001",
         "type": "function", "function": {"name": "add_rule", "arguments": _json.dumps(
             {"effect": "block", "scope": {"to_country": "KZ"}})}}]},
        {"role": "assistant", "content": "Понял, грузы в Казахстан не берём."}])
    monkeypatch.setattr(brain, "chat_completion", lambda p, m, tools=True: next(steps))
    cases = [ai_eval.Case("Qozog'istonga yuk olma", {"add_rule"})]
    scores = ai_eval.run([{"name": "fake", "model": "m", "base_url": "", "key": "k"}],
                         cases, pause=0)
    s = scores[0].summary()
    assert s["tool"] == 100 and s["lang"] == 100 and s["clean"] == 100
    # ishchi bazaga qoida yozilmadi — faqat nusxaga
    monkeypatch.setattr(config, "DB_PATH", original)
    import rules
    rules.reset_cache()
    assert rules.list_rules() == []
    assert "fake:m" in ai_eval.report(scores)


# ---------------------------------------------------------------- bitta fura — bitta reys

def test_truck_cannot_take_two_trips(fleet):
    """Fura reys olgach, uning eski takliflari bekor — ikkinchi reysga yozilmaydi."""
    import actions
    import db
    ids = fleet["ids"]
    m1 = db.find_match(ids[0], "01")
    m2 = db.find_match(ids[1], "01")
    assert m1 and m2
    assert actions.take_match(m1["id"]).ok
    res = actions.take_match(m2["id"])
    assert not res.ok and res.reason == "stale"
    assert db.get_cargo(ids[1])["status"] == "new"       # ikkinchi yuk bo'sh qoldi


def test_stale_button_answer(fleet, tg):
    import actions
    import db
    ids = fleet["ids"]
    actions.take_match(db.find_match(ids[0], "01")["id"])
    stale = db.get_match(db.find_match(ids[1], "02")["id"]) if db.find_match(ids[1], "02") \
        else None
    with db.connect() as conn:
        old = conn.execute("SELECT id FROM matches WHERE cargo_id=? AND truck_id='01'",
                           (ids[1],)).fetchone()
    bot.handle_callback({"id": "cb", "data": f"take:{old['id']}",
                         "message": {"message_id": 3, "chat": {"id": 777}}})
    answers = [c.get("text", "") for c in tg if c["method"] == "answerCallbackQuery"]
    assert "занята" in answers[-1]
    assert stale is None or stale["decision"] is None      # boshqa fura taklifi tegilmagan


def test_ai_can_reoffer_cancelled_pair(fleet):
    """Bekor qilingan juftlikni AI yangi hisob bilan qayta taklif qila oladi."""
    import actions
    import db
    ids = fleet["ids"]
    actions.take_match(db.find_match(ids[0], "01")["id"])
    cancelled = db.get_match(
        db.connect().execute("SELECT id FROM matches WHERE cargo_id=? AND truck_id='01'",
                             (ids[1],)).fetchone()["id"])
    assert cancelled["decision"] == "cancelled"
    revived = ai_tools._ensure_match(dict(db.get_cargo(ids[1])), "01",
                                     {"score": 50, "empty_km": 10, "loaded_km": 100,
                                      "margin_usd": 1, "truck_id": "01"})
    assert revived == cancelled["id"]
    assert db.get_match(revived)["decision"] is None


def test_whisper_silence_is_ignored(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setattr(brain, "_post_audio", lambda k, b, bd: {"text": " Thank you. "})
    assert brain.transcribe(b"x") is None
