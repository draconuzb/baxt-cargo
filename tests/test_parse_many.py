"""
test_parse_many.py — bitta postdagi bir nechta yuk (TZ 5.2).

Eng xavfli xato: 5 ta yukli ro'yxatdan bitta uydirma yuk yasash
(birinchi va oxirgi shahar). Shu yerda qotirilgan.
"""
from __future__ import annotations

from datetime import date

import pytest

import parser
from conftest import ad_today, load_ads

TODAY = date(2026, 9, 20)

LIST_AD = """📦 БУГУНГИ ЮКЛАР
1. Ташкент - Москва, 20т тент, 4000$
2. Самарканд - Казань, 18т реф +2, 3800$
3. Наманган - Екатеринбург, 20т тент, 4200$
Тел: +998901234567"""


def many(text: str) -> list[parser.Cargo]:
    return parser.parse_many(text, source="grp", today=TODAY)


# ---------------------------------------------------------------- TZ mezoni

def test_three_cargos_from_one_post():
    cargos = many(LIST_AD)
    assert len(cargos) == 3
    assert [(c.from_city, c.to_city) for c in cargos] == [
        ("Toshkent", "Moskva"),
        ("Samarqand", "Qozon"),
        ("Namangan", "Yekaterinburg"),
    ]


def test_all_cargos_inherit_phone():
    """Telefon faqat oxirgi qatorda — hammasiga tegishli."""
    assert all(c.phone == "+998901234567" for c in many(LIST_AD))


def test_each_cargo_keeps_own_fields():
    """2-yukning refi 1-yukka o'tib ketmasligi kerak."""
    a, b, c = many(LIST_AD)
    assert (a.body_type, a.temp_c, a.rate) == ("tent", None, 4000)
    assert (b.body_type, b.temp_c, b.rate) == ("ref", 2.0, 3800)
    assert (c.body_type, c.temp_c, c.rate) == ("tent", None, 4200)


def test_all_are_usable_cargos():
    assert all(c.kind == "cargo" and parser.is_usable(c) for c in many(LIST_AD))


# ---------------------------------------------------------------- formatlar

def test_multiline_blocks():
    """Har bir yuk bir necha qatorga yoyilgan."""
    text = """ГРУЗЫ НА 22.09
1) Ташкент - Москва
20 тонн тент
4000$
2) Бухара - Казань
18 тонн реф +4
3800$
тел +998901112233"""
    cargos = many(text)
    assert len(cargos) == 2
    assert cargos[0].weight_t == 20 and cargos[0].rate == 4000
    assert cargos[1].weight_t == 18 and cargos[1].temp_c == 4.0


def test_header_date_inherited():
    """Sana sarlavhada bir marta yozilgan — hamma yukka tegishli."""
    text = """ГРУЗЫ НА 22.09
1) Ташкент - Москва 20т тент 4000$
2) Бухара - Казань 18т тент 3800$"""
    assert [c.load_date for c in many(text)] == [date(2026, 9, 22)] * 2


def test_own_date_beats_header_date():
    text = """ГРУЗЫ НА 22.09
1) Ташкент - Москва 20т тент 4000$ загрузка 25.09
2) Бухара - Казань 18т тент 3800$"""
    a, b = many(text)
    assert a.load_date == date(2026, 9, 25)
    assert b.load_date == date(2026, 9, 22)


def test_header_weight_inherited():
    text = """ГРУЗЫ 20 тонн каждый
• Ташкент - Москва 4000$
• Бухара - Казань 3800$"""
    assert [c.weight_t for c in many(text)] == [20.0, 20.0]


def test_inline_numbered_items():
    """Ro'yxat bitta qatorga yozilgan."""
    text = ("Есть грузы: 1. Ташкент-Москва 4000$ 2. Самарканд-Казань 3800$ "
            "3. Андижан-Алматы 1900$ @logist")
    cargos = many(text)
    assert len(cargos) == 3
    assert all(c.username == "logist" for c in cargos)


def test_bullet_markers():
    text = """Грузы на сегодня:
• Ташкент - Москва, 20т тент, 4000$
• Самарканд - Казань, 18т тент, 3800$
+998901234567"""
    assert len(many(text)) == 2


# ---------------------------------------------------------------- regressiya

@pytest.mark.parametrize("ad", load_ads(), ids=[a["id"] for a in load_ads()])
def test_single_ads_still_return_one_cargo(ad):
    """Oddiy e'lonlar avvalgidek ishlaydi — bo'linib ketmaydi."""
    cargos = parser.parse_many(ad["text"], today=ad_today(ad))
    assert len(cargos) == 1
    single = parser.parse(ad["text"], today=ad_today(ad))
    assert (cargos[0].from_city, cargos[0].to_city) == (single.from_city, single.to_city)


def test_chain_route_is_not_split():
    """"Москва - Казань - Ташкент" — bitta yuk, oraliq shahar bilan."""
    cargos = many("Москва - Казань - Ташкент тент 20т 4500$ есть груз")
    assert len(cargos) == 1
    assert cargos[0].via == ["Qozon"]


def test_long_chain_is_not_split():
    """To'rtta shahar bitta zanjirda — baribir bitta yuk."""
    cargos = many("Есть груз Москва - Казань - Самара - Ташкент, 20т тент, 5000$")
    assert len(cargos) == 1
    assert (cargos[0].from_city, cargos[0].to_city) == ("Moskva", "Toshkent")


def test_free_trucks_list_is_not_cargo():
    """Bo'sh mashinalar ro'yxati yuk sifatida bazaga tushmasligi kerak."""
    text = ("Свободные машины: 1. Москва реф 2. Казань тент "
            "3. Ташкент тент, ищу груз")
    cargos = many(text)
    assert len(cargos) == 1
    assert cargos[0].kind == "truck"
    assert parser.is_usable(cargos[0]) is False


def test_garbage_does_not_produce_cargos():
    text = "Ассалому алайкум, Ташкент Москва Казань Алматы Бухара рахмат"
    for c in many(text):
        assert c.rate is None      # stavkasiz uydirma yuklar yasalmadi


# ---------------------------------------------------------------- haqiqiy guruh formati

FLAG_POST = ("🇺🇿Ташкент-🇷🇺Москва\nпаркет\nтент\n22 тонн\nгруз готов\nоплата наличными\n"
             "+998XXXXXXXXX\n\n🇺🇿Ташкент-🇵🇼Алмата\nбумага\nтент\n10.2026\n"
             "оплата наличными\n+998XXXXXXXXX")


def test_flag_post_with_two_cargos():
    """Haqiqiy guruhdan (2026-09-30): ilgari "Toshkent → Toshkent" bo'lib saqlanardi."""
    from datetime import date
    items = parser.parse_many(FLAG_POST, today=date(2026, 9, 30))
    routes = [(c.from_city, c.to_city) for c in items]
    assert routes == [("Toshkent", "Moskva"), ("Toshkent", "Almaty")]
    assert items[0].load_date == date(2026, 9, 30)      # "груз готов" = bugun
    assert items[0].weight_t == 22 and items[0].body_type == "tent"


def test_same_city_route_is_not_usable():
    c = parser.parse("Груз Ташкент — Ташкент, 20т тент, 500$")
    assert c.to_city is None and not parser.is_usable(c)


@pytest.mark.parametrize("text", ["груз готов", "Yuk tayyor, mashina kerak",
                                  "готов к загрузке", "юк тайёр"])
def test_ready_means_today(text):
    from datetime import date
    assert parser._parse_date(parser.normalize(text), date(2026, 9, 30)) == date(2026, 9, 30)
