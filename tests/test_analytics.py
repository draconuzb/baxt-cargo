"""
test_analytics.py — statistika va o'z-o'zidan sozlanish (TZ 5.6).
"""
from __future__ import annotations

import itertools

import pytest

import analytics
import config
import db
import geo
import scoring


@pytest.fixture(autouse=True)
def fresh_cache():
    analytics.reset_cache()
    yield
    analytics.reset_cache()


_counter = itertools.count(1)


def add_cargo(source="grp1", from_city="Toshkent", to_city="Moskva",
              rate_usd=4000.0, status="new") -> int:
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO cargos (fingerprint, from_city, to_city, rate_usd,
                                   rate, currency, source, status, confidence, raw_text)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (f"fp-{next(_counter)}", from_city, to_city, rate_usd, rate_usd,
             "USD", source, status, 0.9, "test"))
        return cur.lastrowid


def add_match(cargo_id, truck_id="01", score=80.0, margin=1500.0,
              decision=None, actual=None) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO matches (cargo_id, truck_id, score, empty_km, loaded_km,
                                    margin_usd, decision, actual_margin_usd, notified)
               VALUES (?,?,?,?,?,?,?,?,1)""",
            (cargo_id, truck_id, score, 100, 3000, margin, decision, actual))
        return cur.lastrowid


# ---------------------------------------------------------------- bozor stavkasi

def test_route_rate_needs_enough_data(clean_db):
    """Ikkita e'londan stavka chiqarmaymiz — tasodif bo'lishi mumkin."""
    add_cargo(rate_usd=4000.0)
    add_cargo(rate_usd=4200.0)
    assert analytics.route_rate("Toshkent", "Moskva") is None


def test_route_rate_median(clean_db):
    km = geo.road_km("Toshkent", "Moskva")
    for rate in (4000.0, 4400.0, 4800.0):
        add_cargo(rate_usd=rate)

    rate_per_km = analytics.route_rate("Toshkent", "Moskva")
    assert rate_per_km == pytest.approx(4400.0 / km, abs=0.01)


def test_route_rate_ignores_absurd_values(clean_db):
    """Noto'g'ri tahlil qilingan stavka medianani buzmasligi kerak."""
    km = geo.road_km("Toshkent", "Moskva")
    for rate in (4000.0, 4400.0, 4800.0, 999_000.0):
        add_cargo(rate_usd=rate)
    assert analytics.route_rate("Toshkent", "Moskva") == pytest.approx(4400.0 / km,
                                                                      abs=0.01)


def test_country_rate_fallback(clean_db):
    """Aniq yo'nalish bo'yicha ma'lumot yo'q — davlat juftligi ishlatiladi."""
    for i, (a, b) in enumerate([("Toshkent", "Moskva"), ("Samarqand", "Qozon"),
                                ("Buxoro", "Samara"), ("Namangan", "Ufa"),
                                ("Andijon", "Penza")]):
        add_cargo(from_city=a, to_city=b, rate_usd=4000.0 + i)
    # bu yo'nalish bazada yo'q, lekin UZ->RU bo'yicha ma'lumot bor
    assert analytics.route_rate("Qarshi", "Voronej") is not None


def test_unknown_route_returns_none(clean_db):
    assert analytics.route_rate("Toshkent", "Moskva") is None
    assert analytics.route_rate(None, "Moskva") is None


def test_scoring_uses_route_rate(clean_db, truck_tent, costs):
    """TZ 5.6 mezoni: yetarli ma'lumot bo'lsa ball yo'nalish stavkasiga tayanadi."""
    km = geo.road_km("Toshkent", "Moskva")
    for rate in (7000.0, 7200.0, 7400.0):      # bu yo'nalishda bozor qimmat
        add_cargo(rate_usd=rate)
    analytics.reset_cache()

    cargo = {"id": 99, "from_city": "Toshkent", "to_city": "Moskva",
             "weight_t": 20.0, "body_type": "tent", "rate_usd": 4000.0,
             "rate": 4000.0, "currency": "USD", "load_date": "2026-09-22"}
    result = scoring.evaluate(cargo, truck_tent, costs)

    # bozor 7200$ bo'lsa, 4000$ li yuk arzon — stavka bali to'liq emas
    assert result["market_rate_per_km"] == pytest.approx(7200.0 / km, abs=0.01)
    assert result["score_parts"]["stavka"] < 15


def test_scoring_falls_back_to_config(clean_db, truck_tent, costs):
    """Baza bo'sh — eski xulq-atvor saqlanadi."""
    cargo = {"id": 99, "from_city": "Toshkent", "to_city": "Moskva",
             "weight_t": 20.0, "body_type": "tent", "rate_usd": 4000.0,
             "rate": 4000.0, "currency": "USD", "load_date": "2026-09-22"}
    result = scoring.evaluate(cargo, truck_tent, costs)
    assert result["market_rate_per_km"] == costs.market_rate_per_km


# ---------------------------------------------------------------- guruh sifati

def test_group_quality(clean_db):
    taken = add_cargo(source="grp_yaxshi", status="taken")
    add_match(taken, decision="taken")
    skipped = add_cargo(source="grp_yaxshi")
    add_match(skipped, decision="skipped")
    for _ in range(3):
        add_match(add_cargo(source="grp_yomon"), decision="skipped")

    groups = {g["source"]: g for g in analytics.group_quality()}
    assert groups["grp_yaxshi"]["taken"] == 1
    assert groups["grp_yaxshi"]["cargos"] == 2
    assert groups["grp_yaxshi"]["take_rate"] == 50.0
    assert groups["grp_yomon"]["taken"] == 0
    # foydali guruh ro'yxat boshida turadi
    assert analytics.group_quality()[0]["source"] == "grp_yaxshi"


# ---------------------------------------------------------------- ball sifati

def test_score_quality_detects_working_formula(clean_db):
    for _ in range(12):
        add_match(add_cargo(), score=85.0, decision="taken")
    for _ in range(12):
        add_match(add_cargo(), score=60.0, decision="skipped")

    result = analytics.score_quality()
    assert result["gap"] == 25.0
    assert "ishlayapti" in result["verdict"]


def test_score_quality_detects_broken_formula(clean_db):
    """Dispetcher past balli yuklarni olayotgan bo'lsa — formula noto'g'ri."""
    for _ in range(12):
        add_match(add_cargo(), score=55.0, decision="taken")
    for _ in range(12):
        add_match(add_cargo(), score=80.0, decision="skipped")

    result = analytics.score_quality()
    assert result["gap"] < 0
    assert "teskari" in result["verdict"]


def test_score_quality_without_data(clean_db):
    assert analytics.score_quality()["gap"] is None


def test_cancelled_matches_are_not_decisions(clean_db):
    """`cancelled` — tizimning tozalashi, dispetcher qarori emas."""
    for _ in range(3):
        add_match(add_cargo(), decision="cancelled")
    assert analytics.score_quality()["taken"] is None


# ---------------------------------------------------------------- marja aniqligi

def test_margin_accuracy(clean_db):
    """Har ikkala reysda ham haqiqat prognozdan 10% past — bu tasodif emas,
    xarajat parametrlari past olingan."""
    add_match(add_cargo(), margin=1000.0, actual=900.0, decision="taken")
    add_match(add_cargo(), margin=2000.0, actual=1800.0, decision="taken")

    result = analytics.margin_accuracy()
    assert result["n"] == 2
    assert result["avg_error_pct"] == 10.0
    assert result["bias_pct"] == -10.0
    assert "optimistik" in result["verdict"]      # doimiy og'ish ko'rsatiladi


def test_margin_accuracy_far_off(clean_db):
    add_match(add_cargo(), margin=1000.0, actual=400.0, decision="taken")
    result = analytics.margin_accuracy()
    assert result["avg_error_pct"] == 60.0
    assert "optimistik" in result["verdict"]


def test_margin_accuracy_within_target(clean_db):
    """Kichik va tasodifiy farq — hammasi joyida."""
    add_match(add_cargo(), margin=1000.0, actual=1020.0, decision="taken")
    add_match(add_cargo(), margin=1000.0, actual=980.0, decision="taken")
    verdict = analytics.margin_accuracy()["verdict"]
    assert "aniq" in verdict and "og'ish" not in verdict


def test_margin_accuracy_without_data(clean_db):
    assert analytics.margin_accuracy()["n"] == 0


# ---------------------------------------------------------------- hisobot

def test_totals(clean_db):
    add_cargo(status="taken")
    add_cargo(status="expired")
    add_cargo()
    t = analytics.totals()
    assert (t["cargos"], t["taken"], t["expired"]) == (3, 1, 1)


def test_route_stats(clean_db):
    for _ in range(3):
        add_cargo(from_city="Toshkent", to_city="Moskva")
    add_cargo(from_city="Buxoro", to_city="Qozon")

    routes = analytics.route_stats()
    assert routes[0]["from_city"] == "Toshkent"
    assert routes[0]["count"] == 3
    assert routes[0]["km"] > 0


def test_report_and_formats(clean_db):
    cargo_id = add_cargo(status="taken")
    add_match(cargo_id, decision="taken", actual=1400.0)
    rep = analytics.report(days=30)

    text = analytics.format_report(rep)
    assert "Статистика" in text
    analytics.print_report(rep)      # xato bermasligi kerak


def test_report_on_empty_db_does_not_crash(clean_db):
    rep = analytics.report(days=30)
    assert rep["totals"]["cargos"] == 0
    analytics.format_report(rep)
    analytics.print_report(rep)
