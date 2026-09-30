"""
insight.py — "nega aynan shu yuk?" izohi (aqlli bildirishnoma).

Kartochka faqat raqam bermasin, qarorni tushuntirsin:

    Почему этот груз:
    🏆 №01 — лучший вариант (у №02 маржа была бы $900)
    📈 Ставка выше рынка на 12%
    🔁 Обратно есть: Almaty → Toshkent #9 · круг $3 100
    📜 +10 qoida #3: …

Hammasi **dasturda** hisoblanadi (model chaqirilmaydi): listener har bir
yangi e'lonni shu yerdan o'tkazadi — AI kutish vaqti, limit va xato
bo'lmasligi kerak. Raqamlar `scoring` natijasidan, taxmin yo'q.
"""
from __future__ import annotations

import logging

import db
import scoring

log = logging.getLogger("insight")

MARKET_EQUAL_PCT = 5          # ±5% — "bozor darajasida"
BIG_EMPTY_RATIO = 0.25        # bo'sh probeg yuk masofasining 25% dan ko'p


def _money(v) -> str:
    return f"${v:,.0f}".replace(",", " ") if v is not None else "—"


def explain(cargo: dict, result: dict, others: list[dict] | None = None,
            with_return: bool = True) -> list[str]:
    """Izoh qatorlari (ruscha — Telegram kartochkasi uchun). Xato bo'lsa — bo'sh."""
    try:
        return _explain(cargo, result, others or [], with_return)
    except Exception:
        log.exception("Izoh tuzishda xato")
        return []


def _explain(cargo: dict, result: dict, others: list[dict], with_return: bool) -> list[str]:
    lines: list[str] = []
    truck_id = result.get("truck_id")
    per_day = result.get("margin_per_day")

    # 1. Boshqa furalar bilan solishtirish — nega aynan shu fura
    alt = [r for r in others if r.get("truck_id") != truck_id
           and r.get("margin_per_day") is not None]
    if per_day is not None and alt:
        best_alt = max(alt, key=lambda r: r["margin_per_day"])
        if best_alt["margin_per_day"] < per_day:
            lines.append(f"🏆 №{truck_id} — лучший вариант (у №{best_alt['truck_id']} "
                         f"маржа была бы {_money(best_alt.get('margin_usd'))})")
    elif per_day is not None and not alt and others:
        lines.append(f"🏆 Подходит только №{truck_id}")

    # 2. Bozor bilan solishtirish
    rpk, market = result.get("rate_per_km"), result.get("market_rate_per_km")
    if rpk and market:
        pct = (rpk / market - 1) * 100
        if pct >= MARKET_EQUAL_PCT:
            lines.append(f"📈 Ставка выше рынка на {pct:.0f}%")
        elif pct <= -MARKET_EQUAL_PCT:
            lines.append(f"📉 Ставка ниже рынка на {-pct:.0f}% — можно торговаться")
        else:
            lines.append("📊 Ставка на уровне рынка")

    # 3. Bo'sh probeg
    empty = result.get("empty_km")
    if empty == 0:
        lines.append("✅ Без пустого пробега — машина уже на месте")
    elif empty and result.get("empty_ratio", 0) > BIG_EMPTY_RATIO:
        lines.append(f"⚠️ Большой пустой пробег: {empty} км")

    # 4. Qaytish yuki — mashina bo'sh qaytmasin
    if with_return and truck_id:
        truck = db.get_truck(truck_id)
        if truck is not None:
            pool = [r for r in db.active_cargos(hours=48) if r["id"] != cargo.get("id")]
            chains = scoring.best_roundtrip(cargo, truck, pool, top=1)
            if chains:
                b = chains[0]["back_cargo"]
                lines.append(f"🔁 Обратно есть: {b.get('from_city')} → {b.get('to_city')} "
                             f"#{b.get('id')} · круг {_money(chains[0]['total_margin_usd'])}")
            elif cargo.get("to_city"):
                lines.append(f"🔁 Из г. {cargo['to_city']} обратного груза пока нет")

    # 5. Kompaniya qoidalari ta'siri
    for note in result.get("rules", [])[:2]:
        lines.append(f"📜 {note}")
    return lines
