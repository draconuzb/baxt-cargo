"""
config.py — barcha sozlamalar shu yerda. Raqamlarni o'z real xarajatlaringizga
moslab o'zgartiring: natijaning aniqligi to'g'ridan-to'g'ri shularga bog'liq.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "cargo.db"))

# ---------------------------------------------------------------- Telegram
TG_API_ID = int(os.getenv("TG_API_ID", "0"))          # my.telegram.org dan
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION = os.getenv("TG_SESSION", str(BASE_DIR / "baxt.session"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "")                 # @BotFather dan
DISPATCHER_CHAT_ID = os.getenv("DISPATCHER_CHAT_ID", "")  # bildirishnoma boradigan chat

# Kuzatiladigan guruhlar: @username yoki -100... id. sources.json da ham bo'ladi.
SOURCES_FILE = BASE_DIR / "sources.json"
TRUCKS_FILE = BASE_DIR / "trucks.json"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # ixtiyoriy: LLM tahlil

# O'z OSRM serveringiz bo'lsa shu yerga yozing — masofa aniq bo'ladi.
# Masalan: "http://127.0.0.1:5000". Bo'sh bo'lsa taxminiy hisob ishlatiladi.
OSRM_URL = os.getenv("OSRM_URL", "")


# ---------------------------------------------------------------- xarajatlar
@dataclass
class Costs:
    """Barcha qiymatlar USD da (valyuta kursi pastda)."""
    fuel_price_usd: float = 0.95      # 1 litr dizel narxi (o'rtacha marshrut bo'yicha)
    driver_usd_per_km: float = 0.06   # haydovchi ulushi + kunlik
    road_usd_per_km: float = 0.025    # yo'l to'lovi, platon, ruxsatnoma
    border_usd: float = 120.0         # bitta chegara o'tish (broker, navbat, rasmiylashtirish)
    fixed_usd: float = 80.0           # yuklash/tushirish, ekspeditor, boshqa
    avg_speed_kmh: float = 55.0       # real o'rtacha tezlik (dam olish bilan)
    driving_hours_per_day: float = 9.0
    road_factor: float = 1.30         # to'g'ri chiziq -> real yo'l koeffitsienti

    # Ballash uchun maqsadli ko'rsatkichlar
    target_margin_per_day: float = 260.0   # kuniga qancha sof foyda yaxshi hisoblanadi
    market_rate_per_km: float = 1.15       # bozordagi o'rtacha stavka, $/km (yuk bilan)
    max_empty_km: float = 700.0            # bundan uzoq bo'sh yurishni ko'rib chiqmaymiz
    date_tolerance_days: int = 2           # yuklash sanasiga necha kun kechikish mumkin


# Valyuta kurslari — haftada bir yangilab turing (yoki API ga ulang)
RATES_TO_USD = {
    "USD": 1.0,
    "UZS": 1 / 12800.0,
    "RUB": 1 / 92.0,
    "KZT": 1 / 480.0,
}


def to_usd(amount: float | None, currency: str | None) -> float | None:
    if amount is None or not currency:
        return None
    rate = RATES_TO_USD.get(currency.upper())
    return round(amount * rate, 2) if rate else None


def load_json(path: Path, default):
    if not Path(path).exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_sources() -> list[str]:
    return load_json(SOURCES_FILE, [])


COSTS = Costs()
