"""
analytics.py — statistika va o'z-o'zidan sozlanish (TZ 5.6).

Bu modulning qiymati vaqt o'tishi bilan oshadi: `matches.decision` —
dispetcher nimani olgani va nimani tashlagani — tizimdagi eng qimmatli
ma'lumot. Undan to'rt narsa chiqadi:

1. Yo'nalish bo'yicha bozor stavkasi — `config` dagi yagona
   `market_rate_per_km` o'rniga. Toshkent→Moskva va Toshkent→Almaty
   stavkasi bir xil emas, shuning uchun bitta o'rtacha raqam ball
   hisobini buzadi.
2. Guruh sifati — qaysi guruhdan kelgan yuklar ko'proq olingan.
   Foydasiz guruhni kuzatishda davom etish — vaqtni behuda sarflash.
3. Ball to'g'rimi — "olingan" yuklarning o'rtacha bali "tashlangan"
   larnikidan sezilarli yuqori bo'lishi kerak. Bo'lmasa — formulani
   qayta ko'rish vaqti keldi.
4. Prognoz va haqiqat farqi — `/done` orqali kiritilgan real marja.
"""
from __future__ import annotations

import json
import logging
import time
from statistics import median

import config
import db
import geo

log = logging.getLogger("analytics")

MIN_SAMPLES_ROUTE = 3       # yo'nalish stavkasiga ishonish uchun eng kam e'lon
MIN_SAMPLES_COUNTRY = 5
RATE_WINDOW_DAYS = 30
CACHE_TTL_SEC = 3600        # stavka jadvali soatiga bir yangilanadi


# ---------------------------------------------------------------- bozor stavkasi

# {db_path: (vaqt, yo'nalish stavkalari, davlat juftligi stavkalari)}
_rate_cache: dict[str, tuple[float, dict, dict]] = {}


def reset_cache() -> None:
    _rate_cache.clear()


def _rate_rows(days: int) -> list:
    with db.connect() as conn:
        return conn.execute(
            """SELECT from_city, to_city, rate_usd FROM cargos
               WHERE rate_usd IS NOT NULL AND rate_usd > 0
                 AND from_city IS NOT NULL AND to_city IS NOT NULL
                 AND created_at >= ?""",
            (db._ago(days=days),)).fetchall()


def _build_rate_tables(days: int) -> tuple[dict, dict]:
    """E'lonlardan $/km stavkalarini yig'adi: yo'nalish va davlat bo'yicha."""
    routes: dict[tuple[str, str], list[float]] = {}
    countries: dict[tuple[str, str], list[float]] = {}

    for row in _rate_rows(days):
        km = geo.road_km(row["from_city"], row["to_city"])
        if not km or km < 100:          # juda qisqa reysda $/km yanglishtiradi
            continue
        per_km = row["rate_usd"] / km
        if not 0.05 <= per_km <= 10:    # aniq xato tahlil — hisobga olmaymiz
            continue
        routes.setdefault((row["from_city"], row["to_city"]), []).append(per_km)
        a, b = geo.CITIES.get(row["from_city"]), geo.CITIES.get(row["to_city"])
        if a and b:
            countries.setdefault((a.country, b.country), []).append(per_km)

    route_rates = {k: round(median(v), 3) for k, v in routes.items()
                   if len(v) >= MIN_SAMPLES_ROUTE}
    country_rates = {k: round(median(v), 3) for k, v in countries.items()
                     if len(v) >= MIN_SAMPLES_COUNTRY}
    return route_rates, country_rates


def rate_tables(days: int = RATE_WINDOW_DAYS) -> tuple[dict, dict]:
    """Keshlangan stavka jadvallari (baza yo'li bo'yicha alohida)."""
    key = str(config.DB_PATH)
    cached = _rate_cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL_SEC:
        return cached[1], cached[2]
    routes, countries = _build_rate_tables(days)
    _rate_cache[key] = (time.time(), routes, countries)
    return routes, countries


def route_rate(from_city: str | None, to_city: str | None,
               days: int = RATE_WINDOW_DAYS) -> float | None:
    """Shu yo'nalishdagi bozor stavkasi ($/km) yoki None.

    Avval aniq yo'nalish, keyin davlat juftligi. Ma'lumot yetarli
    bo'lmasa None — `config.Costs.market_rate_per_km` ishlatiladi.
    """
    if not from_city or not to_city:
        return None
    try:
        routes, countries = rate_tables(days)
    except Exception:
        log.debug("Bozor stavkasi o'qilmadi", exc_info=True)
        return None

    if (from_city, to_city) in routes:
        return routes[(from_city, to_city)]
    a, b = geo.CITIES.get(from_city), geo.CITIES.get(to_city)
    if a and b:
        return countries.get((a.country, b.country))
    return None


# ---------------------------------------------------------------- hisobotlar

def group_quality(days: int = 30) -> list[dict]:
    """Qaysi guruh foydali: nechta yuk berdi, nechtasi olindi."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT c.source AS source,
                      COUNT(DISTINCT c.id) AS cargos,
                      COUNT(DISTINCT CASE WHEN m.decision='taken'
                                          THEN c.id END) AS taken,
                      COUNT(DISTINCT CASE WHEN m.decision='skipped'
                                          THEN c.id END) AS skipped,
                      AVG(m.score) AS avg_score
               FROM cargos c LEFT JOIN matches m ON m.cargo_id = c.id
               WHERE c.created_at >= ?
               GROUP BY c.source ORDER BY taken DESC, cargos DESC""",
            (db._ago(days=days),)).fetchall()

    out = []
    for r in rows:
        cargos = r["cargos"] or 0
        out.append({
            "source": r["source"] or "?",
            "cargos": cargos,
            "taken": r["taken"] or 0,
            "skipped": r["skipped"] or 0,
            "avg_score": round(r["avg_score"], 1) if r["avg_score"] else None,
            "take_rate": round((r["taken"] or 0) / cargos * 100, 1) if cargos else 0.0,
        })
    return out


def score_quality(days: int = 30) -> dict:
    """Ball to'g'ri ishlayaptimi: olinganlar bali tashlanganlardan yuqorimi."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT decision, COUNT(*) AS n, AVG(score) AS avg_score
               FROM matches
               WHERE decision IN ('taken','skipped') AND created_at >= ?
               GROUP BY decision""", (db._ago(days=days),)).fetchall()

    stats = {r["decision"]: {"n": r["n"], "avg_score": round(r["avg_score"], 1)}
             for r in rows}
    taken = stats.get("taken", {}).get("avg_score")
    skipped = stats.get("skipped", {}).get("avg_score")
    gap = round(taken - skipped, 1) if taken is not None and skipped is not None else None

    verdict = "недостаточно данных"
    if gap is not None:
        total = stats["taken"]["n"] + stats["skipped"]["n"]
        if total < 20:
            verdict = f"мало данных (решений: {total})"
        elif gap >= 10:
            verdict = "балл совпадает с решениями — формула работает"
        elif gap > 0:
            verdict = "разница небольшая — стоит пересмотреть веса"
        else:
            verdict = "⚠️ балл работает наоборот — пересмотрите формулу"

    return {"taken": stats.get("taken"), "skipped": stats.get("skipped"),
            "gap": gap, "verdict": verdict}


def margin_accuracy(days: int = 90) -> dict:
    """Prognoz marja haqiqatdan qancha farq qiladi (`/done` ma'lumotlari)."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT margin_usd, actual_margin_usd FROM matches
               WHERE actual_margin_usd IS NOT NULL AND margin_usd IS NOT NULL
                 AND created_at >= ?""", (db._ago(days=days),)).fetchall()

    if not rows:
        return {"n": 0, "avg_error_pct": None, "bias_pct": None,
                "verdict": "фактическая маржа не введена (/done)"}

    errors, biases = [], []
    for r in rows:
        planned, actual = r["margin_usd"], r["actual_margin_usd"]
        if not planned:
            continue
        diff = (actual - planned) / abs(planned) * 100
        biases.append(diff)
        errors.append(abs(diff))

    if not errors:
        return {"n": 0, "avg_error_pct": None, "bias_pct": None,
                "verdict": "недостаточно данных"}

    avg_error = round(sum(errors) / len(errors), 1)
    bias = round(sum(biases) / len(biases), 1)

    # Og'ish yo'nalishi xatoning kattaligidan ham muhimroq: doimiy bir
    # tomonga og'ish xarajat parametrlari noto'g'ri ekanini ko'rsatadi va
    # uni `config.Costs` da to'g'rilash mumkin. Tasodifiy xatoni esa
    # to'g'rilab bo'lmaydi.
    drift = ("прогноз оптимистичный — увеличьте расходы (config.Costs)"
             if bias < 0 else "прогноз осторожный — уменьшите расходы")
    if avg_error <= 15:
        verdict = "прогноз точный (критерий ТЗ: <15%)"
        if abs(bias) >= 5:
            verdict += f", но есть постоянное отклонение: {drift}"
    else:
        verdict = drift
    return {"n": len(errors), "avg_error_pct": avg_error, "bias_pct": bias,
            "verdict": verdict}


def route_stats(days: int = 30, limit: int = 10) -> list[dict]:
    """Eng ko'p uchraydigan yo'nalishlar va ularning stavkasi."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT from_city, to_city, COUNT(*) AS n,
                      AVG(rate_usd) AS avg_rate
               FROM cargos
               WHERE created_at >= ? AND from_city IS NOT NULL AND to_city IS NOT NULL
               GROUP BY from_city, to_city ORDER BY n DESC LIMIT ?""",
            (db._ago(days=days), limit)).fetchall()

    routes, _ = rate_tables(days)
    out = []
    for r in rows:
        km = geo.road_km(r["from_city"], r["to_city"])
        out.append({
            "from_city": r["from_city"], "to_city": r["to_city"],
            "count": r["n"], "km": km,
            "avg_rate_usd": round(r["avg_rate"]) if r["avg_rate"] else None,
            "rate_per_km": routes.get((r["from_city"], r["to_city"])),
        })
    return out


def totals(days: int = 30) -> dict:
    with db.connect() as conn:
        cargos = conn.execute(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN status='taken' THEN 1 ELSE 0 END) AS taken,
                      SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END) AS expired,
                      AVG(confidence) AS confidence
               FROM cargos WHERE created_at >= ?""",
            (db._ago(days=days),)).fetchone()
        matches = conn.execute(
            """SELECT COUNT(*) AS n,
                      SUM(notified) AS notified,
                      AVG(CASE WHEN decision='taken' THEN margin_usd END) AS taken_margin
               FROM matches WHERE created_at >= ?""",
            (db._ago(days=days),)).fetchone()

    return {
        "days": days,
        "cargos": cargos["n"] or 0,
        "taken": cargos["taken"] or 0,
        "expired": cargos["expired"] or 0,
        "avg_confidence": round(cargos["confidence"], 2) if cargos["confidence"] else None,
        "matches": matches["n"] or 0,
        "notified": matches["notified"] or 0,
        "avg_taken_margin": round(matches["taken_margin"]) if matches["taken_margin"] else None,
    }


def report(days: int = 30) -> dict:
    return {
        "totals": totals(days),
        "groups": group_quality(days),
        "score": score_quality(days),
        "margin": margin_accuracy(max(days, 90)),
        "routes": route_stats(days),
    }


# ---------------------------------------------------------------- ko'rsatish

def format_report(rep: dict) -> str:
    """Telegram uchun qisqa hisobot (ruscha — dispetcher o'qiydi)."""
    t = rep["totals"]
    lines = [f"📊 <b>Статистика за {t['days']} дн.</b>",
             f"Грузов: {t['cargos']} · взято: {t['taken']} · "
             f"устарело: {t['expired']}",
             f"Уведомлений: {t['notified']} из {t['matches']} совпадений"]
    if t["avg_taken_margin"]:
        lines.append(f"Средняя маржа взятых рейсов: ${t['avg_taken_margin']:,}"
                     .replace(",", " "))

    s = rep["score"]
    if s["gap"] is not None:
        lines += ["", f"🎯 Балл взятых {s['taken']['avg_score']} vs "
                      f"пропущенных {s['skipped']['avg_score']} "
                      f"(разница {s['gap']})"]

    groups = [g for g in rep["groups"] if g["cargos"] >= 3][:5]
    if groups:
        lines += ["", "<b>Группы:</b>"]
        for g in groups:
            lines.append(f"  {g['source']}: {g['cargos']} груз., "
                         f"взято {g['taken']} ({g['take_rate']}%)")

    routes = rep["routes"][:5]
    if routes:
        lines += ["", "<b>Частые направления:</b>"]
        for r in routes:
            rate = f" · {r['rate_per_km']}$/км" if r["rate_per_km"] else ""
            lines.append(f"  {r['from_city']} → {r['to_city']}: "
                         f"{r['count']} шт.{rate}")

    m = rep["margin"]
    if m["n"]:
        lines += ["", f"💰 Прогноз vs факт: ошибка {m['avg_error_pct']}% "
                      f"по {m['n']} рейсам"]
    return "\n".join(lines)


def print_report(rep: dict) -> None:
    """Konsol uchun batafsil hisobot (o'zbekcha)."""
    t = rep["totals"]
    print(f"\n{'=' * 62}")
    print(f"  BAXT TRANSPORT — {t['days']} kunlik hisobot")
    print(f"{'=' * 62}")
    print(f"\nYuklar: {t['cargos']} ta · olingan: {t['taken']} · "
          f"eskirgan: {t['expired']}")
    print(f"Mosliklar: {t['matches']} ta · bildirishnoma: {t['notified']}")
    if t["avg_confidence"]:
        print(f"Tahlil ishonchi (o'rtacha): {t['avg_confidence']}")
    if t["avg_taken_margin"]:
        print(f"Olingan reyslarning o'rtacha marjasi: ${t['avg_taken_margin']}")

    print(f"\n--- GURUHLAR ---")
    if not rep["groups"]:
        print("   ma'lumot yo'q")
    for g in rep["groups"]:
        score = f"{g['avg_score']:5.1f}" if g["avg_score"] is not None else "    —"
        print(f"   {g['source'][:24]:24} {g['cargos']:4} yuk · olindi "
              f"{g['taken']:3} ({g['take_rate']:5.1f}%) · o'rt.ball {score}")

    print(f"\n--- BALL TO'G'RIMI ---")
    s = rep["score"]
    if s["taken"]:
        print(f"   olingan:   {s['taken']['n']:3} ta, o'rtacha ball "
              f"{s['taken']['avg_score']}")
    if s["skipped"]:
        print(f"   tashlangan:{s['skipped']['n']:3} ta, o'rtacha ball "
              f"{s['skipped']['avg_score']}")
    print(f"   xulosa: {s['verdict']}")

    print(f"\n--- YO'NALISHLAR ---")
    if not rep["routes"]:
        print("   ma'lumot yo'q")
    for r in rep["routes"]:
        rate = f"{r['rate_per_km']:.2f} $/km" if r["rate_per_km"] else "stavka kam"
        km = f"{r['km']:.0f} km" if r["km"] else "? km"
        print(f"   {r['from_city']:14} -> {r['to_city']:16} {r['count']:3} ta · "
              f"{km:9} · {rate}")

    print(f"\n--- PROGNOZ vs HAQIQAT ---")
    m = rep["margin"]
    if m["n"]:
        print(f"   {m['n']} ta reys · o'rtacha xato {m['avg_error_pct']}% · "
              f"og'ish {m['bias_pct']:+}%")
    print(f"   xulosa: {m['verdict']}")
    print()
