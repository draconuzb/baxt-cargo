"""
search.py — dispetcher so'rovi bo'yicha yuk qidirish.

Buyurtmachi talabi: "Менга Тошкент–Москва юк топиб бер" va "бизда нечта
мошина бор, тент/реф — шуни билиб қидирса зўр бўларди".

Ya'ni qidiruv ikki narsani bilishi kerak:
  1. dispetcher nima so'rayapti (yo'nalish, kuzov, mashina);
  2. bizda qanday mashinalar bor — javob shu park uchun hisoblanadi.

Natija har doim **kunlik marja** bo'yicha saralanadi (loyihaning asosiy
tamoyili), chunki "Toshkent–Moskva yuk bormi?" degan savolning foydali
javobi — "ha, mana shu yuk shu mashinaga eng foydali".
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import db
import geo
import parser as ad_parser
import scoring

log = logging.getLogger("search")

DEFAULT_HOURS = 48
HISTORY_DAYS = 7

# "мошина 01", "№01", "машина №2", "truck 3"
_RE_TRUCK = re.compile(r"(?:машин\w*|мошин\w*|mashina|truck|№|#)\s*(\d{1,2})\b")


@dataclass
class Query:
    """Dispetcher so'rovining tushunilgan ko'rinishi."""
    from_city: str | None = None
    to_city: str | None = None
    single_city: str | None = None      # bitta shahar aytilgan: qayerdan yoki qayerga
    body_type: str | None = None
    truck_id: str | None = None
    raw: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.from_city or self.to_city or self.single_city
                    or self.body_type or self.truck_id)

    def describe(self) -> str:
        """So'rovni odam o'qiydigan ko'rinishda (ruscha — dispetcher uchun)."""
        parts = []
        if self.from_city and self.to_city:
            parts.append(f"{self.from_city} → {self.to_city}")
        elif self.single_city:
            parts.append(f"{self.single_city} (в любую сторону)")
        if self.body_type:
            parts.append(self.body_type)
        if self.truck_id:
            parts.append(f"машина №{self.truck_id}")
        return " · ".join(parts) or "все грузы"


def parse_query(text: str) -> Query:
    """Erkin yozilgan so'rovni tushunadi.

    "Тошкент Москва", "Ташкент - Москва реф", "Москва мошина 01" —
    hammasi ishlaydi. Shahar nomlari `geo` lug'atidan o'tadi, shuning
    uchun lotin/kirill/xato yozuv farqi yo'q.
    """
    query = Query(raw=text.strip())
    normalized = ad_parser.normalize(text)

    cities = [name for _, name in geo.find_cities(normalized)]
    if len(cities) >= 2:
        query.from_city, query.to_city = cities[0], cities[1]
    elif len(cities) == 1:
        query.single_city = cities[0]

    query.body_type = ad_parser._parse_body(normalized)

    match = _RE_TRUCK.search(normalized)
    if match:
        query.truck_id = match.group(1).zfill(2)
    return query


# ---------------------------------------------------------------- qidiruv

def _matching_cargos(query: Query, hours: int) -> list:
    """Bazadan so'rovga mos yuklar (hali olinmaganlari)."""
    if query.from_city and query.to_city:
        rows = db.search_cargos(from_city=query.from_city, to_city=query.to_city,
                                body_type=query.body_type, status="new",
                                hours=hours, limit=200)
    elif query.single_city:
        # Bitta shahar aytilgan — u yo yuklash, yo tushirish joyi bo'lishi mumkin
        rows = db.search_cargos(from_city=query.single_city,
                                body_type=query.body_type, status="new",
                                hours=hours, limit=100)
        rows += db.search_cargos(to_city=query.single_city,
                                 body_type=query.body_type, status="new",
                                 hours=hours, limit=100)
    else:
        rows = db.search_cargos(body_type=query.body_type, status="new",
                                hours=hours, limit=200)

    seen, unique = set(), []
    for row in rows:
        if row["id"] not in seen:
            seen.add(row["id"])
            unique.append(row)
    return unique


def find(query: Query | str, hours: int = DEFAULT_HOURS, top: int = 5) -> dict:
    """So'rov bo'yicha eng foydali yuklarni topadi.

    Qaytaradi:
        query     — tushunilgan so'rov
        results   — [{cargo, truck_id, score, margin_usd, margin_per_day, ...}]
        trucks    — qidiruvda qatnashgan mashinalar soni
        scanned   — ko'rib chiqilgan yuklar soni
        history   — hozir yo'q bo'lsa: oxirgi 7 kunda shunday yuk necha marta chiqqan
    """
    if isinstance(query, str):
        query = parse_query(query)

    trucks = db.get_trucks()
    if query.truck_id:
        trucks = [t for t in trucks if t["id"] == query.truck_id]

    cargos = _matching_cargos(query, hours)
    out = {"query": query, "results": [], "trucks": len(trucks),
           "scanned": len(cargos), "history": 0}
    if not trucks or not cargos:
        out["history"] = _history_count(query)
        return out

    # Har bir yuk uchun eng yaxshi mashina — park bo'yicha hisoblanadi
    for cargo in cargos:
        best = scoring.best_trucks(cargo, trucks, top=1)
        if not best:
            continue          # birorta mashina ko'tara olmaydi (sig'im, ref, masofa)
        result = best[0]
        out["results"].append({
            "cargo": {k: cargo[k] for k in cargo.keys()},
            "truck_id": result["truck_id"],
            "score": result["score"],
            "margin_usd": result["margin_usd"],
            "margin_per_day": result["margin_per_day"],
            "empty_km": result["empty_km"],
            "trip_days": result["trip_days"],
        })

    # Kunlik marja bo'yicha — stavkasi yo'q yuklar oxirida
    out["results"].sort(
        key=lambda r: (r["margin_per_day"] is not None, r["margin_per_day"] or 0,
                       r["score"]), reverse=True)
    out["results"] = out["results"][:top]
    if not out["results"]:
        out["history"] = _history_count(query)
    return out


def _history_count(query: Query) -> int:
    """Oxirgi hafta shunday yuk necha marta chiqqan.

    "Hozir yo'q" javobi yetarli emas: dispetcher bu yo'nalish umuman
    bormi yoki bugun kechikdikmi — shuni bilishi kerak.
    """
    try:
        rows = db.search_cargos(from_city=query.from_city, to_city=query.to_city,
                                body_type=query.body_type,
                                hours=HISTORY_DAYS * 24, limit=500)
        if not (query.from_city or query.to_city) and query.single_city:
            rows = db.search_cargos(from_city=query.single_city,
                                    hours=HISTORY_DAYS * 24, limit=500)
            rows += db.search_cargos(to_city=query.single_city,
                                     hours=HISTORY_DAYS * 24, limit=500)
        return len(rows)
    except Exception:
        log.debug("Tarix hisoblanmadi", exc_info=True)
        return 0


# ---------------------------------------------------------------- kuzatuv

def fleet_summary() -> list[dict]:
    """Park holati: qaysi mashina qayerda, qachon bo'shaydi.

    "Бизда нечта мошина бор?" degan savolga javob.
    """
    out = []
    for truck in db.get_trucks(active_only=False):
        trips = db.active_trips(truck["id"])
        out.append({
            "id": truck["id"], "body_type": truck["body_type"],
            "capacity_t": truck["capacity_t"], "city": truck["current_city"],
            "free_date": truck["free_date"], "active": bool(truck["active"]),
            "driver": truck["driver"],
            "trip": (f"{trips[-1]['from_city']} → {trips[-1]['to_city']}" if trips else None),
        })
    return out


def matches_watch(cargo: dict, watch) -> bool:
    """Yangi yuk dispetcher kutayotgan so'rovga mos keladimi."""
    if watch["from_city"] and cargo.get("from_city") != watch["from_city"]:
        return False
    if watch["to_city"] and cargo.get("to_city") != watch["to_city"]:
        return False
    if watch["body_type"] and cargo.get("body_type") != watch["body_type"]:
        return False
    return True


# ---------------------------------------------------------------- takliflar tartibi

def details(row) -> dict:
    """Moslik yozuvidagi to'liq hisob (JSON)."""
    import json
    try:
        return json.loads(row["details"]) if row["details"] else {}
    except (ValueError, TypeError):
        return {}


def rank_offers(rows) -> list:
    """Asosiy tamoyil: kunlik marja. Avval narxi bor va foydali yuklar,
    keyin narxi yozilmaganlar (qo'ng'iroq qilib so'rash kerak), oxirida zararlilar.
    Panel, bot va eslatmalar shu bitta tartibdan foydalanadi."""
    def key(r):
        per_day = details(r).get("margin_per_day")
        if per_day is None:
            return (1, -r["score"])
        return (0 if per_day > 0 else 2, -per_day)
    return sorted(rows, key=key)


def best_offers(truck_id: str, n: int) -> list:
    return rank_offers(db.top_matches(limit=60, truck_id=truck_id))[:n]
