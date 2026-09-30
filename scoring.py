"""
scoring.py — loyihaning yuragi.

Har bir yuk + har bir mashina juftligi uchun: bo'sh probeg, umumiy masofa,
yoqilg'i, xarajatlar, tushum va marja hisoblanadi. Keyin 0–100 ball beriladi.

Muhim: ball eng qimmat yukka emas, aynan shu mashina uchun eng foydali
yukka beriladi. 5000$ li Moskva yuki 800 km bo'sh yurish bilan 3000$ li
yaqin yukdan yomonroq bo'lishi mumkin.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import config
import geo
import settings


def _as_dict(obj) -> dict:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "keys"):          # sqlite3.Row
        return {k: obj[k] for k in obj.keys()}
    return obj.to_dict() if hasattr(obj, "to_dict") else vars(obj)


def _as_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v)[:10]).date()
    except ValueError:
        return None


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def market_rate(from_city: str | None, to_city: str | None, costs) -> float:
    """Shu yo'nalish uchun bozor stavkasi ($/km).

    Toshkent→Moskva va Toshkent→Almaty stavkasi bir xil emas, shuning
    uchun yig'ilgan e'lonlardan hisoblangan mediana ishlatiladi. Ma'lumot
    yetarli bo'lmasa (yoki baza hali bo'sh bo'lsa) — `config` dagi
    umumiy qiymat. Statistika modulidagi xato ball hisobini to'xtatmaydi.
    """
    try:
        import analytics
        rate = analytics.route_rate(from_city, to_city)
        if rate:
            return rate
    except Exception:
        pass
    return costs.market_rate_per_km


# ---------------------------------------------------------------- filtrlar

def hard_checks(c: dict, t: dict, costs) -> list[str]:
    """Umuman mos kelmaydigan sabablar ro'yxati (bo'sh bo'lsa — mos)."""
    fails = []

    if c.get("weight_t") and t.get("capacity_t") and c["weight_t"] > t["capacity_t"] + 0.5:
        fails.append(f"vazn {c['weight_t']}t > sig'im {t['capacity_t']}t")

    needs_ref = c.get("temp_c") is not None or c.get("body_type") == "ref"
    if needs_ref and t.get("body_type") not in ("ref",):
        fails.append("yuk refrijerator talab qiladi")

    if c.get("body_type") in ("tral", "konteyner", "samosval") \
            and t.get("body_type") != c["body_type"]:
        fails.append(f"kuzov turi mos emas ({c['body_type']})")

    temp = c.get("temp_c")
    if temp is not None:
        lo, hi = t.get("temp_min"), t.get("temp_max")
        if lo is not None and temp < lo:
            fails.append(f"harorat {temp}° mashina imkoniyatidan past")
        if hi is not None and temp > hi:
            fails.append(f"harorat {temp}° mashina imkoniyatidan yuqori")

    return fails


# ---------------------------------------------------------------- hisob

def evaluate(cargo, truck, costs=None, ignore_date: bool = False) -> dict:
    """ignore_date=True — qaytish yukini baholashda ishlatiladi: u yerda
    aniq e'lonning sanasi emas, o'sha yo'nalishdagi bozor stavkasi muhim."""
    costs = costs or settings.current_costs()
    c, t = _as_dict(cargo), _as_dict(truck)

    res: dict = {
        "ok": False, "score": 0.0, "reasons": [], "warnings": [],
        "truck_id": t.get("id"), "cargo_id": c.get("id"),
    }

    empty_km = geo.road_km(t.get("current_city"), c.get("from_city"), costs.road_factor)
    loaded_km = geo.road_km(c.get("from_city"), c.get("to_city"), costs.road_factor)
    if empty_km is None or loaded_km is None:
        res["reasons"].append("shahar koordinatasi topilmadi")
        return res

    fails = hard_checks(c, t, costs)
    if empty_km > costs.max_empty_km:
        fails.append(f"bo'sh probeg juda uzoq ({empty_km:.0f} km)")

    # --- vaqt: mashina yuklashga yetib boradimi?
    free = _as_date(t.get("free_date")) or date.today()
    load_date = _as_date(c.get("load_date"))
    travel_days = empty_km / (costs.avg_speed_kmh * costs.driving_hours_per_day)
    arrival = free + timedelta(days=math.ceil(travel_days))
    slack_days = (load_date - arrival).days if load_date else None
    if not ignore_date and slack_days is not None \
            and slack_days < -costs.date_tolerance_days:
        fails.append(f"yuklashga ulgurmaydi ({arrival:%d.%m} da yetadi)")

    # --- masofa va yoqilg'i
    total_km = empty_km + loaded_km
    fuel_rate = t.get("fuel_l_100km") or 33.0
    fuel_l = total_km / 100 * fuel_rate
    fuel_cost = fuel_l * costs.fuel_price_usd

    borders = geo.border_crossings(t.get("current_city"), c.get("from_city")) \
        + geo.border_crossings(c.get("from_city"), c.get("to_city"))
    border_cost = borders * costs.border_usd
    driver_cost = total_km * costs.driver_usd_per_km
    road_cost = total_km * costs.road_usd_per_km
    total_cost = fuel_cost + border_cost + driver_cost + road_cost + costs.fixed_usd

    revenue = c.get("rate_usd")
    if revenue is None:
        revenue = config.to_usd(c.get("rate"), c.get("currency"))

    trip_days = max(1.0, (total_km / (costs.avg_speed_kmh * costs.driving_hours_per_day)) + 1)

    res.update({
        "empty_km": round(empty_km),
        "loaded_km": round(loaded_km),
        "total_km": round(total_km),
        "fuel_l": round(fuel_l),
        "fuel_cost": round(fuel_cost),
        "border_cost": round(border_cost),
        "borders": borders,
        "driver_cost": round(driver_cost),
        "road_cost": round(road_cost),
        "fixed_cost": round(costs.fixed_usd),
        "total_cost": round(total_cost),
        "revenue_usd": round(revenue) if revenue else None,
        "trip_days": round(trip_days, 1),
        "arrival_date": arrival.isoformat(),
        "slack_days": slack_days,
        "empty_ratio": round(empty_km / max(loaded_km, 1), 3),
    })

    if revenue:
        margin = revenue - total_cost
        res["margin_usd"] = round(margin)
        res["margin_per_km"] = round(margin / max(total_km, 1), 3)
        res["margin_per_day"] = round(margin / trip_days)
        res["rate_per_km"] = round(revenue / max(loaded_km, 1), 3)
    else:
        res["margin_usd"] = None
        res["margin_per_day"] = None
        res["rate_per_km"] = None
        res["warnings"].append("stavka ko'rsatilmagan — marja hisoblanmadi")

    # Kompaniya qoidalari (rules.py) — marjaga tegmaydi, faqat to'sadi yoki
    # ballni suradi. Qoidalar modulidagi xato hisobni to'xtatmaydi.
    rule_fails, rule_delta, rule_notes = _company_rules(c, t, res)
    fails += rule_fails
    if rule_notes:
        res["rules"] = rule_notes

    if fails:
        res["reasons"] = fails
        return res

    res["ok"] = True
    res["estimate_only"] = ignore_date
    res["score"], res["score_parts"] = _score(res, c, t, costs, ignore_date)
    if rule_delta:
        res["score"] = round(_clamp(res["score"] + rule_delta, 0, 100), 1)
        res["score_parts"]["qoidalar"] = round(rule_delta, 1)
    return res


def _company_rules(c: dict, t: dict, res: dict) -> tuple[list[str], float, list[str]]:
    try:
        import rules
        return rules.apply(c, t, res)
    except Exception:
        import logging
        logging.getLogger("scoring").exception("Qoidalarni qo'llashda xato")
        return [], 0.0, []


def _score(r: dict, c: dict, t: dict, costs, ignore_date: bool = False) -> tuple[float, dict]:
    parts: dict[str, float] = {}
    max_parts: dict[str, float] = {}

    # 1. Kunlik marja — eng og'irlikdagi mezon (40)
    if r["margin_per_day"] is not None:
        parts["marja"] = 40 * _clamp(r["margin_per_day"] / costs.target_margin_per_day)
        max_parts["marja"] = 40

    # 2. Bo'sh probeg (20): 0% -> to'liq ball, 35%+ -> 0
    parts["bosh_probeg"] = 20 * (1 - _clamp(r["empty_ratio"] / 0.35))
    max_parts["bosh_probeg"] = 20

    # 3. Stavka bozorga nisbatan (15)
    if r["rate_per_km"] is not None:
        market = market_rate(c.get("from_city"), c.get("to_city"), costs)
        r["market_rate_per_km"] = market
        parts["stavka"] = 15 * _clamp(r["rate_per_km"] / market)
        max_parts["stavka"] = 15

    # 4. Yo'nalish mosligi (15)
    pref = (t.get("preferred_dir") or "").strip()
    to_city = c.get("to_city") or ""
    if not pref:
        parts["yonalish"] = 10.0
    else:
        city = geo.CITIES.get(to_city)
        match = pref.upper() == (city.country if city else "") or pref == to_city
        parts["yonalish"] = 15.0 if match else 5.0
    max_parts["yonalish"] = 15

    # 5. Sana mosligi (10)
    s = r["slack_days"]
    if ignore_date:
        pass
    elif s is None:
        parts["sana"] = 5.0
    elif -costs.date_tolerance_days <= s <= 2:
        parts["sana"] = 10.0
    elif s <= 5:
        parts["sana"] = 7.0
    else:
        parts["sana"] = 4.0   # uzoq kutish — mashina bekor turadi
    if not ignore_date:
        max_parts["sana"] = 10

    raw = sum(parts.values())
    available = sum(max_parts.values())
    score = round(raw / available * 100, 1) if available else 0.0
    return score, {k: round(v, 1) for k, v in parts.items()}


# ---------------------------------------------------------------- eng yaxshi

def best_trucks(cargo, trucks, costs=None, top: int = 3) -> list[dict]:
    """Bitta yuk uchun mashinalarni ballab, eng yaxshilarini qaytaradi."""
    out = [evaluate(cargo, t, costs) for t in trucks]
    ok = [r for r in out if r["ok"]]
    ok.sort(key=lambda r: r["score"], reverse=True)
    return ok[:top]


def best_cargos(truck, cargos, costs=None, top: int = 5) -> list[dict]:
    """Bitta mashina uchun eng foydali yuklar."""
    out = [evaluate(c, truck, costs) for c in cargos]
    ok = [r for r in out if r["ok"]]
    ok.sort(key=lambda r: r["score"], reverse=True)
    return ok[:top]


# ---------------------------------------------------------------- qaytish yuki

def best_roundtrip(cargo, truck, candidates, costs=None, top: int = 3) -> list[dict]:
    """Tanlangan reysdan keyin qaytish yukini qidiradi va butun zanjir
    foydasini hisoblaydi (borish + qaytish)."""
    costs = costs or settings.current_costs()
    first = evaluate(cargo, truck, costs)
    if not first["ok"]:
        return []

    c = _as_dict(cargo)
    # birinchi reys tugagandan keyingi holat = virtual mashina
    virtual = {**_as_dict(truck),
               "current_city": c.get("to_city"),
               "free_date": (_as_date(c.get("load_date")) or date.today()
                             ) + timedelta(days=math.ceil(first["trip_days"]))}

    chains = []
    for back in candidates:
        b = _as_dict(back)
        if b.get("id") == c.get("id"):
            continue
        second = evaluate(back, virtual, costs, ignore_date=True)
        if not second["ok"] or second["margin_usd"] is None:
            continue
        total_margin = (first["margin_usd"] or 0) + second["margin_usd"]
        total_days = first["trip_days"] + second["trip_days"]
        chains.append({
            "back_cargo": b,
            "leg1": first,
            "leg2": second,
            "total_margin_usd": round(total_margin),
            "total_days": round(total_days, 1),
            "margin_per_day": round(total_margin / max(total_days, 1)),
            "total_km": first["total_km"] + second["total_km"],
            "empty_km": first["empty_km"] + second["empty_km"],
        })
    chains.sort(key=lambda x: x["margin_per_day"], reverse=True)
    return chains[:top]


# Eslatma: qaytish yuki hozirgi e'lonlar asosidagi BAHO. Mashina yetib
# borgunicha o'sha yuk ketib qolishi mumkin — bu raqam yo'nalish qanchalik
# foydali ekanini ko'rsatadi, kafolat emas.
