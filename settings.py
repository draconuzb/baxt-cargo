"""
settings.py — brauzerdan o'zgartiriladigan sozlamalar (TZ 5.7, /settings).

`config.py` tahrirlanmaydi (CLAUDE.md, 7-qoida). Uning qiymatlari —
boshlang'ich nuqta; dispetcher panelda o'zgartirgan qiymatlar bazadagi
`settings` jadvaliga yoziladi va ustiga qo'yiladi.

Qayta ishga tushirish shart emas: har bir process (listener, bot, panel)
sozlamalarni bazadan o'qiydi va 30 soniyalik kesh bilan yangilab turadi.
"""
from __future__ import annotations

import dataclasses
import logging
import time

import config
import db

log = logging.getLogger("settings")

CACHE_TTL_SEC = 30

# (kalit, nomi, birligi, min, max) — panel shu ro'yxatdan forma quradi.
# Chegara — noto'g'ri kiritilgan raqam (masalan 95 o'rniga 0.95) butun
# ball hisobini buzmasligi uchun.
FIELDS: list[tuple[str, str, str, float, float]] = [
    ("fuel_price_usd", "Dizel narxi", "$/litr", 0.3, 3.0),
    ("driver_usd_per_km", "Haydovchi ulushi", "$/km", 0.0, 1.0),
    ("road_usd_per_km", "Yo'l to'lovi, platon", "$/km", 0.0, 1.0),
    ("border_usd", "Bitta chegara o'tish", "$", 0.0, 2000.0),
    ("fixed_usd", "Yuklash/tushirish va boshqa", "$", 0.0, 2000.0),
    ("avg_speed_kmh", "O'rtacha tezlik (dam olish bilan)", "km/soat", 20.0, 90.0),
    ("driving_hours_per_day", "Kunlik haydash soati", "soat", 4.0, 20.0),
    ("road_factor", "Yo'l koeffitsienti (zaxira)", "×", 1.0, 2.0),
    ("target_margin_per_day", "Maqsadli kunlik marja", "$/kun", 10.0, 5000.0),
    ("market_rate_per_km", "Bozor stavkasi (zaxira)", "$/km", 0.1, 10.0),
    ("max_empty_km", "Eng uzoq bo'sh probeg", "km", 0.0, 3000.0),
    ("date_tolerance_days", "Yuklash sanasiga kechikish", "kun", 0.0, 10.0),
]
# Costs dan tashqari sozlamalar
EXTRA_FIELDS: list[tuple[str, str, str, float, float]] = [
    ("notify_threshold", "Bildirishnoma chegarasi (ball)", "0–100", 0.0, 100.0),
    ("briefing_hour", "Ertalabki reja soati (Toshkent, −1 = o'chiq)", "soat", -1.0, 23.0),
]
BRIEFING_HOUR_DEFAULT = 8
ALL_FIELDS = {f[0]: f for f in FIELDS + EXTRA_FIELDS}
_INT_FIELDS = {"date_tolerance_days", "briefing_hour"}

_cache: dict[str, tuple[float, dict]] = {}


def reset_cache() -> None:
    _cache.clear()


def overrides() -> dict[str, float]:
    """Panelda o'zgartirilgan qiymatlar (kesh bilan)."""
    key = str(config.DB_PATH)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_SEC:
        return hit[1]
    values: dict[str, float] = {}
    try:
        with db.connect() as conn:
            for row in conn.execute("SELECT key, value FROM settings"):
                if row["key"] in ALL_FIELDS:
                    try:
                        values[row["key"]] = float(row["value"])
                    except (TypeError, ValueError):
                        log.warning("Sozlama buzuq: %s=%r", row["key"], row["value"])
    except Exception:
        # jadval hali yo'q yoki baza band — boshlang'ich qiymatlar bilan ishlaymiz
        log.debug("Sozlamalar o'qilmadi", exc_info=True)
    _cache[key] = (time.time(), values)
    return values


def validate(raw: dict) -> tuple[dict[str, float], dict[str, str]]:
    """Formadan kelgan qiymatlarni tekshiradi: (to'g'rilari, xatolar)."""
    clean: dict[str, float] = {}
    errors: dict[str, str] = {}
    for name, value in raw.items():
        field = ALL_FIELDS.get(name)
        if field is None or value in (None, ""):
            continue
        _, label, unit, lo, hi = field
        try:
            number = float(str(value).replace(",", ".").strip())
        except ValueError:
            errors[name] = f"{label}: son emas"
            continue
        if not lo <= number <= hi:
            errors[name] = f"{label}: {lo:g}–{hi:g} {unit} oralig'ida bo'lsin"
            continue
        clean[name] = int(number) if name in _INT_FIELDS else number
    return clean, errors


def save(raw: dict) -> dict[str, str]:
    """Qiymatlarni saqlaydi. Xatolar lug'atini qaytaradi (bo'sh — hammasi joyida).

    Xato bo'lsa hech narsa yozilmaydi: yarim saqlangan sozlama ikki xil
    hisob-kitobga olib keladi.
    """
    clean, errors = validate(raw)
    if errors:
        return errors
    with db.connect() as conn:
        for name, value in clean.items():
            conn.execute(
                "INSERT INTO settings (key, value, updated_at)"
                " VALUES (?, ?, CURRENT_TIMESTAMP)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " updated_at=CURRENT_TIMESTAMP", (name, str(value)))
    reset_cache()
    log.info("Sozlamalar yangilandi: %s", clean)
    return {}


def reset(names: list[str] | None = None) -> None:
    """Boshlang'ich (`config.py`) qiymatlarga qaytarish."""
    with db.connect() as conn:
        if names:
            conn.executemany("DELETE FROM settings WHERE key=?", [(n,) for n in names])
        else:
            conn.execute("DELETE FROM settings")
    reset_cache()


def current_costs() -> config.Costs:
    """`config.COSTS` + panelda o'zgartirilganlar."""
    changes = {k: v for k, v in overrides().items()
               if k in {f[0] for f in FIELDS}}
    if not changes:
        return config.COSTS
    return dataclasses.replace(config.COSTS, **changes)


def notify_threshold(default: float) -> float:
    return overrides().get("notify_threshold", default)


def briefing_hour() -> int:
    """Ertalabki reja soati (mahalliy vaqt). −1 — o'chirilgan."""
    return int(overrides().get("briefing_hour", BRIEFING_HOUR_DEFAULT))


def current_values(default_threshold: float) -> dict[str, float]:
    """Panel formasi uchun: har bir maydonning hozirgi qiymati."""
    costs = current_costs()
    values = {name: getattr(costs, name) for name, *_ in FIELDS}
    values["notify_threshold"] = notify_threshold(default_threshold)
    values["briefing_hour"] = briefing_hour()
    return values
