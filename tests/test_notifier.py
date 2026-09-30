"""
test_notifier.py — kartochka matni va bildirishnoma cheklovi (TZ 5.8.2, 5.8.4).

Kartochka — dispetcher ko'radigan yagona narsa: undagi raqam noto'g'ri
bo'lsa, qolgan barcha hisob-kitobning ma'nosi yo'q.
"""
from __future__ import annotations

import pytest

import notifier


@pytest.fixture
def sent(monkeypatch):
    box = []
    monkeypatch.setattr(notifier, "send",
                        lambda text, reply_markup=None, chat_id=None:
                        box.append(text) or {"ok": True})
    return box


CARGO = {
    "id": 7, "from_city": "Toshkent", "to_city": "Moskva",
    "weight_t": 20.0, "body_type": "ref", "temp_c": -18.0,
    "rate": 42_000_000.0, "currency": "UZS", "rate_usd": 3281.25,
    "load_date": "2026-09-22", "phone": "+998901234567",
    "username": None, "source": "grp1", "via": "[]",
    "raw_text": "Есть груз Ташкент → Москва, 20т реф -18, 42 млн сум",
}

RESULT = {
    "truck_id": "01", "score": 88.0, "empty_km": 0, "loaded_km": 3965,
    "total_km": 3965, "trip_days": 9.0, "fuel_l": 1309, "fuel_cost": 1243,
    "borders": 2, "border_cost": 240, "driver_cost": 238, "road_cost": 99,
    "fixed_cost": 80, "total_cost": 1900, "revenue_usd": 3281,
    "margin_usd": 1381, "margin_per_day": 153, "warnings": [],
    "arrival_date": "2026-09-21",
}


# ---------------------------------------------------------------- kartochka

def test_card_has_key_numbers():
    card = notifier.format_card(CARGO, RESULT)
    for piece in ("Toshkent → Moskva", "42.0 млн сум", "Реф", "-18°C", "20 т",
                  "Машина №01", "88/100", "Маржа", "$1 381", "💵"):
        assert piece in card, f"kartochkada yo'q: {piece}"
    assert "/день" not in card                          # buyurtmachi talabi


def test_card_shows_usd_equivalent():
    """So'mdagi stavka yonida USD — taqqoslash uchun."""
    assert "≈$3 281" in notifier.format_card(CARGO, RESULT)


def test_card_without_rate_warns():
    cargo = {**CARGO, "rate": None, "currency": None, "rate_usd": None}
    result = {**RESULT, "margin_usd": None, "revenue_usd": None,
              "warnings": ["stavka ko'rsatilmagan — marja hisoblanmadi"]}
    card = notifier.format_card(cargo, result)
    assert "ставка не указана" in card
    assert "stavka ko'rsatilmagan" in card


def test_card_contact_and_source():
    card = notifier.format_card(CARGO, RESULT)
    assert "+998901234567" in card
    assert "grp1" in card


def test_money_format():
    assert notifier.money(1381) == "$1 381"
    assert notifier.money(None) == "—"
    assert notifier.money(0) == "$0"


def test_keyboard_callbacks():
    kb = notifier._keyboard(CARGO, match_id=42)
    data = [b.get("callback_data") for row in kb["inline_keyboard"] for b in row]
    assert "info:7" in data and "call:7" in data
    assert "take:42" in data and "skip:42" in data


def test_keyboard_uses_link_when_username_known():
    kb = notifier._keyboard({**CARGO, "username": "logist"}, 42)
    urls = [b.get("url") for row in kb["inline_keyboard"] for b in row]
    assert "https://t.me/logist" in urls


def test_details_include_original_text():
    text = notifier.format_details(CARGO, RESULT)
    assert "Есть груз Ташкент" in text
    assert "итого расходы" in text


def test_details_escape_html():
    """E'lon matnidagi < > belgilari xabarni buzmasligi kerak."""
    cargo = {**CARGO, "raw_text": "груз <b>срочно</b> & дешево"}
    text = notifier.format_details(cargo, None)
    assert "&lt;b&gt;" in text and "&amp;" in text


def test_contact_message():
    text = notifier.format_contact(CARGO)
    assert "+998901234567" in text and "<code>" in text


def test_contact_missing():
    text = notifier.format_contact({**CARGO, "phone": None, "username": None})
    assert "не указан" in text


def test_roundtrip_message():
    chains = [{
        "back_cargo": {"id": 9, "from_city": "Moskva", "to_city": "Toshkent",
                       "phone": "+79001112233"},
        "leg1": {"margin_usd": 1381}, "leg2": {"margin_usd": 1200, "empty_km": 0},
        "total_margin_usd": 2581, "total_days": 18.0, "margin_per_day": 143,
        "total_km": 7930, "empty_km": 0,
    }]
    text = notifier.format_roundtrip(CARGO, chains)
    assert "Moskva → Toshkent" in text
    assert "$2 581" in text and "/день" not in text
    assert "+79001112233" in text


def test_roundtrip_empty():
    assert "не найден" in notifier.format_roundtrip(CARGO, [])


# ---------------------------------------------------------------- cheklov

def test_notifications_are_capped(sent, monkeypatch):
    """Soatiga N tadan ko'p kartochka yuborilmaydi — aks holda dispetcher
    hech birini o'qimaydi."""
    monkeypatch.setattr(notifier, "MAX_NOTIFY_PER_HOUR", 3)
    notifier.reset_limits()

    results = [notifier.notify_match(CARGO, RESULT, i) for i in range(5)]
    assert results == [True, True, True, False, False]
    cards = [t for t in sent if "НОВЫЙ ГРУЗ" in t]
    assert len(cards) == 3


def test_suppressed_matches_go_to_digest(sent, monkeypatch):
    """Ortiqchasi yig'ma xabarda ko'rsatiladi."""
    monkeypatch.setattr(notifier, "MAX_NOTIFY_PER_HOUR", 1)
    monkeypatch.setattr(notifier, "DIGEST_INTERVAL_SEC", 0)
    notifier.reset_limits()

    for i in range(4):
        notifier.notify_match(CARGO, RESULT, i)

    digests = [t for t in sent if "Ещё" in t]
    assert digests, "yig'ma xabar yuborilmadi"
    assert "/list" in digests[-1]


def test_quota_frees_up_after_an_hour(sent, monkeypatch):
    monkeypatch.setattr(notifier, "MAX_NOTIFY_PER_HOUR", 1)
    notifier.reset_limits()
    assert notifier.notify_match(CARGO, RESULT, 1) is True
    assert notifier.notify_match(CARGO, RESULT, 2) is False

    # bir soat oldingi xabar hisobdan chiqadi
    notifier._sent_at[0] -= 3601
    assert notifier.notify_match(CARGO, RESULT, 3) is True


def test_reset_limits(sent, monkeypatch):
    monkeypatch.setattr(notifier, "MAX_NOTIFY_PER_HOUR", 1)
    notifier.reset_limits()
    notifier.notify_match(CARGO, RESULT, 1)
    notifier.reset_limits()
    assert notifier.notify_match(CARGO, RESULT, 2) is True


def test_dispatcher_message_has_russian_city_names(monkeypatch):
    """Bildirishnoma ham ruscha: "Toshkent" -> "Ташкент", callback_data esa kanonik."""
    import json as _json
    from urllib.parse import parse_qs
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=15):
        seen.update({k: v[0] for k, v in parse_qs(req.data.decode()).items()})
        return Resp()

    monkeypatch.setattr(notifier.urllib.request, "urlopen", fake_urlopen)
    notifier._send_to("1", "<b>Toshkent → Moskva</b>",
                      {"inline_keyboard": [[{"text": "Qozon", "callback_data": "x:Qozon"}]]}, "t")
    assert seen["text"] == "<b>Ташкент → Москва</b>"
    markup = _json.loads(seen["reply_markup"])
    assert markup["inline_keyboard"][0][0] == {"text": "Казань", "callback_data": "x:Qozon"}
