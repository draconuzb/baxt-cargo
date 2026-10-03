"""
listener.py — Telegram guruhlardan yangi e'lonlarni o'qish.

MUHIM: oddiy bot guruh xabarlarini o'qiy olmaydi (admin bo'lmasa). Shuning
uchun bu yerda Telethon "user session" ishlatiladi — ya'ni tizim sizning
Telegram akkauntingiz nomidan guruhlarni o'qiydi. Akkaunt o'sha guruhlarga
a'zo bo'lishi kifoya, hech kimga yozmaydi, faqat o'qiydi.

Birinchi ishga tushirishda telefon raqam va kod so'raydi — bir marta.
"""
from __future__ import annotations

import asyncio
import logging
import time

from telethon import TelegramClient, events

try:
    from telethon.errors import FloodWaitError
except ImportError:      # telethon versiyasi boshqacha bo'lsa ham yiqilmaymiz
    class FloodWaitError(Exception):
        seconds = 60

import config
import db
import pipeline
import sources

log = logging.getLogger("listener")

# Guruhlarni birma-bir ulashda tanaffus: yangi akkauntni 20 ta guruhga
# birdan ulash — bloklanishning eng tez yo'li (TZ 5.8.1).
RESOLVE_DELAY_SEC = 1.0
MAX_FLOOD_WAIT_SEC = 3600


async def _sleep_flood(e: FloodWaitError, where: str) -> None:
    """Telegram "sekinroq" desa — kutamiz. To'xtatmaymiz."""
    wait = min(int(getattr(e, "seconds", 60)) + 5, MAX_FLOOD_WAIT_SEC)
    log.warning("Telegram limiti (%s): %d soniya kutamiz", where, wait)
    await asyncio.sleep(wait)


async def _resolve_sources(client, sources: list[str]):
    """@username yoki id ro'yxatini haqiqiy entity larga aylantiradi."""
    resolved = []
    for s in sources:
        try:
            entity = await client.get_entity(int(s) if str(s).lstrip("-").isdigit() else s)
            resolved.append(entity)
            log.info("Ulanildi: %s", getattr(entity, "title", s))
        except FloodWaitError as e:
            await _sleep_flood(e, f"get_entity {s}")
            try:
                resolved.append(await client.get_entity(s))
            except Exception as e2:
                log.warning("Guruhga ulanib bo'lmadi (%s): %s", s, e2)
        except Exception as e:
            log.warning("Guruhga ulanib bo'lmadi (%s): %s", s, e)
        await asyncio.sleep(RESOLVE_DELAY_SEC)
    return resolved


# ---------------------------------------------------------------- guruhlar ro'yxati (bazadan)
#
# Guruhlar panel/botdan qo'shiladi (`sources.py`). Listener har SYNC_SEC da
# bazaga qaraydi: yangi qo'shilganini ulaydi (kerak bo'lsa a'zo bo'ladi),
# o'chirilganini tinglashdan chiqaradi. Qayta ishga tushirish shart emas.

SYNC_SEC = 30
DIALOGS_EVERY_SEC = 600          # akkaunt guruhlari ro'yxati (panel uchun)
JOINS_PER_CYCLE = 1              # bir siklda bitta guruh — akkaunt bloklanmasin
BACKFILL_NEW = 60                # yangi guruhdan oxirgi xabarlar…
BACKFILL_HOURS = 24              # …faqat shu muddat ichidagilari

WATCH: set[int] = set()          # tinglanayotgan chat id lar (-100…)


def _peer_id(entity) -> int:
    try:
        from telethon import utils
        return int(utils.get_peer_id(entity))
    except Exception:
        return int(getattr(entity, "id"))


def _source_name(entity) -> str:
    return getattr(entity, "username", None) or getattr(entity, "title", None) or "?"


def _human_error(ex: Exception) -> str:
    """Telethon xatosi -> panelda ko'rsatiladigan ruscha sabab."""
    name = type(ex).__name__
    known = {
        "UsernameNotOccupiedError": "такой группы нет",
        "UsernameInvalidError": "неверное имя группы",
        "InviteHashExpiredError": "приглашение устарело — нужна новая ссылка",
        "InviteHashInvalidError": "неверная ссылка-приглашение",
        "InviteRequestSentError": "заявка на вступление отправлена — ждём одобрения админа",
        "ChannelPrivateError": "группа закрыта — нужна ссылка-приглашение",
        "ChannelsTooMuchError": "аккаунт состоит в слишком многих группах",
        "UserBannedInChannelError": "аккаунт заблокирован в этой группе",
    }
    if name in known:
        return known[name]
    if isinstance(ex, ValueError):
        return "группа не найдена (аккаунт не видит её — нужна ссылка)"
    return f"не удалось подключить ({name})"


async def _resolve_ref(client, ref: str):
    """"user:name" | "invite:HASH" | "id:-100…" -> guruh entity (kerak bo'lsa a'zo bo'ladi)."""
    kind, _, value = ref.partition(":")
    if kind == "invite":
        from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
        try:
            updates = await client(ImportChatInviteRequest(value))
            return updates.chats[0]
        except Exception as ex:
            if type(ex).__name__ != "UserAlreadyParticipantError":
                raise
            info = await client(CheckChatInviteRequest(value))
            return info.chat
    entity = await client.get_entity(int(value) if kind == "id" else value)
    if hasattr(entity, "first_name"):
        raise ValueError("это пользователь, а не группа")
    # Ochiq guruh/kanal, akkaunt hali a'zo emas — qo'shilamiz
    if getattr(entity, "left", False):
        from telethon.tl.functions.channels import JoinChannelRequest
        try:
            await client(JoinChannelRequest(entity))
        except Exception as ex:
            if type(ex).__name__ != "UserAlreadyParticipantError":
                raise
    return entity


async def _backfill_recent(client, entity) -> int:
    """Yangi ulangan guruhning oxirgi kungi e'lonlari — panelda darhol ko'rinsin
    (bildirishnomasiz: eski e'lonlar uchun Telegram'ga kartochka ketmaydi)."""
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(hours=BACKFILL_HOURS)
    source, saved = _source_name(entity), 0
    async for m in client.iter_messages(entity, limit=BACKFILL_NEW):
        if m.date is not None and m.date < since:
            break
        if not m.message or len(m.message) < 15:
            continue
        try:
            saved += len(await asyncio.to_thread(pipeline.handle_message, m.message, source,
                                                 m.id, m.date, notify=False))
        except Exception:
            log.exception("Xabar #%s tahlil qilinmadi", m.id)
    return saved


async def sync_sources(client) -> None:
    """Bitta sikl: kutayotgan guruhlarni ulash, tinglash ro'yxatini yangilash."""
    for row in sources.pending(limit=JOINS_PER_CYCLE):
        try:
            entity = await _resolve_ref(client, row["ref"])
        except FloodWaitError as e:
            sources.set_error(row["id"], "лимит Telegram — повторю позже", keep_pending=True)
            await _sleep_flood(e, f"ulash {row['ref']}")
            break
        except Exception as ex:
            log.warning("Guruh ulanmadi (%s): %s", row["ref"], ex)
            sources.set_error(row["id"], _human_error(ex))
            continue
        cid = _peer_id(entity)
        if sources.set_active(row["id"], cid, getattr(entity, "title", None),
                              getattr(entity, "username", None)):
            WATCH.add(cid)
            log.info("Guruh ulandi: %s (%s)", getattr(entity, "title", row["ref"]), cid)
            if row["backfill"]:
                try:
                    n = await _backfill_recent(client, entity)
                    log.info("Yangi guruhdan %d ta yuk o'qildi", n)
                except FloodWaitError as e:
                    await _sleep_flood(e, "backfill")
                except Exception:
                    log.exception("Yangi guruhni o'qishda xato")
                sources.done_backfill(row["id"])
        await asyncio.sleep(RESOLVE_DELAY_SEC)
    WATCH.clear()
    WATCH.update(sources.active_chat_ids())
    sources.heartbeat()


async def refresh_dialogs(client) -> int:
    """Akkaunt a'zo bo'lgan guruh/kanallar — panelda "bir bosishda kuzatish" uchun."""
    items = []
    async for d in client.iter_dialogs(limit=500):
        if not (getattr(d, "is_group", False) or getattr(d, "is_channel", False)):
            continue
        ent = d.entity
        items.append({"chat_id": _peer_id(ent), "title": getattr(ent, "title", None),
                      "username": getattr(ent, "username", None),
                      "members": getattr(ent, "participants_count", None)})
    sources.save_dialogs(items)
    return len(items)


async def _sync_loop(client) -> None:
    last_dialogs = 0.0
    while True:
        try:
            await sync_sources(client)
            if time.monotonic() - last_dialogs > DIALOGS_EVERY_SEC:
                n = await refresh_dialogs(client)
                last_dialogs = time.monotonic()
                log.debug("Akkaunt guruhlari: %d", n)
        except FloodWaitError as e:
            await _sleep_flood(e, "sync")
        except Exception:
            log.exception("Guruhlar ro'yxatini yangilashda xato")
        await asyncio.sleep(SYNC_SEC)


async def run() -> None:
    db.init()
    sources.migrate_from_file(config.load_sources())

    client = TelegramClient(config.TG_SESSION, config.TG_API_ID, config.TG_API_HASH)
    await client.start()
    # Ishga tushganda kutayotganlarning hammasini ulab olamiz (sources.json dan
    # ko'chganlar ham). Har sikl bittadan — soni chegaralangan, cheksiz aylanmaydi.
    for _ in range(len(sources.pending(limit=500)) + 1):
        await sync_sources(client)
    if not WATCH:
        log.warning("Kuzatiladigan guruh yo'q — panelda qo'shing (Ещё → Группы)")

    @client.on(events.NewMessage())
    async def handler(event):
        if event.chat_id not in WATCH:
            return                       # shaxsiy xabarlar va kuzatilmaydigan guruhlar
        text = event.message.message or ""
        if len(text) < 15:
            return
        chat = await event.get_chat()
        source = _source_name(chat)
        try:
            await asyncio.to_thread(
                pipeline.handle_message, text, source, event.message.id,
                event.message.date,
            )
        except Exception:
            log.exception("Xabarni qayta ishlashda xato")

    asyncio.get_running_loop().create_task(_sync_loop(client))
    log.info("Tinglash boshlandi — %d guruh", len(WATCH))
    # Uzilib qolsa qayta ulanadi: 24/7 ishlashi kerak
    while True:
        try:
            await client.run_until_disconnected()
            log.warning("Telegram aloqasi uzildi — 30 soniyada qayta ulanamiz")
        except FloodWaitError as e:
            await _sleep_flood(e, "run")
        except Exception:
            log.exception("Tinglashda kutilmagan xato")
        await asyncio.sleep(30)
        try:
            if not client.is_connected():
                await client.connect()
        except Exception:
            log.exception("Qayta ulanib bo'lmadi")


async def backfill(limit: int = 200) -> None:
    """Guruhlardagi oxirgi xabarlarni o'qib, bazani to'ldiradi.
    Tizimni tekshirish uchun qulay — kutib o'tirmaysiz."""
    client = TelegramClient(config.TG_SESSION, config.TG_API_ID, config.TG_API_HASH)
    await client.start()
    refs = list(config.load_sources())
    refs += [str(c) for c in sources.active_chat_ids() if str(c) not in refs]
    entities = await _resolve_sources(client, refs)
    total = 0
    for ent in entities:
        source = getattr(ent, "username", None) or getattr(ent, "title", "?")
        try:
            async for msg in client.iter_messages(ent, limit=limit):
                if not msg.message or len(msg.message) < 15:
                    continue
                try:
                    saved = pipeline.handle_message(msg.message, source, msg.id,
                                                    msg.date, notify=False)
                    total += len(saved)
                except Exception:
                    log.exception("Xabar #%s tahlil qilinmadi", msg.id)
        except FloodWaitError as e:
            await _sleep_flood(e, f"iter_messages {source}")
        except Exception:
            log.exception("Guruhni o'qishda xato: %s", source)
    log.info("Backfill tugadi: %d ta yuk saqlandi", total)
    await client.disconnect()
