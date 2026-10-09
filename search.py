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
# "Москва" deganda Подольск, Химки, Балашиха ham — dispetcher shaharni emas, joyni aytadi
NEAR_KM = 50

# "мошина 01", "№01", "машина №2", "truck 3"
_RE_TRUCK = re.compile(r"(?:машин\w*|мошин\w*|mashina|truck|№|#)\s*(\d{1,2})\b")


@dataclass
class Query:
    """Dispetcher so'rovining tushunilgan ko'rinishi."""
    from_city: str | None = None
    to_city: str | None = None
    single_city: str | None = None      # bitta shahar aytilgan: qayerdan yoki qayerga
    from_country: str | None = None     # "Россия Ташкент" — butun davlat
    to_country: str | None = None
    single_country: str | None = None
    body_type: str | None = None
    truck_id: str | None = None
    raw: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.from_city or self.to_city or self.single_city or self.from_country
                    or self.to_country or self.single_country or self.body_type
                    or self.truck_id)

    @property
    def has_place(self) -> bool:
        return bool(self.from_city or self.to_city or self.single_city or self.from_country
                    or self.to_country or self.single_country)

    def describe(self) -> str:
        """So'rovni odam o'qiydigan ko'rinishda (ruscha — dispetcher uchun)."""
        parts = []
        a = geo.ru(self.from_city) if self.from_city else _COUNTRY_RU.get(self.from_country, "")
        b = geo.ru(self.to_city) if self.to_city else _COUNTRY_RU.get(self.to_country, "")
        if a and b:
            parts.append(f"{a} → {b}")
        elif self.single_city or self.single_country:
            name = geo.ru(self.single_city) if self.single_city \
                else _COUNTRY_RU.get(self.single_country, "")
            parts.append(f"{name} (в любую сторону)")
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

    # Shahar va davlatlar matndagi tartibida: birinchisi — qayerdan, ikkinchisi — qayerga
    places = [(pos, "city", name) for pos, name in geo.find_cities(normalized)]
    taken = {pos for pos, _, _ in places}
    for i, word in enumerate(geo._norm(normalized).split()):
        code = None if i in taken else geo.country_of_word(word)
        if code:
            places.append((i, "country", code))
    places.sort()
    if len(places) >= 2:
        (_, ka, va), (_, kb, vb) = places[0], places[1]
        query.from_city, query.from_country = (va, None) if ka == "city" else (None, va)
        query.to_city, query.to_country = (vb, None) if kb == "city" else (None, vb)
    elif places:
        _, kind, value = places[0]
        if kind == "city":
            query.single_city = value
        else:
            query.single_country = value

    query.body_type = ad_parser._parse_body(normalized)

    match = _RE_TRUCK.search(normalized)
    if match:
        query.truck_id = match.group(1).zfill(2)
    return query


_COUNTRY_RU = {
    "UZ": "Узбекистан", "RU": "Россия", "KZ": "Казахстан", "KG": "Кыргызстан",
    "TJ": "Таджикистан", "TM": "Туркменистан", "BY": "Беларусь", "TR": "Турция",
    "CN": "Китай", "IR": "Иран", "AF": "Афганистан", "AZ": "Азербайджан", "GE": "Грузия",
    "AM": "Армения", "PL": "Польша", "LT": "Литва", "LV": "Латвия", "IT": "Италия",
    "DE": "Германия", "NL": "Нидерланды", "FR": "Франция", "ES": "Испания",
    "BE": "Бельгия", "CZ": "Чехия", "AT": "Австрия", "HU": "Венгрия", "FI": "Финляндия",
    "EE": "Эстония", "DK": "Дания", "SE": "Швеция", "RO": "Румыния", "BG": "Болгария",
    "SK": "Словакия", "UA": "Украина", "MD": "Молдова",
}


def country_ru(code: str | None) -> str:
    return _COUNTRY_RU.get(code or "", code or "")


# ---------------------------------------------------------------- qidiruv

def place_set(city: str | None, country: str | None) -> set[str] | None:
    """So'rovdagi joy -> shaharlar to'plami (atrofi bilan yoki butun davlat)."""
    if city:
        return geo.cities_near(city, NEAR_KM)
    if country:
        return geo.cities_in_country(country)
    return None


def _matching_cargos(query: Query, hours: int) -> list:
    """Bazadan so'rovga mos yuklar (hali olinmaganlari)."""
    a = place_set(query.from_city, query.from_country)
    b = place_set(query.to_city, query.to_country)
    if a is not None or b is not None:
        rows = db.search_cargos(from_cities=a, to_cities=b, body_type=query.body_type,
                                status="new", hours=hours, limit=300)
    elif query.single_city or query.single_country:
        # Bitta joy aytilgan — u yo yuklash, yo tushirish joyi bo'lishi mumkin
        one = place_set(query.single_city, query.single_country)
        rows = db.search_cargos(from_cities=one, body_type=query.body_type, status="new",
                                hours=hours, limit=150)
        rows += db.search_cargos(to_cities=one, body_type=query.body_type, status="new",
                                 hours=hours, limit=150)
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
           "scanned": len(cargos), "fit": 0, "history": 0}
    if not cargos:
        out["history"] = _history_count(query)
        return out

    # Har bir yuk uchun eng yaxshi mashina — park bo'yicha hisoblanadi. Hech bir
    # fura mos kelmasa ham yuk ko'rsatiladi (bozor): dispetcher yo'nalishni so'radi,
    # "hech narsa yo'q" deyish noto'g'ri — ilgari shunday yuklar yashirinardi.
    for n, cargo in enumerate(cargos):
        best = scoring.best_trucks(cargo, trucks, top=1) if trucks else []
        item = {"cargo": {k: cargo[k] for k in cargo.keys()}, "truck_id": None,
                "score": None, "margin_usd": None, "margin_per_day": None,
                "empty_km": None, "trip_days": None, "fits": bool(best), "order": n}
        if best:
            r = best[0]
            item.update({k: r[k] for k in ("truck_id", "score", "margin_usd",
                                          "margin_per_day", "empty_km", "trip_days")})
        out["results"].append(item)
    out["fit"] = sum(1 for r in out["results"] if r["fits"])

    # Avval furamizga mos va foydalilari (kunlik marja), keyin mos — narxi
    # noma'lum, keyin bozordagi qolganlari (eng yangisi tepada)
    def rank(r):
        if r["fits"] and r["margin_per_day"] is not None:
            return (0, -r["margin_per_day"], r["order"])
        return (1 if r["fits"] else 2, r["order"])
    out["results"].sort(key=rank)
    out["results"] = out["results"][:top]
    return out


def _history_count(query: Query) -> int:
    """Oxirgi hafta shunday yuk necha marta chiqqan.

    "Hozir yo'q" javobi yetarli emas: dispetcher bu yo'nalish umuman
    bormi yoki bugun kechikdikmi — shuni bilishi kerak.
    """
    try:
        hours = HISTORY_DAYS * 24
        one = place_set(query.single_city, query.single_country)
        if one is not None:
            rows = db.search_cargos(from_cities=one, hours=hours, limit=500)
            rows += db.search_cargos(to_cities=one, hours=hours, limit=500)
        else:
            rows = db.search_cargos(from_cities=place_set(query.from_city, query.from_country),
                                    to_cities=place_set(query.to_city, query.to_country),
                                    body_type=query.body_type, hours=hours, limit=500)
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
    """Yangi yuk dispetcher kutayotgan so'rovga mos keladimi (shahar atrofi bilan)."""
    if watch["from_city"] and cargo.get("from_city") not in geo.cities_near(
            watch["from_city"], NEAR_KM):
        return False
    if watch["to_city"] and cargo.get("to_city") not in geo.cities_near(
            watch["to_city"], NEAR_KM):
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
