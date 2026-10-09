"""
bot.py — dispetcher boti: kartochkadagi tugmalar va buyruqlar.

`listener.py` guruhlarni o'qiydi va kartochka yuboradi; bu modul esa
dispetcherning javobini qayta ishlaydi. Ikkalasi alohida process'da
ishlaydi (`python main.py listen` va `python main.py bot`).

Webhook emas, `getUpdates` long-polling: VPS'da domen va sertifikat
kerak bo'lmaydi. Kutubxona ham kerak emas — oddiy HTTP so'rov.

Tugmalar (callback formatlari `notifier._keyboard()` da belgilangan):
    info:<cargo_id>   to'liq hisob + e'lonning asl matni
    call:<cargo_id>   telefon raqami alohida xabarda
    take:<match_id>   reysni olish: mashina holati yangilanadi
    skip:<match_id>   o'tkazib yuborish (statistika uchun yoziladi)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request

import actions
import config
import db
import geo
import notifier
import parser as ad_parser

log = logging.getLogger("bot")

OFFSET_FILE = config.BASE_DIR / ".bot_offset"
POLL_TIMEOUT = 25            # getUpdates necha soniya kutadi
EXPIRE_EVERY_SEC = 600       # eskirgan yuklarni qanchada bir tozalaymiz
GPS_EVERY_SEC = 1800         # GPS holatini qanchada bir yangilaymiz
LEARN_EVERY_SEC = 24 * 3600  # qarorlardan o'rganish — kuniga bir marta
RETURNS_EVERY_SEC = 3600     # qaytish yuki eslatmasi — soatiga bir tekshiruv
ROUNDTRIP_TOP = 3


# ---------------------------------------------------------------- Bot API

def api(method: str, http_timeout: int = 15, **params) -> dict:
    """Telegram Bot API chaqiruvi. Xato bo'lsa bo'sh dict qaytaradi.

    `http_timeout` — HTTP so'rovi uchun; Telegram'ning o'z `timeout`
    parametri (long-polling) `params` orqali uzatiladi.
    """
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN sozlanmagan — .env ga yozing")
    # Chiqish ruscha: shahar nomlari "Ташкент" (notifier.localize)
    if "text" in params:
        params["text"] = notifier.localize(params["text"])
    if "reply_markup" in params:
        params["reply_markup"] = notifier.localize_markup(params["reply_markup"])
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}"
    data = json.dumps(params).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        log.error("%s xatosi: %s %s", method, e.code, body)
    except Exception as e:
        log.error("%s xatosi: %s", method, e)
    return {}


def send(chat_id, text: str, keyboard: dict | None = None) -> dict:
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True}
    if keyboard:
        params["reply_markup"] = keyboard
    return api("sendMessage", **params)


def answer(callback_id: str, text: str = "", alert: bool = False) -> None:
    """Tugma bosilganidagi javob. Telegram 2 soniya kutadi — kechiksa
    foydalanuvchi "soat" belgisini ko'raveradi."""
    api("answerCallbackQuery", http_timeout=8, callback_query_id=callback_id,
        text=text[:200], show_alert=alert)


def set_keyboard(chat_id, message_id: int, keyboard: dict) -> None:
    api("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
        reply_markup=keyboard)


# ---------------------------------------------------------------- ruxsat

def allowed(chat_id) -> bool:
    """Faqat ro'yxatdagi dispetcher chatlariga javob beramiz.

    Bot havolasi tarqalib ketsa begona odam mashinalarni "band" qilib
    qo'yishi mumkin. Ro'yxat hali bo'sh bo'lsa — birinchi /start uni to'ldiradi.
    """
    if config.DISPATCHER_CHAT_ID:
        return str(chat_id) == str(config.DISPATCHER_CHAT_ID)
    chats = db.dispatcher_chats()
    return not chats or str(chat_id) in chats


# ---------------------------------------------------------------- qulaylik: menyu

# Doimiy tugmalar — dispetcher komanda yodlamasin, bossin. Tugma matni
# oddiy xabar bo'lib keladi, shuning uchun `MENU` orqali yo'naltiramiz.
KB_SEARCH = "🔍 Поиск"
KB_LIST = "📋 Список"
KB_FLEET = "🚛 Парк"
KB_STATS = "📊 Статистика"
KB_WATCH = "🔔 Отслеживание"
KB_HELP = "❓ Помощь"
KB_PLAN = "🧠 План"
KB_RULES = "📜 Правила"
# Eski (o'zbekcha) tugmalar — telefonda klaviatura yangilanguncha ular ham ishlasin
KB_LEGACY = {"🔍 Qidiruv": KB_SEARCH, "📋 Ro'yxat": KB_LIST, "🚛 Park": KB_FLEET,
             "📊 Statistika": KB_STATS, "🔔 Kuzatuvlar": KB_WATCH, "❓ Yordam": KB_HELP,
             "🧠 Reja": KB_PLAN, "📜 Qoidalar": KB_RULES}


def main_keyboard() -> dict:
    return {
        "keyboard": [[{"text": KB_SEARCH}, {"text": KB_PLAN}],
                     [{"text": KB_LIST}, {"text": KB_FLEET}],
                     [{"text": KB_RULES}, {"text": KB_WATCH}],
                     [{"text": KB_STATS}, {"text": KB_HELP}]],
        "resize_keyboard": True, "is_persistent": True,
    }


def _web_secret() -> bytes:
    """web.py bilan bir xil sirni hisoblaydi — magic-havola imzosi uchun."""
    raw = os.getenv("WEB_SECRET") or f"{os.getenv('WEB_PASSWORD', '')}|{config.BOT_TOKEN}|baxt"
    return hashlib.sha256(raw.encode()).digest()


def panel_link() -> str | None:
    """Parolsiz kirish havolasi (15 daqiqa amal qiladi).

    Dispetcher bot tugmasini bossa — panel darhol ochiladi, parol so'ralmaydi.
    """
    base = os.getenv("PANEL_URL", "").strip().rstrip("/")
    if not base:
        return None
    exp = int(time.time() + 15 * 60)
    sig = hmac.new(_web_secret(), b"m" + str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{base}/enter?t={exp}.{sig}"


def panel_button(chat_id=None) -> dict | None:
    """Shaxsiy chatda — Telegram ichida ochiladigan Web App tugmasi
    (kirish Telegram imzosi bilan, `web.telegram_user`), yonida brauzer
    uchun magic-havola. Guruhda Web App tugmasi ishlamaydi — faqat havola."""
    link = panel_link()
    if not link:
        return None
    base = link.split("/enter?", 1)[0]
    row = [{"text": "🌐 Браузер", "url": link}]
    try:
        private = int(chat_id) > 0
    except (TypeError, ValueError):
        private = False
    if private:
        row.insert(0, {"text": "📱 Открыть панель", "web_app": {"url": base + "/"}})
    return {"inline_keyboard": [row]}


# ---------------------------------------------------------------- tugmalar

def on_info(cargo_id: int, chat_id) -> str:
    cargo = db.get_cargo(cargo_id)
    if cargo is None:
        return "Груз не найден"
    row = _best_match_row(cargo_id)
    details = json.loads(row["details"]) if row and row["details"] else None
    send(chat_id, notifier.format_details(dict(cargo), details))
    return "Отправил"


def on_call(cargo_id: int, chat_id) -> str:
    cargo = db.get_cargo(cargo_id)
    if cargo is None:
        return "Груз не найден"
    send(chat_id, notifier.format_contact(dict(cargo)))
    return "Контакт отправлен"


def _decided(markup: dict | None, data: str, label: str) -> dict:
    """Qaror qabul qilingan tugma o'rniga yozuv.

    AI javobida bir nechta taklif bo'ladi (har furaga bittadan) — bittasini
    olganda qolganlari yo'qolmasligi kerak. Oddiy kartochkada esa butun
    klaviatura yozuvga almashadi.
    """
    rows = (markup or {}).get("inline_keyboard") or []
    if len(rows) <= 1:
        return notifier.decided_keyboard(label)
    out = []
    for row in rows:
        if any(b.get("callback_data") == data for b in row):
            out.append([{"text": label, "callback_data": "noop"}])
        else:
            out.append(row)
    return {"inline_keyboard": out}


def trip_keyboard(match_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "↩️ Отменить (ошибка)", "callback_data": f"undo:{match_id}"},
        {"text": "🏁 Рейс завершён", "callback_data": f"finish:{match_id}"}]]}


def on_undo(match_id: int, chat_id, message_id: int, markup: dict | None = None) -> str:
    """↩️ Xato bosilgan "Беру" — yuk yana bo'sh, fura oldingi joyida."""
    import pipeline
    m = db.get_match(match_id)
    status = actions.undo_take(match_id)
    if status == "ok":
        set_keyboard(chat_id, message_id,
                     notifier.decided_keyboard("↩️ Рейс отменён — груз снова свободен"))
        cargo = db.get_cargo(m["cargo_id"])
        if cargo is not None:
            notifier.notify_driver(m["truck_id"], notifier.format_driver_cancel(
                dict(cargo), {"id": m["truck_id"]}))
        try:
            pipeline.rematch_cargo(m["cargo_id"])
            pipeline.rematch_all(48, [m["truck_id"]])
        except Exception:
            log.exception("Bekor qilingandan keyin qayta hisobda xato")
        return "Отменено"
    return {"not_latest": "Сначала отмените следующий рейс этой машины",
            "finished": "Рейс уже завершён — отменить нельзя",
            "not_taken": "Этот рейс не активен"}.get(status, "Не найдено")


def on_finish(match_id: int, chat_id, message_id: int) -> str:
    status = actions.finish_trip(match_id)
    if status in ("ok", "finished"):
        set_keyboard(chat_id, message_id, notifier.decided_keyboard("🏁 Рейс завершён"))
        if status == "ok":
            send(chat_id, f"🏁 Рейс завершён. Фактическую маржу можно записать: "
                          f"<code>/done {match_id} 1850</code>")
        return "Готово"
    return "Этот рейс не активен"


def on_price(cargo_id: int, chat_id) -> str:
    """💰 Цена — qancha so'rash kerak (AI'siz, aniq hisob)."""
    import ai_tools
    if db.get_cargo(cargo_id) is None:
        return "Груз не найден"
    send(chat_id, notifier.format_price(ai_tools.price_advice(ai_tools.Ctx(), cargo_id=cargo_id)))
    return "Посчитал"


def on_take(match_id: int, chat_id, message_id: int, markup: dict | None = None) -> str:
    """✅ Беру — reysni biriktirish.

    Ketma-ketlik muhim: avval yukni "band" qilamiz (tez), keyin sekin
    ishlar (qaytish yukini qidirish) bajariladi — tugma javobi kechikmasin.
    """
    res = actions.take_match(match_id)
    if res.reason == "not_found":
        return "Заявка не найдена"
    if res.reason == "stale":
        set_keyboard(chat_id, message_id,
                     _decided(markup, f"take:{match_id}", "⌛ Машина уже взяла другой рейс"))
        return "Машина уже занята другим рейсом — спросите новый план"
    if res.reason == "cargo_missing":
        return "Груз не найден"
    if res.reason == "already_taken":
        holder = res.holder or "—"
        set_keyboard(chat_id, message_id,
                     _decided(markup, f"take:{match_id}", f"✅ Уже взято — машина №{holder}"))
        return f"Этот груз уже взят (машина №{holder})"

    truck_id = res.match["truck_id"]
    set_keyboard(chat_id, message_id,
                 _decided(markup, f"take:{match_id}", f"✅ Взято — машина №{truck_id}"))
    driver_ok = notifier.notify_driver(
        truck_id, notifier.format_driver_trip(res.cargo, db.get_truck(truck_id) or {},
                                              res.free_date))
    note = ("\n📨 Водителю отправлено." if driver_ok else
            "\n\n" + notifier.driver_hint(dict(db.get_truck(truck_id) or {"id": truck_id})))
    send(chat_id, notifier.format_taken(res.cargo, res.truck or {}, res.free_date) + note,
         trip_keyboard(match_id))

    # qaytish yuki — mashina bo'sh qaytmasligi uchun darhol taklif qilamiz
    try:
        chains = actions.roundtrip_for(res, top=ROUNDTRIP_TOP)
        send(chat_id, notifier.format_roundtrip(res.cargo, chains))
    except Exception:
        log.exception("Qaytish yukini hisoblashda xato")

    return f"Машина №{truck_id} назначена"


def on_skip(match_id: int, chat_id, message_id: int) -> str:
    status = actions.skip_match(match_id)
    if status == "not_found":
        return "Заявка не найдена"
    if status == "decided":
        return "Решение уже принято"
    set_keyboard(chat_id, message_id, notifier.decided_keyboard("⏭ Пропущено"))
    return "Пропущено"


def on_rule(rule_id: int, action: str, chat_id, message_id: int,
            markup: dict | None = None, data: str = "") -> str:
    """Qoida tugmalari: o'rganilgan taklifni qabul/rad qilish, qoidani bekor qilish."""
    import rules
    rule = rules.get(rule_id)
    if rule is None:
        set_keyboard(chat_id, message_id, _decided(markup, data, "Правило уже удалено"))
        return "Правило не найдено"
    if action == "delete":
        rules.delete(rule_id)
        label, answer_text = f"↩️ Правило #{rule_id} отменено", "Отменено"
    elif action == "active":
        rules.set_status(rule_id, "active")
        label, answer_text = f"✅ Правило #{rule_id} работает", "Применено"
    else:
        rules.set_status(rule_id, "rejected")
        label, answer_text = f"❌ #{rule_id} отклонено — больше не предложу", "Понял"
    if action != "delete" and markup and len(markup.get("inline_keyboard") or []) > 1:
        # "✅ / ❌" bir qatorda — qatorni butunlay almashtiramiz
        rows = [[{"text": label, "callback_data": "noop"}]
                if any(b.get("callback_data", "").endswith(f":{rule_id}")
                       and b.get("callback_data", "").startswith("rule_") for b in row)
                else row for row in markup["inline_keyboard"]]
        set_keyboard(chat_id, message_id, {"inline_keyboard": rows})
    else:
        set_keyboard(chat_id, message_id, _decided(markup, data, label))
    return answer_text


def _on_watch_button(chat_id, arg: str) -> str:
    """«Сообщить, когда появится» — callback: `watch:<from>|<to>|<body>`."""
    import search
    parts = (arg.split("|") + ["", "", ""])[:3]
    query = search.Query(from_city=parts[0] or None, to_city=parts[1] or None,
                         body_type=parts[2] or None)
    if query.is_empty:
        return "Кнопка устарела"
    return _add_watch(chat_id, query, query.describe())


def _best_match_row(cargo_id: int):
    with db.connect() as conn:
        return conn.execute(
            "SELECT * FROM matches WHERE cargo_id=? ORDER BY score DESC LIMIT 1",
            (cargo_id,)).fetchone()


# ---------------------------------------------------------------- buyruqlar

HELP = """🚛 <b>BAXT TRANSPORT — диспетчер</b>

Пишите обычными словами — по-узбекски или по-русски:
• <code>01 Москвада, атрофидан яхши юк топ</code>
• <code>Ташкент Москва реф</code>
• <code>Россиядан 30 млн дан арзон юк олма</code> — я запомню как правило
• <code>Ҳар фурага юк ва қайтиш юкини режала</code>
• <code>12-юкка қанча сўрайлик?</code> — цена и готовый текст владельцу
🎙 Можно <b>голосом</b> — просто отправьте голосовое сообщение.

Я считаю по нашим машинам и советую самое выгодное — с учётом пустого пробега
(не самое дорогое). Груз берёте вы — кнопкой «✅ Беру».

<b>Кнопки внизу:</b>
🔍 Поиск · 🧠 План — груз + обратный для каждой машины
📋 Список — лучшие предложения · 🚛 Парк — где машины
📜 Правила — что я учитываю · 🔔 Слежу — направления
📊 Статистика · ❓ Помощь

<b>Каждое утро в 08:00</b> пришлю план на день: груз + обратный для каждой
машины и совет. Сейчас — /brief

<b>Я учусь:</b> по вашим «Беру/Пропустить» замечаю, что вам нравится,
и предлагаю правило — вы подтверждаете кнопкой.

<b>Под карточкой груза:</b>
✅ Беру — назначает машину и сразу ищет обратный груз
⏭ Пропустить — запоминает решение для статистики

<b>Команды (по желанию):</b>
/group @группа — читать новую группу Telegram (/groups — список)
/pos 01 Казань 22.09 — положение машины вручную
/done 42 1850 — фактическая маржа по рейсу
/panel — открыть веб-панель без пароля"""

WELCOME = """👋 <b>Добро пожаловать в BAXT TRANSPORT!</b>

Я помогаю найти самый выгодный груз для наших машин —
не самый дорогой, а самый выгодный с учётом пустого пробега и дней в пути.

Напишите направление, например <code>Ташкент Москва</code>,
или пользуйтесь кнопками внизу 👇"""


def cmd_start(chat_id, args: list[str]) -> None:
    # Birinchi /start — shu chatni bildirishnoma ro'yxatiga qo'shamiz.
    # Bu chat id ni qo'lda qidirishni bekor qiladi.
    if not config.DISPATCHER_CHAT_ID and db.add_dispatcher_chat(chat_id):
        log.info("Yangi dispetcher chati ro'yxatga olindi: %s", chat_id)
    send(chat_id, WELCOME, main_keyboard())
    link = panel_button(chat_id)
    if link:
        send(chat_id, "🌐 Веб-панель (открывается без пароля):", link)


def cmd_help(chat_id, args: list[str]) -> None:
    send(chat_id, HELP, main_keyboard())


def cmd_panel(chat_id, args: list[str]) -> None:
    btn = panel_button(chat_id)
    if btn:
        send(chat_id, "🌐 Панель: «Открыть» — внутри Telegram, «Браузер» — "
                      "ссылка на 15 минут:", btn)
    else:
        send(chat_id, "Веб-панель не настроена (PANEL_URL).")


def cmd_list(chat_id, args: list[str]) -> None:
    rows = db.top_matches(limit=10)
    if not rows:
        send(chat_id, "Сейчас подходящих грузов нет.")
        return
    lines = ["📋 <b>Актуальные предложения</b>"]
    for r in rows:
        lines.append(
            f"\n<b>{r['score']:.0f}</b> · {r['from_city']} → {r['to_city']} · "
            f"машина №{r['truck_id']}"
            f"\n   маржа {notifier.money(r['margin_usd'])} · пустой {r['empty_km']:.0f} км"
            f" · груз #{r['cargo_id']} · заявка #{r['id']}")
    send(chat_id, "\n".join(lines))


def cmd_trucks(chat_id, args: list[str]) -> None:
    trucks = db.get_trucks(active_only=False)
    if not trucks:
        send(chat_id, "Парк пуст — загрузите trucks.json (python main.py init).")
        return
    lines = ["🚛 <b>Парк</b>"]
    for t in trucks:
        mark = "" if t["active"] else " (выкл)"
        src = {"gps": "GPS", "manual": "вручную", "trip": "после рейса"}.get(
            t["pos_source"] or "", "—")
        lines.append(
            f"\n<b>№{t['id']}</b>{mark} · {t['body_type']} {t['capacity_t']:g} т"
            f"\n   {t['current_city'] or '—'}, свободна {t['free_date'] or '—'}"
            f"\n   водитель: {t['driver'] or '—'} · положение: {src}")
    send(chat_id, "\n".join(lines))


def cmd_pos(chat_id, args: list[str]) -> None:
    """/pos 01 Казань 22.09 — holatni qo'lda yangilash.

    GPS ulanmagan paytda asosiy usul, ulangandan keyin ham zaxira bo'lib
    qoladi (haydovchi telefoni o'chsa).
    """
    if len(args) < 2:
        send(chat_id, "Формат: <code>/pos 01 Казань 22.09</code>")
        return
    truck_id = args[0].lstrip("№#")
    truck = db.get_truck(truck_id)
    if truck is None:
        send(chat_id, f"Машина №{truck_id} не найдена.")
        return

    rest = " ".join(args[1:])
    cities = geo.find_cities(rest)
    if not cities:
        send(chat_id, f"Не понял город: «{notifier.escape(rest)}»")
        return
    city = cities[0][1]
    free = ad_parser._parse_date(ad_parser.normalize(rest))

    db.set_truck_position(truck_id, city=city,
                          free_date=free.isoformat() if free else None,
                          source="manual")
    updated = db.get_truck(truck_id)
    send(chat_id, f"📍 Машина №{truck_id}: {city}, "
                  f"свободна {updated['free_date'] or '—'}")


def cmd_cargo(chat_id, args: list[str]) -> None:
    if not args or not args[0].lstrip("#").isdigit():
        send(chat_id, "Формат: <code>/cargo 15</code>")
        return
    cargo_id = int(args[0].lstrip("#"))
    if on_info(cargo_id, chat_id) == "Груз не найден":
        send(chat_id, f"Груз #{cargo_id} не найден.")


def cmd_done(chat_id, args: list[str]) -> None:
    """/done 42 1850 — reys tugagach haqiqiy marja.

    Prognoz va haqiqat farqi — xarajat parametrlarini sozlash uchun yagona
    ishonchli manba (TZ 5.6.4).
    """
    if len(args) < 2:
        send(chat_id, "Формат: <code>/done 42 1850</code> — заявка и маржа в $")
        return
    try:
        match_id, margin = int(args[0].lstrip("#")), float(args[1].replace(",", "."))
    except ValueError:
        send(chat_id, "Не понял числа. Пример: <code>/done 42 1850</code>")
        return
    m = db.get_match(match_id)
    if m is None:
        send(chat_id, f"Заявка #{match_id} не найдена.")
        return
    if m["decision"] == "taken":
        actions.finish_trip(match_id, margin)
    else:
        db.set_actual_margin(match_id, margin)
    planned = m["margin_usd"]
    diff = f" (план {notifier.money(planned)})" if planned is not None else ""
    send(chat_id, f"💰 Заявка #{match_id}: факт {notifier.money(margin)}{diff}")


def cmd_expire(chat_id, args: list[str]) -> None:
    n = db.expire_old_cargos()
    send(chat_id, f"🧹 Устаревших объявлений убрано: {n}")


def cmd_find(chat_id, args: list[str]) -> None:
    """/find Ташкент Москва реф — so'rov bo'yicha yuk qidirish.

    Dispetcher buyruqsiz, oddiy matn bilan ham yozishi mumkin —
    `handle_message` uni shu yerga yo'naltiradi.
    """
    import search
    text = " ".join(args).strip()
    if not text:
        send(chat_id, "Напишите направление, например: "
                      "<code>Ташкент Москва реф</code>")
        return

    query = search.parse_query(text)
    if query.is_empty:
        send(chat_id, f"Не понял «{notifier.escape(text)}». "
                      f"Напишите города: <code>Ташкент Москва</code>")
        return

    found = search.find(query)
    send(chat_id, notifier.format_search(found),
         None if found["results"] else notifier.watch_keyboard(query))


def cmd_fleet(chat_id, args: list[str]) -> None:
    """Park holati — "бизда нечта мошина бор"."""
    import search
    send(chat_id, notifier.format_fleet(search.fleet_summary()))


def cmd_watch(chat_id, args: list[str]) -> None:
    """/watch Ташкент Москва — yo'nalishni kuzatuvga qo'yish."""
    import search
    text = " ".join(args).strip()
    query = search.parse_query(text)
    if query.is_empty:
        send(chat_id, "Формат: <code>/watch Ташкент Москва реф</code>")
        return
    _add_watch(chat_id, query, text)


def cmd_group(chat_id, args: list[str]) -> None:
    """/group @name | t.me/... | t.me/+HASH — yangi guruhni kuzatishga qo'shish."""
    import sources
    if not args:
        send(chat_id, "Пришлите ссылку на группу: <code>/group @logistika_uz</code> или "
                      "<code>/group https://t.me/+AbCdEf…</code>\n"
                      "Закрытую группу — по ссылке-приглашению: аккаунт системы вступит сам.")
        return
    sid, error = sources.add(args[0])
    if error:
        send(chat_id, f"⚠️ {notifier.escape(error)}")
        return
    send(chat_id, "✅ Группа добавлена. Подключусь в течение минуты и сразу прочитаю "
                  "объявления за последние сутки. Список: /groups")


def cmd_groups(chat_id, args: list[str]) -> None:
    """Kuzatilayotgan guruhlar va holati."""
    import sources
    rows = sources.list_sources()
    if not rows:
        send(chat_id, "Группы пока не добавлены. Пример: <code>/group @logistika_uz</code>")
        return
    mark = {"active": "🟢", "pending": "🟡", "error": "🔴"}
    lines = [f"📡 <b>Группы</b> ({sum(1 for r in rows if r['status'] == 'active')} читаются)"]
    for r in rows:
        name = notifier.escape(r["title"] or sources.describe_ref(r["ref"]))
        extra = f" — {notifier.escape(r['error'])}" if r["status"] == "error" and r["error"] else ""
        lines.append(f"{mark.get(r['status'], '⚪')} {name}{extra}")
    lines.append("\nДобавить: <code>/group ссылка</code> · управлять — в панели: Ещё → Группы")
    send(chat_id, "\n".join(lines))


def cmd_watches(chat_id, args: list[str]) -> None:
    send(chat_id, notifier.format_watches(db.active_watches()))


def cmd_unwatch(chat_id, args: list[str]) -> None:
    if not args or not args[0].lstrip("#").isdigit():
        send(chat_id, "Формат: <code>/unwatch 3</code>")
        return
    watch_id = int(args[0].lstrip("#"))
    if db.delete_watch(watch_id):
        send(chat_id, f"🔕 Запрос #{watch_id} убран.")
    else:
        send(chat_id, f"Запрос #{watch_id} не найден.")


def _add_watch(chat_id, query, text: str) -> str:
    """Kuzatuvni qo'shadi (takrorlanmasin)."""
    for w in db.active_watches():
        if (w["from_city"], w["to_city"], w["body_type"]) == \
                (query.from_city, query.to_city, query.body_type):
            send(chat_id, f"Уже отслеживаю: {notifier.escape(query.describe())} "
                          f"(#{w['id']})")
            return "Уже отслеживаю"

    watch_id = db.add_watch(query.from_city, query.to_city, query.body_type,
                            query=text or query.describe(), chat_id=chat_id)
    send(chat_id, f"🔔 Хорошо. Сообщу, как только появится: "
                  f"<b>{notifier.escape(query.describe())}</b>\n"
                  f"Убрать: /unwatch_{watch_id}")
    return "Буду следить"


def cmd_gps(chat_id, args: list[str]) -> None:
    """GPS holatini qo'lda sinxronlash va ko'rsatish."""
    import gps
    report = gps.format_report(gps.sync())
    send(chat_id, report or "📍 Изменений по GPS нет.")


# ---------------------------------------------------------------- haydovchi

def _plate_key(plate: str) -> str:
    return "".join(ch for ch in (plate or "").lower() if ch.isalnum())


def cmd_link(chat_id, user_id, args: list[str]) -> None:
    """/link 01 01A123AA — haydovchi o'z akkauntini mashinaga bog'laydi.

    Davlat raqami tasdiq sifatida so'raladi: bot havolasi begonaga
    tushib qolsa, u mashinaning joylashuvini soxtalashtira olmasin.
    """
    if len(args) < 2:
        send(chat_id, "Формат: <code>/link 01 01A123AA</code> "
                      "(номер машины и госномер)")
        return
    truck = db.get_truck(args[0].lstrip("№#").zfill(2) if args[0].lstrip("№#").isdigit()
                         else args[0].lstrip("№#"))
    if truck is not None and not truck["plate"]:
        send(chat_id, "Для этой машины ещё не указан госномер. Попросите диспетчера "
                      "вписать его в панели, затем повторите /link.")
        return
    if truck is None or _plate_key(truck["plate"]) != _plate_key(args[1]):
        send(chat_id, "Машина или госномер не совпадают.")
        return
    if not user_id:
        send(chat_id, "Не вижу ваш Telegram ID.")
        return

    db.link_truck_tg_user(truck["id"], user_id)
    send(chat_id, f"✅ Машина №{truck['id']} привязана. Новые рейсы будут приходить "
                  f"вам сюда автоматически.\n\n"
                  f"Теперь отправьте <b>Live Location</b> (скрепка → "
                  f"Геопозиция → Транслировать), и диспетчер будет видеть, "
                  f"где вы находитесь.")


def on_location(msg: dict) -> None:
    """Haydovchidan kelgan joylashuv — bazaga yoziladi.

    Live Location har necha daqiqada `edited_message` bo'lib keladi,
    shuning uchun javob faqat birinchi xabarga beriladi (spam bo'lmasin).
    """
    loc = msg.get("location") or {}
    user_id = (msg.get("from") or {}).get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    if "latitude" not in loc or "longitude" not in loc:
        return

    truck = db.truck_by_tg_user(user_id) if user_id else None
    if truck is None:
        send(chat_id, "Сначала привяжите машину: <code>/link 01 01A123AA</code>")
        return

    db.save_gps_position(truck["id"], float(loc["latitude"]),
                         float(loc["longitude"]), source="telegram")
    if not msg.get("_edited"):
        import geo as _geo
        city = _geo.nearest_city(loc["latitude"], loc["longitude"])
        send(chat_id, f"📍 Принято, машина №{truck['id']}"
                      + (f" — рядом {city}" if city else ""))


def cmd_stats(chat_id, args: list[str]) -> None:
    days = int(args[0]) if args and args[0].isdigit() else 30
    try:
        import analytics
    except ImportError:
        send(chat_id, "Модуль статистики недоступен.")
        return
    send(chat_id, analytics.format_report(analytics.report(days=days)))


# ---------------------------------------------------------------- AI yurak

PLAN_PROMPT = ("Составь план: для каждой свободной машины лучший груз + обратный груз. "
               "Коротко, по машинам.")


def _ai(chat_id, text: str) -> bool:
    """Savolni AI ga beradi. Javob yuborilgan bo'lsa True.

    AI ulanmagan yoki ishlamasa False — chaqiruvchi oddiy qidiruvga qaytadi.
    """
    try:
        import brain
        if not brain.enabled():
            return False
        api("sendChatAction", http_timeout=5, chat_id=chat_id, action="typing")
        res = brain.reply(chat_id, text)
    except Exception:
        log.exception("AI xatosi")
        return False
    if res is None:
        return False
    resp = send(chat_id, res.text, res.keyboard)
    if not resp.get("ok"):
        # HTML buzilgan bo'lsa — teglarsiz qayta yuboramiz
        import re
        plain = re.sub(r"</?[a-z]+>", "", res.text)
        api("sendMessage", chat_id=chat_id, text=plain[:4000],
            disable_web_page_preview=True,
            **({"reply_markup": res.keyboard} if res.keyboard else {}))
    return True


MAX_VOICE_SEC = 120
MAX_FILE_BYTES = 10 * 1024 * 1024


def download_file(file_id: str) -> bytes | None:
    """Telegram faylini yuklab oladi (ovozli xabar). Xato bo'lsa None."""
    info = api("getFile", file_id=file_id).get("result") or {}
    path = info.get("file_path")
    if not path or (info.get("file_size") or 0) > MAX_FILE_BYTES:
        return None
    url = f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{path}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read(MAX_FILE_BYTES + 1)[:MAX_FILE_BYTES]
    except Exception as e:
        log.warning("Faylni yuklab bo'lmadi: %s", e)
        return None


def on_voice(msg: dict) -> None:
    """🎙 Ovozli xabar → matn (Whisper) → AI javobi."""
    import brain
    chat_id = (msg.get("chat") or {}).get("id")
    media = msg.get("voice") or msg.get("audio") or {}
    if (media.get("duration") or 0) > MAX_VOICE_SEC:
        send(chat_id, "🎙 Слишком длинное сообщение — до 2 минут, пожалуйста.")
        return
    if not os.getenv("GROQ_API_KEY"):
        send(chat_id, "🎙 Голосовые пока не подключены — напишите текстом.")
        return
    api("sendChatAction", http_timeout=5, chat_id=chat_id, action="typing")
    audio = download_file(media.get("file_id", ""))
    text = brain.transcribe(audio, "voice.ogg") if audio else None
    if not text:
        send(chat_id, "🎙 Не расслышал. Повторите или напишите текстом.")
        return
    # Nima eshitilganini ko'rsatamiz — xato eshitilgan bo'lsa rahbar darhol ko'radi
    send(chat_id, f"🎙 <i>{notifier.escape(text)}</i>")
    if not _ai(chat_id, text):
        _free_text(chat_id, text, "voice")


def cmd_plan(chat_id, args: list[str]) -> None:
    """🧠 Har furaga yuk + qaytish yuki."""
    if _ai(chat_id, PLAN_PROMPT):
        return
    import ai_tools
    ctx = ai_tools.Ctx(chat_id=str(chat_id))
    plan = ai_tools.plan_fleet(ctx)["plan"]
    send(chat_id, format_plan(plan), _offer_keyboard(ctx))


def cmd_brief(chat_id, args: list[str]) -> None:
    """Ertalabki rejani hozir ko'rsatish (vaqtini kutmasdan)."""
    import briefing
    import settings
    api("sendChatAction", http_timeout=5, chat_id=chat_id, action="typing")
    text, keyboard = briefing.build()
    hour = settings.briefing_hour()
    when = (f"\n\n<i>Каждый день в {hour:02d}:00 (Ташкент). Время меняется в панели: "
            f"Ещё → Настройки.</i>" if hour >= 0 else
            "\n\n<i>Утренняя сводка выключена (панель: Ещё → Настройки).</i>")
    send(chat_id, text + when, keyboard)


def format_plan(plan: list[dict]) -> str:
    if not plan:
        return "🧠 Нет активных машин — проверьте парк."
    lines = ["🧠 <b>План по машинам</b> (груз + обратный)"]
    for p in plan:
        if "cargo_id" not in p:
            lines.append(f"\n<b>№{notifier.escape(p['truck'])}</b> "
                         f"({notifier.escape(p['at'] or '—')}) — подходящего груза нет")
            continue
        back = p.get("return")
        back_txt = (f"\n   🔁 обратно: {notifier.escape(back['route'])} #{back['cargo_id']}"
                    if isinstance(back, dict) else "\n   🔁 обратного груза пока нет")
        lines.append(
            f"\n<b>№{notifier.escape(p['truck'])}</b> ({notifier.escape(p['at'] or '—')}) → "
            f"<b>{notifier.escape(p['route'])}</b> #{p['cargo_id']}"
            f"\n   {notifier.escape(p.get('date') or 'дата не указана')} · "
            f"пустой {p.get('empty_km', 0)} км · маржа {notifier.money(p.get('margin_usd'))}"
            f"{back_txt}"
            + (f"\n   💵 <b>{notifier.escape(p['rate'])}</b>" if p.get("rate") else ""))
    return "\n".join(lines)


def _offer_keyboard(ctx) -> dict | None:
    rows = [[{"text": f"✅ Беру #{o['cargo_id']} → №{o['truck_id']} ({o['label']})"[:60],
              "callback_data": f"take:{o['match_id']}"}] for o in ctx.offers[:6]]
    return {"inline_keyboard": rows} if rows else None


def cmd_rules(chat_id, args: list[str]) -> None:
    """📜 Kompaniya qoidalari va o'rganilgan takliflar."""
    import rules
    items = [r for r in rules.list_rules() if r["status"] in ("active", "proposed")]
    if not items:
        send(chat_id, "📜 Правил пока нет.\n\nПросто напишите, например:\n"
                      "<code>Россиядан 30 млн дан арзон юк олма</code>\n"
                      "<code>Қозоғистонга юк олмайлик</code>\n"
                      "<code>Қозон йўналиши бизга ёқади</code>")
        return
    lines = ["📜 <b>Правила компании</b>"]
    keyboard = []
    for r in items:
        mark = "🧠 предложение" if r["status"] == "proposed" else (
            "🧠 выучено" if r["source"] == "learned" else "✍️")
        lines.append(f"\n#{r['id']} {mark}\n   {notifier.escape(rules.describe(r))}"
                     + (f"\n   убрать: /unrule_{r['id']}" if r["status"] == "active" else ""))
        if r["status"] == "proposed":
            keyboard.append([{"text": f"✅ Применить #{r['id']}",
                              "callback_data": f"rule_ok:{r['id']}"},
                             {"text": "❌ Нет", "callback_data": f"rule_no:{r['id']}"}])
    send(chat_id, "\n".join(lines), {"inline_keyboard": keyboard} if keyboard else None)


def cmd_unrule(chat_id, args: list[str]) -> None:
    import rules
    if not args or not args[0].lstrip("#").isdigit():
        send(chat_id, "Формат: <code>/unrule 3</code>")
        return
    rule_id = int(args[0].lstrip("#"))
    if rules.delete(rule_id):
        send(chat_id, f"🗑 Правило #{rule_id} удалено.")
    else:
        send(chat_id, f"Правило #{rule_id} не найдено.")


def cmd_learn(chat_id, args: list[str]) -> None:
    """Qarorlardan o'rganish — takliflarni hozir ko'rsatish."""
    import learn
    items = learn.propose()
    if not items:
        send(chat_id, "🧠 Пока новых закономерностей нет — нужно больше решений "
                      "«Беру/Пропустить».")
        return
    for item in items:
        send(chat_id, learn.format_proposal(item),
             learn.proposal_keyboard(item["rule"]["id"]))


def cmd_new(chat_id, args: list[str]) -> None:
    """AI suhbatini yangidan boshlash (eski kontekst chalg'itmasin)."""
    db.clear_ai_history(chat_id)
    send(chat_id, "🆕 Начали разговор заново. Правила и память компании сохранены.")


def cmd_ai(chat_id, args: list[str]) -> None:
    import brain
    send(chat_id, f"🧠 AI: {notifier.escape(brain.describe())}")


COMMANDS = {
    "start": cmd_start,
    "plan": cmd_plan,
    "brief": cmd_brief,
    "rules": cmd_rules,
    "unrule": cmd_unrule,
    "learn": cmd_learn,
    "new": cmd_new,
    "ai": cmd_ai,
    "help": cmd_help,
    "panel": cmd_panel,
    "list": cmd_list,
    "trucks": cmd_trucks,
    "pos": cmd_pos,
    "cargo": cmd_cargo,
    "done": cmd_done,
    "expire": cmd_expire,
    "stats": cmd_stats,
    "gps": cmd_gps,
    "find": cmd_find,
    "fleet": cmd_fleet,
    "watch": cmd_watch,
    "watches": cmd_watches,
    "unwatch": cmd_unwatch,
    "group": cmd_group,
    "groups": cmd_groups,
}


# ---------------------------------------------------------------- yo'naltirish

def handle_callback(cq: dict) -> None:
    message = cq.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    data = cq.get("data") or ""

    if not allowed(chat_id):
        answer(cq["id"], "Нет доступа")
        return

    action, _, arg = data.partition(":")
    if action == "noop":
        answer(cq["id"])
        return
    if action == "watch":
        answer(cq["id"], _on_watch_button(chat_id, arg))
        return
    if not arg.isdigit():
        answer(cq["id"], "Кнопка устарела")
        return

    handlers = {
        "info": lambda: on_info(int(arg), chat_id),
        "call": lambda: on_call(int(arg), chat_id),
        "price": lambda: on_price(int(arg), chat_id),
        "undo": lambda: on_undo(int(arg), chat_id, message_id, message.get("reply_markup")),
        "finish": lambda: on_finish(int(arg), chat_id, message_id),
        "take": lambda: on_take(int(arg), chat_id, message_id,
                                message.get("reply_markup")),
        "rule_ok": lambda: on_rule(int(arg), "active", chat_id, message_id,
                                   message.get("reply_markup"), data),
        "rule_no": lambda: on_rule(int(arg), "rejected", chat_id, message_id,
                                   message.get("reply_markup"), data),
        "rule_del": lambda: on_rule(int(arg), "delete", chat_id, message_id,
                                    message.get("reply_markup"), data),
        "skip": lambda: on_skip(int(arg), chat_id, message_id),
    }
    handler = handlers.get(action)
    if handler is None:
        answer(cq["id"], "Неизвестная кнопка")
        return

    try:
        answer(cq["id"], handler() or "")
    except Exception:
        log.exception("Tugma (%s) xatosi", data)
        answer(cq["id"], "Ошибка, посмотрите логи", alert=True)


def handle_message(msg: dict) -> None:
    chat_id = (msg.get("chat") or {}).get("id")
    user_id = (msg.get("from") or {}).get("id")

    # Joylashuv haydovchining shaxsiy chatidan keladi — dispetcher
    # chati cheklovi bu yerga taalluqli emas, himoya `/link` orqali.
    if msg.get("location"):
        on_location(msg)
        return

    # Ovozli xabar — rulda yozish qiyin. Faqat shaxsiy chatda (guruhda
    # boshqalarning ovozlari ham bor).
    if msg.get("voice") or msg.get("audio"):
        if allowed(chat_id) and (msg.get("chat") or {}).get("type", "private") == "private":
            on_voice(msg)
        return

    text = (msg.get("text") or "").strip()
    if not text:
        return

    # Pastdagi doimiy tugmalar — matn bo'lib keladi, buyruqqa yo'naltiramiz.
    text = KB_LEGACY.get(text, text)
    menu = {
        KB_LIST: cmd_list, KB_FLEET: cmd_fleet, KB_STATS: cmd_stats,
        KB_WATCH: cmd_watches, KB_HELP: cmd_help,
        KB_PLAN: cmd_plan, KB_RULES: cmd_rules,
    }
    if text in menu:
        if allowed(chat_id):
            menu[text](chat_id, [])
        return
    if text == KB_SEARCH:
        if allowed(chat_id):
            send(chat_id, "Напишите направление, например <code>Ташкент Москва</code> "
                          "или <code>Москва реф</code>.\nМожно и словами: "
                          "<code>01 Москвада, атрофидан юк топ</code>", main_keyboard())
        return

    # Buyruqsiz matn — bu qidiruv so'rovi. Dispetcher shunday yozadi:
    # "Тошкент Москва юк топиб бер". Buyruq eslab o'tirmasin.
    if not text.startswith("/"):
        if allowed(chat_id):
            _free_text(chat_id, text, (msg.get("chat") or {}).get("type", "private"))
        return

    # "/pos@baxt_bot 01 Казань" — guruhda bot nomi qo'shiladi
    head, *args = text.split()
    name = head[1:].split("@")[0].lower()

    if name == "link":                      # haydovchi o'zini bog'laydi
        cmd_link(chat_id, user_id, args)
        return
    if not allowed(chat_id):
        return
    # "/info_15", "/cargo_15", "/unwatch_3" ko'rinishidagi havolalar
    for prefix, handler in (("info_", cmd_cargo), ("cargo_", cmd_cargo),
                            ("unwatch_", cmd_unwatch), ("unrule_", cmd_unrule)):
        if name.startswith(prefix) and name[len(prefix):].isdigit():
            handler(chat_id, [name[len(prefix):]])
            return
    handler = COMMANDS.get(name)
    if handler is None:
        send(chat_id, f"Не знаю команду /{notifier.escape(name)}. /help")
        return
    handler(chat_id, args)


def _free_text(chat_id, text: str, chat_type: str = "private") -> None:
    """Oddiy matn — rahbarning savoli yoki topshirig'i.

    Shaxsiy chatda — AI yurak javob beradi (qidiruv, reja, qoida, xotira).
    Guruhda esa faqat shahar nomi bor matnga javob beramiz: u yerda odatiy
    yozishmalar ham bo'ladi, bot ularga aralashmasligi kerak. AI ulanmagan
    yoki ishlamasa — oddiy qidiruv (`search.py`).
    """
    import search
    if chat_type == "private" and _ai(chat_id, text):
        return
    query = search.parse_query(text)
    if not query.has_place:
        if any(word in text.lower() for word in
               ("мошина", "машин", "парк", "mashina", "fleet")):
            cmd_fleet(chat_id, [])
        return
    found = search.find(query)
    send(chat_id, notifier.format_search(found),
         None if found["results"] else notifier.watch_keyboard(query))


def handle_update(update: dict) -> None:
    if "callback_query" in update:
        handle_callback(update["callback_query"])
    elif "message" in update:
        handle_message(update["message"])
    elif "edited_message" in update:
        # Live Location yangilanishi shu ko'rinishda keladi
        edited = dict(update["edited_message"])
        edited["_edited"] = True
        if edited.get("location"):
            on_location(edited)


# ---------------------------------------------------------------- asosiy tsikl

def _load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except (OSError, ValueError):
        return 0


def _save_offset(offset: int) -> None:
    try:
        OFFSET_FILE.write_text(str(offset))
    except OSError as e:
        log.warning("Offset saqlanmadi: %s", e)


COMMAND_MENU = [
    ("plan", "План: груз + обратный для каждой машины"),
    ("brief", "Утренняя сводка прямо сейчас"),
    ("list", "Лучшие предложения сейчас"),
    ("fleet", "Где наши машины"),
    ("stats", "Статистика"),
    ("watches", "Сохранённые направления"),
    ("rules", "Правила компании"),
    ("groups", "Группы Telegram, которые читаем"),
    ("panel", "Открыть веб-панель"),
    ("new", "Начать разговор с AI заново"),
    ("help", "Помощь"),
]


def setup_bot_ui() -> None:
    """Komanda menyusi va menyu tugmasi — bir marta, startupda.

    Buyruq eslash shart emas: "/" bosilganda ro'yxat chiqadi, chapdagi
    menyu tugmasi to'g'ridan-to'g'ri panelni ochadi.
    """
    try:
        api("setMyCommands", commands=[{"command": c, "description": d}
                                       for c, d in COMMAND_MENU])
        base = os.getenv("PANEL_URL", "").strip().rstrip("/")
        if base:
            # Menyu tugmasi panelni Telegram ichida ochadi (Web App)
            api("setChatMenuButton", menu_button={
                "type": "web_app", "text": "Панель",
                "web_app": {"url": base}})
    except Exception:
        log.exception("Bot menyusini sozlashda xato")


def _learn_and_notify() -> None:
    """Qarorlardan yangi naqsh topilsa — rahbarga tasdiqlash uchun yuboradi."""
    try:
        import learn
        for item in learn.propose():
            notifier.send(learn.format_proposal(item),
                          learn.proposal_keyboard(item["rule"]["id"]))
    except Exception:
        log.exception("O'rganishda xato")


def run() -> None:
    """Long-polling tsikli. Xato bo'lsa to'xtamaydi — kutib, davom etadi."""
    db.init()
    offset = _load_offset()
    me = api("getMe").get("result", {})
    log.info("Bot ishga tushdi: @%s", me.get("username", "?"))
    setup_bot_ui()
    last_expire = 0.0
    last_gps = time.time()          # ishga tushishda darhol yugurtirmaymiz
    last_learn = time.time()
    last_returns = 0.0

    while True:
        if time.time() - last_expire > EXPIRE_EVERY_SEC:
            last_expire = time.time()
            try:
                n = db.expire_old_cargos()
                if n:
                    log.info("%d ta eskirgan e'lon yopildi", n)
            except Exception:
                log.exception("Eskirganlarni tozalashda xato")

        # GPS: alohida process kerak emas, shu tsiklda 30 daqiqada bir
        if time.time() - last_gps > GPS_EVERY_SEC:
            last_gps = time.time()
            try:
                import gps
                report = gps.format_report(gps.sync())
                if report:
                    notifier.send(report)
                db.trim_gps_positions()
            except Exception:
                log.exception("GPS sinxronlashda xato")

        if time.time() - last_learn > LEARN_EVERY_SEC:
            last_learn = time.time()
            _learn_and_notify()

        # Qaytish yuki eslatmasi — soatiga bir marta tekshiriladi
        if time.time() - last_returns > RETURNS_EVERY_SEC:
            last_returns = time.time()
            try:
                import returns
                returns.remind_due()
            except Exception:
                log.exception("Qaytish eslatmasida xato")

        # Ertalabki reja — vaqti kelganini o'zi tekshiradi (kuniga bir marta)
        try:
            import briefing
            briefing.send_if_due()
        except Exception:
            log.exception("Ertalabki rejada xato")

        resp = api("getUpdates", http_timeout=POLL_TIMEOUT + 10, offset=offset,
                   timeout=POLL_TIMEOUT,
                   allowed_updates=["message", "edited_message", "callback_query"])
        if not resp.get("ok"):
            time.sleep(3)
            continue

        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            try:
                handle_update(update)
            except Exception:
                log.exception("Xabarni qayta ishlashda xato")
        if resp.get("result"):
            _save_offset(offset)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run()
