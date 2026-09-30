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

from telethon import TelegramClient, events

try:
    from telethon.errors import FloodWaitError
except ImportError:      # telethon versiyasi boshqacha bo'lsa ham yiqilmaymiz
    class FloodWaitError(Exception):
        seconds = 60

import config
import pipeline

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


async def run() -> None:
    sources = config.load_sources()
    if not sources:
        raise SystemExit("sources.json bo'sh — kuzatiladigan guruhlarni qo'shing")

    client = TelegramClient(config.TG_SESSION, config.TG_API_ID, config.TG_API_HASH)
    await client.start()
    entities = await _resolve_sources(client, sources)
    if not entities:
        raise SystemExit("Birorta guruhga ulanib bo'lmadi")

    @client.on(events.NewMessage(chats=entities))
    async def handler(event):
        text = event.message.message or ""
        if len(text) < 15:
            return
        chat = await event.get_chat()
        source = getattr(chat, "username", None) or getattr(chat, "title", "?")
        try:
            await asyncio.to_thread(
                pipeline.handle_message, text, source, event.message.id,
                event.message.date,
            )
        except Exception:
            log.exception("Xabarni qayta ishlashda xato")

    log.info("Tinglash boshlandi — %d guruh", len(entities))
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
    entities = await _resolve_sources(client, config.load_sources())
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
