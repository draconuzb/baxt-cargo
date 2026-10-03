"""
test_parser.py — e'lon matnini tahlil qilish testlari.

Ikki qism:
1. `ads.jsonl` dagi real e'lonlar — har bir maydon kutilgan qiymat bilan
   solishtiriladi, aniqlik foizi konsolga chiqadi.
2. Alohida funksiyalar (vazn, stavka, sana, shahar) uchun nuqtali testlar.
"""
from __future__ import annotations

from datetime import date

import pytest

import geo
import parser
from conftest import ACCURACY, ad_today, load_ads

ADS = load_ads()


def _same(want, got) -> bool:
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        return abs(float(want) - float(got)) < 0.01
    return want == got


@pytest.mark.parametrize("ad", ADS, ids=[a["id"] for a in ADS])
def test_ad_fields(ad):
    """Har bir e'lon kutilgan maydonlarni to'g'ri beradimi."""
    c = parser.parse(ad["text"], today=ad_today(ad))
    got = c.to_dict()

    misses = []
    for key, want in ad["expect"].items():
        ACCURACY["fields"] += 1
        if _same(want, got.get(key)):
            ACCURACY["fields_ok"] += 1
        else:
            misses.append(f"{key}: kutilgan {want!r}, olingan {got.get(key)!r}")

    ACCURACY["ads"] += 1
    if misses:
        ACCURACY["misses"].append(f"{ad['id']}: " + "; ".join(misses))
    else:
        ACCURACY["ads_ok"] += 1

    assert not misses, f"[{ad['id']}]\n  " + "\n  ".join(misses)


# ---------------------------------------------------------------- vazn

@pytest.mark.parametrize("text,expected", [
    ("20 тонн", 20.0),
    ("20т", 20.0),
    ("20тн", 20.0),
    ("18-20 тонн", 20.0),          # diapazon -> yuqori chegara
    ("20,5 т", 20.5),
    ("5000 кг", 5.0),
    ("20 tonna", 20.0),
    ("massasi 500 кг", 0.5),
])
def test_weight(text, expected):
    assert parser._parse_weight(parser.normalize(text)) == expected


def test_weight_ignores_unrealistic():
    """60 tonna yuk fura uchun real emas — qabul qilinmaydi."""
    assert parser._parse_weight(parser.normalize("60 тонн")) is None


# ---------------------------------------------------------------- stavka

@pytest.mark.parametrize("text,rate,cur", [
    ("4000$", 4000, "USD"),
    ("$4000", 4000, "USD"),
    ("4 200 $", 4200, "USD"),
    ("4200 usd", 4200, "USD"),
    ("4000 у.е.", 4000, "USD"),
    ("42 млн сум", 42_000_000, "UZS"),
    ("42 млн", 42_000_000, "UZS"),
    ("1.5 млн сум", 1_500_000, "UZS"),
    ("8 500 000 сум", 8_500_000, "UZS"),
    ("85000 руб", 85_000, "RUB"),
    ("450000 тг", 450_000, "KZT"),
])
def test_rate(text, rate, cur):
    got_rate, got_cur, _ = parser._parse_rate(parser.normalize(text))
    assert (got_rate, got_cur) == (rate, cur)


def test_rate_not_glued_to_previous_number():
    """Regressiya: "реф -18, 1800$" da stavka 181800$ bo'lib ketgan edi."""
    r, cur, _ = parser._parse_rate(parser.normalize("реф -18, 1800$"))
    assert (r, cur) == (1800, "USD")


def test_rate_per_ton_multiplied():
    """Tonnasiga berilgan narx umumiy stavkaga aylantiriladi."""
    c = parser.parse("Андижан → Алматы 20т реф +2, за тонну 250$, нужна машина")
    assert c.rate == 5000
    assert c.rate_per_ton is False


def test_rate_per_ton_kept_when_weight_unknown():
    """Vazn noma'lum bo'lsa ko'paytirib bo'lmaydi — bayroq saqlanadi."""
    c = parser.parse("Есть груз Ташкент → Москва, за тонну 250$")
    assert c.rate_per_ton is True
    assert c.rate == 250


# ---------------------------------------------------------------- sana

def test_date_relative_words():
    today = date(2026, 5, 10)
    assert parser._parse_date("загрузка сегодня", today) == today
    assert parser._parse_date("завтра", today) == date(2026, 5, 11)
    assert parser._parse_date("послезавтра", today) == date(2026, 5, 12)
    assert parser._parse_date("ertaga yuklash", today) == date(2026, 5, 11)


def test_date_numeric():
    assert parser._parse_date("загрузка 12.05", date(2026, 5, 1)) == date(2026, 5, 12)
    assert parser._parse_date("18.05.2026", date(2026, 5, 1)) == date(2026, 5, 18)


def test_date_word_month():
    assert parser._parse_date("15 мая", date(2026, 5, 1)) == date(2026, 5, 15)


def test_date_picks_nearest_year():
    """Yil ko'rsatilmasa — bugunga eng yaqin yil olinadi.

    Dekabrda kelgan "05.01" keyingi yilning yanvari.
    """
    assert parser._parse_date("05.01", date(2026, 12, 20)) == date(2027, 1, 5)


# ---------------------------------------------------------------- kuzov, harorat

@pytest.mark.parametrize("text,body", [
    ("реф 20т", "ref"),
    ("рефрижератор", "ref"),
    ("тент", "tent"),
    ("фура", "tent"),
    ("изотерм", "izoterm"),
    ("борт", "bort"),
    ("контейнер", "konteyner"),
    ("трал", "tral"),
    ("самосвал", "samosval"),
])
def test_body_type(text, body):
    assert parser._parse_body(parser.normalize(text)) == body


@pytest.mark.parametrize("text,temp", [
    ("реф +5", 5.0),
    ("ref -18", -18.0),
    ("режим +2", 2.0),
    ("заморозка", -18.0),
    ("охлажденка", 4.0),
])
def test_temp(text, temp):
    assert parser._parse_temp(parser.normalize(text)) == temp


def test_temp_implies_ref_body():
    """Harorat ko'rsatilgan bo'lsa kuzov avtomatik ref bo'ladi."""
    c = parser.parse("Есть груз Ташкент → Москва, +5 градусов, 20 тонн, 4000$")
    assert c.body_type == "ref"


# ---------------------------------------------------------------- shahar

def test_find_cities_dash_route():
    assert [c[1] for c in geo.find_cities("ташкент-москва")] == ["Toshkent", "Moskva"]


def test_find_cities_keeps_compound_name():
    """"Ростов-на-Дону" chiziqcha bo'yicha bo'linib ketmasligi kerak."""
    assert [c[1] for c in geo.find_cities("ростов-на-дону - ташкент")] \
        == ["Rostov-na-Donu", "Toshkent"]


def test_find_cities_two_word_name():
    assert [c[1] for c in geo.find_cities("нижний новгород - ташкент")] \
        == ["Nijniy Novgorod", "Toshkent"]


def test_city_canonical_names():
    """Baza faqat kanonik nomni ko'radi — xom matn emas."""
    for raw in ("казань", "kazan", "Казань", "қозон"):
        assert geo.lookup(raw) == "Qozon"


def test_stopwords_are_not_cities():
    for word in ("груз", "машина", "тонн", "ставка", "телефон"):
        assert geo.lookup(word) is None


def test_chain_route_fills_via():
    c = parser.parse("Москва - Казань - Ташкент тент 20т 4500$ есть груз")
    assert (c.from_city, c.to_city, c.via) == ("Moskva", "Toshkent", ["Qozon"])


# ---------------------------------------------------------------- tasnif

def test_truck_ad_not_saved_as_cargo():
    c = parser.parse("Свободная машина реф 20т в Алматы, ищу груз на Ташкент")
    assert c.kind == "truck"
    assert parser.is_usable(c) is False


def test_cargo_needs_both_cities():
    c = parser.parse("Есть груз из Ташкента, 20 тонн тент, 4000$")
    assert parser.is_usable(c) is False


def test_usable_cargo():
    c = parser.parse("Есть груз Ташкент → Москва, 20т тент, 4000$, завтра")
    assert c.kind == "cargo"
    assert parser.is_usable(c) is True
    assert c.confidence >= 0.45


def test_raw_text_preserved():
    """raw_text hech qachon o'zgartirilmaydi — parserni yaxshilash uchun kerak."""
    text = "  Есть груз Ташкент → Москва, 20т тент, 4000$  "
    c = parser.parse(text)
    assert c.raw_text == text.strip()


# ---------------------------------------------------------------- kontakt

@pytest.mark.parametrize("text,phone", [
    ("тел: +998 90 123 45 67", "+998901234567"),
    ("+998 (93) 456-78-90", "+998934567890"),
    ("998901234567", "+998901234567"),
    ("87011234567", "+87011234567"),
])
def test_phone(text, phone):
    assert parser._parse_contact(text)[0] == phone


def test_username():
    assert parser._parse_contact("пишите @uzlogistic")[1] == "uzlogistic"


# ---------------------------------------------------------------- o'zbekcha qo'shimchalar

@pytest.mark.parametrize("word,city", [
    ("kazandan", "Qozon"), ("toshkentdan", "Toshkent"), ("almatiga", "Almaty"),
    ("moskvagacha", "Moskva"), ("buxoroga", "Buxoro"), ("ташкентдан", "Toshkent"),
    ("москвага", "Moskva"), ("москвы", "Moskva"), ("москву", "Moskva"),
    ("казани", "Qozon"), ("ташкента", "Toshkent"),
])
def test_city_with_case_suffix(word, city):
    import geo
    assert geo.lookup(word) == city


@pytest.mark.parametrize("word", ["tonnaga", "bugun", "kerak", "narxiga", "mashinaga"])
def test_suffix_does_not_invent_cities(word):
    import geo
    assert geo.lookup(word) is None


def test_messy_uzbek_ads_parse_without_ai():
    from datetime import date
    c = parser.parse("bratishka mashina kerak ertaga, kazandan tashkentga sovutgichli "
                     "20 tonnaga yaqin", today=date(2026, 9, 30))
    assert (c.kind, c.from_city, c.to_city, c.weight_t) == ("cargo", "Qozon", "Toshkent", 20)
    c = parser.parse("kim Toshkentdan Almatiga ketyapti? 15 tonna qog'oz bor")
    assert (c.from_city, c.to_city, c.weight_t) == ("Toshkent", "Almaty", 15)


# ---------------------------------------------------------------- narx xatolari (haqiqiy e'lonlar)

def test_rate_with_cents_is_not_multiplied():
    """"200000.00 KZT" — tiyin ".00" raqamga qo'shilmaydi (avval 20 mln bo'lardi)."""
    c = parser.parse("Toshkent viloyati → Shymkent\nVAZNI: 6.00 tonna\nTO'LOV: 200000.00 KZT Naqd")
    assert (c.rate, c.currency) == (200_000.0, "KZT")


def test_full_sum_with_redundant_mln():
    """"Narxi 1.500.000 mln" — 1,5 mln so'm (avval "500.000 mln" = 500 mln bo'lardi)."""
    c = parser.parse("Samarqand\nTaxta bozordan\n\nJizzah\nShaharga\n\nNarxi 1.500.000 mln")
    assert (c.rate, c.currency) == (1_500_000.0, "UZS")


def test_route_labels_qayerdan_qayerga():
    """Shablon e'lon: yorliq yo'nalishni belgilaydi, viloyat — markazi."""
    c = parser.parse("📍 Qayerdan: 🇷🇺 Tatariston Respublikasi, Rossiya\n"
                     "🏁 Qayerga: 🇺🇿 Toshkent shahri, O'zbekiston\n💰 4000 USD")
    assert (c.from_city, c.to_city) == ("Qozon", "Toshkent")


def test_route_label_kuda_before_city():
    c = parser.parse("Куда: Ташкент\nОткуда: Москва\nтент 20т 4500$")
    assert (c.from_city, c.to_city) == ("Moskva", "Toshkent")
