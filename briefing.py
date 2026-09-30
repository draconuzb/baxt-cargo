"""
briefing.py — ertalabki reja: har kuni belgilangan soatda bot o'zi yozadi.

    ☀️ Доброе утро! План на 30 сентября
    За сутки: 45 грузов · взято 2 рейса · маржа $3 100
    🚛 №01 · Toshkent → Toshkent → Almaty #4 · $459/день
       🔁 обратно: Almaty → Toshkent #9 · круг $265/день
    💡 Совет: ...
    [✅ Беру #4 → №01]

Reja **dasturda** hisoblanadi (`ai_tools.plan_fleet` — har furaga bittadan
yuk, bitta yuk ikki furaga berilmaydi). AI faqat oxirida bitta qisqa
maslahat qo'shadi (`brain.advise`, bitta so'rov); AI ishlamasa reja
baribir yuboriladi.

Qachon: `settings.briefing_hour` (Toshkent vaqti, sukut 08:00, −1 = o'chiq).
Kuniga bir marta — oxirgi yuborilgan sana bazada (`briefing_last`), shuning
uchun bot qayta ishga tushsa ham ikki marta yubormaydi.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone

import ai_tools
import db
import notifier
import settings

log = logging.getLogger("briefing")

LAST_KEY = "briefing_last"
_MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]


def local_now(now_utc: datetime | None = None) -> datetime:
    """Toshkent vaqti (UTC+5). Server UTC da ishlaydi — farq shu yerda hisoblanadi."""
    now_utc = now_utc or datetime.now(timezone.utc).replace(tzinfo=None)
    return now_utc + timedelta(hours=float(os.getenv("TZ_OFFSET_HOURS", "5")))


def due(now_utc: datetime | None = None) -> bool:
    """Hozir reja yuborish vaqti keldimi (va bugun hali yuborilmaganmi)."""
    hour = settings.briefing_hour()
    if hour < 0:
        return False
    now = local_now(now_utc)
    if now.hour < hour:
        return False
    return db.get_setting(LAST_KEY) != now.date().isoformat()


def mark_sent(now_utc: datetime | None = None) -> None:
    db.set_setting(LAST_KEY, local_now(now_utc).date().isoformat())


# ---------------------------------------------------------------- matn

def _arrow(route: str) -> str:
    """AI uchun "A -> B" (ASCII), odam uchun "A → B"."""
    return route.replace(" -> ", " → ")


def build(now_utc: datetime | None = None, with_ai: bool = True) -> tuple[str, dict | None]:
    """Reja matni va "✅ Беру" tugmalari."""
    today = local_now(now_utc).date()
    ctx = ai_tools.Ctx(chat_id="briefing")
    plan = ai_tools.plan_fleet(ctx)["plan"]
    day = db.day_summary(hours=24)

    lines = [f"☀️ <b>Доброе утро! План на {today.day} {_MONTHS_RU[today.month - 1]}</b>",
             f"За сутки: {day['cargos']} грузов · взято {day['taken']} рейс(ов)"
             + (f" · маржа {notifier.money(day['margin'])}" if day["taken"] else ""),
             f"Сейчас активных грузов: {day['active']}"]

    if not plan:
        lines.append("\n🚛 Активных машин нет — проверьте парк.")
    for p in plan:
        truck = notifier.escape(p["truck"])
        at = notifier.escape(p.get("at") or "—")
        if "cargo_id" not in p:
            lines.append(f"\n🚛 <b>№{truck}</b> · {at} — подходящего груза пока нет")
            continue
        per_day = p.get("margin_per_day")
        lines.append(
            f"\n🚛 <b>№{truck}</b> · {at}"
            f"\n   → <b>{notifier.escape(_arrow(p['route']))}</b> #{p['cargo_id']}"
            f" · {notifier.escape(p.get('date') or 'дата не указана')}"
            f"\n   пустой {p.get('empty_km', 0)} км · маржа {notifier.money(p.get('margin_usd'))}"
            + (f" · <b>{notifier.money(per_day)}/день</b>" if per_day is not None else ""))
        back = p.get("return")
        if isinstance(back, dict):
            lines.append(f"   🔁 обратно: {notifier.escape(_arrow(back['route']))} #{back['cargo_id']}"
                         f" · круг {notifier.money(p.get('round_margin_per_day'))}/день")
        else:
            lines.append("   🔁 обратного груза пока нет")

    if with_ai:
        try:
            import brain
            tip = brain.advise({"date": today.isoformat(), "last_24h": day, "plan": plan})
        except Exception:
            log.exception("AI maslahatida xato")
            tip = None
        if tip:
            lines.append(f"\n💡 <i>{notifier.escape(tip)}</i>")

    rows = [[{"text": f"✅ Беру #{o['cargo_id']} → №{o['truck_id']} ({o['label']})"[:60],
              "callback_data": f"take:{o['match_id']}"}] for o in ctx.offers[:6]]
    return "\n".join(lines), ({"inline_keyboard": rows} if rows else None)


def send_if_due(now_utc: datetime | None = None) -> bool:
    """Bot tsikli har aylanishda chaqiradi. Yuborsa True.

    Belgi yuborishdan OLDIN qo'yiladi: xato bo'lsa ham reja soatiga
    bir necha marta takrorlanmaydi (spam yomonroq, bitta kun o'tib ketsa —
    /brief bilan qo'lda so'rash mumkin).
    """
    if not due(now_utc):
        return False
    mark_sent(now_utc)
    text, keyboard = build(now_utc)
    notifier.send(text, keyboard)
    log.info("Ertalabki reja yuborildi")
    return True
