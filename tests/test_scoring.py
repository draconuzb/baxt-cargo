"""
test_scoring.py — marja va ball hisobi.

Eng muhim test — `test_daily_margin_wins_over_total`: loyihaning asosiy
tamoyili (eng qimmat yuk ≠ eng foydali yuk) shu yerda qotirilgan. Bu test
buzilsa — tizimning ma'nosi yo'qoladi, formulani o'zgartirmang.
"""
from __future__ import annotations

import config
import geo
import scoring


def cargo(**kw) -> dict:
    """Standart yuk; kerakli maydonlar kw bilan almashtiriladi."""
    base = {
        "id": 1, "from_city": "Toshkent", "to_city": "Moskva",
        "weight_t": 20.0, "body_type": "tent", "temp_c": None,
        "rate": 4000.0, "currency": "USD", "rate_usd": 4000.0,
        "load_date": "2026-09-22", "status": "new",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- ASOSIY TAMOYIL

def test_daily_margin_wins_over_total(truck_tent):
    """TZ 5.3: A = 5000$ lekin 855 km bo'sh probeg, B = 1800$ bo'sh probegsiz.

    A ko'proq pul qoldiradi, lekin B kuniga ko'proq qoldiradi — tizim B ni
    yuqori baholashi SHART. Mashina resursi pul emas, vaqt.
    """
    # bo'sh probeg chegarasi kengaytirilgan: aks holda A qat'iy filtrda
    # umuman ko'rilmaydi va taqqoslash ma'nosiz bo'lib qoladi
    costs = config.Costs(max_empty_km=1000.0)

    far_expensive = cargo(id=1, from_city="Almaty", to_city="Moskva",
                          rate=5000.0, rate_usd=5000.0, load_date="2026-09-23")
    near_cheap = cargo(id=2, from_city="Toshkent", to_city="Almaty",
                       rate=1800.0, rate_usd=1800.0, load_date="2026-09-21")

    a = scoring.evaluate(far_expensive, truck_tent, costs)
    b = scoring.evaluate(near_cheap, truck_tent, costs)

    assert a["ok"] and b["ok"]
    assert a["empty_km"] > 800 and b["empty_km"] == 0
    # A jami ko'proq foyda beradi...
    assert a["margin_usd"] > b["margin_usd"]
    # ...lekin kuniga kamroq
    assert b["margin_per_day"] > a["margin_per_day"]
    # demak ball ham B da yuqori
    assert b["score"] > a["score"], (
        f"Asosiy tamoyil buzildi: A={a['score']} ({a['margin_per_day']}$/kun), "
        f"B={b['score']} ({b['margin_per_day']}$/kun)"
    )


def test_daily_margin_wins_with_default_costs(truck_tent, costs):
    """Xuddi shu tamoyil standart sozlamalarda ham ishlaydi."""
    far = cargo(id=1, from_city="Buxoro", to_city="Moskva",
                rate=3000.0, rate_usd=3000.0, load_date="2026-09-23")
    near = cargo(id=2, from_city="Toshkent", to_city="Almaty",
                 rate=1200.0, rate_usd=1200.0, load_date="2026-09-21")

    a = scoring.evaluate(far, truck_tent, costs)
    b = scoring.evaluate(near, truck_tent, costs)

    assert a["margin_usd"] > b["margin_usd"]
    assert b["margin_per_day"] > a["margin_per_day"]
    assert b["score"] > a["score"]


def test_empty_run_lowers_score(truck_tent, costs):
    """Bir xil yuk, bir xil stavka — bo'sh probeg qancha ko'p, ball shuncha past."""
    near = cargo(id=1, from_city="Toshkent", to_city="Moskva")
    far = cargo(id=2, from_city="Buxoro", to_city="Moskva")

    r_near = scoring.evaluate(near, truck_tent, costs)
    r_far = scoring.evaluate(far, truck_tent, costs)

    assert r_near["empty_km"] < r_far["empty_km"]
    assert r_near["score"] > r_far["score"]


# ---------------------------------------------------------------- qat'iy filtrlar

def test_overweight_rejected(truck_tent, costs):
    r = scoring.evaluate(cargo(weight_t=25.0), truck_tent, costs)
    assert r["ok"] is False
    assert any("грузоподъёмность" in x for x in r["reasons"])


def test_weight_tolerance_allows_half_ton(truck_tent, costs):
    """22.5 t 22 t li mashinaga sig'adi deb hisoblanadi (+0.5 t bag'rikenglik)."""
    assert scoring.evaluate(cargo(weight_t=22.5), truck_tent, costs)["ok"] is True


def test_ref_cargo_rejected_for_tent_truck(truck_tent, costs):
    r = scoring.evaluate(cargo(body_type="ref", temp_c=-18.0), truck_tent, costs)
    assert r["ok"] is False
    assert any("рефрижератор" in x for x in r["reasons"])


def test_tent_cargo_allowed_on_ref_truck(truck_ref, costs):
    """TZ 4.1: tent yukni ref mashinaga yuklash mumkin — qat'iy taqiq emas."""
    r = scoring.evaluate(cargo(body_type="tent", weight_t=19.0), truck_ref, costs)
    assert r["ok"] is True


def test_temp_below_truck_range(truck_ref, costs):
    """Mashina -22° gacha sovutadi, yuk -25° talab qiladi."""
    r = scoring.evaluate(cargo(body_type="ref", temp_c=-25.0, weight_t=19.0),
                         truck_ref, costs)
    assert r["ok"] is False
    assert any("ниже" in x for x in r["reasons"])


def test_temp_above_truck_range(truck_ref, costs):
    r = scoring.evaluate(cargo(body_type="ref", temp_c=20.0, weight_t=19.0),
                         truck_ref, costs)
    assert r["ok"] is False
    assert any("выше" in x for x in r["reasons"])


def test_special_body_must_match(truck_tent, costs):
    r = scoring.evaluate(cargo(body_type="tral", weight_t=20.0), truck_tent, costs)
    assert r["ok"] is False
    assert any("кузова" in x for x in r["reasons"])


def test_empty_run_limit(truck_tent, costs):
    """Bo'sh probeg chegaradan uzoq bo'lsa — yuk umuman ko'rsatilmaydi."""
    far = cargo(from_city="Almaty", to_city="Moskva")   # ~855 km bo'sh
    assert geo.road_km("Toshkent", "Almaty") > costs.max_empty_km
    r = scoring.evaluate(far, truck_tent, costs)
    assert r["ok"] is False
    assert any("пустой пробег" in x for x in r["reasons"])


def test_truck_cannot_arrive_in_time(truck_tent, costs):
    """Mashina 20-sentabrda bo'shaydi, yuk 21-avgustda yuklanadi — ulgurmaydi."""
    r = scoring.evaluate(cargo(load_date="2026-08-21"), truck_tent, costs)
    assert r["ok"] is False
    assert any("не успевает" in x for x in r["reasons"])


def test_unknown_city_is_not_a_crash(truck_tent, costs):
    r = scoring.evaluate(cargo(to_city="Qandaydir Shahar"), truck_tent, costs)
    assert r["ok"] is False
    assert "нет координат города" in r["reasons"]


# ---------------------------------------------------------------- xarajat hisobi

def test_cost_components_add_up(truck_tent, costs):
    r = scoring.evaluate(cargo(), truck_tent, costs)
    parts = (r["fuel_cost"] + r["border_cost"] + r["driver_cost"]
             + r["road_cost"] + r["fixed_cost"])
    assert abs(parts - r["total_cost"]) <= 2      # yaxlitlash farqi


def test_fuel_follows_truck_consumption(truck_tent, costs):
    r = scoring.evaluate(cargo(), truck_tent, costs)
    assert r["fuel_l"] == round(r["total_km"] / 100 * truck_tent["fuel_l_100km"])


def test_margin_is_revenue_minus_cost(truck_tent, costs):
    r = scoring.evaluate(cargo(rate_usd=4000.0), truck_tent, costs)
    assert r["margin_usd"] == round(4000.0 - r["total_cost"])


def test_trip_days_formula(truck_tent, costs):
    r = scoring.evaluate(cargo(), truck_tent, costs)
    expected = r["total_km"] / (costs.avg_speed_kmh * costs.driving_hours_per_day) + 1
    assert abs(r["trip_days"] - expected) < 0.2


def test_borders_counted_on_both_legs(truck_tent, costs):
    """Bo'sh yurishdagi chegara ham hisobga olinadi."""
    r = scoring.evaluate(cargo(from_city="Shymkent", to_city="Moskva"),
                         truck_tent, costs)
    assert r["borders"] == 2          # Toshkent→Shymkent + Shymkent→Moskva
    assert r["border_cost"] == 2 * costs.border_usd


def test_uzs_rate_converted_to_usd(truck_tent, costs):
    """Stavka so'mda berilsa ham hisob USD da ketadi."""
    c = cargo(rate=42_000_000.0, currency="UZS", rate_usd=None)
    r = scoring.evaluate(c, truck_tent, costs)
    assert r["revenue_usd"] == round(config.to_usd(42_000_000.0, "UZS"))


# ---------------------------------------------------------------- ball

def test_no_rate_scores_without_margin(truck_tent, costs):
    """Stavka yo'q — marja va stavka mezonlari hisobdan chiqadi, ball qoladi."""
    r = scoring.evaluate(cargo(rate=None, currency=None, rate_usd=None),
                         truck_tent, costs)
    assert r["ok"] is True
    assert r["margin_usd"] is None
    assert "marja" not in r["score_parts"]
    assert "stavka" not in r["score_parts"]
    assert 0 < r["score"] <= 100
    assert any("ставка" in w for w in r["warnings"])


def test_score_within_bounds(truck_tent, costs):
    for rate in (500.0, 2000.0, 4000.0, 20000.0):
        r = scoring.evaluate(cargo(rate=rate, rate_usd=rate), truck_tent, costs)
        if r["ok"]:
            assert 0 <= r["score"] <= 100


def test_preferred_direction_bonus(truck_tent, costs):
    """Yo'nalish mashina afzalligiga mos bo'lsa ball yuqoriroq."""
    to_ru = {**truck_tent, "preferred_dir": "RU"}
    to_kz = {**truck_tent, "preferred_dir": "KZ"}
    c = cargo(to_city="Moskva")
    assert scoring.evaluate(c, to_ru, costs)["score_parts"]["yonalish"] == 15.0
    assert scoring.evaluate(c, to_kz, costs)["score_parts"]["yonalish"] == 5.0


def test_late_date_scores_lower(truck_tent, costs):
    """Uzoq kutish — mashina bekor turadi, ball pasayadi."""
    soon = scoring.evaluate(cargo(load_date="2026-09-22"), truck_tent, costs)
    late = scoring.evaluate(cargo(load_date="2026-10-15"), truck_tent, costs)
    assert soon["score_parts"]["sana"] > late["score_parts"]["sana"]


# ---------------------------------------------------------------- tanlov

def test_best_trucks_sorted_and_filtered(truck_tent, truck_ref, costs):
    """Mos kelmaydigan mashina ro'yxatga tushmaydi, qolganlari ball bo'yicha."""
    ref_cargo = cargo(body_type="ref", temp_c=-18.0, weight_t=19.0)
    best = scoring.best_trucks(ref_cargo, [truck_tent, truck_ref], costs)
    assert [r["truck_id"] for r in best] == ["02"]      # tent tushib qoldi


def test_best_cargos_returns_top_n(truck_tent, costs):
    cargos = [cargo(id=i, rate=1500.0 + i * 500, rate_usd=1500.0 + i * 500)
              for i in range(1, 6)]
    best = scoring.best_cargos(truck_tent, cargos, costs, top=3)
    assert len(best) == 3
    assert best == sorted(best, key=lambda r: r["score"], reverse=True)


def test_best_roundtrip_chain(truck_tent, costs):
    """Qaytish yuki zanjir foydasini oshiradi va bo'sh qaytishni oldini oladi."""
    out = cargo(id=1, from_city="Toshkent", to_city="Moskva",
                rate=4000.0, rate_usd=4000.0)
    back = cargo(id=2, from_city="Moskva", to_city="Toshkent",
                 rate=3500.0, rate_usd=3500.0, load_date="2026-10-05")
    chains = scoring.best_roundtrip(out, truck_tent, [back], costs)

    assert len(chains) == 1
    ch = chains[0]
    assert ch["leg2"]["empty_km"] == 0          # Moskvada turibdi, o'sha yerdan yuk
    assert ch["total_margin_usd"] == ch["leg1"]["margin_usd"] + ch["leg2"]["margin_usd"]
    assert ch["margin_per_day"] == round(ch["total_margin_usd"] / ch["total_days"])


def test_roundtrip_skips_same_cargo(truck_tent, costs):
    """Bir xil yuk qaytish yuki sifatida taklif qilinmaydi."""
    out = cargo(id=7)
    assert scoring.best_roundtrip(out, truck_tent, [cargo(id=7)], costs) == []
