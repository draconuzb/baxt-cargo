"""
actions.py — dispetcher qarorlari: reysni olish va o'tkazib yuborish.

Bu mantiq ikki joydan chaqiriladi — Telegram bot tugmalari va veb-panel.
Bitta joyda yozilgani muhim: "bitta yuk ikki marta olinmasin" qoidasi
ikkala kanalda ham bir xil ishlashi kerak, aks holda dispetcher telefondan,
boshqasi brauzerdan bir yukni ikki mashinaga berib qo'yishi mumkin.

Bu modul Telegram yoki HTML haqida hech narsa bilmaydi — faqat baza.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import db
import scoring


def _row(r) -> dict | None:
    return {k: r[k] for k in r.keys()} if r is not None else None


@dataclass
class TakeResult:
    ok: bool
    reason: str = ""                 # not_found | cargo_missing | already_taken | stale
    match: dict | None = None
    cargo: dict | None = None
    truck: dict | None = None        # mashina reysdan OLDINGI holatda
    free_date: str | None = None
    holder: str | None = None        # yukni allaqachon olgan mashina
    cancelled: int = 0               # bekor qilingan boshqa takliflar
    roundtrip: list = field(default_factory=list)


def free_date_after(cargo, details: dict) -> str:
    """Reysdan keyin mashina qachon bo'shaydi: yuklash sanasi + reys kunlari."""
    start = None
    load_date = cargo["load_date"] if cargo else None
    if load_date:
        try:
            start = date.fromisoformat(str(load_date)[:10])
        except ValueError:
            start = None
    start = start or date.today()
    days = math.ceil(float(details.get("trip_days") or 3))
    return (start + timedelta(days=days)).isoformat()


def take_match(match_id: int) -> TakeResult:
    """✅ Беру: yukni mashinaga biriktiradi.

    Tartib muhim: avval yuk atomar "band" qilinadi (`db.claim_cargo`),
    faqat shundan keyin boshqa o'zgarishlar. Ikki kishi bir vaqtda bossa
    ham faqat bittasi o'tadi.
    """
    m = db.get_match(match_id)
    if m is None:
        return TakeResult(ok=False, reason="not_found")
    cargo = db.get_cargo(m["cargo_id"])
    if cargo is None:
        return TakeResult(ok=False, reason="cargo_missing", match=_row(m))
    if m["decision"] == "cancelled" and cargo["status"] == "new":
        # Yuk bo'sh, lekin fura shu orada boshqa reys olgan — taklif eski joy
        # bo'yicha edi. (Yuk o'zi olingan bo'lsa — pastda "already_taken".)
        return TakeResult(ok=False, reason="stale", match=_row(m), cargo=_row(cargo))

    if not db.claim_cargo(cargo["id"]):
        return TakeResult(ok=False, reason="already_taken", match=_row(m),
                          cargo=_row(cargo),
                          holder=db.taken_truck_for_cargo(cargo["id"]))

    truck_before = db.get_truck(m["truck_id"])
    details = json.loads(m["details"]) if m["details"] else {}
    free_date = free_date_after(cargo, details)

    db.set_decision(match_id, "taken")
    # Bekor qilinsa fura oldingi holatiga qaytishi uchun
    db.set_trip_prev(match_id, truck_before["current_city"] if truck_before else None,
                     truck_before["free_date"] if truck_before else None)
    cancelled = db.cancel_other_matches(cargo["id"], match_id)
    cancelled += db.cancel_truck_matches(m["truck_id"], match_id)
    db.set_truck_position(m["truck_id"], city=cargo["to_city"],
                          free_date=free_date, source="trip")

    return TakeResult(ok=True, match=_row(m), cargo=_row(cargo),
                      truck=_row(truck_before), free_date=free_date,
                      cancelled=cancelled)


def skip_match(match_id: int) -> str:
    """⏭ Пропустить. Qaytaradi: `ok` | `not_found` | `decided`."""
    m = db.get_match(match_id)
    if m is None:
        return "not_found"
    if m["decision"]:
        return "decided"
    db.set_decision(match_id, "skipped")
    return "ok"


def undo_take(match_id: int) -> str:
    """↩️ Xato bosilgan "Olaman"ni bekor qilish.

    Yuk yana bo'sh bo'ladi, fura olishdan oldingi joyiga qaytadi, qaror
    `undone` bo'ladi (statistikaga kirmaydi). Faqat furaning OXIRGI yo'ldagi
    reysi bekor qilinadi — aks holda fura joyi zanjiri buziladi.
    Qaytaradi: ok | not_found | not_taken | finished | not_latest
    """
    m = db.get_match(match_id)
    if m is None:
        return "not_found"
    if m["decision"] != "taken":
        return "not_taken"
    if m["finished_at"]:
        return "finished"
    trips = db.active_trips(m["truck_id"])
    if trips and trips[-1]["id"] != match_id:
        return "not_latest"

    db.set_decision(match_id, "undone")
    cargo = db.get_cargo(m["cargo_id"])
    if cargo is not None and cargo["status"] == "taken":
        db.set_cargo_status(cargo["id"], "new")
    if m["prev_city"] or m["prev_free_date"]:
        db.set_truck_position(m["truck_id"], city=m["prev_city"],
                              free_date=m["prev_free_date"], source="manual")
    return "ok"


def finish_trip(match_id: int, actual_margin: float | None = None) -> str:
    """✅ Reys tugadi. Haqiqiy marja ixtiyoriy (keyin ham kiritsa bo'ladi).

    Reja bo'yicha bo'shash sanasi hali kelmagan bo'lsa — fura bugundan bo'sh
    (reys erta tugadi). Qaytaradi: ok | not_found | not_taken | finished
    """
    m = db.get_match(match_id)
    if m is None:
        return "not_found"
    if m["decision"] != "taken":
        return "not_taken"
    if m["finished_at"]:
        if actual_margin is not None:
            db.set_actual_margin(match_id, actual_margin)
        return "finished"
    db.finish_trip(match_id, actual_margin)
    if not db.active_trips(m["truck_id"]):
        truck = db.get_truck(m["truck_id"])
        today = date.today().isoformat()
        if truck is not None and (truck["free_date"] or "") > today:
            db.set_truck_position(m["truck_id"], free_date=today, source="trip")
    return "ok"


def roundtrip_for(result: TakeResult, top: int = 3) -> list[dict]:
    """Olingan reysdan keyingi qaytish yuki takliflari."""
    if not result.ok or not result.truck or not result.cargo:
        return []
    return scoring.best_roundtrip(result.cargo, result.truck,
                                  db.active_cargos(hours=48), top=top)
