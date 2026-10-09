"""
pipeline.py — bitta xabarning to'liq yo'li:

matn -> tahlil -> dubl tekshiruvi -> bazaga yozish -> mashinalar bilan
solishtirish -> eng yaxshi moslik bo'lsa dispetcherga xabar.
"""
from __future__ import annotations

import logging
import threading
from collections import Counter
from datetime import datetime

import config
import db
import dedup
import notifier
import parser
import scoring
import settings

log = logging.getLogger("pipeline")

# Shu balldan past mosliklar bildirishnoma qilinmaydi (bazada qoladi)
NOTIFY_THRESHOLD = 65.0

# Oqim hisobi: {(manba, "messages"|"saved"|"dup"|"skipped"): n}. Listener har
# 30 s bazaga yozadi (`take_stats` -> `sources.flush_stats`) — panelda guruh
# bo'yicha "keldi / yangi yuk / takror / yuk emas" ko'rinadi.
STATS: Counter = Counter()
_STATS_LOCK = threading.Lock()


def _count(source: str, key: str, n: int = 1) -> None:
    with _STATS_LOCK:
        STATS[(source, key)] += n


def take_stats() -> dict:
    with _STATS_LOCK:
        out = dict(STATS)
        STATS.clear()
    return out


def handle_message(text: str, source: str = "", msg_id: int | None = None,
                   posted_at: datetime | None = None, notify: bool = True) -> list[int]:
    """Bitta xabarni qayta ishlaydi va saqlangan yuklar id'sini qaytaradi.

    Bitta postda bir nechta yuk bo'lishi mumkin (ekspeditorlar ro'yxat
    tashlaydi), shuning uchun natija — ro'yxat. Bitta yukdagi xato
    qolganlarini to'xtatmaydi.
    """
    saved: list[int] = []
    outcomes: list[str] = []
    for cargo in parser.parse_many(text, source=source, msg_id=msg_id,
                                   posted_at=posted_at):
        try:
            cargo_id = _handle_one(cargo, notify=notify, outcome=outcomes)
        except Exception:
            log.exception("Yukni qayta ishlashda xato: %s -> %s",
                          cargo.from_city, cargo.to_city)
            continue
        if cargo_id is not None:
            saved.append(cargo_id)
    _count(source, "messages")
    if saved:
        _count(source, "saved", len(saved))
    elif "dup" in outcomes:
        _count(source, "dup")
    else:
        _count(source, "skipped")
    return saved


def _handle_one(cargo: parser.Cargo, notify: bool = True,
                outcome: list | None = None) -> int | None:
    if not parser.is_usable(cargo):
        # Regex uddalay olmadi. LLM ulangan bo'lsa — qayta urinib ko'ramiz.
        # Bo'sh mashina e'loni uchun so'ramaymiz: u bizga baribir kerak emas,
        # so'rasak esa har biri uchun pul ketadi.
        if cargo.kind != "truck":
            try:
                import llm_parser
                if llm_parser.enabled():
                    cargo = llm_parser.enrich(cargo)
            except Exception as e:
                log.debug("LLM tahlil ishlamadi: %s", e)
        if not parser.is_usable(cargo):
            log.debug("O'tkazildi (%s, ishonch %.2f)", cargo.kind, cargo.confidence)
            return None

    is_dup, existing = dedup.is_duplicate(cargo)
    if is_dup:
        # E'lon qayta joylandi — asl yuk hali tirik (ro'yxatdan eskirib ketmasin)
        db.touch_cargo(existing, cargo.posted_at)
        if outcome is not None:
            outcome.append("dup")
        log.debug("Dubl: %s -> %s (mavjud #%s)", cargo.from_city, cargo.to_city, existing)
        return None

    cargo_id = db.insert_cargo(cargo, dedup.fingerprint(cargo))
    if cargo_id is None:
        return None
    log.info("Yangi yuk #%s: %s -> %s, %s %s", cargo_id, cargo.from_city,
             cargo.to_city, cargo.rate, cargo.currency)

    if notify:
        match_and_notify(cargo_id)
    return cargo_id


def match_and_notify(cargo_id: int) -> list[dict]:
    row = db.get_cargo(cargo_id)
    if row is None:
        return []
    cargo = {k: row[k] for k in row.keys()}
    trucks = db.get_trucks()
    if not trucks:
        log.warning("Bazada mashina yo'q — trucks.json ni yuklang")
        return []

    results = scoring.best_trucks(cargo, trucks, top=3)
    # chegara panelda o'zgartirilgan bo'lishi mumkin (qayta ishga tushirmasdan)
    threshold = settings.notify_threshold(NOTIFY_THRESHOLD)

    # Dispetcher shu yo'nalishni o'zi so'ragan bo'lsa ("Toshkent-Moskva yuk
    # topib ber") — ball past bo'lsa ham ko'rsatamiz. Bu uning o'z so'rovi.
    watch = _matching_watch(cargo)
    reason = None
    if watch is not None:
        threshold = 0.0
        reason = f"🔎 По вашему запросу: {watch['query'] or ''}".strip()
        db.touch_watch(watch["id"])

    for r in results:
        match_id = db.save_match(cargo_id, r["truck_id"], r)
        # Yo'ldagi fura uchun qaytish yuki: belgi va pastroq chegara (returns.py)
        return_reason = _return_reason(r["truck_id"])
        limit = min(threshold, returns_threshold()) if return_reason else threshold
        if r["score"] >= limit and match_id:
            # Bildirishnoma yuborilmasa ham moslik bazada qoladi — dispetcher
            # uni `report` da ko'radi. Telegram xatosi oqimni to'xtatmaydi.
            try:
                why = _insight(cargo, r, results)
                if notifier.notify_match(cargo, r, match_id, reason=reason or return_reason,
                                         insight=why):
                    db.mark_notified(match_id)
            except Exception:
                log.exception("Bildirishnoma yuborilmadi (moslik #%s)", match_id)
            reason = None          # zanjirdagi qolgan mashinalarga takrorlamaymiz
    return results


def rematch_all(hours: int = 24, truck_ids: list[str] | None = None) -> int:
    """Park o'zgarganda (yangi fura, joy o'zgardi) aktiv yuklarni qayta hisoblaydi.

    Bildirishnoma YUBORILMAYDI — bu eski yuklar, faqat takliflar ro'yxati
    (bosh sahifa, /list) yangilanadi. Qaytaradi: yozilgan takliflar soni.
    """
    trucks = db.get_trucks()
    if truck_ids:
        trucks = [t for t in trucks if t["id"] in truck_ids]
    if not trucks:
        return 0
    written = 0
    for row in db.active_cargos(hours=hours):
        cargo = {k: row[k] for k in row.keys()}
        try:
            for r in scoring.best_trucks(cargo, trucks, top=3):
                if db.save_match(cargo["id"], r["truck_id"], r) or \
                        db.revive_match(cargo["id"], r["truck_id"], r):
                    written += 1
        except Exception:
            log.exception("Qayta hisobda xato: yuk #%s", cargo.get("id"))
    log.info("Qayta hisob: %d ta taklif", written)
    return written


def rescore_open(hours: int = 72) -> dict[str, int]:
    """Aktiv yuklarning OCHIQ takliflarini joriy formula bilan qayta hisoblaydi.

    `save_match` mavjud taklifga tegmaydi, shuning uchun hisob qoidasi
    o'zgarganda (masalan, shubhali narx) eski raqamlar bazada qolib ketadi.
    Endi mos kelmaydigan taklif `cancelled` (statistikaga kirmaydi), qolganining
    marjasi va bali yangilanadi. Dispetcher qarori bor takliflarga tegilmaydi.
    """
    trucks = {t["id"]: t for t in db.get_trucks(active_only=False)}
    out = {"updated": 0, "cancelled": 0}
    for row in db.active_cargos(hours=hours):
        cargo = {k: row[k] for k in row.keys()}
        for m in db.matches_for_cargo(cargo["id"]):
            truck = trucks.get(m["truck_id"])
            if m["decision"] is not None or truck is None:
                continue
            try:
                r = scoring.evaluate(cargo, truck)
            except Exception:
                log.exception("Qayta hisobda xato: taklif #%s", m["id"])
                continue
            if r["ok"]:
                db.update_match_calc(m["id"], r)
                out["updated"] += 1
            else:
                db.set_decision(m["id"], "cancelled")
                out["cancelled"] += 1
    log.info("Takliflar qayta hisoblandi: %s", out)
    return out


def rematch_cargo(cargo_id: int) -> int:
    """Bitta yuk uchun takliflarni qayta ochadi (reys bekor qilinganda)."""
    row = db.get_cargo(cargo_id)
    if row is None or row["status"] != "new":
        return 0
    cargo = {k: row[k] for k in row.keys()}
    written = 0
    for r in scoring.best_trucks(cargo, db.get_trucks(), top=3):
        if db.save_match(cargo_id, r["truck_id"], r) or \
                db.revive_match(cargo_id, r["truck_id"], r):
            written += 1
    return written


def returns_threshold() -> float:
    import returns
    return returns.RETURN_THRESHOLD


def _return_reason(truck_id: str) -> str | None:
    try:
        import returns
        return returns.match_reason(truck_id)
    except Exception:
        log.exception("Qaytish yuki belgisida xato")
        return None


def _insight(cargo: dict, result: dict, results: list[dict]) -> list[str]:
    """"Nega shu yuk" izohi — xato bo'lsa bildirishnoma izohsiz ketadi."""
    try:
        import insight
        return insight.explain(cargo, result, results)
    except Exception:
        log.exception("Izohda xato")
        return []


def _matching_watch(cargo: dict):
    """Yuk dispetcher kutayotgan so'rovlardan biriga mos keladimi."""
    try:
        import search
        for watch in db.active_watches():
            if search.matches_watch(cargo, watch):
                log.info("So'rovga mos yuk: #%s (kuzatuv #%s)",
                         cargo.get("id"), watch["id"])
                return watch
    except Exception:
        log.exception("Kuzatuvlarni tekshirishda xato")
    return None
