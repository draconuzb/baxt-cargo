"""
test_ai.py — AI yurak: asboblar (ai_tools), suhbat tsikli (brain), bot bilan ulanish.

Model soxta: `brain.chat_completion` o'rniga skript qo'yiladi — tarmoqsiz,
tekin va har safar bir xil. Tekshiriladigan narsa — model aytgan asbob
to'g'ri ishlashi, raqamlar asbobdan kelishi va tugmalar to'g'ri chiqishi.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

import ai_tools
import bot
import brain
import config
import db
import pipeline
import rules


@pytest.fixture
def fleet(clean_db, truck_tent, truck_ref, monkeypatch):
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)
    monkeypatch.setattr(config, "BOT_TOKEN", "test-token")
    for t in (truck_tent, truck_ref):
        clean_db.upsert_truck(t)
    rules.reset_cache()
    ids = []
    for text in ("Груз Ташкент → Москва, 20т тент, 4000$, 22.09",
                 "Груз Ташкент → Казань, 20т тент, 3500$, 22.09",
                 # sana nisbiy: qotirilgan "28.09" vaqt o'tib o'tgan sanaga aylangan
                 "Груз Москва → Ташкент, 18т тент, 3800$, "
                 + (date.today() + timedelta(days=5)).strftime("%d.%m"),
                 "Груз Ташкент → Алматы, 15т тент, 900$, 22.09"):
        ids += pipeline.handle_message(text, source="grp")
    yield {"ids": ids}
    rules.reset_cache()


# ---------------------------------------------------------------- asboblar

def test_find_cargo_ranked_by_margin_per_day(fleet):
    ctx = ai_tools.Ctx()
    out = ai_tools.find_cargo(ctx, from_city="Ташкент")
    per_day = [r.get("margin_per_day") for r in out["results"] if r.get("margin_per_day")]
    assert per_day == sorted(per_day, reverse=True)
    assert out["results"][0]["offer"]              # tugma uchun moslik yozuvi
    assert ctx.offers


def test_find_cargo_unknown_city_is_error(fleet):
    out = ai_tools.find_cargo(ai_tools.Ctx(), from_city="Qwertyuiop")
    assert "error" in out


def test_find_cargo_near_city(fleet):
    """"01 Moskvada — atrofidan yuk top": Moskvadan yuklanadiganlar."""
    out = ai_tools.find_cargo(ai_tools.Ctx(), truck_id="1", near_city="Москва")
    routes = [r["route"] for r in out["results"]]
    assert routes and all(r.startswith("Moskva") for r in routes)
    # mashina hali Toshkentda — virtual holat uchun tugma berilmaydi
    assert all("offer" not in r for r in out["results"])


def test_find_cargo_respects_rules(fleet):
    ai_tools.add_rule(ai_tools.Ctx(), effect="block", scope={"to_country": "KZ"})
    out = ai_tools.find_cargo(ai_tools.Ctx(), from_city="Toshkent")
    assert all("Almaty" not in r["route"] for r in out["results"])
    assert out["blocked_by_rules"] >= 1


def test_add_rule_converts_mln_uzs(fleet):
    ctx = ai_tools.Ctx()
    out = ai_tools.add_rule(ctx, effect="block", scope={"from_country": "Россия"},
                            min_rate=30_000_000, currency="UZS", text="30 mln dan arzon olma")
    assert out["ok"]
    rule = rules.get(out["rule_id"])
    assert rule["require"]["min_rate_usd"] == round(config.to_usd(30_000_000, "UZS"))
    assert ctx.new_rules == [out["rule_id"]]


def test_add_rule_bad_input_returns_error(fleet):
    out = ai_tools.add_rule(ai_tools.Ctx(), effect="block", scope={})
    assert "error" in out
    assert rules.list_rules() == []


def test_plan_truck_includes_return(fleet):
    out = ai_tools.plan_truck(ai_tools.Ctx(), truck_id="01")
    assert out["plans"]
    first = out["plans"][0]
    assert first["offer"] and "round_margin_per_day" in first
    moscow = [p for p in out["plans"] if p["route"] == "Toshkent -> Moskva"]
    if moscow:
        assert isinstance(moscow[0]["return"], dict)
        assert moscow[0]["return"]["route"] == "Moskva -> Toshkent"


def test_plan_fleet_no_cargo_twice(fleet):
    out = ai_tools.plan_fleet(ai_tools.Ctx())["plan"]
    used = [p["cargo_id"] for p in out if "cargo_id" in p]
    assert len(used) == len(set(used))


def test_set_position_and_memory(fleet):
    ctx = ai_tools.Ctx()
    later = (date.today() + timedelta(days=2)).isoformat()
    out = ai_tools.set_truck_position(ctx, truck_id="01", city="Москва", free_date=later)
    assert out["city"] == "Moskva" and out["free_date"] == later
    assert db.get_truck("01")["pos_source"] == "manual"
    mem = ai_tools.remember(ctx, note="01 haydovchisi Qozog'istonga bormaydi")
    assert "Qozog'iston" in brain._context()
    assert ai_tools.forget(ctx, memory_id=mem["memory_id"])["ok"]


def test_past_free_date_means_free_now(fleet):
    """Model o'ylab topgan eski sana mashinani o'tmishda bo'sh qilmasin."""
    out = ai_tools.set_truck_position(ai_tools.Ctx(), truck_id="02", free_date="2020-01-01")
    assert out["free_date"] == date.today().isoformat()


def test_same_city_is_not_rewritten(fleet):
    """Shahar o'zgarmagan bo'lsa — GPS/reys manbasi "qo'lda" ga almashmaydi."""
    db.set_truck_position("01", city="Toshkent", source="gps")
    out = ai_tools.set_truck_position(ai_tools.Ctx(), truck_id="01", city="Toshkent")
    assert out["note"] == "unchanged"
    assert db.get_truck("01")["pos_source"] == "gps"


def test_call_is_safe(fleet):
    assert "error" in ai_tools.call(ai_tools.Ctx(), "rm_rf", {})
    assert "error" in ai_tools.call(ai_tools.Ctx(), "find_cargo", "bad json")
    assert "error" in ai_tools.call(ai_tools.Ctx(), "find_cargo", {"top": "abc"})


def test_no_take_tool():
    """Yukni faqat odam oladi (tugma) — modelda bunday asbob yo'q."""
    names = {s["function"]["name"] for s in ai_tools.SCHEMAS}
    assert not any("take" in n for n in names)
    assert names == set(ai_tools.REGISTRY)


# ---------------------------------------------------------------- brain

class Script:
    """Soxta model: navbat bilan oldindan yozilgan javoblarni qaytaradi."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.seen: list[list[dict]] = []

    def __call__(self, p, messages):
        self.seen.append([dict(m) for m in messages])
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def tool_call(name: str, args: dict, call_id: str = "call00001") -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture
def ai_on(monkeypatch):
    monkeypatch.setenv("AI_PROVIDERS", "mistral,groq")
    monkeypatch.setenv("MISTRAL_API_KEY", "m-key")
    monkeypatch.setenv("GROQ_API_KEY", "g-key")
    brain.reset_state()


def test_disabled_without_keys(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert not brain.enabled()
    assert brain.reply(1, "salom") is None


def test_reply_uses_tools_and_offers_buttons(fleet, ai_on, monkeypatch):
    cid = fleet["ids"][0]
    script = Script(tool_call("find_cargo", {"from_city": "Ташкент", "to_city": "Москва"}),
                    {"role": "assistant",
                     "content": f"**Toshkent → Moskva** #{cid}: eng foydalisi."})
    monkeypatch.setattr(brain, "chat_completion", script)
    res = brain.reply(777, "Toshkent Moskva yuk bormi?")
    assert res.provider == "mistral"
    assert res.tools_used == ["find_cargo"]
    assert "<b>Toshkent → Moskva</b>" in res.text          # markdown → HTML
    buttons = res.keyboard["inline_keyboard"]
    assert buttons[0][0]["callback_data"].startswith("take:")
    assert f"#{cid}" in buttons[0][0]["text"]
    # model asbob natijasini ko'rdi
    tool_msgs = [m for m in script.seen[1] if m["role"] == "tool"]
    assert tool_msgs and "results" in tool_msgs[0]["content"]


def test_system_prompt_has_fleet_and_rules(fleet, ai_on, monkeypatch):
    rules.add("block", {"to_country": "KZ"}, text="Qozog'istonga olmaymiz")
    script = Script({"role": "assistant", "content": "2 ta mashina bor."})
    monkeypatch.setattr(brain, "chat_completion", script)
    brain.reply(777, "nechta mashinamiz bor?")
    system = script.seen[0][0]["content"]
    assert "#01" in system and "#02" in system
    assert "Казахстан" in system


@pytest.mark.parametrize("text,expect", [
    ("01 учун энг фойдали юк қайси?", "UZBEK CYRILLIC"),
    ("Какой груз выгоднее для 01?", "RUSSIAN"),
    ("01 uchun eng foydali yuk qaysi?", "UZBEK LATIN"),
])
def test_script_hint(text, expect):
    hint = brain._script_hint(text)
    assert expect in hint
    assert "ANSWER IN RUSSIAN" in hint           # buyurtmachi: AI doim ruscha


def test_system_prompt_demands_russian():
    assert "ALWAYS reply in RUSSIAN" in brain.SYSTEM


def test_tool_output_uses_russian_city_names():
    """AI ruscha yozadi — asbob natijasida ham shahar nomi ruscha bo'lsin."""
    out = ai_tools.dumps({"route": "Toshkent -> Moskva", "at": "Farg'ona"})
    assert "Ташкент -> Москва" in out and "Фергана" in out and "Toshkent" not in out


def test_script_hint_is_sent(fleet, ai_on, monkeypatch):
    script = Script({"role": "assistant", "content": "ок"})
    monkeypatch.setattr(brain, "chat_completion", script)
    brain.reply(777, "01 учун юк топ")
    assert "UZBEK CYRILLIC" in script.seen[0][-2]["content"]


def test_empty_rules_are_explicit(fleet):
    """Qoida yo'qligi aniq yoziladi — model uni to'qib chiqarmasin."""
    ctx = brain._context()
    assert "Company rules" in ctx and "Remembered facts" in ctx
    assert ctx.count("- none") == 2


def test_history_is_kept_short(fleet, ai_on, monkeypatch):
    monkeypatch.setattr(brain, "chat_completion",
                        Script({"role": "assistant", "content": "birinchi"},
                               {"role": "assistant", "content": "ikkinchi"}))
    brain.reply(777, "savol 1")
    script = Script({"role": "assistant", "content": "uchinchi"})
    monkeypatch.setattr(brain, "chat_completion", script)
    brain.reply(777, "savol 2")
    roles = [m["role"] for m in script.seen[0]]
    assert roles == ["system", "user", "assistant", "system", "user"]   # + alifbo ko'rsatmasi
    db.clear_ai_history(777)
    assert db.ai_history(777) == []


def test_falls_back_to_next_provider(fleet, ai_on, monkeypatch):
    calls = []

    def fake(p, messages):
        calls.append(p["name"])
        if p["name"] == "mistral":
            raise brain.ProviderError("429 rate limit")
        return {"role": "assistant", "content": "groq javobi"}

    monkeypatch.setattr(brain, "chat_completion", fake)
    res = brain.reply(777, "salom")
    assert calls == ["mistral", "groq"]
    assert res.provider == "groq"


def test_all_providers_down_returns_none(fleet, ai_on, monkeypatch):
    monkeypatch.setattr(brain, "chat_completion",
                        Script(brain.ProviderError("x"), brain.ProviderError("y")))
    assert brain.reply(777, "salom") is None


def test_rule_button_after_add_rule(fleet, ai_on, monkeypatch):
    monkeypatch.setattr(brain, "chat_completion", Script(
        tool_call("add_rule", {"effect": "block", "scope": {"from_country": "RU"},
                               "min_rate": 30000000, "currency": "UZS"}),
        {"role": "assistant", "content": "Tushundim: Rossiyadan 30 mln dan arzon olmaymiz."}))
    res = brain.reply(777, "rossiyadan 30 mln dan arzon yuk olmagin")
    assert rules.list_rules("active")
    cb = [b["callback_data"] for row in res.keyboard["inline_keyboard"] for b in row]
    assert any(c.startswith("rule_del:") for c in cb)


def test_step_limit_forces_answer(fleet, ai_on, monkeypatch):
    loop = [tool_call("fleet", {}, f"call{i:05d}") for i in range(brain.MAX_STEPS)]
    monkeypatch.setattr(brain, "chat_completion",
                        Script(*loop, {"role": "assistant", "content": "tayyor"}))
    assert brain.reply(777, "park").text == "tayyor"


def test_daily_budget(monkeypatch, ai_on):
    monkeypatch.setenv("AI_MAX_CALLS_PER_DAY", "0")
    with pytest.raises(brain.ProviderError):
        brain._post(brain._providers()[0], {})


@pytest.mark.parametrize("raw,expected", [
    ("a < b & c", "a &lt; b &amp; c"),
    ("<b>ok</b> <script>x</script>", "<b>ok</b> x"),
    ("## Sarlavha\n- band", "<b>Sarlavha</b>\n• band"),
    ('✅ <a href="tg://x?t=1">Беру</a>', ""),                 # soxta tugma-qator o'chadi
    ("Diqqat: #4 yuk", "Diqqat: #4 yuk"),
    ("Xabar beraymi? <button>🔔 Ha</button>", "Xabar beraymi? 🔔 Ha"),
    ("#4 Toshkent → Almaty\n✅ <b>✅ Беру</b> (bosib yukni olishingiz mumkin)",
     "#4 Toshkent → Almaty"),
    ("55 mln UZS (✅ Беру).", "55 mln UZS."),
])
def test_telegram_html(raw, expected):
    assert brain.to_telegram_html(raw) == expected


# ---------------------------------------------------------------- bot

@pytest.fixture
def tg(monkeypatch):
    calls = []

    def fake_api(method, http_timeout=15, **params):
        calls.append({"method": method, **params})
        return {"ok": True, "result": {}}

    monkeypatch.setattr(bot, "api", fake_api)
    return calls


def msg(text, chat_id=777, chat_type="private"):
    return {"message_id": 1, "chat": {"id": chat_id, "type": chat_type}, "text": text}


def test_bot_private_text_goes_to_ai(fleet, ai_on, tg, monkeypatch):
    monkeypatch.setattr(brain, "chat_completion",
                        Script({"role": "assistant", "content": "Salom, rahbar!"}))
    bot.handle_message(msg("salom, bugun nima qilamiz?"))
    sent = [c for c in tg if c["method"] == "sendMessage"]
    assert sent[-1]["text"] == "Salom, rahbar!"
    assert any(c["method"] == "sendChatAction" for c in tg)


def test_bot_group_chat_does_not_call_ai(fleet, ai_on, tg, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("guruhda AI chaqirilmasin")
    monkeypatch.setattr(brain, "chat_completion", boom)
    bot.handle_message(msg("kim choy ichadi?", chat_id=-100, chat_type="group"))
    assert not [c for c in tg if c["method"] == "sendMessage"]


def test_bot_falls_back_to_search_when_ai_fails(fleet, ai_on, tg, monkeypatch):
    monkeypatch.setattr(brain, "chat_completion",
                        Script(brain.ProviderError("a"), brain.ProviderError("b")))
    bot.handle_message(msg("Ташкент Москва"))
    assert "Toshkent" in [c for c in tg if c["method"] == "sendMessage"][-1]["text"]


def test_bot_plan_button_without_ai(fleet, tg, monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    bot.handle_message(msg(bot.KB_PLAN))
    sent = [c for c in tg if c["method"] == "sendMessage"][-1]
    assert "План" in sent["text"]
    assert sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("take:")


def test_take_in_multi_offer_message_keeps_other_buttons(fleet, tg):
    ctx = ai_tools.Ctx()
    ai_tools.plan_fleet(ctx)
    kb = bot._offer_keyboard(ctx)
    assert len(kb["inline_keyboard"]) >= 2
    first = kb["inline_keyboard"][0][0]["callback_data"]
    bot.handle_callback({"id": "cb", "data": first, "message": {
        "message_id": 9, "chat": {"id": 777}, "reply_markup": kb}})
    edit = [c for c in tg if c["method"] == "editMessageReplyMarkup"][-1]
    rows = edit["reply_markup"]["inline_keyboard"]
    assert len(rows) == len(kb["inline_keyboard"])
    assert rows[0][0]["callback_data"] == "noop"
    assert rows[1] == kb["inline_keyboard"][1]


def test_rule_buttons(fleet, tg):
    rid, _ = rules.add("penalty", {"to_country": "KZ"}, points=15,
                       source="learned", status="proposed")
    bot.handle_callback({"id": "cb", "data": f"rule_ok:{rid}",
                         "message": {"message_id": 3, "chat": {"id": 777}}})
    assert rules.get(rid)["status"] == "active"
    bot.handle_callback({"id": "cb", "data": f"rule_del:{rid}",
                         "message": {"message_id": 3, "chat": {"id": 777}}})
    assert rules.get(rid) is None


def test_rules_command_lists(fleet, tg):
    rules.add("block", {"from_country": "RU"}, {"min_rate_usd": 2300})
    bot.handle_message(msg("/rules"))
    text = [c for c in tg if c["method"] == "sendMessage"][-1]["text"]
    assert "Россия" in text and "/unrule_1" in text
    bot.handle_message(msg("/unrule_1"))
    assert rules.list_rules() == []


def test_several_models_per_provider(monkeypatch):
    """Bepul limit har model uchun alohida — biri to'lsa keyingisi."""
    monkeypatch.setenv("AI_PROVIDERS", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("AI_MODEL_GROQ", "openai/gpt-oss-120b, openai/gpt-oss-20b")
    assert [p["model"] for p in brain._providers()] == ["openai/gpt-oss-120b",
                                                        "openai/gpt-oss-20b"]


def test_rate_limit_waits_and_retries(monkeypatch):
    calls, slept = [], []

    def fake_post(p, payload):
        calls.append(payload)
        if len(calls) == 1:
            raise brain.ProviderError("429", retry_after=2.0)
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    monkeypatch.setattr(brain, "_post", fake_post)
    monkeypatch.setattr(brain.time, "sleep", slept.append)
    msg = brain.chat_completion({"name": "groq", "model": "openai/gpt-oss-120b"}, [])
    assert msg["content"] == "ok" and len(calls) == 2 and slept == [2.5]
    assert calls[0]["reasoning_effort"] == "low"


def test_long_rate_limit_not_waited(monkeypatch):
    def fake_post(p, payload):
        raise brain.ProviderError("429", retry_after=60)
    monkeypatch.setattr(brain, "_post", fake_post)
    with pytest.raises(brain.ProviderError):
        brain.chat_completion({"name": "groq", "model": "m"}, [])


def test_retry_after_parsed_from_groq_body():
    class Ex:
        code = 429
        headers = {}
    body = '{"error":{"message":"Rate limit reached ... Please try again in 9.5925s."}}'
    assert brain._retry_after(Ex(), body) == pytest.approx(9.5925)


def test_button_for_cargo_mentioned_without_tool(fleet, ai_on, monkeypatch):
    """"1182 ga tugma ber" — AI asbobsiz javob bersa ham "✅ Беру" chiqadi."""
    cid = fleet["ids"][0]
    monkeypatch.setattr(brain, "chat_completion", Script(
        {"role": "assistant", "content": f"#{cid} — Toshkent → Moskva, №01 uchun eng yaxshisi"}))
    res = brain.reply(777, f"{cid} yubor tugmani")
    assert res.tools_used == []
    btn = res.keyboard["inline_keyboard"][0][0]
    assert btn["callback_data"].startswith("take:") and f"#{cid}" in btn["text"]
    assert "№01" in btn["text"]                         # aytilgan fura


def test_no_button_for_taken_or_unknown_cargo(fleet, ai_on, monkeypatch):
    monkeypatch.setattr(brain, "chat_completion",
                        Script({"role": "assistant", "content": "#999999 bunday yuk yo'q"}))
    assert brain.reply(777, "999999").keyboard is None
