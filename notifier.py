"""
notifier.py — dispetcherga Telegram orqali xabar yuborish.

Kutubxona shart emas, oddiy HTTP so'rov (Bot API). Tugmalar: Batafsil /
Bog'lanish / O'tkazib yuborish.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from collections import deque

import config

log = logging.getLogger("notifier")

_SCORE_ICON = [(90, "🟢"), (75, "🟡"), (60, "🟠")]
_BODY_NAME = {"ref": "Реф", "tent": "Тент", "izoterm": "Изотерм", "bort": "Борт",
              "konteyner": "Контейнер", "tral": "Трал", "samosval": "Самосвал"}


def _icon(score: float) -> str:
    for limit, ic in _SCORE_ICON:
        if score >= limit:
            return ic
    return "⚪️"


def _borders_word(n: int) -> str:
    word = "граница" if n == 1 else ("границы" if n < 5 else "границ")
    return f"{n} {word}"


def money(v, cur="$") -> str:
    return f"{cur}{v:,.0f}".replace(",", " ") if v is not None else "—"


def price_text(cargo) -> str | None:
    """Yuk narxi asl valyutada: "5 000 $", "55 млн сум", "400 000 ₽" (yo'q bo'lsa None)."""
    c = dict(cargo) if not isinstance(cargo, dict) else cargo
    rate, cur = c.get("rate"), c.get("currency")
    if not rate or not cur:
        return None
    if cur == "USD":
        return money(rate)
    if cur == "UZS":
        return f"{rate / 1_000_000:g} млн сум"
    if cur == "RUB":
        return f"{rate:,.0f} ₽".replace(",", " ")
    return f"{rate:,.0f} {cur}".replace(",", " ")


def format_card(cargo: dict, result: dict, insight: list[str] | None = None) -> str:
    """Spec dagi kartochka ko'rinishi + marja qo'shilgan.

    `insight` — "nega aynan shu yuk" izohi (`insight.explain`).
    """
    body = _BODY_NAME.get(cargo.get("body_type"), cargo.get("body_type") or "—")
    temp = f" {cargo['temp_c']:+.0f}°C" if cargo.get("temp_c") is not None else ""
    weight = f"{cargo['weight_t']:g} т" if cargo.get("weight_t") else "вес не указан"
    rate = ""
    if cargo.get("rate") and cargo.get("currency"):
        if cargo["currency"] == "UZS":
            rate = f"{cargo['rate'] / 1_000_000:.1f} млн сум"
        else:
            rate = f"{cargo['rate']:,.0f} {cargo['currency']}".replace(",", " ")
        if cargo.get("rate_usd"):
            rate += f" (≈{money(cargo['rate_usd'])})"
    else:
        rate = "ставка не указана"

    lines = [
        "🔥 <b>НОВЫЙ ГРУЗ</b>",
        f"<b>{cargo.get('from_city')} → {cargo.get('to_city')}</b>",
        f"💵 <b>{rate}</b>",
        f"{body}{temp} · {weight}" + (f" · {cargo['load_date']}" if cargo.get("load_date") else ""),
        "",
        f"🚛 <b>Машина №{result['truck_id']}</b> — пустой пробег {result['empty_km']} км",
        f"{_icon(result['score'])} Подходит: <b>{result['score']:.0f}/100</b>",
        "",
        f"📏 {result['loaded_km']} км с грузом · всего {result['total_km']} км · "
        f"{result['trip_days']} дн.",
        f"⛽️ {result['fuel_l']} л ≈ {money(result['fuel_cost'])}"
        + (f" · 🛂 {_borders_word(result['borders'])} {money(result['border_cost'])}"
           if result['borders'] else ""),
        f"💸 Расходы: {money(result['total_cost'])} · Выручка: {money(result['revenue_usd'])}",
    ]
    if result.get("margin_usd") is not None:
        lines.append(f"💰 Маржа: <b>{money(result['margin_usd'])}</b>")
    for w in result.get("warnings", []):
        lines.append(f"⚠️ {w}")
    if insight:
        lines += ["", "<b>Почему этот груз:</b>"] + [escape(x) for x in insight]

    contact = []
    if cargo.get("phone"):
        contact.append(cargo["phone"])
    if cargo.get("username"):
        contact.append("@" + cargo["username"])
    if contact:
        lines.append(f"📞 {' · '.join(contact)}")
    lines.append(f"<i>источник: {cargo.get('source', '?')}</i>")
    return "\n".join(lines)


def _keyboard(cargo: dict, match_id: int | None) -> dict:
    row = [{"text": "📋 Подробнее", "callback_data": f"info:{cargo.get('id')}"}]
    if cargo.get("username"):
        row.append({"text": "📞 Связаться", "url": f"https://t.me/{cargo['username']}"})
    else:
        row.append({"text": "📞 Связаться", "callback_data": f"call:{cargo.get('id')}"})
    # Narx maslahati: qancha so'rash kerak + yuk egasiga tayyor matn
    row.append({"text": "💰 Цена", "callback_data": f"price:{cargo.get('id')}"})
    row2 = [
        {"text": "✅ Беру", "callback_data": f"take:{match_id}"},
        {"text": "⏭ Пропустить", "callback_data": f"skip:{match_id}"},
    ]
    return {"inline_keyboard": [row, row2]}


def _dispatcher_chats() -> list[str]:
    """Bildirishnoma boradigan chatlar: .env dagi qiymat yoki bazadagilar.

    Bazadagilar /start bosilganda avtomatik qo'shiladi — chat id ni qo'lda
    qidirish shart emas.
    """
    if config.DISPATCHER_CHAT_ID:
        return [config.DISPATCHER_CHAT_ID]
    try:
        import db
        return db.dispatcher_chats()
    except Exception:
        return []


def _send_to(chat: str, text: str, reply_markup, token: str) -> dict | None:
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    data = urllib.parse.urlencode(payload).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log.error("Telegram yuborishda xato (%s): %s", chat, e)
        return None


def send(text: str, reply_markup: dict | None = None, chat_id: str | None = None) -> dict | None:
    """Xabar yuboradi. `chat_id` berilmasa — barcha dispetcher chatlariga."""
    token = config.BOT_TOKEN
    chats = [chat_id] if chat_id else _dispatcher_chats()
    if not token or not chats:
        log.warning("BOT_TOKEN yoki dispetcher chati sozlanmagan — xabar yuborilmadi")
        print("\n--- TELEGRAM XABARI (demo) ---\n" + text + "\n")
        return None
    result = None
    for chat in chats:
        result = _send_to(chat, text, reply_markup, token)
    return result


# ---------------------------------------------------------------- cheklov

# Telegram Bot API: sekundiga ~30 xabar, bitta chatga daqiqasiga ~20.
# Undan muhimrog'i — dispetcher e'tibori: soatiga 40 ta kartochka kelsa
# u hech birini o'qimaydi. Ortiqchasi yig'ma xabarga tushadi.
MAX_NOTIFY_PER_HOUR = int(os.getenv("MAX_NOTIFY_PER_HOUR", "10"))
MIN_SEND_INTERVAL = float(os.getenv("MIN_SEND_INTERVAL", "1.0"))
DIGEST_INTERVAL_SEC = 1800

_sent_at: deque[float] = deque()
_suppressed = 0
_last_digest = 0.0
_last_send = 0.0


def reset_limits() -> None:
    """Hisoblagichlarni tozalaydi (testlar va qayta ishga tushirish uchun)."""
    global _suppressed, _last_digest, _last_send
    _sent_at.clear()
    _suppressed = 0
    _last_digest = 0.0
    _last_send = 0.0


def _pace() -> None:
    """Xabarlar orasida eng kam tanaffus — API limitiga urilmaslik uchun."""
    global _last_send
    wait = MIN_SEND_INTERVAL - (time.time() - _last_send)
    if wait > 0:
        time.sleep(wait)
    _last_send = time.time()


def _quota_left() -> bool:
    now = time.time()
    while _sent_at and now - _sent_at[0] > 3600:
        _sent_at.popleft()
    return len(_sent_at) < MAX_NOTIFY_PER_HOUR


def _maybe_digest() -> None:
    """Yig'ma xabar: "yana N ta mos yuk bor"."""
    global _suppressed, _last_digest
    if not _suppressed or time.time() - _last_digest < DIGEST_INTERVAL_SEC:
        return
    count, _suppressed = _suppressed, 0
    _last_digest = time.time()
    send(f"📨 Ещё <b>{count}</b> подходящих грузов за последний час.\n"
         f"Посмотреть: /list")


def notify_match(cargo: dict, result: dict, match_id: int | None = None,
                 reason: str | None = None, insight: list[str] | None = None) -> bool:
    """Kartochkani yuboradi. Soatlik chegara to'lgan bo'lsa — yig'ma xabarga.

    `reason` — kartochka tepasidagi qo'shimcha satr (masalan, dispetcher
    o'zi so'ragan yo'nalish bo'yicha topilgani).

    Yuborilgani `True` qaytaradi; `False` — moslik bazada qoldi, lekin
    xabar qilinmadi (dispetcher uni `/list` da ko'radi).
    """
    global _suppressed
    if not _quota_left():
        _suppressed += 1
        log.info("Soatlik chegara (%d) to'ldi — moslik yig'ma xabarga qo'shildi",
                 MAX_NOTIFY_PER_HOUR)
        _maybe_digest()
        return False

    _pace()
    _sent_at.append(time.time())
    card = format_card(cargo, result, insight)
    if reason:
        card = f"<b>{escape(reason)}</b>\n\n{card}"
    send(card, _keyboard(cargo, match_id))
    _maybe_digest()
    return True


# ---------------------------------------------------------------- bot javoblari

def escape(s) -> str:
    """HTML rejimida yuborilayotgan xom matn uchun."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def decided_keyboard(label: str) -> dict:
    """Qaror qabul qilingandan keyin tugmalar o'rniga holat ko'rsatiladi."""
    return {"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]}


def format_details(cargo: dict, result: dict | None = None) -> str:
    """📋 Подробнее — to'liq hisob va e'lonning asl matni."""
    lines = [f"📋 <b>Груз #{cargo.get('id')}</b>: "
             f"{cargo.get('from_city')} → {cargo.get('to_city')}"]
    via = cargo.get("via")
    if via and via not in ("[]", "null"):
        try:
            names = json.loads(via) if isinstance(via, str) else via
            if names:
                lines.append(f"через: {', '.join(names)}")
        except (ValueError, TypeError):
            pass

    if result:
        lines += [
            "",
            f"🚛 Машина №{result.get('truck_id')} · балл {result.get('score', 0):.0f}/100",
            f"📏 пустой {result.get('empty_km')} км + груз {result.get('loaded_km')} км "
            f"= {result.get('total_km')} км · {result.get('trip_days')} дн.",
            f"⛽️ топливо {result.get('fuel_l')} л — {money(result.get('fuel_cost'))}",
            f"🛂 границы ({result.get('borders')}) — {money(result.get('border_cost'))}",
            f"👨‍✈️ водитель — {money(result.get('driver_cost'))}",
            f"🛣 дорога — {money(result.get('road_cost'))}",
            f"📦 прочее — {money(result.get('fixed_cost'))}",
            f"💸 <b>итого расходы {money(result.get('total_cost'))}</b>",
            f"💵 выручка {money(result.get('revenue_usd'))}",
        ]
        if result.get("margin_usd") is not None:
            lines.append(f"💰 <b>маржа {money(result['margin_usd'])}</b> "
                         )
        if result.get("arrival_date"):
            lines.append(f"📅 машина на погрузке: {result['arrival_date']}")

    lines += ["", "<b>Оригинал объявления:</b>",
              f"<code>{escape(cargo.get('raw_text') or '—')}</code>",
              f"<i>источник: {escape(cargo.get('source', '?'))}</i>"]
    return "\n".join(lines)


def format_contact(cargo: dict) -> str:
    """📞 Связаться — raqam alohida xabarda, nusxa olish oson bo'lsin."""
    parts = []
    if cargo.get("phone"):
        parts.append(f"<code>{escape(cargo['phone'])}</code>")
    if cargo.get("username"):
        parts.append(f"@{escape(cargo['username'])}")
    if not parts:
        return (f"📞 У груза #{cargo.get('id')} контакт не указан.\n"
                f"Смотрите оригинал объявления: /info_{cargo.get('id')}")
    return (f"📞 <b>Контакт по грузу #{cargo.get('id')}</b>\n"
            f"{cargo.get('from_city')} → {cargo.get('to_city')}\n\n"
            + "\n".join(parts))


def format_taken(cargo: dict, truck: dict, free_date: str) -> str:
    """Reys olingandan keyingi tasdiq."""
    return (f"✅ <b>Машина №{truck.get('id')}</b> → {cargo.get('to_city')}\n"
            f"Груз #{cargo.get('id')}: {cargo.get('from_city')} → {cargo.get('to_city')}\n"
            f"Свободна с <b>{free_date}</b>\n"
            f"Водитель: {truck.get('driver') or '—'} "
            f"{truck.get('driver_phone') or ''}".strip())


def format_driver_trip(cargo: dict, truck: dict, free_date: str | None) -> str:
    """Haydovchiga: yangi reys (ruscha — bot tili)."""
    truck, cargo = dict(truck or {}), dict(cargo or {})     # sqlite3.Row ham bo'lishi mumkin
    body = _BODY_NAME.get(cargo.get("body_type"), cargo.get("body_type") or "")
    temp = f" {cargo['temp_c']:+.0f}°C" if cargo.get("temp_c") is not None else ""
    weight = f"{cargo['weight_t']:g} т" if cargo.get("weight_t") else ""
    what = " · ".join(x for x in (weight, f"{body}{temp}".strip()) if x) or "—"
    contact = " · ".join(x for x in (cargo.get("phone"),
                                     f"@{cargo['username']}" if cargo.get("username") else "")
                         if x) or "—"
    raw = escape((cargo.get("raw_text") or "")[:500])
    return (f"🚚 <b>Новый рейс — машина №{escape(truck.get('id'))}</b>\n\n"
            f"<b>{escape(cargo.get('from_city'))} → {escape(cargo.get('to_city'))}</b>\n"
            f"📅 Погрузка: {escape(cargo.get('load_date') or 'уточните у отправителя')}\n"
            f"📦 Груз: {escape(what)}\n"
            f"📞 Отправитель: {escape(contact)}\n"
            f"🏁 Освободитесь примерно: {escape(free_date or '—')}\n\n"
            f"<i>Объявление:</i>\n<code>{raw}</code>")


def format_driver_cancel(cargo: dict, truck: dict) -> str:
    truck, cargo = dict(truck or {}), dict(cargo or {})
    return (f"❌ <b>Рейс отменён — машина №{escape(truck.get('id'))}</b>\n"
            f"{escape(cargo.get('from_city'))} → {escape(cargo.get('to_city'))}\n"
            f"Ждите новое задание от диспетчера.")


def notify_driver(truck_id: str, text: str) -> bool:
    """Haydovchiga shaxsan (u botga `/link` bilan ulangan bo'lsa). Yuborilsa True."""
    import db
    truck = db.get_truck(truck_id)
    if truck is None or not truck["tg_user_id"]:
        return False
    try:
        result = send(text, chat_id=str(truck["tg_user_id"]))
        return bool(result and result.get("ok"))
    except Exception:
        log.exception("Haydovchiga yuborilmadi (fura %s)", truck_id)
        return False


def driver_hint(truck: dict) -> str:
    """Dispetcherga: haydovchi botga ulanmagan bo'lsa — nima qilish kerak."""
    truck = dict(truck or {})
    if truck.get("plate"):
        return (f"ℹ️ Водитель №{escape(truck.get('id'))} не подключён к боту — пусть откроет "
                f"бота и отправит: <code>/link {escape(truck.get('id'))} "
                f"{escape(truck.get('plate'))}</code>. Тогда рейсы будут приходить ему сами.")
    return (f"ℹ️ Водитель №{escape(truck.get('id'))} не подключён к боту. Сначала впишите "
            f"госномер машины в панели (Park → №{escape(truck.get('id'))} → Tahrirlash).")


def format_search(found: dict) -> str:
    """Dispetcher so'roviga javob: "Тошкент Москва юк топиб бер".

    Javob park bo'yicha hisoblangan: har bir yuk yonida qaysi mashina va
    kuniga qancha qoldirishi turadi.
    """
    query = found["query"]
    results = found["results"]
    if not results:
        lines = [f"🔍 <b>{escape(query.describe())}</b> — сейчас ничего нет."]
        if found["history"]:
            lines.append(f"\nЗа последние 7 дней такие грузы попадались "
                         f"<b>{found['history']}</b> раз — направление живое.")
        else:
            lines.append("\nЗа последние 7 дней таких грузов тоже не было.")
        if found["trucks"] == 0:
            lines.append("\n⚠️ Нет активных машин — проверьте /trucks")
        lines.append("\nМогу сообщить, как только появится — нажмите кнопку ниже.")
        return "\n".join(lines)

    lines = [f"🔍 <b>{escape(query.describe())}</b> — найдено "
             f"{len(results)} из {found['scanned']} (по {found['trucks']} машинам):"]
    for i, r in enumerate(results, 1):
        c = r["cargo"]
        body = _BODY_NAME.get(c.get("body_type"), c.get("body_type") or "—")
        temp = f" {c['temp_c']:+.0f}°" if c.get("temp_c") is not None else ""
        weight = f"{c['weight_t']:g} т" if c.get("weight_t") else "вес не указан"
        price = price_text(c)
        lines.append(
            f"\n{i}. <b>{escape(c['from_city'])} → {escape(c['to_city'])}</b>"
            + (f" · 💵 <b>{escape(price)}</b>" if price else " · цена не указана")
            + f"\n   {body}{temp} · {weight} · {escape(c.get('load_date') or 'дата не указана')}"
            f"\n   🚛 №{escape(r['truck_id'])} · пустой {r['empty_km']:.0f} км · "
            f"{r['trip_days']} дн."
            f"\n   💰 маржа {money(r['margin_usd'])}"
            + (f"\n   📞 {escape(c['phone'])}" if c.get("phone") else "")
            + f"\n   /cargo_{c['id']}")
    return "\n".join(lines)


def watch_keyboard(query) -> dict | None:
    """«Следить за направлением» tugmasi.

    Telegram callback uchun 64 baytdan oshmasligi kerak — oshsa tugma
    ko'rsatilmaydi (dispetcher `/watch` buyrug'idan foydalanadi).
    """
    data = (f"watch:{query.from_city or ''}|{query.to_city or ''}|"
            f"{query.body_type or ''}")
    if len(data.encode()) > 60 or not (query.from_city or query.to_city):
        return None
    return {"inline_keyboard": [[
        {"text": "🔔 Сообщить, когда появится", "callback_data": data}]]}


def format_fleet(trucks: list[dict]) -> str:
    """"Бизда нечта мошина бор?" — park holati."""
    if not trucks:
        return "Парк пуст — загрузите trucks.json (python main.py init)."
    active = [t for t in trucks if t["active"]]
    by_body: dict[str, int] = {}
    for t in active:
        by_body[t["body_type"] or "?"] = by_body.get(t["body_type"] or "?", 0) + 1
    summary = " · ".join(f"{_BODY_NAME.get(b, b)}: {n}" for b, n in sorted(by_body.items()))

    lines = [f"🚛 <b>Наш парк: {len(active)} машин(ы)</b>",
             summary if summary else ""]
    for t in trucks:
        mark = "" if t["active"] else " (выкл)"
        body = _BODY_NAME.get(t["body_type"], t["body_type"] or "—")
        if t.get("trip"):
            where = f"🚚 в рейсе: {escape(t['trip'])}, свободна {escape(t['free_date'] or '—')}"
        else:
            where = f"🟢 {escape(t['city'] or '—')}, свободна {escape(t['free_date'] or '—')}"
        lines.append(
            f"\n<b>№{escape(t['id'])}</b>{mark} · {body} {t['capacity_t'] or 0:g} т"
            f"\n   {where}")
    return "\n".join(x for x in lines if x)


def format_watches(watches: list) -> str:
    if not watches:
        return ("Сейчас я ничего не отслеживаю.\n"
                "Напишите, например: <code>Ташкент Москва реф</code>")
    lines = ["🔔 <b>Отслеживаю направления</b>"]
    for w in watches:
        route = " → ".join(x for x in (w["from_city"], w["to_city"]) if x) or "—"
        body = f" · {_BODY_NAME.get(w['body_type'], w['body_type'])}" if w["body_type"] else ""
        lines.append(f"\n#{w['id']} {escape(route)}{body}"
                     f"\n   найдено: {w['hits']} · до {escape((w['expires_at'] or '')[:10])}"
                     f" · убрать: /unwatch_{w['id']}")
    return "\n".join(lines)


def format_price(adv: dict) -> str:
    """"💰 Цена" tugmasi: qancha so'rash va yuk egasiga tayyor matn."""
    if "error" in adv:
        reasons = "; ".join(adv.get("reasons") or [])
        return ("💰 Не могу посчитать цену: ни одна машина не подходит."
                + (f"\n<i>{escape(reasons)}</i>" if reasons else ""))
    uzs = lambda key: f" (≈{adv[key]:g} млн сум)" if adv.get(key) else ""  # noqa: E731
    route = escape(adv["route"].replace(" -> ", " → "))
    lines = [f"💰 <b>Цена: {route}</b>",
             f"Считаю по машине №{escape(adv['truck'])}: пустой {adv['empty_km']} км, "
             f"{adv['trip_days']} дн.",
             "",
             f"🎯 <b>Просите: {money(adv['ask_usd'])}</b>{uzs('ask_uzs_mln')}",
             f"↘️ Ниже не уступать: {money(adv['floor_usd'])}{uzs('floor_uzs_mln')}",
             f"⚖️ В ноль (наши расходы): {money(adv['break_even_usd'])}",
             f"📌 Наша цель: {money(adv['target_usd'])} "
             ]
    m = adv.get("market") or {}
    if m.get("median_usd"):
        lines.append(f"📊 Рынок за 30 дн.: медиана {money(m['median_usd'])} "
                     f"({money(m['min_usd'])}–{money(m['max_usd'])}, {m['ads_30d']} объявл.)")
    elif m.get("by_rate_per_km_usd"):
        lines.append(f"📊 Рынок (по ставке за км): ≈{money(m['by_rate_per_km_usd'])}")
    else:
        lines.append("📊 По этому направлению рыночных данных пока мало")
    if adv.get("offered_usd"):
        pct = adv["offered_vs_target_pct"]
        mark = "✅ выше" if pct >= 0 else "⚠️ ниже"
        lines.append(f"В объявлении {money(adv['offered_usd'])} — {mark} нашей цели "
                     f"на {abs(pct)}%")
    if adv.get("warning"):
        lines.append("⚠️ Рынок платит заметно меньше нашей цели — рейс может быть невыгоден")
    lines += ["", "✉️ <b>Текст для владельца груза</b> (нажмите, чтобы скопировать):",
              f"<code>{escape(adv['message'])}</code>"]
    return "\n".join(lines)


def format_roundtrip(cargo: dict, chains: list[dict]) -> str:
    """Qaytish yuki takliflari — mashina bo'sh qaytmasin."""
    if not chains:
        return (f"🔁 Обратный груз из {cargo.get('to_city')} пока не найден.\n"
                f"Проверю снова, когда появятся новые объявления.")
    lines = [f"🔁 <b>Обратный груз из {cargo.get('to_city')}</b> "
             f"(оценка по текущим объявлениям):"]
    for i, ch in enumerate(chains, 1):
        b = ch["back_cargo"]
        lines.append(
            f"\n{i}. <b>{b.get('from_city')} → {b.get('to_city')}</b> — "
            f"{money(ch['leg2'].get('margin_usd'))}"
            f"\n   круг: <b>{money(ch['total_margin_usd'])}</b> за {ch['total_days']} дн."
            f"\n   пустой пробег в круге: {ch['empty_km']} км · груз #{b.get('id')}"
            + (f" · 📞 {b['phone']}" if b.get("phone") else ""))
    lines.append("\n<i>Это оценка: груз может уйти, пока машина в пути.</i>")
    return "\n".join(lines)
