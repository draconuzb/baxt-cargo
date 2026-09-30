"""
test_dedup.py — dubl filtri.

Bir xil yuk 5 ta guruhda chiqadi, ba'zan boshqa so'zlar bilan. Uch bosqich:
fingerprint, telefon+yo'nalish, matn o'xshashligi.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import dedup
import parser


def make(text: str, **kw) -> parser.Cargo:
    c = parser.parse(text, source=kw.pop("source", "grp1"), today=date(2026, 9, 20))
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def save(db, c: parser.Cargo) -> int:
    cargo_id = db.insert_cargo(c, dedup.fingerprint(c))
    assert cargo_id is not None
    return cargo_id


# ---------------------------------------------------------------- fingerprint

def test_fingerprint_is_stable():
    c = make("Есть груз Ташкент → Москва, 20т тент, 4000$, завтра")
    assert dedup.fingerprint(c) == dedup.fingerprint(c)
    assert len(dedup.fingerprint(c)) == 20


def test_same_ad_different_wording_same_fingerprint():
    """Bir xil yuk, boshqacha yozilgan — kalit bir xil bo'lishi kerak."""
    a = make("Есть груз Ташкент → Москва, 20 тонн, тент, 4000$, завтра")
    b = make("Нужна машина Ташкент - Москва 20т тент 4000 долларов завтра")
    assert dedup.fingerprint(a) == dedup.fingerprint(b)


def test_different_route_different_fingerprint():
    a = make("Есть груз Ташкент → Москва, 20т тент, 4000$, завтра")
    b = make("Есть груз Ташкент → Казань, 20т тент, 4000$, завтра")
    assert dedup.fingerprint(a) != dedup.fingerprint(b)


def test_small_rate_difference_is_same_bucket():
    """Stavka 50$ aniqlikda yaxlitlanadi: 4000 va 4020 — bitta yuk."""
    a = make("Есть груз Ташкент → Москва, 20т тент, 4000$, завтра")
    b = make("Есть груз Ташкент → Москва, 20т тент, 4020$, завтра")
    assert dedup.fingerprint(a) == dedup.fingerprint(b)


def test_big_rate_difference_is_another_cargo():
    a = make("Есть груз Ташкент → Москва, 20т тент, 4000$, завтра")
    b = make("Есть груз Ташкент → Москва, 20т тент, 5000$, завтра")
    assert dedup.fingerprint(a) != dedup.fingerprint(b)


# ---------------------------------------------------------------- is_duplicate

def test_exact_repost_is_duplicate(clean_db):
    a = make("Есть груз Ташкент → Москва, 20т тент, 4000$, завтра, тел 998901112233")
    cargo_id = save(clean_db, a)

    b = make("Есть груз Ташкент → Москва, 20т тент, 4000$, завтра, тел 998901112233",
             source="grp2")
    is_dup, existing = dedup.is_duplicate(b)
    assert is_dup is True
    assert existing == cargo_id


def test_same_phone_same_route_is_duplicate(clean_db):
    """Bir ekspeditor, bir yo'nalish, bir kun — stavka boshqacha yozilgan
    bo'lsa ham (so'm/dollar) bu o'sha yukning o'zi."""
    a = make("Есть груз Ташкент → Москва, 20т тент, 4000$, завтра, тел 998901112233")
    cargo_id = save(clean_db, a)

    b = make("Ташкент Москва фура срочно 42 млн сум завтра, тел 998901112233",
             source="grp2")
    assert dedup.fingerprint(a) != dedup.fingerprint(b)      # kalitlar boshqa
    is_dup, existing = dedup.is_duplicate(b)
    assert (is_dup, existing) == (True, cargo_id)


def test_same_phone_other_day_is_not_duplicate(clean_db):
    """Bir ekspeditor har hafta shu yo'nalishga yuk chiqaradi — keyingi
    kunga qo'yilgani yangi yuk, uni tashlab yuborish mumkin emas."""
    save(clean_db, make("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09,"
                        " тел 998901112233"))
    later = make("Есть груз Ташкент → Москва, 20т тент, 4000$, 28.09,"
                 " тел 998901112233")
    assert dedup.is_duplicate(later)[0] is False


def test_forwarded_text_is_duplicate(clean_db):
    """Forward qilingan e'lon: matn deyarli bir xil, telefon yo'q."""
    text = ("СРОЧНО ЕСТЬ ГРУЗ Ташкент - Москва 20 тонн тент ставка 4000 "
            "долларов загрузка завтра пишите в личку")
    a = make(text)
    cargo_id = save(clean_db, a)

    b = make(text + " 🔥", source="grp2")
    b.phone = None
    is_dup, existing = dedup.is_duplicate(b)
    assert (is_dup, existing) == (True, cargo_id)


def test_different_cargo_is_not_duplicate(clean_db):
    save(clean_db, make("Есть груз Ташкент → Москва, 20т тент, 4000$, завтра"))
    other = make("Есть груз Самарканд → Казань, 18т реф +2, 3800$, завтра")
    assert dedup.is_duplicate(other) == (False, None)


def test_same_route_other_date_is_not_duplicate(clean_db):
    """Bir xil yo'nalish, lekin boshqa kunga — bu boshqa yuk."""
    save(clean_db, make("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09"))
    later = make("Есть груз Ташкент → Москва, 20т тент, 4000$, 28.09")
    later.phone = None
    assert dedup.is_duplicate(later)[0] is False


def test_old_cargo_outside_window_is_not_duplicate(clean_db):
    """36 soatlik oynadan tashqaridagi yuk dubl hisoblanmaydi —
    o'sha yo'nalish yana chiqsa bu yangi yuk."""
    a = make("Есть груз Ташкент → Москва, 20т тент, 4000$, завтра")
    cargo_id = save(clean_db, a)
    old = (datetime.now() - timedelta(hours=72)).isoformat(" ", "seconds")
    with clean_db.connect() as conn:
        conn.execute("UPDATE cargos SET created_at=? WHERE id=?", (old, cargo_id))

    assert dedup.is_duplicate(a, window_hours=36)[0] is False


def test_duplicate_is_not_written_twice(clean_db):
    """Baza darajasida ham himoya bor: fingerprint UNIQUE."""
    a = make("Есть груз Ташкент → Москва, 20т тент, 4000$, завтра")
    save(clean_db, a)
    assert clean_db.insert_cargo(a, dedup.fingerprint(a)) is None
