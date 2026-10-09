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


# ---------------------------------------------------------------- guruhlar bazadan (sources)

from datetime import datetime, timezone  # noqa: E402

import sources  # noqa: E402


class FakeTG:
    """Guruh ulash uchun soxta mijoz: get_entity, so'rovlar (join/invite), xabarlar."""

    def __init__(self, entities=None, invites=None, messages=None, fail=None, dialogs=None):
        self.entities = entities or {}
        self.invites = invites or {}
        self.messages = messages or {}
        self.fail = set(fail or [])
        self.dialogs = dialogs or []
        self.joined = []

    async def get_entity(self, key):
        if key in self.fail:
            raise ValueError("topilmadi")
        return self.entities[key]

    async def __call__(self, req):
        name = type(req).__name__
        if name == "JoinChannelRequest":
            self.joined.append(req.channel)
            return None
        if name == "ImportChatInviteRequest":
            return SimpleNamespace(chats=[self.invites[req.hash]])
        raise AssertionError(name)

    async def iter_messages(self, entity, limit=60):
        for m in self.messages.get(entity.id, [])[:limit]:
            yield m

    async def iter_dialogs(self, limit=500):
        for d in self.dialogs:
            yield d


def chan(cid, username=None, title="Guruh", left=False):
    return SimpleNamespace(id=cid, username=username, title=title, left=left, megagroup=True)


def now_msg(i, text):
    return SimpleNamespace(id=i, message=text, date=datetime.now(timezone.utc))


@pytest.fixture
def fresh_watch():
    for state in (listener.WATCH, listener.LAST_ID, listener._SEEN, listener._ENTITIES):
        state.clear()
    yield listener.WATCH
    for state in (listener.WATCH, listener.LAST_ID, listener._SEEN, listener._ENTITIES):
        state.clear()


def test_sync_joins_public_group_and_reads_recent(clean_db, fresh_watch, monkeypatch):
    monkeypatch.setattr("notifier.send", lambda *a, **k: None)
    sid, _ = sources.add("@yuk_markazi")
    g = chan(-1001, "yuk_markazi", "Yuk markazi", left=True)
    fake = FakeTG(entities={"yuk_markazi": g}, messages={-1001: [
        now_msg(1, "Есть груз Ташкент → Москва, 20т тент, 4000$")]})
    asyncio.run(listener.sync_sources(fake))
    row = sources.get(sid)
    assert row["status"] == "active" and row["chat_id"] == "-1001" and row["title"] == "Yuk markazi"
    assert fake.joined == [g]                       # akkaunt a'zo emas edi — qo'shildi
    assert -1001 in fresh_watch
    with clean_db.connect() as conn:
        assert conn.execute("SELECT source FROM cargos").fetchone()["source"] == "yuk_markazi"
    assert row["backfill"] == 0 and sources.last_seen_seconds() is not None


def test_sync_invite_link(clean_db, fresh_watch):
    sources.add("https://t.me/+AbCdEf12345")
    fake = FakeTG(invites={"AbCdEf12345": chan(-1002, None, "Yopiq guruh")})
    asyncio.run(listener.sync_sources(fake))
    assert sources.list_sources()[0]["status"] == "active" and -1002 in fresh_watch


def test_sync_error_is_readable_and_not_watched(clean_db, fresh_watch):
    sid, _ = sources.add("@yoq_guruh")
    asyncio.run(listener.sync_sources(FakeTG(fail={"yoq_guruh"})))
    row = sources.get(sid)
    assert row["status"] == "error" and "не найдена" in row["error"]
    assert not fresh_watch


def test_sync_rejects_person(clean_db, fresh_watch):
    sid, _ = sources.add("@odam_ismi")
    person = SimpleNamespace(id=77, first_name="Ali", username="odam_ismi")
    asyncio.run(listener.sync_sources(FakeTG(entities={"odam_ismi": person})))
    assert sources.get(sid)["status"] == "error"


def test_removed_group_leaves_watch(clean_db, fresh_watch):
    cid = -1001000000003
    sid, _ = sources.add(str(cid))
    asyncio.run(listener.sync_sources(FakeTG(entities={cid: chan(cid)})))
    assert cid in fresh_watch
    sources.remove(sid)
    asyncio.run(listener.sync_sources(FakeTG()))
    assert cid not in fresh_watch


def test_one_join_per_cycle(clean_db, fresh_watch):
    """Akkaunt bloklanmasin: bir siklda bitta guruh ulanadi."""
    sources.add("@grp_one")
    sources.add("@grp_two")
    fake = FakeTG(entities={"grp_one": chan(-11, "grp_one"), "grp_two": chan(-12, "grp_two")})
    asyncio.run(listener.sync_sources(fake))
    assert fresh_watch == {-11}
    asyncio.run(listener.sync_sources(fake))
    assert fresh_watch == {-11, -12}


def test_refresh_dialogs_keeps_only_groups(clean_db):
    dialogs = [SimpleNamespace(is_group=True, is_channel=False, entity=chan(-21, "a", "A")),
               SimpleNamespace(is_group=False, is_channel=False, entity=SimpleNamespace(id=5)),
               SimpleNamespace(is_group=False, is_channel=True, entity=chan(-22, None, "B"))]
    assert asyncio.run(listener.refresh_dialogs(FakeTG(dialogs=dialogs))) == 2
    assert {d["title"] for d in sources.dialogs()} == {"A", "B"}


# ---------------------------------------------------------------- tutib olish (poll)

class FakePoll:
    """iter_messages(reverse, offset_date, min_id) ni Telegram kabi bajaradi."""

    def __init__(self, entities, messages):
        self.entities, self.messages, self.calls = entities, messages, []

    async def get_entity(self, key):
        return self.entities[key]

    async def iter_messages(self, entity, reverse=False, offset_date=None, min_id=0, limit=100):
        self.calls.append(min_id)
        out = [m for m in sorted(self.messages.get(entity.id, []), key=lambda m: m.id)
               if m.id > min_id and (offset_date is None or m.date >= offset_date)]
        for m in out[:limit]:
            yield m


def at(i, text, minutes_ago=0):
    from datetime import timedelta
    return SimpleNamespace(id=i, message=text,
                           date=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))


def test_poll_catches_message_the_event_missed(clean_db, fresh_watch, monkeypatch):
    """Hodisa 5-xabarni oldi, 4-ni Telegram yetkazmadi — so'rov 4-ni topadi, 5-ni qaytarmaydi."""
    monkeypatch.setattr("notifier.send", lambda *a, **k: None)
    g = chan(-1005, "yuklar")
    m4 = at(4, "Есть груз Ташкент → Москва, 20т тент, 4000$")
    m5 = at(5, "Есть груз Бухара → Казань, 20т тент, 4100$")
    fresh_watch.add(-1005)
    asyncio.run(listener._process(-1005, m5, "yuklar"))         # hodisa
    fake = FakePoll({-1005: g}, {-1005: [m4, m5]})
    assert asyncio.run(listener.poll_once(fake)) == 1
    with clean_db.connect() as conn:
        routes = sorted((r["from_city"], r["to_city"]) for r in
                        conn.execute("SELECT from_city, to_city FROM cargos"))
    assert routes == [("Buxoro", "Qozon"), ("Toshkent", "Moskva")]
    assert listener.LAST_ID[-1005] == 5
    assert asyncio.run(listener.poll_once(fake)) == 0              # takror ishlanmaydi
    assert fake.calls[-1] == 5


def test_poll_old_message_does_not_notify(clean_db, fresh_watch, monkeypatch):
    """Qayta ishga tushgandan keyin tutib olingan eski e'lon — kartochkasiz."""
    seen = []
    monkeypatch.setattr(listener.pipeline, "handle_message",
                        lambda text, src, mid, date, notify=True: seen.append(notify) or [])
    fresh_watch.add(-1006)
    fake = FakePoll({-1006: chan(-1006, "g")},
                    {-1006: [at(1, "Есть груз Ташкент → Москва, 20т", minutes_ago=120),
                             at(2, "Есть груз Бухара → Казань, 20т", minutes_ago=1)]})
    asyncio.run(listener.poll_once(fake))
    assert seen == [False, True]


def test_poll_resumes_from_saved_position(clean_db, fresh_watch):
    """Qayta ishga tushganda oxirgi o'qilgan raqam bazadan olinadi."""
    sid, _ = sources.add("-1001000000007")
    sources.set_active(sid, -1001000000007, "G", None)
    sources.save_last_ids({-1001000000007: 120})
    assert sources.last_ids() == {-1001000000007: 120}
    sources.save_last_ids({-1001000000007: 100})               # orqaga qaytmaydi
    assert sources.last_ids() == {-1001000000007: 120}


def test_ingest_stats_are_flushed(clean_db, fresh_watch, monkeypatch):
    """Guruh bo'yicha hisob: xabar, yangi yuk, takror, yuk emas."""
    monkeypatch.setattr("notifier.send", lambda *a, **k: None)
    listener.pipeline.take_stats()
    fresh_watch.add(-1008)
    text = "Есть груз Ташкент → Москва, 20т тент, 4000$"
    for i, t in enumerate([text, text + " срочно!", "Привет всем, как дела у вас?", "ок"], 1):
        asyncio.run(listener._process(-1008, at(i, t), "grp"))
    sources.flush_stats(listener.pipeline.take_stats())
    assert sources.ingest_stats()["grp"] == {"messages": 4, "saved": 1, "dup": 1, "skipped": 2}
