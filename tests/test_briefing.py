"""
test_briefing.py — ertalabki reja (briefing.py).

Asosiy tekshiruvlar: kuniga faqat bir marta, belgilangan Toshkent soatida,
o'chirib qo'yish mumkin, AI ishlamasa ham reja keladi.
"""
from __future__ import annotations

from datetime import datetime

import pytest

import brain
import briefing
import bot
import config
import db
import pipeline
import settings


@pytest.fixture
def scene(clean_db, truck_tent, truck_ref, monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", "test-token")
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)
    for t in (truck_tent, truck_ref):
        clean_db.upsert_truck(t)
    pipeline.handle_message("Груз Ташкент → Москва, 20т тент, 4000$, 22.09", source="g")
    pipeline.handle_message("Груз Москва → Ташкент, 18т тент, 3800$, 28.09", source="g")
    return clean_db


# UTC 03:30 = Toshkent 08:30
MORNING_UTC = datetime(2026, 9, 30, 3, 30)
NIGHT_UTC = datetime(2026, 9, 30, 1, 0)       # Toshkent 06:00


def test_due_only_after_hour_and_once_a_day(scene):
    assert settings.briefing_hour() == 8
    assert not briefing.due(NIGHT_UTC)
    assert briefing.due(MORNING_UTC)
    briefing.mark_sent(MORNING_UTC)
    assert not briefing.due(MORNING_UTC)
    # ertasi kuni yana
    assert briefing.due(datetime(2026, 10, 1, 4, 0))


def test_disabled_with_minus_one(scene):
    assert settings.save({"briefing_hour": "-1"}) == {}
    assert not briefing.due(MORNING_UTC)


def test_hour_setting_validated(scene):
    assert settings.save({"briefing_hour": "25"})       # xato qaytadi
    assert settings.save({"briefing_hour": "7"}) == {}
    assert settings.briefing_hour() == 7


def test_build_has_plan_and_buttons(scene):
    text, keyboard = briefing.build(MORNING_UTC, with_ai=False)
    assert "План на 30 сентября" in text
    assert "№01" in text and "№02" in text
    assert "/день" in text
    assert "→" in text and "-&gt;" not in text
    assert keyboard["inline_keyboard"][0][0]["callback_data"].startswith("take:")


def test_skipped_cargo_not_offered_again(scene):
    """Rahbar "O'tkazish" bosgan yuk shu furaga qayta taklif qilinmaydi."""
    import actions
    import ai_tools
    ctx = ai_tools.Ctx()
    first = ai_tools.plan_truck(ctx, truck_id="01")["plans"][0]
    actions.skip_match(first["offer"])
    again = ai_tools.plan_truck(ai_tools.Ctx(), truck_id="01")["plans"]
    assert all(p["cargo_id"] != first["cargo_id"] for p in again)
    found = ai_tools.find_cargo(ai_tools.Ctx(), truck_id="01")["results"]
    assert all(r["cargo_id"] != first["cargo_id"] for r in found)


def test_ai_tip_added_and_failure_ignored(scene, monkeypatch):
    monkeypatch.setattr(brain, "advise", lambda data: "Сначала забронируйте №01.")
    text, _ = briefing.build(MORNING_UTC)
    assert "Сначала забронируйте" in text

    def boom(data):
        raise RuntimeError("tarmoq")
    monkeypatch.setattr(brain, "advise", boom)
    text, _ = briefing.build(MORNING_UTC)             # xato rejani to'xtatmaydi
    assert "План на" in text


def test_send_if_due_sends_once(scene, monkeypatch):
    sent = []
    monkeypatch.setattr("notifier.send", lambda text, kb=None, **kw: sent.append(text))
    monkeypatch.setattr(brain, "advise", lambda data: None)
    assert briefing.send_if_due(MORNING_UTC)
    assert not briefing.send_if_due(MORNING_UTC)
    assert len(sent) == 1


def test_advise_single_call_without_tools(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("AI_PROVIDERS", "groq")
    seen = []

    def fake(p, messages, tools=True):
        seen.append(tools)
        return {"role": "assistant", "content": "<b>Совет</b> **важно**"}
    monkeypatch.setattr(brain, "chat_completion", fake)
    assert brain.advise({"plan": []}) == "Совет важно"
    assert seen == [False]


def test_brief_command(scene, monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "api", lambda method, http_timeout=15, **p:
                        calls.append({"method": method, **p}) or {"ok": True})
    monkeypatch.setattr(brain, "advise", lambda data: None)
    bot.handle_message({"message_id": 1, "chat": {"id": 777, "type": "private"},
                        "text": "/brief"})
    text = [c for c in calls if c["method"] == "sendMessage"][-1]["text"]
    assert "План на" in text and "08:00" in text


def test_settings_page_shows_briefing_hour(clean_db, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    import web
    monkeypatch.setenv("WEB_PASSWORD", "p")
    web._login_fails.clear()
    c = TestClient(web.create_app())
    c.post("/login", data={"password": "p"})
    assert "Ertalabki reja soati" in c.get("/settings").text
