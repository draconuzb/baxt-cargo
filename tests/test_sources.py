"""
test_sources.py — kuzatiladigan guruhlar: panel/botdan qo'shish (sources.py).

Telethon bu yerda kerak emas — faqat baza. Listener tomoni test_listener.py da.
"""
from __future__ import annotations

import pytest

import bot
import config
import sources


@pytest.mark.parametrize("text,ref", [
    ("@Logistika_UZ", "user:logistika_uz"),
    ("https://t.me/logistika_uz", "user:logistika_uz"),
    ("t.me/logistika_uz/1234", "user:logistika_uz"),
    ("https://t.me/+AbCdEf123_45", "invite:AbCdEf123_45"),
    ("https://t.me/joinchat/AbCdEf12345", "invite:AbCdEf12345"),
    ("-1001397241850", "id:-1001397241850"),
    ("  @lognumber1 ", "user:lognumber1"),
])
def test_normalize_ref(text, ref):
    assert sources.normalize_ref(text) == ref


@pytest.mark.parametrize("text", ["", "salom jigar", "https://example.com/x", "t.me/+ab"])
def test_normalize_ref_rejects_garbage(text):
    assert sources.normalize_ref(text) is None


def test_add_remove_readd(clean_db):
    sid, err = sources.add("@logistika_uz")
    assert sid and not err and sources.get(sid)["status"] == "pending"
    assert sources.add("https://t.me/logistika_uz") == (sid, "")      # takror emas
    sources.remove(sid)
    assert sources.list_sources() == []
    assert sources.add("@logistika_uz") == (sid, "")                  # qaytadan
    assert sources.get(sid)["status"] == "pending"
    assert sources.add("bu nima")[1]                                   # xato matni


def test_set_active_and_duplicate(clean_db):
    a, _ = sources.add("@grp_one")
    b, _ = sources.add("-1001234567890")
    assert sources.set_active(a, -1001234567890, "Yuklar", "grp_one")
    assert not sources.set_active(b, -1001234567890, "Yuklar", None)   # o'sha chat
    assert sources.get(b)["status"] == "removed"
    assert sources.active_chat_ids() == [-1001234567890]


def test_migrate_from_file_once(clean_db):
    assert sources.migrate_from_file(["-1001397241850", "-1001384382030"]) == 2
    assert sources.migrate_from_file(["-1009999999999"]) == 0          # bir marta
    rows = sources.list_sources()
    assert len(rows) == 2 and all(r["backfill"] == 0 for r in rows)   # eski — qayta o'qilmaydi


def test_dialogs_exclude_watched(clean_db):
    sources.save_dialogs([{"chat_id": -1001000000001, "title": "A", "username": "a", "members": 900},
                          {"chat_id": -1001000000002, "title": "B", "username": None, "members": 50}])
    sid, _ = sources.add("-1001000000001")
    sources.set_active(sid, -1001000000001, "A", "a")
    assert [d["title"] for d in sources.dialogs()] == ["B"]


def test_heartbeat(clean_db):
    assert sources.last_seen_seconds() is None
    sources.heartbeat()
    assert sources.last_seen_seconds() < 5


# ---------------------------------------------------------------- panel

@pytest.fixture
def client(clean_db, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import web
    monkeypatch.setenv("WEB_PASSWORD", "test-pass")
    c = TestClient(web.create_app())
    c.post("/login", data={"password": "test-pass"})
    return c


def test_groups_page_add_and_remove(client):
    html = client.get("/groups").text
    assert "Группы Telegram" in html and 'name="ref"' in html
    r = client.post("/groups/add", data={"ref": "https://t.me/logistika_uz"})
    assert r.status_code == 200 and "Группа добавлена" in r.text
    assert "@logistika_uz" in r.text and "подключается" in r.text
    row = sources.list_sources()[0]
    client.post(f"/groups/{row['id']}/remove")
    assert sources.list_sources() == []


def test_groups_page_bad_link(client):
    r = client.post("/groups/add", data={"ref": "salom jigar"})
    assert "Не понял ссылку" in r.text and sources.list_sources() == []


def test_groups_page_shows_account_groups_and_stats(client, clean_db):
    sources.save_dialogs([{"chat_id": -1001000000005, "title": "Yuk markazi", "username": "yukm",
                           "members": 12000}])
    sid, _ = sources.add("@logistikasn")
    sources.set_active(sid, -1007, "Logistika SN", "logistikasn")
    with clean_db.connect() as conn:
        conn.execute("INSERT INTO cargos (raw_text, source, status) VALUES ('x', 'logistikasn', 'new')")
    html = client.get("/groups").text
    assert "Yuk markazi" in html and "12 000 участников" in html
    assert 'value="-1001000000005"' in html
    assert "1 груз за 7 дн." in html and "читается" in html


def test_more_page_links_groups(client):
    assert 'href="/groups"' in client.get("/more").text


# ---------------------------------------------------------------- bot

@pytest.fixture
def tg(monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "api", lambda method, http_timeout=15, **p:
                        calls.append({"method": method, **p}) or {"ok": True, "result": {}})
    monkeypatch.setattr(config, "BOT_TOKEN", "t")
    monkeypatch.setattr(config, "DISPATCHER_CHAT_ID", "")
    return calls


def _msg(text):
    return {"message_id": 1, "chat": {"id": 555, "type": "private"}, "text": text}


def test_bot_group_commands(clean_db, tg):
    clean_db.add_dispatcher_chat(555)
    bot.handle_message(_msg("/group https://t.me/+AbCdEf12345"))
    assert "Группа добавлена" in tg[-1]["text"]
    assert sources.list_sources()[0]["ref"] == "invite:AbCdEf12345"
    bot.handle_message(_msg("/groups"))
    assert "🟡" in tg[-1]["text"] and "приглашение" in tg[-1]["text"]
    bot.handle_message(_msg("/group https://example.com/x"))
    assert "Не понял" in tg[-1]["text"]
