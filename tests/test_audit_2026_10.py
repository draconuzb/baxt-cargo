"""
test_audit_2026_10.py — guruh oqimi auditi (2026-10-08) topgan yo'qotishlar.

Audit: 2 soatda guruhlarda 7 141 xabar, bazaga ~20 yuk tushgan. Sabablari:
  1. barmoq izi (fingerprint) butun vaqt bo'yicha UNIQUE — sanasiz "Москва → Ташкент,
     реф 22 т" bir marta yozilgach, keyingi haftadagisi jimgina tashlanardi;
  2. qayta joylangan e'lon asl yukni yangilamasdi — 24 soatda eskirib, yo'qolardi;
  3. platforma reklama qatori ("bo'sh mashina bormi?") yuk e'lonini "bo'sh mashina" qilardi;
  4. lug'atda 114 shahar: Грозный, Нижний Тагил, viloyatlar, "Olmaota" tanilmasdi;
  5. faqat davlat yozilgan e'lon ("Италия - Ташкент") yo'qolardi;
  6. qidiruv faqat bizning furaga mos yukni ko'rsatardi (park hali haqiqiy emas).
Matnlar — guruhlardagi haqiqiy e'lonlardan.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import ai_tools
import db
import dedup
import geo
import parser
import pipeline


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)


# ---------------------------------------------------------------- 1–2. baza va dubl

def test_old_fingerprint_does_not_block_new_cargo(clean_db, quiet):
    """Bir hafta oldingi bir xil (sanasiz) e'lon yangi yukni to'smaydi."""
    text = "🇷🇺 Москва - 🇺🇿 Ташкент\nАвто: Реф\nГруз: Бумага\nВес: 22 тонны"
    [old] = pipeline.handle_message(text)
    with clean_db.connect() as conn:
        conn.execute("UPDATE cargos SET created_at=datetime('now','-7 day') WHERE id=?", (old,))
    [new] = pipeline.handle_message(text)
    assert new != old
    assert clean_db.get_cargo(old)["fingerprint"].endswith(f"@{old}")   # arxivlandi


def test_repost_keeps_cargo_alive(clean_db, quiet):
    """Qayta joylangan e'lon asl yukni yangilaydi; eskirgan bo'lsa — qaytadan faol."""
    text = "Есть груз Ташкент → Москва, 20т тент, 4000$"
    [cid] = pipeline.handle_message(text)
    with clean_db.connect() as conn:
        conn.execute("UPDATE cargos SET created_at=datetime('now','-30 hour') WHERE id=?", (cid,))
    assert clean_db.expire_old_cargos(hours=24) == 1
    assert pipeline.handle_message(text, posted_at=datetime.now(timezone.utc)) == []   # dubl
    row = clean_db.get_cargo(cid)
    assert row["status"] == "new" and row["seen_at"] is not None
    assert cid in [r["id"] for r in clean_db.active_cargos(hours=24)]


def test_old_repost_does_not_revive(clean_db, quiet):
    """Qayta ishga tushgandan keyin tutib olingan ESKI qayta e'lon yukni jonlantirmaydi."""
    text = "Есть груз Ташкент → Москва, 20т тент, 4000$"
    [cid] = pipeline.handle_message(text)
    with clean_db.connect() as conn:
        conn.execute("UPDATE cargos SET created_at=datetime('now','-30 hour'), status='expired'")
    pipeline.handle_message(text, posted_at=datetime.now(timezone.utc) - timedelta(hours=26))
    assert clean_db.get_cargo(cid)["status"] == "expired"


def test_fresh_duplicate_still_rejected_by_db(clean_db):
    c = parser.parse("Есть груз Ташкент → Москва, 20т тент, 4000$")
    assert clean_db.insert_cargo(c, dedup.fingerprint(c))
    assert clean_db.insert_cargo(c, dedup.fingerprint(c)) is None


# ---------------------------------------------------------------- 3. tasnif

GLOGISTICS = """🔥 YANGI YUK E'LONI!

🇺🇿 O'zbekiston, Namangan shahri ➡️ 🇺🇿 O'zbekiston, Qashqadaryo shahri

📦 Yuk: Namangan ➡️ Qashqadaryo yuk (Tent)
🚛 Transport: Tent
⚖️ Vazni: 20 tonna
💰 To'lov: Kelishiladi

────────────────────
📢 Sizda ham yuk yoki bo'sh mashina bormi?
Saytimizga kirib e'loningizni joylang — aqlli botimiz uni tarqatib beradi! 🚀"""


def test_platform_footer_does_not_make_truck_ad():
    c = parser.parse_many(GLOGISTICS)[0]
    assert c.kind == "cargo" and (c.from_city, c.to_city) == ("Namangan", "Qarshi")
    assert c.via == []                          # shablon yo'nalishni ikki marta yozadi


def test_real_truck_ad_still_truck():
    assert parser.classify("Ташкент-Ашхабад\nНужен груз на 3 РЭФ а\nСтавка 1600$") == "truck"


# ---------------------------------------------------------------- 4–5. shaharlar

@pytest.mark.parametrize("text,route", [
    ("qara grozni samarqand yuk bormi", ("Grozny", "Samarqand")),
    ("Нижний Тагил 🇷🇺 - Алмалык 🇺🇿\nгруз: смола\nвес: 22 тонн", ("Nizhny Tagil", "Olmaliq")),
    ("Челябинск - Хайратан ( Афганстан)\nKerak: Тент", ("Chelyabinsk", "Xayraton")),
    ("📍 Qayerdan: 🇷🇺 Boshqirdiston Respublikasi, Rossiya\n🏁 Qayerga: 🇺🇿 Toshkent shahri",
     ("Ufa", "Toshkent")),
    ("📍 Qayerdan: 🇰🇿 Olmaota shahri, Qozog'iston\n🏁 Qayerga: 🇺🇿 Buxoro viloyati",
     ("Almaty", "Buxoro")),
    ("📍 Qayerdan: 🇷🇺 Saratov viloyati, Rossiya\n🏁 Qayerga: 🇺🇿 Surxondaryo viloyati",
     ("Saratov", "Termiz")),
    ("Италия - ташкент тент стандарт", ("Milan", "Toshkent")),
    ("📦 Yuk: Samara ➡️ O'zbekiston yuk (Tent)", ("Samara", "Toshkent")),
    ("Marshrut: Toshkent → Rossiya (~3 163 km)", ("Toshkent", "Moskva")),
    ("ВОРОНЕЖ (Россия) - ТАШКЕНТ (Узбекистан) нужен тент", ("Voronej", "Toshkent")),
    ("🇺🇿Фаргона_Ставрополь🇷🇺", ("Farg'ona", "Stavropol")),
    ("Москва (Балашиха) - Водий, тент 22т", ("Moskva", "Farg'ona")),
])
def test_routes_from_real_ads(text, route):
    c = parser.parse(text)
    assert (c.from_city, c.to_city) == route


@pytest.mark.parametrize("word", ["tent", "turi", "hajmi", "сахар", "месте", "каштан",
                                  "bor", "marhamat", "свободный"])
def test_common_words_are_not_cities(word):
    """Katta lug'atda oddiy so'z shaharga aylanmasin ("tent" -> Trento edi)."""
    assert geo.lookup(word) is None


def test_country_words():
    assert geo.country_of_word("России") == "RU"
    assert geo.country_of_word("Rossiyadan") == "RU"
    assert geo.country_of_word("Италию") == "IT"
    assert geo.country_of_word("Москва") is None


def test_every_country_hub_is_a_city():
    assert all(hub in geo.CITIES for hub in geo.COUNTRY_HUB.values())


def test_cities_near_includes_suburbs():
    near = geo.cities_near("Moskva", 50)
    assert {"Moskva", "Balashikha", "Podolsk", "Ximki"} <= near and "Tula" not in near
    assert geo.cities_near("Moskva", 0) == {"Moskva"}


def test_dictionary_is_big_and_fast():
    """Lug'at kengaydi, lekin qidiruv tez (kesh) — guruhlardan kuniga 100 mingdan ortiq xabar."""
    import time
    assert len(geo.CITIES) > 2000
    text = GLOGISTICS * 3
    parser.parse_many(text)
    t0 = time.perf_counter()
    for _ in range(20):
        parser.parse_many(text)
    assert (time.perf_counter() - t0) / 20 < 0.25


# ---------------------------------------------------------------- 6. AI qidiruvi

def test_ai_route_search_shows_market_when_no_truck_fits(clean_db, quiet):
    """"Грозный → Самарканд" so'ralganda boshqa yo'nalish emas, shu yo'nalishdagi yuk."""
    db.upsert_truck({"id": "01", "body_type": "tent", "capacity_t": 22,
                     "current_city": "Toshkent", "free_date": None, "fuel_l_100km": 33})
    pipeline.handle_message("Грозный - Самарканд, тент 22т, 3500$")
    pipeline.handle_message("Есть груз Ташкент → Алматы, 20т тент, 1900$")
    out = ai_tools.find_cargo(ai_tools.Ctx(), from_city="Грозный", to_city="Самарканд")
    assert [r["route"] for r in out["results"]] == ["Grozny -> Samarqand"]
    assert out["results"][0]["fleet_fit"] is False and out["fit"] == 0
