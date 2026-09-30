"""
test_geo.py — ruscha ko'rinish nomlari (panel, bot va AI ruscha).

Bazada kanonik nom qoladi ("Toshkent"), odam ko'radigan joyda — "Ташкент".
Ruscha nom qaytib `geo.lookup` dan o'tishi shart: panel formasida yoki AI
javobida yozilgan "Ташкент" yana "Toshkent" ga aylanadi.
"""
from __future__ import annotations

import geo


def test_ru_names():
    assert geo.ru("Toshkent") == "Ташкент"
    assert geo.ru("Qozon") == "Казань"
    assert geo.ru("Rostov-na-Donu") == "Ростов-на-Дону"
    assert geo.ru("Nijniy Novgorod") == "Нижний Новгород"
    assert geo.ru("Kukuevo") == "Kukuevo"            # lug'atda yo'q — o'zi
    assert geo.ru(None) == "" and geo.ru("") == ""


def test_every_russian_name_resolves_back():
    bad = [name for name, ru in geo.RU.items() if geo.lookup(ru) != name]
    assert bad == []


def test_ru_text_whole_words_only():
    out = geo.ru_text("Toshkent -> Moskva, haydovchi Oralbek, Farg'ona")
    assert out == "Ташкент -> Москва, haydovchi Oralbek, Фергана"
    assert geo.ru_text("") == ""
