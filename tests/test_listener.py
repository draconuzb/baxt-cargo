"""
test_listener.py — guruhlarni o'qish (Telegramsiz, soxta mijoz bilan).

Tekshiriladi: ulanmagan guruh qolganlarini to'xtatmaydi, Telegram limiti
(FloodWait) kutib o'tiladi, buzuq xabar backfill'ni to'xtatmaydi.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("telethon")

import listener  # noqa: E402
from telethon.errors import FloodWaitError  # noqa: E402


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Testda haqiqatan kutmaymiz, lekin kutish so'ralganini yozib boramiz."""
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(listener.asyncio, "sleep", fake_sleep)
    return slept


def flood(seconds: int) -> FloodWaitError:
    err = FloodWaitError(request=None, capture=seconds)
    err.seconds = seconds
    return err


class FakeClient:
    def __init__(self, entities=None, messages=None, fail=None, flood_once=None):
        self.entities = entities or {}
        self.messages = messages or {}
        self.fail = fail or set()
        self.flood_once = set(flood_once or [])
        self.disconnected = False

    async def start(self):
        return self

    async def get_entity(self, name):
        if name in self.flood_once:
            self.flood_once.discard(name)
            raise flood(30)
        if name in self.fail:
            raise ValueError("guruh topilmadi")
        return self.entities[name]

    async def iter_messages(self, entity, limit=200):
        for msg in self.messages.get(entity.username, [])[:limit]:
            yield msg

    async def disconnect(self):
        self.disconnected = True


def group(name):
    return SimpleNamespace(username=name, title=name)


def msg(i, text):
    return SimpleNamespace(id=i, message=text, date=None)


def test_bad_group_does_not_stop_others():
    client = FakeClient(entities={"@a": group("a"), "@c": group("c")}, fail={"@b"})
    resolved = asyncio.run(listener._resolve_sources(client, ["@a", "@b", "@c"]))
    assert [g.username for g in resolved] == ["a", "c"]


def test_flood_wait_is_respected(no_sleep):
    """Telegram "30 soniya kuting" desa — kutamiz va qayta urinamiz."""
    client = FakeClient(entities={"@a": group("a")}, flood_once={"@a"})
    resolved = asyncio.run(listener._resolve_sources(client, ["@a"]))
    assert [g.username for g in resolved] == ["a"]
    assert any(s >= 30 for s in no_sleep)


def test_groups_are_joined_slowly(no_sleep):
    """Akkaunt bloklanmasligi uchun guruhlar orasida tanaffus bor."""
    client = FakeClient(entities={"@a": group("a"), "@b": group("b")})
    asyncio.run(listener._resolve_sources(client, ["@a", "@b"]))
    assert no_sleep.count(listener.RESOLVE_DELAY_SEC) == 2


def test_flood_wait_is_capped(no_sleep):
    """Juda uzoq kutish so'ralsa ham — cheklangan vaqt."""
    asyncio.run(listener._sleep_flood(flood(999_999), "test"))
    assert no_sleep == [listener.MAX_FLOOD_WAIT_SEC]


def test_backfill_saves_and_survives_errors(clean_db, monkeypatch):
    messages = {"a": [
        msg(1, "Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09"),
        msg(2, "короткий"),                                   # 15 belgidan qisqa
        msg(3, "ЭТОТ ТЕКСТ ЛОМАЕТ ПАРСЕР — ошибка внутри"),
        msg(4, "Есть груз Бухара → Казань, 20т тент, 4100$, 23.09"),
    ]}
    fake = FakeClient(entities={"@a": group("a")}, messages=messages)
    monkeypatch.setattr(listener, "TelegramClient", lambda *a, **kw: fake)
    monkeypatch.setattr(listener.config, "load_sources", lambda: ["@a"])

    real_handle = listener.pipeline.handle_message

    def flaky(text, *args, **kw):
        if "ЛОМАЕТ" in text:
            raise RuntimeError("parser yiqildi")
        return real_handle(text, *args, **kw)

    monkeypatch.setattr(listener.pipeline, "handle_message", flaky)
    asyncio.run(listener.backfill(limit=10))

    with clean_db.connect() as conn:
        routes = {(r["from_city"], r["to_city"]) for r in
                  conn.execute("SELECT from_city, to_city FROM cargos")}
    assert routes == {("Toshkent", "Moskva"), ("Buxoro", "Qozon")}
    assert fake.disconnected


def test_backfill_does_not_notify(clean_db, monkeypatch):
    """Eski xabarlar uchun dispetcherga kartochka yuborilmaydi."""
    sent = []
    monkeypatch.setattr("notifier.send", lambda *a, **kw: sent.append(a))
    fake = FakeClient(entities={"@a": group("a")}, messages={"a": [
        msg(1, "Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09")]})
    monkeypatch.setattr(listener, "TelegramClient", lambda *a, **kw: fake)
    monkeypatch.setattr(listener.config, "load_sources", lambda: ["@a"])
    asyncio.run(listener.backfill())
    assert sent == []
