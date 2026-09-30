"""
returns.py — qaytish yukini kuzatish: fura manzilga bo'sh bormasin, bo'sh qaytmasin.

Fura reys olgach uning holati manzil shahriga va bo'shash sanasiga o'tadi
(`actions.take_match`) — yangi e'lonlar allaqachon shu joy bo'yicha
hisoblanadi. Bu modul ikki narsa qo'shadi:

1. **Belgi va past chegara.** Yo'ldagi furaga mos yangi yuk — "🔁 Обратный груз"
   deb keladi va bildirishnoma chegarasi pastroq (`RETURN_THRESHOLD`):
   bo'sh qaytishdan o'rtacha yuk ham yaxshi.
2. **Eslatma.** Fura bo'shashidan bir kun oldin (va o'sha kuni) — eng yaxshi
   3 ta qaytish yuki "✅ Беру" tugmalari bilan, yoki "hali yo'q — kuzatyapman".
   Har reys uchun bir marta (`return_reminded:<match_id>`).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import db
import notifier
import search

log = logging.getLogger("returns")

RETURN_THRESHOLD = 50.0
REMIND_DAYS_BEFORE = 1


def current_trip(truck_id: str):
    """Furaning yo'ldagi oxirgi reysi (yo'q bo'lsa None)."""
    trips = db.active_trips(truck_id)
    return trips[-1] if trips else None


def match_reason(truck_id: str) -> str | None:
    """Yangi yuk yo'ldagi fura uchun — kartochka tepasidagi belgi."""
    trip = current_trip(truck_id)
    if trip is None:
        return None
    truck = db.get_truck(truck_id)
    free = truck["free_date"] if truck is not None else None
    return (f"🔁 Обратный груз для №{truck_id}: свободна {free or '—'} "
            f"в {trip['to_city']}")


def _reminded_key(match_id: int) -> str:
    return f"return_reminded:{match_id}"


def format_reminder(truck, trip, offers) -> str:
    when = "сегодня" if (truck["free_date"] or "") <= date.today().isoformat() else "завтра"
    lines = [f"⏰ <b>№{notifier.escape(truck['id'])} {when} освобождается в "
             f"{notifier.escape(trip['to_city'])}</b>"]
    if not offers:
        lines.append("Обратного груза пока нет — слежу и сразу сообщу, как появится.")
        return "\n".join(lines)
    lines.append("Лучший обратный груз (по марже за день):")
    for i, o in enumerate(offers, 1):
        d = search.details(o)
        per_day = d.get("margin_per_day")
        money = (f"{notifier.money(per_day)}/день" if per_day is not None
                 else "цена не указана")
        lines.append(f"\n{i}. <b>{notifier.escape(o['from_city'])} → "
                     f"{notifier.escape(o['to_city'])}</b> · {money}"
                     f"\n   пустой {o['empty_km']:.0f} км · груз #{o['cargo_id']}")
    return "\n".join(lines)


def remind_due(today: date | None = None, send=None) -> int:
    """Bo'shashiga 1 kun (yoki kamroq) qolgan yo'ldagi furalar uchun eslatma.

    Qaytaradi: nechta eslatma yuborildi.
    """
    send = send or notifier.send
    today = today or date.today()
    limit = (today + timedelta(days=REMIND_DAYS_BEFORE)).isoformat()
    sent = 0
    for truck in db.get_trucks():
        trip = current_trip(truck["id"])
        if trip is None or not truck["free_date"] or truck["free_date"] > limit:
            continue
        key = _reminded_key(trip["id"])
        if db.get_setting(key):
            continue
        db.set_setting(key, today.isoformat())       # avval belgi — xato bo'lsa ham takrorlamaydi
        offers = search.best_offers(truck["id"], 3)
        keyboard = {"inline_keyboard": [[{
            "text": f"✅ Беру #{o['cargo_id']} → №{truck['id']} "
                    f"({o['from_city']}→{o['to_city']})"[:60],
            "callback_data": f"take:{o['id']}"}] for o in offers]} if offers else None
        try:
            send(format_reminder(truck, trip, offers), keyboard)
            sent += 1
        except Exception:
            log.exception("Qaytish eslatmasi yuborilmadi (fura %s)", truck["id"])
    return sent
