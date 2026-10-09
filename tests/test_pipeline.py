"""
test_pipeline.py — xabarning to'liq yo'li: matn → baza → moslik → xabar.

Bu yerda modullar birgalikda tekshiriladi: parser, dedup, db, scoring,
notifier bitta oqimda.
"""
from __future__ import annotations

import json

import pytest

import notifier
import pipeline


@pytest.fixture
def sent(monkeypatch):
    """Telegramga chiqishni to'xtatib, yuborilgan xabarlarni yig'adi."""
    box = []

    def fake_send(text, reply_markup=None, chat_id=None):
        box.append({"text": text, "keyboard": reply_markup})
        return {"ok": True}

    monkeypatch.setattr(notifier, "send", fake_send)
    return box


@pytest.fixture
def park(clean_db, truck_tent, truck_ref):
    for t in (truck_tent, truck_ref):
        clean_db.upsert_truck(t)
    return clean_db


# ---------------------------------------------------------------- saqlash

def test_cargo_is_saved(park, sent):
    ids = pipeline.handle_message(
        "Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09", source="grp1")
    assert len(ids) == 1
    row = park.get_cargo(ids[0])
    assert (row["from_city"], row["to_city"]) == ("Toshkent", "Moskva")
    assert row["rate_usd"] == 4000
    assert row["status"] == "new"
    assert row["source"] == "grp1"


def test_raw_text_is_stored(park, sent):
    """raw_text hech qachon yo'qolmaydi — parserni yaxshilash uchun kerak."""
    text = "Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09"
    ids = pipeline.handle_message(text, source="grp1")
    assert park.get_cargo(ids[0])["raw_text"] == text


def test_multi_cargo_post_saves_each(park, sent):
    text = ("📦 ЮКЛАР\n1. Ташкент - Москва, 20т тент, 4000$\n"
            "2. Самарканд - Казань, 18т реф +2, 3800$\n"
            "3. Наманган - Екатеринбург, 20т тент, 4200$\nТел: +998901234567")
    ids = pipeline.handle_message(text, source="grp1")
    assert len(ids) == 3
    routes = {(park.get_cargo(i)["from_city"], park.get_cargo(i)["to_city"]) for i in ids}
    assert routes == {("Toshkent", "Moskva"), ("Samarqand", "Qozon"),
                      ("Namangan", "Yekaterinburg")}
    assert all(park.get_cargo(i)["phone"] == "+998901234567" for i in ids)


def test_duplicate_is_skipped(park, sent):
    text = "Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09"
    assert len(pipeline.handle_message(text, source="grp1")) == 1
    assert pipeline.handle_message(text, source="grp2") == []


def test_truck_ad_is_skipped(park, sent):
    assert pipeline.handle_message(
        "Свободная машина реф 20т в Ташкенте, ищу груз на Москву") == []


def test_garbage_is_skipped(park, sent):
    assert pipeline.handle_message("Всем привет, как дела коллеги") == []


def test_cargo_without_destination_is_skipped(park, sent):
    assert pipeline.handle_message("Есть груз из Ташкента, 20 тонн, 4000$") == []


# ---------------------------------------------------------------- moslik

def test_match_is_saved_for_each_truck(park, sent):
    ids = pipeline.handle_message(
        "Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09", source="grp1")
    with park.connect() as conn:
        rows = conn.execute("SELECT * FROM matches WHERE cargo_id=?", (ids[0],)).fetchall()
    assert {r["truck_id"] for r in rows} == {"01", "02"}
    assert all(r["details"] for r in rows)


def test_good_cargo_is_notified(park, sent):
    pipeline.handle_message(
        "Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09", source="grp1")
    assert sent, "yaxshi yuk uchun bildirishnoma kelmadi"
    card = sent[0]["text"]
    assert "Toshkent" in card and "Moskva" in card
    assert "Маржа" in card


def test_notification_has_working_buttons(park, sent):
    ids = pipeline.handle_message(
        "Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09", source="grp1")
    buttons = [b for row in sent[0]["keyboard"]["inline_keyboard"] for b in row]
    data = [b.get("callback_data", "") for b in buttons]

    assert f"info:{ids[0]}" in data
    with park.connect() as conn:
        match_id = conn.execute(
            "SELECT id FROM matches WHERE cargo_id=? ORDER BY score DESC",
            (ids[0],)).fetchone()["id"]
    assert f"take:{match_id}" in data
    assert f"skip:{match_id}" in data


def test_weak_match_is_saved_but_not_notified(park, sent, monkeypatch):
    """Ball pastligi uchun xabar ketmaydi, lekin moslik bazada qoladi."""
    monkeypatch.setattr(pipeline, "NOTIFY_THRESHOLD", 99.9)
    ids = pipeline.handle_message(
        "Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09", source="grp1")
    with park.connect() as conn:
        rows = conn.execute("SELECT * FROM matches WHERE cargo_id=?", (ids[0],)).fetchall()
    assert rows and all(r["notified"] == 0 for r in rows)
    assert sent == []


def test_notified_flag_is_set(park, sent):
    ids = pipeline.handle_message(
        "Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09", source="grp1")
    with park.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM matches WHERE cargo_id=? AND notified=1", (ids[0],)).fetchall()
    assert len(rows) == len(sent)


def test_details_json_is_readable(park, sent):
    ids = pipeline.handle_message(
        "Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09", source="grp1")
    with park.connect() as conn:
        row = conn.execute("SELECT details FROM matches WHERE cargo_id=?",
                           (ids[0],)).fetchone()
    details = json.loads(row["details"])
    assert details["total_km"] > 0
    assert "score_parts" in details


# ---------------------------------------------------------------- ishonchlilik

def test_notify_failure_does_not_lose_cargo(park, monkeypatch):
    """TZ 6.7: xabar yuborishdagi xato butun oqimni to'xtatmaydi."""
    def boom(*a, **kw):
        raise RuntimeError("telegram yiqildi")

    monkeypatch.setattr(notifier, "notify_match", boom)
    ids = pipeline.handle_message(
        "Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09", source="grp1")
    assert len(ids) == 1
    assert park.get_cargo(ids[0]) is not None


def test_no_trucks_does_not_crash(clean_db, sent):
    """Mashina yuklanmagan bo'lsa ham yuk saqlanadi."""
    ids = pipeline.handle_message(
        "Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09", source="grp1")
    assert len(ids) == 1
    assert sent == []


def test_db_connections_are_closed(clean_db):
    """`with db.connect()` ulanishni yopadi — aks holda listener fayl
    deskriptorlari chegarasiga yetib, yuklarni yo'qotadi."""
    import sqlite3
    with clean_db.connect() as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")                    # yopilgan


def test_db_uses_wal(clean_db):
    with clean_db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


# ---------------------------------------------------------------- qayta hisob (formula o'zgargach)

def test_rescore_open_updates_and_cancels(park, sent):
    """Ochiq taklif yangi formula bilan qayta hisoblanadi; mos kelmay qolgani bekor."""
    ids = pipeline.handle_message(
        "Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09", source="grp1")
    cid = ids[0]
    m = park.find_match(cid, "01")
    assert m is not None
    with park.connect() as conn:                       # eski formula qoldig'i
        conn.execute("UPDATE matches SET margin_usd=99999 WHERE id=?", (m["id"],))
    out = pipeline.rescore_open(hours=72)
    assert out["updated"] >= 1
    assert park.get_match(m["id"])["margin_usd"] < 99999

    # yuk endi mos kelmaydi (yo'nalish bir shahar) — ochiq taklif bekor bo'ladi
    with park.connect() as conn:
        conn.execute("UPDATE cargos SET to_city='Toshkent' WHERE id=?", (cid,))
    pipeline.rescore_open(hours=72)
    assert park.get_match(m["id"])["decision"] == "cancelled"


# ---------------------------------------------------------------- kartochka siyosati (audit 2026-10-09)

def test_one_card_per_cargo(park, sent):
    """Ikkala fura ham mos — kartochka bitta (eng yaxshisi), moslik ikkalasiga yoziladi."""
    ids = pipeline.handle_message("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09")
    with park.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM matches WHERE cargo_id=?",
                            (ids[0],)).fetchone()[0] == 2
    assert len(sent) == 1


def test_unpriced_cargo_gets_no_card(park, sent):
    """Narxsiz yuk ham yuqori ball oladi, lekin kartochka emas — panel/qidiruvda ko'rinadi."""
    ids = pipeline.handle_message("Есть груз Ташкент → Москва, 20т тент, 22.09")
    assert ids and sent == []


def test_watched_route_is_notified_even_without_price(park, sent):
    park.add_watch("Toshkent", "Moskva", None, query="ташкент москва")
    pipeline.handle_message("Есть груз Ташкент → Москва, 20т тент, 22.09")
    assert len(sent) == 1 and "По вашему запросу" in sent[0]["text"]
