"""
gps.py — mashinaning qayerdaligini avtomatik aniqlash (TZ 5.5, 4-bosqich).

Manba almashsa qolgan kod o'zgarmasin uchun adapter interfeysi ishlatiladi:
har bir manba `positions()` beradi, qolgani — umumiy.

Uchta manba, ustuvorlik tartibida:

1. `TelegramLiveLocation` — haydovchi botga "Live Location" yuboradi
   (Telegram'ning o'zida bor, 8 soatgacha). Bepul, bugunoq ishlaydi.
   Haydovchi botga `/link 01` yozib mashinasini bog'laydi, keyin
   jonli joylashuvni yuboradi; `bot.py` nuqtalarni bazaga yozadi.
2. `FileSource` — tashqi skript yoki dispetcher `positions.json` ni
   to'ldiradi. Zaxira variant.
3. `WialonAdapter` — mashinalarda treker bo'lsa. Buyurtmachidan qaysi
   provayder ekanini so'rash kerak (TZ 7-bo'lim, 5-savol).

Qoida: **qo'lda kiritilgan holat GPS'dan ustun turadi 24 soat davomida.**
Dispetcher "mashina Qozonda" desa, GPS eski signal bilan uni qaytarib
yubormasligi kerak.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import config
import db
import geo

log = logging.getLogger("gps")

# Holat = (lat, lon, signal vaqti)
Position = tuple[float, float, datetime]

MAX_CITY_KM = 150.0        # bundan uzoqda shahar topilmasa — holat yangilanmaydi
MANUAL_TTL_HOURS = 24      # qo'lda kiritilgan holat qancha vaqt ustun turadi
STALE_HOURS = 2            # signal shuncha yo'q bo'lsa — ogohlantirish
SYNC_INTERVAL_SEC = 1800   # har 30 daqiqada


class GpsSource(Protocol):
    """Har qanday GPS manbai shu interfeysni beradi."""

    name: str

    def positions(self) -> dict[str, Position]:
        """{truck_id: (lat, lon, vaqt)}"""
        ...


# ---------------------------------------------------------------- manbalar

class TelegramLiveLocation:
    """Haydovchining "Live Location" nuqtalari (bot yozib boradi)."""

    name = "telegram"

    def positions(self) -> dict[str, Position]:
        out: dict[str, Position] = {}
        for truck_id, row in db.latest_gps_positions().items():
            when = _as_datetime(row["recorded_at"]) or _as_datetime(row["created_at"])
            if when is None:
                continue
            out[truck_id] = (row["lat"], row["lon"], when)
        return out


class FileSource:
    """`positions.json` faylidan o'qiydi.

    Format: {"01": {"lat": 55.79, "lon": 49.12, "at": "2026-09-20 14:30"}}
    Tashqi skript yoki boshqa tizim bilan ulanish uchun eng oddiy yo'l.
    """

    name = "file"

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or config.BASE_DIR / "positions.json")

    def positions(self) -> dict[str, Position]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            log.warning("%s o'qilmadi: %s", self.path.name, e)
            return {}

        out: dict[str, Position] = {}
        for truck_id, item in (raw or {}).items():
            try:
                when = _as_datetime(item.get("at")) or db.utc_now()
                out[str(truck_id)] = (float(item["lat"]), float(item["lon"]), when)
            except (KeyError, TypeError, ValueError):
                log.warning("Mashina %s uchun koordinata buzuq", truck_id)
        return out


class WialonError(Exception):
    """Wialon xatoni HTTP 200 bilan, javob ichida qaytaradi."""

    # Eng ko'p uchraydiganlari; qolganida raqamning o'zi ko'rsatiladi.
    MEANINGS = {
        1: "sessiya yaroqsiz",
        4: "so'rov parametrlari noto'g'ri",
        7: "ruxsat yo'q (token huquqlarini tekshiring)",
        8: "login yoki parol noto'g'ri",
        1011: "IP o'zgardi yoki sessiya muddati tugadi",
    }

    def __init__(self, code: int, svc: str = ""):
        self.code = code
        meaning = self.MEANINGS.get(code, "noma'lum xato")
        super().__init__(f"Wialon xatosi {code} ({meaning}) — {svc}")


def wialon_api_url(host: str) -> str:
    """Manzilni to'liq API yo'liga keltiradi.

    Buyurtmachi "gpsmonitor.uz" deb beradi, API esa
    "https://gpsmonitor.uz/wialon/ajax.html" da turadi.
    """
    host = (host or "").strip().rstrip("/")
    if not host:
        return ""
    if "://" not in host:
        host = "https://" + host
    if host.endswith("ajax.html"):
        return host
    return host + "/wialon/ajax.html"


def _key(text) -> str:
    """Davlat raqamini solishtirish uchun: probel, tire, registr — hisobga olinmaydi."""
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


class WialonAdapter:
    """Wialon treker API'si — bulutli (hosting) va **Wialon Local** uchun.

    Buyurtmachida Wialon Local o'rnatilgan (gpsmonitor.uz). Farqi faqat
    server manzilida: API va so'rovlar bir xil.

        WIALON_URL=https://gpsmonitor.uz
        WIALON_TOKEN=...

    Mashinalar avtomatik bog'lanadi: treker obyektining nomi mashinaning
    davlat raqamiga (yoki raqamiga) mos kelsa yetarli. Mos kelmasa —
    `WIALON_UNITS` da qo'lda ko'rsatiladi: {"12345": "01"}.
    """

    name = "wialon"
    DEFAULT_URL = "https://hst-api.wialon.com/wialon/ajax.html"

    def __init__(self, token: str | None = None, base_url: str | None = None,
                 units: dict | None = None):
        # `config.py` tahrirlanmaydi — sozlamalar .env orqali keladi
        self.token = token if token is not None else os.getenv("WIALON_TOKEN", "")
        self.base_url = (wialon_api_url(base_url or os.getenv("WIALON_URL", ""))
                         or self.DEFAULT_URL)
        self.timeout = int(os.getenv("WIALON_TIMEOUT", "15"))
        if units is not None:
            self.unit_map = {str(k): str(v) for k, v in units.items()}
        else:
            try:
                self.unit_map = {str(k): str(v) for k, v in
                                 json.loads(os.getenv("WIALON_UNITS", "{}") or "{}").items()}
            except ValueError:
                log.warning("WIALON_UNITS buzuq JSON — treker xaritasi bo'sh")
                self.unit_map = {}

    # ---------------------------------------------------------- so'rov

    def _call(self, svc: str, params: dict, sid: str | None = None) -> dict:
        """API chaqiruvi. Token URL'da emas, POST tanasida ketadi."""
        import urllib.parse
        import urllib.request

        body = {"svc": svc, "params": json.dumps(params)}
        if sid:
            body["sid"] = sid
        request = urllib.request.Request(
            self.base_url, data=urllib.parse.urlencode(body).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        # Wialon xatoni HTTP 200 bilan qaytaradi — shuning uchun tekshiramiz
        if isinstance(data, dict) and "error" in data and data["error"]:
            raise WialonError(int(data["error"]), svc)
        return data

    def login(self) -> str | None:
        """Token bo'yicha sessiya ochadi, `sid` qaytaradi."""
        if not self.token:
            return None
        return (self._call("token/login", {"token": self.token}) or {}).get("eid")

    def units(self) -> list[dict]:
        """Serverdagi obyektlar: id, nom, koordinata, signal vaqti.

        `python main.py wialon` shu ro'yxatni ko'rsatadi — treker nomlarini
        mashinalar bilan solishtirish uchun.
        """
        sid = self.login()
        if not sid:
            log.warning("Wialon: kirish amalga oshmadi (token tekshiring)")
            return []
        # flags 1 = nom, 1024 = oxirgi joylashuv
        params = {"spec": {"itemsType": "avl_unit", "propName": "sys_name",
                           "propValueMask": "*", "sortType": "sys_name"},
                  "force": 1, "flags": 1 + 1024, "from": 0, "to": 0}
        data = self._call("core/search_items", params, sid=sid)

        out = []
        for item in (data.get("items") or []):
            pos = item.get("pos") or {}
            out.append({
                "id": str(item.get("id")),
                "name": item.get("nm") or "",
                "lat": pos.get("y"), "lon": pos.get("x"),
                # Wialon Unix vaqt (UTC) beradi. `fromtimestamp` uni mahalliy
                # vaqtga aylantiradi — baza esa UTC da, shuning uchun UTC da olamiz.
                "at": (datetime.fromtimestamp(pos["t"], timezone.utc)
                       .replace(tzinfo=None)) if pos.get("t") else None,
            })
        return out

    # ---------------------------------------------------------- bog'lash

    def match_truck(self, unit: dict, trucks: list) -> str | None:
        """Treker obyekti qaysi mashinaniki.

        Tartib: qo'lda ko'rsatilgan xarita → davlat raqami → mashina raqami.
        """
        if unit["id"] in self.unit_map:
            return self.unit_map[unit["id"]]
        name = _key(unit["name"])
        if not name:
            return None
        for truck in trucks:
            plate = _key(truck["plate"])
            if plate and (name == plate or plate in name or name in plate):
                return truck["id"]
        for truck in trucks:
            if name == _key(truck["id"]):
                return truck["id"]
        return None

    def positions(self) -> dict[str, Position]:
        if not self.token:
            return {}
        try:
            units = self.units()
        except WialonError as e:
            log.warning("%s", e)
            return {}
        except Exception as e:
            log.warning("Wialon javob bermadi: %s", e)
            return {}

        trucks = db.get_trucks(active_only=False)
        out: dict[str, Position] = {}
        unknown = []
        for unit in units:
            truck_id = self.match_truck(unit, trucks)
            if truck_id is None:
                unknown.append(unit["name"] or unit["id"])
                continue
            if unit["lat"] is None or unit["lon"] is None:
                continue
            out[truck_id] = (float(unit["lat"]), float(unit["lon"]),
                             unit["at"] or db.utc_now())
        if unknown:
            log.info("Wialon: mashinaga bog'lanmagan obyektlar: %s "
                     "(WIALON_UNITS ga qo'shing)", ", ".join(unknown[:10]))
        return out


def default_sources() -> list[GpsSource]:
    """Sozlangan manbalar, ustuvorlik tartibida."""
    sources: list[GpsSource] = [TelegramLiveLocation(), FileSource()]
    if os.getenv("WIALON_TOKEN", ""):
        sources.append(WialonAdapter())
    return sources


# ---------------------------------------------------------------- sinxronlash

def _as_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("T", " ")[:19])
    except ValueError:
        return None


def collect(sources: list[GpsSource] | None = None) -> dict[str, Position]:
    """Barcha manbalardan holatlarni yig'adi — yangisi ustun turadi."""
    merged: dict[str, Position] = {}
    for source in (sources if sources is not None else default_sources()):
        try:
            positions = source.positions()
        except Exception:
            log.exception("GPS manbai (%s) xatosi", getattr(source, "name", "?"))
            continue
        for truck_id, pos in positions.items():
            old = merged.get(truck_id)
            if old is None or pos[2] > old[2]:
                merged[truck_id] = pos
    return merged


def manual_override_active(truck, now: datetime | None = None) -> bool:
    """Dispetcher yaqinda qo'lda kiritganmi?

    `pos_updated_at` bazaga UTC da yoziladi, shuning uchun taqqoslash ham
    UTC da (`db.utc_now`).
    """
    if (truck["pos_source"] or "") != "manual":
        return False
    when = _as_datetime(truck["pos_updated_at"])
    if when is None:
        return False
    return (now or db.utc_now()) - when < timedelta(hours=MANUAL_TTL_HOURS)


def sync(sources: list[GpsSource] | None = None, now: datetime | None = None,
         max_km: float = MAX_CITY_KM) -> dict:
    """Holatlarni bazaga yozadi.

    Qaytaradi: {"updated": [...], "stale": [...], "unknown": [...]}
        updated — shahar o'zgargan mashinalar
        stale   — 2 soatdan beri signal yo'q (dispetcherga ogohlantirish)
        unknown — koordinata bor, lekin yaqin shahar topilmadi
    """
    # vaqt taqqoslashlari UTC da — baza ham UTC da yozadi
    now = now or db.utc_now()
    positions = collect(sources)
    result = {"updated": [], "stale": [], "unknown": [], "skipped_manual": []}

    for truck in db.get_trucks():
        truck_id = truck["id"]
        pos = positions.get(truck_id)
        if pos is None:
            continue

        lat, lon, when = pos
        if now - when > timedelta(hours=STALE_HOURS):
            result["stale"].append({"truck_id": truck_id, "at": when.isoformat(" ")})
            continue

        if manual_override_active(truck, now):
            result["skipped_manual"].append(truck_id)
            continue

        city = geo.nearest_city(lat, lon, max_km)
        if city is None:
            result["unknown"].append({"truck_id": truck_id, "lat": lat, "lon": lon})
            continue
        if city == truck["current_city"]:
            continue

        db.set_truck_position(truck_id, city=city, source="gps")
        result["updated"].append({"truck_id": truck_id, "city": city,
                                  "was": truck["current_city"]})
        log.info("Mashina %s: %s -> %s (GPS)", truck_id, truck["current_city"], city)

    return result


def format_report(result: dict) -> str:
    """Dispetcherga yuboriladigan qisqa hisobot (ruscha)."""
    lines = []
    for item in result["updated"]:
        lines.append(f"📍 Машина №{item['truck_id']}: {item['was'] or '—'} "
                     f"→ <b>{item['city']}</b>")
    for item in result["stale"]:
        lines.append(f"⚠️ Машина №{item['truck_id']}: нет сигнала GPS "
                     f"с {item['at'][:16]}")
    for item in result["unknown"]:
        lines.append(f"❓ Машина №{item['truck_id']}: рядом нет известного города "
                     f"({item['lat']:.2f}, {item['lon']:.2f})")
    return "\n".join(lines)


def run(interval: int = SYNC_INTERVAL_SEC, notify: bool = True) -> None:
    """Fon vazifasi: har 30 daqiqada holatlarni yangilaydi."""
    db.init()
    log.info("GPS sinxronlash boshlandi (har %d daqiqada)", interval // 60)
    while True:
        try:
            result = sync()
            report = format_report(result)
            if report:
                log.info("GPS: %s", report.replace("\n", " | "))
                if notify:
                    import notifier
                    notifier.send(report)
            db.trim_gps_positions()
        except Exception:
            log.exception("GPS sinxronlashda xato")
        time.sleep(interval)
