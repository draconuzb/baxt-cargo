"""
ai_tools.py — AI yuragining "qo'llari": model chaqira oladigan asboblar.

Model hech narsani o'zi hisoblamaydi va o'ylab topmaydi. U faqat qaysi
asbobni qanday argument bilan chaqirishni hal qiladi; raqamlarning hammasi
shu yerda — `scoring`, `search`, `rules` orqali — aniq hisoblanadi.

Har bir asbob:
  • oddiy Python funksiyasi (LLM'siz test qilinadi);
  • qisqa JSON qaytaradi — token tejaladi;
  • xato bo'lsa `{"error": ...}` qaytaradi, istisno tashlamaydi
    (model xatoni ko'rib, o'zi tuzatadi).

Muhim: yukni **olish** asbobi yo'q. Model yukni topib, taklif qiladi;
"✅ Беру" tugmasini odam bosadi (`actions.take_match`, CLAUDE.md 10-qoida).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date

import config
import db
import geo
import rules
import scoring

log = logging.getLogger("ai_tools")

CARGO_HOURS = 48
DEFAULT_TOP = 4              # bepul reja limiti: javob ixcham bo'lsin
MAX_TOP = 6
NEAR_RADIUS_KM = 300
_BODY_TYPES = rules.BODY_TYPES


@dataclass
class Ctx:
    """Bitta suhbat navbati davomida yig'iladigan narsalar (tugmalar uchun)."""
    chat_id: str = ""
    offers: list[dict] = field(default_factory=list)       # [{match_id, cargo_id, truck_id, label}]
    new_rules: list[int] = field(default_factory=list)
    proposals: list[dict] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)      # asbob javoblari (sifat sinovi uchun)

    def add_offer(self, match_id: int | None, cargo: dict, truck_id: str) -> None:
        if not match_id or any(o["match_id"] == match_id for o in self.offers):
            return
        self.offers.append({"match_id": match_id, "cargo_id": cargo["id"],
                            "truck_id": truck_id,
                            "label": f"{cargo['from_city']}→{cargo['to_city']}"})


# ---------------------------------------------------------------- yordamchi

def _row(r) -> dict:
    return {k: r[k] for k in r.keys()} if r is not None and not isinstance(r, dict) else r


def _city(value) -> str | None:
    return geo.lookup(str(value)) if value else None


def _truck_id(value) -> str | None:
    if value in (None, ""):
        return None
    tid = str(value).lstrip("№#").strip()
    return tid.zfill(2) if tid.isdigit() else tid


def _rate_text(c: dict) -> str | None:
    if not c.get("rate") or not c.get("currency"):
        return None
    if c["currency"] == "UZS":
        text = f"{c['rate'] / 1_000_000:g} mln UZS"
    else:
        text = f"{c['rate']:,.0f} {c['currency']}".replace(",", " ")
    if c.get("rate_usd") and c["currency"] != "USD":
        text += f" (~${c['rate_usd']:,.0f})".replace(",", " ")
    return text


def _cargo_brief(c: dict) -> dict:
    out = {"cargo_id": c["id"], "route": f"{c['from_city']} -> {c['to_city']}",
           "date": c.get("load_date"), "body": c.get("body_type"),
           "weight_t": c.get("weight_t"), "rate": _rate_text(c)}
    if c.get("temp_c") is not None:
        out["temp_c"] = c["temp_c"]
    if c.get("phone"):
        out["phone"] = c["phone"]
    return {k: v for k, v in out.items() if v not in (None, "")}


def _result_brief(r: dict) -> dict:
    out = {"truck": r.get("truck_id"), "empty_km": r.get("empty_km"),
           "total_km": r.get("total_km"), "days": r.get("trip_days"),
           "margin_usd": r.get("margin_usd"), "margin_per_day": r.get("margin_per_day"),
           "score": r.get("score")}
    if r.get("rules"):
        out["rules"] = r["rules"]
    return {k: v for k, v in out.items() if v is not None}


def _ensure_match(cargo: dict, truck_id: str, result: dict) -> int | None:
    """Taklif uchun moslik yozuvi (tugma shu id bilan ishlaydi)."""
    match_id = db.save_match(cargo["id"], truck_id, result)
    if match_id:
        return match_id
    row = db.find_match(cargo["id"], truck_id)
    if row is not None:
        return row["id"]
    return db.revive_match(cargo["id"], truck_id, result)


def _rank_key(item: dict):
    """Asosiy tamoyil: kunlik marja; stavkasi yo'qlar oxirida."""
    r = item["result"]
    return (r.get("margin_per_day") is not None, r.get("margin_per_day") or 0,
            r.get("score") or 0)


def _candidates(from_city=None, to_city=None, from_country=None, to_country=None,
                body_type=None, near_city=None, radius_km=None,
                hours: int = CARGO_HOURS) -> list[dict]:
    rows = [_row(r) for r in db.active_cargos(hours=hours)]
    center = geo.CITIES.get(near_city) if near_city else None
    radius = float(radius_km or NEAR_RADIUS_KM)
    out = []
    for c in rows:
        if from_city and c["from_city"] != from_city:
            continue
        if to_city and c["to_city"] != to_city:
            continue
        if from_country and rules.city_country(c["from_city"]) != from_country:
            continue
        if to_country and rules.city_country(c["to_city"]) != to_country:
            continue
        if body_type:
            body = c.get("body_type") or ("ref" if c.get("temp_c") is not None else None)
            if body != body_type:
                continue
        if center is not None:
            origin = geo.CITIES.get(c["from_city"] or "")
            if origin is None or geo.haversine_km(center, origin) > radius:
                continue
        out.append(c)
    return out


def _skipped_pairs() -> set[tuple[int, str]]:
    """Rahbar "O'tkazish" bosgan (yuk, fura) juftliklari.

    Qaror qabul qilingan — AI va reja ularni qayta taklif qilmasin
    (aks holda bir xil yuk har kuni qaytib chiqaveradi).
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT m.cargo_id, m.truck_id FROM matches m JOIN cargos c ON c.id = m.cargo_id"
            " WHERE m.decision='skipped' AND c.status='new'").fetchall()
    return {(r["cargo_id"], str(r["truck_id"])) for r in rows}


def _trucks(truck_id: str | None = None) -> list[dict]:
    if truck_id:
        t = db.get_truck(truck_id)
        return [_row(t)] if t is not None else []
    return [_row(t) for t in db.get_trucks()]


# ---------------------------------------------------------------- asboblar

def fleet(ctx: Ctx, **_) -> dict:
    """Park holati: har bir mashina qayerda, qachon bo'shaydi."""
    out = []
    gps = db.latest_gps_positions()
    for t in db.get_trucks(active_only=False):
        item = {"truck": t["id"], "plate": t["plate"], "body": t["body_type"],
                "capacity_t": t["capacity_t"], "city": t["current_city"],
                "free_date": t["free_date"], "active": bool(t["active"]),
                "position_source": t["pos_source"], "driver": t["driver"]}
        if t["id"] in gps:
            item["gps_time"] = gps[t["id"]]["recorded_at"]
        if t["temp_min"] is not None:
            item["temp_range"] = f"{t['temp_min']:g}..{t['temp_max']:g}"
        out.append({k: v for k, v in item.items() if v not in (None, "")})
    return {"trucks": out, "count": len(out)}


def find_cargo(ctx: Ctx, truck_id=None, near_city=None, radius_km=None,
               from_city=None, to_city=None, from_country=None, to_country=None,
               body_type=None, top=None, **_) -> dict:
    """Yuk qidirish. Mashina berilsa — shu mashina uchun; `near_city` berilsa —
    shu shahar atrofidan yuklanadiganlar (mashina o'sha yerda deb hisoblanadi)."""
    tid = _truck_id(truck_id)
    fc, tc, nc = _city(from_city), _city(to_city), _city(near_city)
    for name, raw, got in (("from_city", from_city, fc), ("to_city", to_city, tc),
                           ("near_city", near_city, nc)):
        if raw and not got:
            return {"error": f"unknown city in {name}: {raw}"}
    fco, tco = rules.country_code(from_country), rules.country_code(to_country)
    if from_country and not fco:
        return {"error": f"unknown country: {from_country}"}
    if to_country and not tco:
        return {"error": f"unknown country: {to_country}"}
    body = str(body_type).lower() if body_type else None
    if body and body not in _BODY_TYPES:
        return {"error": f"body_type must be one of {_BODY_TYPES}"}

    trucks = _trucks(tid)
    if tid and not trucks:
        return {"error": f"truck {tid} not found"}
    if not trucks:
        return {"error": "no active trucks in the fleet"}
    if nc:
        # "Moskva atrofida" — mashina(lar) o'sha yerda turibdi deb hisoblaymiz
        trucks = [{**t, "current_city": nc,
                   "free_date": max(str(t.get("free_date") or ""), date.today().isoformat())}
                  for t in trucks]

    cargos = _candidates(fc, tc, fco, tco, body, nc, radius_km)
    top = max(1, min(int(top or DEFAULT_TOP), MAX_TOP))
    found, blocked = [], 0
    skipped = _skipped_pairs()
    for c in cargos:
        best = None
        for t in trucks:
            if (c["id"], str(t["id"])) in skipped:
                continue          # rahbar shu fura uchun rad etgan — qayta taklif qilmaymiz
            r = scoring.evaluate(c, t)
            if not r["ok"]:
                if any(str(x).startswith("qoida #") for x in r["reasons"]):
                    blocked += 1
                continue
            if best is None or _rank_key({"result": r}) > _rank_key({"result": best}):
                best = r
        if best is not None:
            found.append({"cargo": c, "result": best})

    found.sort(key=_rank_key, reverse=True)
    items = []
    for f in found[:top]:
        c, r = f["cargo"], f["result"]
        # "atrofida" rejimida mashina holati virtual — taklif tugmasi faqat
        # mashina haqiqatan o'sha yerda bo'lsa beriladi
        real = db.get_truck(r["truck_id"])
        offer = None
        if not nc or (real is not None and real["current_city"] == nc):
            offer = _ensure_match(c, r["truck_id"], r)
            ctx.add_offer(offer, c, r["truck_id"])
        items.append({**_cargo_brief(c), **_result_brief(r),
                      **({"offer": offer} if offer else {})})
    return {"results": items, "scanned": len(cargos), "fit": len(found),
            "blocked_by_rules": blocked, "trucks_considered": len(trucks)}


def plan_truck(ctx: Ctx, truck_id=None, **_) -> dict:
    """Bitta fura uchun reja: yuk + qaytish yuki, butun aylanma kunlik marjasi."""
    tid = _truck_id(truck_id)
    if not tid:
        return {"error": "truck_id is required"}
    truck = db.get_truck(tid)
    if truck is None:
        return {"error": f"truck {tid} not found"}
    truck = _row(truck)
    pool = [_row(r) for r in db.active_cargos(hours=CARGO_HOURS)]
    plans = _plans_for(truck, pool, top=3)
    items = []
    for p in plans:
        offer = _ensure_match(p["cargo"], tid, p["leg1"])
        ctx.add_offer(offer, p["cargo"], tid)
        item = {**_cargo_brief(p["cargo"]), **_result_brief(p["leg1"]),
                "offer": offer, "round_margin_per_day": p["per_day"]}
        if p["back"]:
            item["return"] = {**_cargo_brief(p["back"]["back_cargo"]),
                              "margin_usd": p["back"]["leg2"].get("margin_usd"),
                              "round_total_usd": p["back"]["total_margin_usd"],
                              "round_days": p["back"]["total_days"]}
        else:
            item["return"] = "no return cargo in current ads"
        items.append(item)
    return {"truck": tid, "at": truck["current_city"], "free_date": truck["free_date"],
            "plans": items}


def _plans_for(truck: dict, pool: list[dict], top: int = 3,
               exclude: set | None = None) -> list[dict]:
    exclude = exclude or set()
    skipped = _skipped_pairs()
    firsts = []
    for c in pool:
        if c["id"] in exclude or (c["id"], str(truck["id"])) in skipped:
            continue
        r = scoring.evaluate(c, truck)
        if r["ok"] and r.get("margin_usd") is not None:
            firsts.append((c, r))
    firsts.sort(key=lambda x: (x[1]["margin_per_day"] or 0), reverse=True)

    plans = []
    for c, r in firsts[:6]:
        backs = scoring.best_roundtrip(
            c, truck, [x for x in pool if x["id"] not in exclude
                       and (x["id"], str(truck["id"])) not in skipped], top=1)
        back = backs[0] if backs else None
        per_day = back["margin_per_day"] if back else r["margin_per_day"]
        plans.append({"cargo": c, "leg1": r, "back": back, "per_day": per_day})
    plans.sort(key=lambda p: p["per_day"] or 0, reverse=True)
    return plans[:top]


def plan_fleet(ctx: Ctx, **_) -> dict:
    """Butun park uchun reja: har bir furaga bittadan yuk (+qaytish),
    bitta yuk ikki furaga berilmaydi. Avval eng erta bo'shaydigan fura."""
    trucks = sorted((_row(t) for t in db.get_trucks()),
                    key=lambda t: str(t.get("free_date") or ""))
    pool = [_row(r) for r in db.active_cargos(hours=CARGO_HOURS)]
    used: set = set()
    out = []
    for t in trucks:
        plans = _plans_for(t, pool, top=1, exclude=used)
        if not plans:
            out.append({"truck": t["id"], "at": t["current_city"],
                        "plan": "nothing suitable now"})
            continue
        p = plans[0]
        used.add(p["cargo"]["id"])
        offer = _ensure_match(p["cargo"], t["id"], p["leg1"])
        ctx.add_offer(offer, p["cargo"], t["id"])
        item = {"truck": t["id"], "at": t["current_city"], **_cargo_brief(p["cargo"]),
                **_result_brief(p["leg1"]), "offer": offer,
                "round_margin_per_day": p["per_day"]}
        if p["back"]:
            b = p["back"]["back_cargo"]
            used.add(b["id"])
            item["return"] = {"cargo_id": b["id"],
                              "route": f"{b['from_city']} -> {b['to_city']}",
                              "rate": _rate_text(b)}
        out.append(item)
    return {"plan": out}


def set_truck_position(ctx: Ctx, truck_id=None, city=None, free_date=None, **_) -> dict:
    tid = _truck_id(truck_id)
    if not tid or db.get_truck(tid) is None:
        return {"error": f"truck {truck_id} not found"}
    canon = _city(city) if city else None
    if city and not canon:
        return {"error": f"unknown city: {city}"}
    fd = None
    if free_date:
        try:
            fd = date.fromisoformat(str(free_date)[:10])
        except ValueError:
            return {"error": "free_date must be YYYY-MM-DD"}
        # O'tgan sana = "hozir bo'sh". Model ba'zan eski sanani o'ylab topadi —
        # u mashinani o'tmishda bo'sh qilib, hisobni buzmasin.
        fd = max(fd, date.today()).isoformat()
    if not canon and not fd:
        return {"error": "give city and/or free_date"}
    current = db.get_truck(tid)
    if canon == current["current_city"] and not fd:
        # O'zgarish yo'q — GPS manbasini "qo'lda" ga almashtirmaymiz
        return {"ok": True, "truck": tid, "city": canon, "free_date": current["free_date"],
                "note": "unchanged"}
    db.set_truck_position(tid, city=canon, free_date=fd, source="manual")
    t = db.get_truck(tid)
    return {"ok": True, "truck": tid, "city": t["current_city"], "free_date": t["free_date"]}


def add_rule(ctx: Ctx, effect=None, scope=None, require=None, points=None,
             min_rate=None, currency=None, text="", **_) -> dict:
    """Qoida qo'shish. `min_rate`+`currency` — "30 mln so'mdan arzon olma"
    uchun qulaylik: USD ga shu yerda aylantiriladi (model hisoblamaydi)."""
    require = dict(require or {})
    if min_rate not in (None, ""):
        cur = str(currency or "UZS").upper()
        if cur not in config.RATES_TO_USD:
            return {"error": f"currency must be one of {list(config.RATES_TO_USD)}"}
        try:
            usd = config.to_usd(float(min_rate), cur)
        except (TypeError, ValueError):
            return {"error": "min_rate must be a number"}
        require["min_rate_usd"] = round(usd)
    rule_id, error = rules.add(effect or "", scope or {}, require, points,
                               text=text or "", source="user")
    if rule_id is None:
        return {"error": error}
    ctx.new_rules.append(rule_id)
    rule = rules.get(rule_id)
    return {"ok": True, "rule_id": rule_id, "rule": rules.describe(rule)}


def list_rules(ctx: Ctx, **_) -> dict:
    out = []
    for r in rules.list_rules():
        if r["status"] in ("active", "proposed"):
            out.append({"rule_id": r["id"], "status": r["status"], "source": r["source"],
                        "rule": rules.describe(r)})
    return {"rules": out}


def remove_rule(ctx: Ctx, rule_id=None, **_) -> dict:
    try:
        rid = int(str(rule_id).lstrip("#"))
    except (TypeError, ValueError):
        return {"error": "rule_id must be a number"}
    return {"ok": rules.delete(rid), "rule_id": rid}


def remember(ctx: Ctx, note=None, **_) -> dict:
    if not note or not str(note).strip():
        return {"error": "note is empty"}
    return {"ok": True, "memory_id": db.add_memory(str(note))}


def forget(ctx: Ctx, memory_id=None, **_) -> dict:
    try:
        mid = int(str(memory_id).lstrip("#"))
    except (TypeError, ValueError):
        return {"error": "memory_id must be a number"}
    return {"ok": db.delete_memory(mid)}


def cargo_info(ctx: Ctx, cargo_id=None, **_) -> dict:
    try:
        cid = int(str(cargo_id).lstrip("#"))
    except (TypeError, ValueError):
        return {"error": "cargo_id must be a number"}
    c = db.get_cargo(cid)
    if c is None:
        return {"error": f"cargo {cid} not found"}
    c = _row(c)
    out = {**_cargo_brief(c), "status": c["status"], "source": c["source"],
           "posted": c["created_at"], "text": (c["raw_text"] or "")[:700]}
    if c.get("username"):
        out["username"] = c["username"]
    return out


def market(ctx: Ctx, from_city=None, to_city=None, **_) -> dict:
    fc, tc = _city(from_city), _city(to_city)
    if not fc or not tc:
        return {"error": "give from_city and to_city"}
    import analytics
    week = db.search_cargos(from_city=fc, to_city=tc, hours=7 * 24, limit=500)
    rates = sorted(r["rate_usd"] for r in week if r["rate_usd"])
    km = geo.road_km(fc, tc)
    out = {"route": f"{fc} -> {tc}", "road_km": km, "ads_last_7_days": len(week),
           "active_now": sum(1 for r in week if r["status"] == "new"),
           "rate_per_km_median": analytics.route_rate(fc, tc)}
    if rates:
        out["rate_usd_min"], out["rate_usd_max"] = rates[0], rates[-1]
        out["rate_usd_median"] = rates[len(rates) // 2]
    return out


def stats(ctx: Ctx, days=None, **_) -> dict:
    import analytics
    d = max(1, min(int(days or 30), 365))
    rep = analytics.report(days=d)
    return {"totals": rep["totals"], "top_routes": rep["routes"][:5],
            "groups": rep["groups"][:5]}


def watch_route(ctx: Ctx, from_city=None, to_city=None, body_type=None, **_) -> dict:
    fc, tc = _city(from_city), _city(to_city)
    if not fc and not tc:
        return {"error": "give from_city and/or to_city"}
    body = str(body_type).lower() if body_type else None
    watch_id = db.add_watch(fc, tc, body, query=f"{fc or ''} {tc or ''} {body or ''}".strip(),
                            chat_id=ctx.chat_id or None)
    return {"ok": True, "watch_id": watch_id, "days": db.WATCH_DAYS}


PRICE_STEP = 50              # narx shu qadamga yaxlitlanadi
MARKET_DAYS = 30


def _round_up(value: float, step: int = PRICE_STEP) -> int:
    return int(-(-value // step) * step)


def price_advice(ctx: Ctx, cargo_id=None, from_city=None, to_city=None, body_type=None,
                 weight_t=None, truck_id=None, **_) -> dict:
    """Bu yukka qancha so'rash kerak: xarajat, maqsad, bozor → tavsiya + tayyor matn.

    Narx eng arzon xarajatli furaga (yoki berilgan furaga) hisoblanadi.
    Hammasi USD, ko'rsatish uchun so'mga ham aylantiriladi.
    """
    import analytics
    import settings as settings_mod

    if cargo_id not in (None, ""):
        try:
            row = db.get_cargo(int(str(cargo_id).lstrip("#")))
        except ValueError:
            return {"error": "cargo_id must be a number"}
        if row is None:
            return {"error": f"cargo {cargo_id} not found"}
        cargo = _row(row)
    else:
        fc, tc = _city(from_city), _city(to_city)
        if not fc or not tc:
            return {"error": "give cargo_id, or from_city and to_city"}
        cargo = {"id": 0, "from_city": fc, "to_city": tc, "load_date": None,
                 "body_type": (str(body_type).lower() if body_type else None),
                 "weight_t": float(weight_t) if weight_t else None, "temp_c": None,
                 "rate": None, "currency": None, "rate_usd": None}

    trucks = _trucks(_truck_id(truck_id))
    if not trucks:
        return {"error": "no trucks"}
    costs = settings_mod.current_costs()
    target_day = costs.target_margin_per_day
    probe = {**cargo, "rate": None, "rate_usd": None, "currency": None}
    best, best_need, reasons = None, None, []
    for t in trucks:
        r = scoring.evaluate(probe, t, costs, ignore_date=True)
        if not r["ok"]:
            reasons += r["reasons"]
            continue
        need = r["total_cost"] + target_day * r["trip_days"]
        if best is None or need < best_need:
            best, best_need, best_truck = r, need, t
    if best is None:
        return {"error": "no truck can carry this cargo",
                "reasons": sorted(set(str(x) for x in reasons))[:4]}

    cost, days = best["total_cost"], best["trip_days"]
    target = cost + target_day * days
    floor = cost + 0.5 * target_day * days

    recent = [r["rate_usd"] for r in db.search_cargos(
        from_city=cargo["from_city"], to_city=cargo["to_city"], hours=MARKET_DAYS * 24,
        limit=500) if r["rate_usd"] and r["id"] != cargo["id"]]
    recent.sort()
    market = {"ads_30d": len(recent)}
    if recent:
        market.update(median_usd=round(recent[len(recent) // 2]),
                      min_usd=round(recent[0]), max_usd=round(recent[-1]))
    per_km = analytics.route_rate(cargo["from_city"], cargo["to_city"])
    if per_km:
        market["by_rate_per_km_usd"] = round(per_km * best["loaded_km"])
    market_price = market.get("median_usd") or market.get("by_rate_per_km_usd")

    ask = _round_up(max(target, market_price or 0))
    floor = _round_up(floor)
    uzs_rate = config.RATES_TO_USD.get("UZS")
    out = {"cargo_id": cargo["id"] or None,
           "route": f"{cargo['from_city']} -> {cargo['to_city']}",
           "truck": best_truck["id"], "empty_km": best["empty_km"],
           "loaded_km": best["loaded_km"], "trip_days": days,
           "break_even_usd": round(cost), "target_usd": round(target),
           "target_margin_per_day": round(target_day),
           "ask_usd": ask, "floor_usd": floor, "market": market}
    if uzs_rate:
        out["ask_uzs_mln"] = round(ask / uzs_rate / 1_000_000, 1)
        out["floor_uzs_mln"] = round(floor / uzs_rate / 1_000_000, 1)
    if market_price and market_price < target * 0.8:
        out["warning"] = "market pays much less than our target — trip may be unprofitable"
    offered = cargo.get("rate_usd")
    if offered:
        out["offered_usd"] = round(offered)
        out["offered_vs_target_pct"] = round((offered / target - 1) * 100)
    out["message"] = _price_message(cargo, best_truck, ask, out.get("ask_uzs_mln"))
    return out


def _price_message(cargo: dict, truck: dict, ask: int, ask_uzs_mln) -> str:
    """Yuk egasiga yuborish uchun tayyor matn (ruscha — bozor tili)."""
    body = {"ref": "реф", "tent": "тент", "izoterm": "изотерм"}.get(truck.get("body_type"),
                                                                  truck.get("body_type") or "")
    weight = f", {cargo['weight_t']:g} т" if cargo.get("weight_t") else ""
    uzs = f" (≈{ask_uzs_mln:g} млн сум)" if ask_uzs_mln else ""
    free = truck.get("free_date") or ""
    when = "сегодня" if not free or free <= date.today().isoformat() else free
    return (f"Здравствуйте! По грузу {cargo['from_city']} → {cargo['to_city']}{weight}: "
            f"готовы взять за ${ask}{uzs}. Машина {body} {truck.get('capacity_t') or ''} т, "
            f"подача {when}. Подтвердите, пожалуйста.").replace("  ", " ")


def learn_suggestions(ctx: Ctx, **_) -> dict:
    import learn
    items = learn.propose()
    ctx.proposals.extend(items)
    return {"proposals": [{"rule_id": i["rule"]["id"], "rule": rules.describe(i["rule"]),
                           "why": i["why"]} for i in items],
            "note": "user confirms with buttons under your message"}


REGISTRY = {
    "fleet": fleet,
    "find_cargo": find_cargo,
    "plan_truck": plan_truck,
    "plan_fleet": plan_fleet,
    "set_truck_position": set_truck_position,
    "add_rule": add_rule,
    "list_rules": list_rules,
    "remove_rule": remove_rule,
    "remember": remember,
    "forget": forget,
    "cargo_info": cargo_info,
    "market": market,
    "stats": stats,
    "watch_route": watch_route,
    "learn_suggestions": learn_suggestions,
    "price_advice": price_advice,
}

_SCOPE = {"type": "object", "properties": {
    "from_country": {"type": "string", "description": "RU, UZ, KZ..."},
    "to_country": {"type": "string"},
    "from_city": {"type": "string"}, "to_city": {"type": "string"},
    "body_type": {"type": "string", "enum": list(_BODY_TYPES)},
    "truck_id": {"type": "string"}}}
_REQUIRE = {"type": "object", "description": "rule fires only when this is NOT met",
            "properties": {k: {"type": "number"} for k in rules.REQUIRE_KEYS}}


def _fn(name: str, desc: str, props: dict | None = None,
        required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {},
                       **({"required": required} if required else {})}}}


_S = {"type": "string"}

SCHEMAS = [
    _fn("fleet", "Company trucks: where, free date, body, capacity."),
    _fn("find_cargo", "Search current cargo ads, ranked by net margin per day for our trucks. "
        "Use truck_id for one truck; near_city for cargo loading around a city.",
        {"truck_id": _S, "near_city": _S, "radius_km": {"type": "number"},
         "from_city": _S, "to_city": _S, "from_country": _S, "to_country": _S,
         "body_type": {"type": "string", "enum": list(_BODY_TYPES)},
         "top": {"type": "integer"}}),
    _fn("plan_truck", "Best cargo + return cargo for one truck (whole round trip per day).",
        {"truck_id": _S}, ["truck_id"]),
    _fn("plan_fleet", "Plan for all trucks: one cargo (+return) each, no cargo used twice."),
    _fn("set_truck_position", "Update where a truck is / when it is free.",
        {"truck_id": _S, "city": _S, "free_date": {"type": "string", "description": "YYYY-MM-DD"}},
        ["truck_id"]),
    _fn("add_rule", "Save a company rule; applied automatically to every cargo. "
        "effect: block (never offer), penalty (-points), boost (+points). "
        "For a minimum price in UZS/RUB use min_rate+currency.",
        {"effect": {"type": "string", "enum": list(rules.EFFECTS)}, "scope": _SCOPE,
         "require": _REQUIRE, "points": {"type": "number"},
         "min_rate": {"type": "number"}, "currency": {"type": "string",
                                                      "enum": list(config.RATES_TO_USD)},
         "text": {"type": "string", "description": "user's words"}},
        ["effect"]),
    _fn("list_rules", "List company rules."),
    _fn("remove_rule", "Delete a rule.", {"rule_id": {"type": "integer"}}, ["rule_id"]),
    _fn("remember", "Save a fact about the company that is not a rule.",
        {"note": _S}, ["note"]),
    _fn("forget", "Delete a saved fact.", {"memory_id": {"type": "integer"}}, ["memory_id"]),
    _fn("cargo_info", "Full cargo ad: original text, contacts.",
        {"cargo_id": {"type": "integer"}}, ["cargo_id"]),
    _fn("market", "Route market: rates, how often such cargo appears.",
        {"from_city": _S, "to_city": _S}, ["from_city", "to_city"]),
    _fn("stats", "Business statistics for N days.", {"days": {"type": "integer"}}),
    _fn("watch_route", "Notify when cargo on this route appears.",
        {"from_city": _S, "to_city": _S, "body_type": {"type": "string",
                                                       "enum": list(_BODY_TYPES)}}),
    _fn("learn_suggestions", "Find patterns in past take/skip decisions and propose rules."),
    _fn("price_advice", "How much to ask for a cargo: break-even, our target, market, "
        "recommended price and a ready message to the cargo owner. Use cargo_id, or "
        "from_city+to_city for a hypothetical cargo.",
        {"cargo_id": {"type": "integer"}, "from_city": _S, "to_city": _S,
         "body_type": {"type": "string", "enum": list(_BODY_TYPES)},
         "weight_t": {"type": "number"}, "truck_id": _S}),
]


def call(ctx: Ctx, name: str, args: dict) -> dict:
    """Asbobni xavfsiz chaqiradi: noma'lum nom yoki xato — modelga matn bo'lib qaytadi."""
    fn = REGISTRY.get(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    if not isinstance(args, dict):
        return {"error": "arguments must be an object"}
    try:
        return fn(ctx, **args)
    except Exception as ex:
        log.exception("Asbob xatosi: %s(%s)", name, args)
        return {"error": f"{type(ex).__name__}: {ex}"}


def dumps(value: dict, limit: int = 2800) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if len(text) <= limit else text[:limit] + "…(truncated)"
