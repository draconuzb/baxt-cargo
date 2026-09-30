"""
test_rules.py — kompaniya qoidalari va o'rganish (rules.py, learn.py).

Qoida tokensiz, har bir yuk hisobida aniq qo'llanishi kerak — AI faqat
uni yozib qo'yadi. Shu yerda tekshiriladi.
"""
from __future__ import annotations

import pytest

import config
import db
import learn
import rules
import scoring


@pytest.fixture
def base(clean_db, truck_tent):
    clean_db.upsert_truck(truck_tent)
    rules.reset_cache()
    yield clean_db
    rules.reset_cache()


def cargo(**kw) -> dict:
    c = {"id": 1, "from_city": "Toshkent", "to_city": "Moskva", "load_date": "2026-09-21",
         "weight_t": 20.0, "body_type": "tent", "temp_c": None,
         "rate": 4000, "currency": "USD", "rate_usd": 4000}
    c.update(kw)
    return c


# ---------------------------------------------------------------- tekshirish

def test_normalize_canonical_values(base):
    rule, err = rules.normalize("block", {"from_country": "Россия", "to_city": "Москва"},
                                {"min_rate_usd": "2000"})
    assert err == ""
    assert rule["scope"] == {"from_country": "RU", "to_city": "Moskva"}
    assert rule["require"] == {"min_rate_usd": 2000.0}


@pytest.mark.parametrize("effect,scope,require", [
    ("delete_all", {}, {}),
    ("block", {}, {}),                                 # hammasini to'sib qo'yardi
    ("block", {"from_country": "Atlantida"}, {}),
    ("block", {"to_city": "Qwertyuiop"}, {}),
    ("block", {"truck_id": "99"}, {}),
    ("block", {"from_country": "RU"}, {"min_rate_usd": -5}),
    ("block", {"from_country": "RU"}, {"rate": 5}),
])
def test_normalize_rejects_bad_input(base, effect, scope, require):
    rule, err = rules.normalize(effect, scope, require)
    assert rule is None and err


def test_add_is_idempotent(base):
    a, _ = rules.add("block", {"to_country": "KZ"})
    b, _ = rules.add("block", {"to_country": "kazakhstan"})
    assert a == b
    assert len(rules.list_rules()) == 1


# ---------------------------------------------------------------- qo'llash

def test_block_rule_hides_cargo(base, truck_tent, costs):
    assert scoring.evaluate(cargo(), truck_tent, costs)["ok"]
    rules.add("block", {"to_country": "RU"})
    res = scoring.evaluate(cargo(), truck_tent, costs)
    assert not res["ok"]
    assert any("qoida #" in r for r in res["reasons"])


def test_min_price_rule_from_russia(base, truck_tent, costs):
    """"Rossiyadan 30 mln dan arzon yuk olma" — arzoni to'siladi, qimmati qoladi."""
    base.set_truck_position("01", city="Moskva")
    truck_tent = {**truck_tent, "current_city": "Moskva"}
    min_usd = config.to_usd(30_000_000, "UZS")
    rules.add("block", {"from_country": "RU"}, {"min_rate_usd": min_usd})
    cheap = cargo(from_city="Moskva", to_city="Toshkent",
                  rate=20_000_000, currency="UZS", rate_usd=config.to_usd(20_000_000, "UZS"))
    rich = cargo(from_city="Moskva", to_city="Toshkent",
                 rate=45_000_000, currency="UZS", rate_usd=config.to_usd(45_000_000, "UZS"))
    other = cargo(from_city="Toshkent", to_city="Moskva", rate=500, rate_usd=500)
    assert not scoring.evaluate(cheap, truck_tent, costs)["ok"]
    assert scoring.evaluate(rich, truck_tent, costs)["ok"]
    # qoida faqat Rossiyadan yuklarga tegishli
    assert "qoida" not in str(scoring.evaluate(other, truck_tent, costs)["reasons"])


def test_unknown_price_is_not_blocked(base, truck_tent, costs):
    """Stavka yozilmagan yukni taxmin bilan yashirmaymiz."""
    rules.add("block", {"to_country": "RU"}, {"min_rate_usd": 3000})
    res = scoring.evaluate(cargo(rate=None, currency=None, rate_usd=None), truck_tent, costs)
    assert res["ok"]


def test_boost_and_penalty_move_score(base, truck_tent, costs):
    plain = scoring.evaluate(cargo(), truck_tent, costs)["score"]
    rules.add("boost", {"to_city": "Moskva"}, points=10)
    boosted = scoring.evaluate(cargo(), truck_tent, costs)
    assert boosted["score"] == pytest.approx(min(100, plain + 10), abs=0.2)
    assert boosted["rules"]
    rules.add("penalty", {"body_type": "tent"}, points=30)
    both = scoring.evaluate(cargo(), truck_tent, costs)
    assert both["score"] == pytest.approx(max(0, min(100, plain + 10) - 30), abs=0.2) \
        or both["score"] < boosted["score"]


def test_rule_does_not_change_margin(base, truck_tent, costs):
    """Qoida ballga ta'sir qiladi, marjaga emas — asosiy tamoyil saqlanadi."""
    before = scoring.evaluate(cargo(), truck_tent, costs)
    rules.add("penalty", {"to_city": "Moskva"}, points=20)
    after = scoring.evaluate(cargo(), truck_tent, costs)
    assert after["margin_usd"] == before["margin_usd"]
    assert after["margin_per_day"] == before["margin_per_day"]


def test_truck_scoped_rule(base, truck_tent, truck_ref, costs):
    base.upsert_truck(truck_ref)
    rules.add("block", {"truck_id": "01", "to_country": "RU"})
    assert not scoring.evaluate(cargo(), truck_tent, costs)["ok"]
    assert scoring.evaluate(cargo(), truck_ref, costs)["ok"]


def test_inactive_rules_ignored(base, truck_tent, costs):
    rid, _ = rules.add("block", {"to_country": "RU"}, status="proposed", source="learned")
    assert scoring.evaluate(cargo(), truck_tent, costs)["ok"]
    rules.set_status(rid, "active")
    assert not scoring.evaluate(cargo(), truck_tent, costs)["ok"]


def test_scoring_survives_without_rules_table(isolate_db, truck_tent, costs):
    """Eski baza (jadval yo'q) — hisob to'xtamaydi."""
    rules.reset_cache()
    assert scoring.evaluate(cargo(), truck_tent, costs)["ok"]


def test_describe_is_human(base):
    rid, _ = rules.add("block", {"from_country": "RU"}, {"min_rate_usd": 2300})
    text = rules.describe(rules.get(rid))
    assert "Россия" in text and "2 300" in text


# ---------------------------------------------------------------- o'rganish

def _decide(n: int, decision: str, to_city: str = "Almaty", start: int = 0):
    for i in range(n):
        with db.connect() as conn:
            cid = conn.execute(
                "INSERT INTO cargos (fingerprint, from_city, to_city, body_type, rate_usd,"
                " raw_text) VALUES (?,?,?,?,?,?)",
                (f"fp{to_city}{decision}{start + i}", "Toshkent", to_city, "tent", 1500,
                 "x")).lastrowid
            conn.execute("INSERT INTO matches (cargo_id, truck_id, score, decision)"
                         " VALUES (?,?,?,?)", (cid, "01", 70, decision))


def test_learn_proposes_penalty_for_skipped_direction(base):
    _decide(8, "skipped", "Almaty")
    _decide(3, "taken", "Moskva")
    items = learn.propose()
    scopes = [i["rule"]["scope"] for i in items]
    assert {"to_country": "KZ"} in scopes or \
        {"from_city": "Toshkent", "to_city": "Almaty"} in scopes
    item = items[0]
    assert item["rule"]["status"] == "proposed"
    assert item["rule"]["effect"] == "penalty"
    assert "0 из 8" in item["why"]
    assert "закономерность" in learn.format_proposal(item)


def test_learn_does_not_repeat_rejected(base):
    _decide(8, "skipped", "Almaty")
    first = learn.propose(limit=10)
    for item in first:
        rules.set_status(item["rule"]["id"], "rejected")
    assert learn.propose(limit=10) == []


def test_learn_needs_enough_decisions(base):
    _decide(3, "skipped", "Almaty")
    assert learn.propose() == []


def test_learn_proposes_boost_for_taken(base):
    _decide(5, "taken", "Moskva")
    items = learn.propose(limit=10)
    assert any(i["rule"]["effect"] == "boost" for i in items)
