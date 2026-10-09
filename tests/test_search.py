"""
test_search.py — dispetcher so'rovi bo'yicha qidirish (buyurtmachi talabi).

"Менга Тошкент–Москва юк топиб бер" va "бизда нечта мошина бор, тент/реф —
шуни билиб қидирса зўр бўларди".

Ya'ni: erkin yozilgan so'rovni tushunish + javobni PARK bo'yicha hisoblash.
"""
from __future__ import annotations

import pytest

import pipeline
import search


@pytest.fixture
def park(clean_db, truck_tent, truck_ref, monkeypatch):
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)
    for t in (truck_tent, truck_ref):
        clean_db.upsert_truck(t)
    return clean_db


def add(text: str) -> list[int]:
    return pipeline.handle_message(text, source="grp1")


# ---------------------------------------------------------------- so'rovni tushunish

@pytest.mark.parametrize("text,expected", [
    ("Тошкент Москва", ("Toshkent", "Moskva")),
    ("Ташкент - Москва", ("Toshkent", "Moskva")),
    ("Toshkent Moskva yuk bormi", ("Toshkent", "Moskva")),
    ("менга ташкент москва юк топиб бер", ("Toshkent", "Moskva")),
    ("КАЗАНЬ ТАШКЕНТ", ("Qozon", "Toshkent")),
])
def test_route_is_understood(text, expected):
    q = search.parse_query(text)
    assert (q.from_city, q.to_city) == expected


def test_single_city():
    q = search.parse_query("Москва юк бормi")
    assert q.single_city == "Moskva"
    assert (q.from_city, q.to_city) == (None, None)


def test_body_type_is_understood():
    assert search.parse_query("Ташкент Москва реф").body_type == "ref"
    assert search.parse_query("тент керак Ташкент Москва").body_type == "tent"


def test_truck_number_is_understood():
    for text in ("Москва машина 01", "мошина 1 учун юк", "№02 Ташкент"):
        assert search.parse_query(text).truck_id in ("01", "02")


def test_empty_query():
    assert search.parse_query("салом жигар").is_empty is True


def test_describe_is_readable():
    q = search.parse_query("Ташкент Москва реф")
    assert "Ташкент → Москва" in q.describe()


# ---------------------------------------------------------------- qidiruv

def test_finds_cargo_on_route(park):
    add("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09")
    add("Есть груз Бухара → Казань, 20т тент, 4100$, 23.09")

    found = search.find("Тошкент Москва")
    assert len(found["results"]) == 1
    assert found["results"][0]["cargo"]["to_city"] == "Moskva"
    assert found["results"][0]["truck_id"] in ("01", "02")


def test_answer_is_calculated_for_our_fleet(park):
    """Javobda mashina, bo'sh probeg va kunlik marja bo'lishi shart —
    "eng yaxshi yuk" emas, "bizning mashinaga eng yaxshi yuk"."""
    add("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09")
    r = search.find("Ташкент Москва")["results"][0]
    assert r["truck_id"] and r["margin_usd"] and r["margin_per_day"]
    assert r["empty_km"] is not None and r["trip_days"] > 0


def test_sorted_by_daily_margin(park):
    """Loyihaning asosiy tamoyili qidiruvda ham saqlanadi."""
    add("Есть груз Ташкент → Москва, 20т тент, 4200$, 22.09")
    add("Есть груз Ташкент → Алматы, 20т тент, 1900$, 22.09")

    results = search.find("Ташкент")["results"]
    per_day = [r["margin_per_day"] for r in results if r["fits"]]
    assert per_day == sorted(per_day, reverse=True)


def test_body_filter(park):
    add("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09")
    add("Есть груз Ташкент → Москва, 20т реф +2, 4300$, 23.09")

    found = search.find("Ташкент Москва реф")
    assert len(found["results"]) == 1
    assert found["results"][0]["cargo"]["body_type"] == "ref"


def test_search_for_one_truck(park):
    add("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09")
    found = search.find("Ташкент Москва машина 02")
    assert found["trucks"] == 1
    assert all(r["truck_id"] == "02" for r in found["results"])


def test_single_city_matches_both_directions(park):
    """"Ташкент" deyilsa — u yerdan chiqadigan ham, u yerga keladigan ham."""
    add("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09")
    add("Есть груз Самарканд → Ташкент, 20т тент, 900$, 23.09")
    routes = {(r["cargo"]["from_city"], r["cargo"]["to_city"])
              for r in search.find("Ташкент")["results"]}
    assert routes == {("Toshkent", "Moskva"), ("Samarqand", "Toshkent")}


def test_unreachable_cargo_is_shown_without_truck(park):
    """Mashinalar Toshkentda — Qozondan chiqadigan yuk uchun 3000 km bo'sh yurish.
    Yuk baribir ko'rsatiladi (dispetcher yo'nalishni so'radi, 2026-10-09:
    "botdan yoqadigan yuk topa olmayapman" — ilgari bunday yuklar yashirinardi),
    lekin fura va marjasiz."""
    add("Есть груз Казань → Самара, 20т тент, 2000$, 23.09")
    found = search.find("Казань")
    [r] = found["results"]
    assert r["fits"] is False and r["truck_id"] is None and r["margin_usd"] is None
    assert found["fit"] == 0


def test_fitting_cargo_ranks_above_market(park):
    """Furamizga mos kelganlari tepada, qolgani — keyin."""
    add("Есть груз Ташкент → Москва, 30 тонн тент, 5000$, 22.09")      # sig'maydi
    add("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09")
    results = search.find("Ташкент Москва")["results"]
    assert [r["fits"] for r in results] == [True, False]
    assert results[1]["cargo"]["weight_t"] == 30


def test_city_includes_suburbs(park):
    """"Москва" — Подольск, Балашиха ham (dispetcher joyni aytadi, shaharni emas)."""
    add("Есть груз Ташкент → Балашиха, 20т тент, 4000$, 22.09")
    add("Есть груз Ташкент → Тула, 20т тент, 3900$, 22.09")
    found = search.find("Ташкент Москва")
    assert [r["cargo"]["to_city"] for r in found["results"]] == ["Balashikha"]


def test_country_query(park):
    """"Узбекистан Россия" — davlat bo'yicha."""
    add("Есть груз Самарканд → Тула, 20т тент, 3900$, 22.09")
    add("Есть груз Самарканд → Алматы, 20т тент, 1500$, 22.09")
    q = search.parse_query("из Узбекистана в Россию")
    assert (q.from_country, q.to_country) == ("UZ", "RU")
    assert [r["cargo"]["to_city"] for r in search.find(q)["results"]] == ["Tula"]
    assert "Узбекистан → Россия" in q.describe()


def test_taken_cargo_is_not_offered(park):
    ids = add("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09")
    park.set_cargo_status(ids[0], "taken")
    assert search.find("Ташкент Москва")["results"] == []


def test_nothing_found_reports_history(park):
    """"Hozir yo'q" yetarli emas — bu yo'nalish umuman bormi?"""
    ids = add("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09")
    park.set_cargo_status(ids[0], "taken")

    found = search.find("Ташкент Москва")
    assert found["results"] == []
    assert found["history"] >= 1


def test_search_without_trucks(clean_db, monkeypatch):
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)
    add("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09")
    found = search.find("Ташкент Москва")
    assert found["trucks"] == 0 and [r["fits"] for r in found["results"]] == [False]


def test_fleet_summary(park):
    fleet = {t["id"]: t for t in search.fleet_summary()}
    assert fleet["01"]["body_type"] == "tent" and fleet["01"]["city"] == "Toshkent"
    assert fleet["02"]["body_type"] == "ref"


# ---------------------------------------------------------------- kuzatuv

def test_watch_matching():
    class Watch(dict):
        def __getitem__(self, k):
            return self.get(k)

    cargo = {"from_city": "Toshkent", "to_city": "Moskva", "body_type": "tent"}
    assert search.matches_watch(cargo, Watch(from_city="Toshkent", to_city="Moskva"))
    assert search.matches_watch(cargo, Watch(to_city="Moskva"))
    assert not search.matches_watch(cargo, Watch(from_city="Buxoro"))
    assert not search.matches_watch(cargo, Watch(to_city="Moskva", body_type="ref"))


def test_watched_route_notifies_below_threshold(park, monkeypatch):
    """Dispetcher o'zi so'ragan yo'nalish — ball past bo'lsa ham xabar keladi."""
    sent = []
    monkeypatch.setattr("notifier.send",
                        lambda text, *a, **kw: sent.append(text))
    monkeypatch.setattr(pipeline, "NOTIFY_THRESHOLD", 99.9)
    park.add_watch("Toshkent", "Moskva", None, query="Ташкент Москва")

    add("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09")

    assert sent, "so'ralgan yo'nalish bo'yicha xabar kelmadi"
    assert "По вашему запросу" in sent[0]


def test_unwatched_route_stays_silent(park, monkeypatch):
    sent = []
    monkeypatch.setattr("notifier.send", lambda text, *a, **kw: sent.append(text))
    monkeypatch.setattr(pipeline, "NOTIFY_THRESHOLD", 99.9)
    park.add_watch("Buxoro", "Qozon", None, query="Бухара Казань")

    add("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09")
    assert sent == []


def test_watch_hit_counter(park, monkeypatch):
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)
    watch_id = park.add_watch("Toshkent", "Moskva", None, query="тест")
    add("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09")
    assert park.active_watches()[0]["hits"] == 1
    assert park.delete_watch(watch_id) is True


def test_expired_watch_is_ignored(park):
    park.add_watch("Toshkent", "Moskva", None, query="тест", days=-1)
    assert park.active_watches() == []
