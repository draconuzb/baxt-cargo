"""
test_settings.py — panel orqali o'zgartiriladigan sozlamalar (TZ 5.7).

Mezon: sozlama o'zgarsa qayta ishga tushirish shart emas.
"""
from __future__ import annotations

import config
import pipeline
import scoring
import settings


def cargo() -> dict:
    return {"id": 1, "from_city": "Toshkent", "to_city": "Moskva",
            "weight_t": 20.0, "body_type": "tent", "rate": 4000.0,
            "currency": "USD", "rate_usd": 4000.0, "load_date": "2026-09-22"}


def test_defaults_come_from_config(clean_db):
    assert settings.current_costs() == config.COSTS


def test_saved_value_is_applied(clean_db):
    assert settings.save({"fuel_price_usd": "1.20"}) == {}
    assert settings.current_costs().fuel_price_usd == 1.20
    # boshqa qiymatlar o'zgarmaydi
    assert settings.current_costs().border_usd == config.COSTS.border_usd


def test_config_object_is_not_mutated(clean_db):
    """`config.py` qiymatlari boshlang'ich nuqta bo'lib qoladi."""
    before = config.COSTS.fuel_price_usd
    settings.save({"fuel_price_usd": "2.0"})
    assert config.COSTS.fuel_price_usd == before


def test_scoring_uses_new_value_without_restart(clean_db, truck_tent):
    """Dizel qimmatlashsa — marja kamayadi, dasturni qayta yoqmasdan."""
    before = scoring.evaluate(cargo(), truck_tent)["margin_usd"]
    settings.save({"fuel_price_usd": "1.50"})
    after = scoring.evaluate(cargo(), truck_tent)["margin_usd"]
    assert after < before


def test_other_process_sees_change_after_ttl(clean_db, monkeypatch):
    """Boshqa process (bot, listener) kesh eskirgach yangi qiymatni ko'radi."""
    settings.overrides()                           # keshga tushdi
    with clean_db.connect() as conn:               # "boshqa process" yozdi
        conn.execute("INSERT INTO settings (key, value) VALUES ('border_usd', '300')")
    assert settings.current_costs().border_usd == config.COSTS.border_usd  # hali kesh

    monkeypatch.setattr(settings, "CACHE_TTL_SEC", 0)
    assert settings.current_costs().border_usd == 300.0


def test_invalid_number_is_rejected(clean_db):
    errors = settings.save({"fuel_price_usd": "arzon"})
    assert "fuel_price_usd" in errors
    assert settings.current_costs().fuel_price_usd == config.COSTS.fuel_price_usd


def test_out_of_range_is_rejected(clean_db):
    """95 o'rniga 0.95 — ko'p uchraydigan xato, hisobni buzmasligi kerak."""
    errors = settings.save({"fuel_price_usd": "95"})
    assert "fuel_price_usd" in errors


def test_partial_errors_save_nothing(clean_db):
    """Biri xato bo'lsa — hech narsa saqlanmaydi (yarim sozlama xavfli)."""
    errors = settings.save({"fuel_price_usd": "1.1", "border_usd": "-5"})
    assert "border_usd" in errors
    assert settings.current_costs().fuel_price_usd == config.COSTS.fuel_price_usd


def test_unknown_keys_are_ignored(clean_db):
    assert settings.save({"hacker_field": "1", "fuel_price_usd": "1.0"}) == {}
    assert "hacker_field" not in settings.overrides()


def test_comma_decimal(clean_db):
    settings.save({"fuel_price_usd": "1,05"})
    assert settings.current_costs().fuel_price_usd == 1.05


def test_reset_to_defaults(clean_db):
    settings.save({"fuel_price_usd": "1.5", "border_usd": "200"})
    settings.reset(["fuel_price_usd"])
    assert settings.current_costs().fuel_price_usd == config.COSTS.fuel_price_usd
    assert settings.current_costs().border_usd == 200.0
    settings.reset()
    assert settings.current_costs() == config.COSTS


def test_notify_threshold_setting(clean_db):
    assert settings.notify_threshold(65.0) == 65.0
    settings.save({"notify_threshold": "80"})
    assert settings.notify_threshold(65.0) == 80.0


def test_pipeline_respects_threshold(clean_db, truck_tent, monkeypatch):
    """Chegara panelda 100 qilinsa — kartochka ketmaydi."""
    sent = []
    monkeypatch.setattr("notifier.send", lambda *a, **kw: sent.append(a))
    clean_db.upsert_truck(truck_tent)
    settings.save({"notify_threshold": "100"})
    pipeline.handle_message("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09")
    assert sent == []


def test_integer_field(clean_db):
    settings.save({"date_tolerance_days": "3"})
    assert settings.current_costs().date_tolerance_days == 3


def test_current_values_for_form(clean_db):
    values = settings.current_values(65.0)
    assert values["fuel_price_usd"] == config.COSTS.fuel_price_usd
    assert values["notify_threshold"] == 65.0
