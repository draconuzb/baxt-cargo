"""
parser.py — Telegram e'lon matnini strukturaga aylantirish.

Kirish: xom matn. Chiqish: Cargo obyekti (shahar, sana, vazn, stavka, kuzov...).
Regex + shahar lug'ati asosida ishlaydi. Ishonch past bo'lsa llm_parser.py
qo'shimcha tahlil qiladi (ixtiyoriy).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta

import geo

# ---------------------------------------------------------------- data model


@dataclass
class Cargo:
    raw_text: str = ""
    source: str = ""            # kanal nomi / id
    source_msg_id: int | None = None
    posted_at: datetime | None = None

    from_city: str | None = None
    to_city: str | None = None
    via: list[str] = field(default_factory=list)

    load_date: date | None = None
    weight_t: float | None = None
    volume_m3: float | None = None
    body_type: str | None = None      # ref | tent | izoterm | bort | konteyner | tral
    temp_c: float | None = None

    rate: float | None = None
    currency: str | None = None       # UZS | USD | RUB | KZT
    rate_per_ton: bool = False

    phone: str | None = None
    username: str | None = None

    kind: str = "cargo"               # cargo | truck | other
    confidence: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["load_date"] = self.load_date.isoformat() if self.load_date else None
        d["posted_at"] = self.posted_at.isoformat() if self.posted_at else None
        return d


# ---------------------------------------------------------------- normalize

def normalize(text: str) -> str:
    t = text.lower().replace("ё", "е")
    t = t.replace("\u00a0", " ").replace("ʻ", "'").replace("`", "'").replace("’", "'")
    t = re.sub(r"[ \t]+", " ", t)
    return t


# ---------------------------------------------------------------- classify

_TRUCK_MARKERS = [
    "ищу груз", "ищем груз", "нужен груз", "нужны грузы", "груз ищу",
    "свободн", "машина свободна", "есть машина", "есть машины", "предлагаю машину",
    "bo'sh mashina", "mashina bo'sh", "yuk kerak", "yuk izlayapman", "yuk qidiryapman",
    "под загрузку встанем", "стоим пустые", "пустая машина",
]
_CARGO_MARKERS = [
    "есть груз", "груз есть", "имеется груз", "нужна машина", "нужны машины",
    "требуется машина", "требуются машины", "ищу машину", "ищем машину",
    "ищу транспорт", "под загрузку", "загрузка", "yuk bor", "mashina kerak",
    "yuk chiqdi", "mashina kutilmoqda", "груз:", "gruz",
    "yuk e'loni", "yangi yuk", "xalqaro yuk", "mahalliy yuk", "yuk:", "юк бор",
]

# Platformalar (glogistics, trunck.uz…) e'lon ostiga reklama qo'shadi:
# "Sizda ham yuk yoki bo'sh mashina bormi?" — "bo'sh mashina" so'zi tufayli
# YUK e'loni bo'sh mashina deb tashlanardi (audit 2026-10-08: 2 soatda 1350 ta).
_RE_PROMO_LINE = re.compile(
    r"^.*(?:sizda ham|e'loningiz|joylang|saytga kir|saytimiz|ilovani|ilovasida|"
    r"у вас тоже|у вас есть груз|разместите|скачайте|наш сайт|наш бот).*$",
    re.MULTILINE)


def classify(text: str) -> str:
    t = _RE_PROMO_LINE.sub("", normalize(text))
    truck_hit = any(m in t for m in _TRUCK_MARKERS)
    cargo_hit = any(m in t for m in _CARGO_MARKERS)
    if truck_hit and not cargo_hit:
        return "truck"
    if cargo_hit:
        return "cargo"
    # marker yo'q: shahar + vazn yoki stavka bo'lsa ham yuk deb qaraymiz
    return "unknown"


# ---------------------------------------------------------------- fields

_RE_WEIGHT_T = re.compile(
    r"(\d{1,2}(?:[.,]\d{1,2})?)\s*(?:[-–]\s*(\d{1,2}(?:[.,]\d{1,2})?))?\s*"
    r"(?:тонн\w*|тн\b|т\b|ton\w*|tn\b|t\b)"
)
_RE_WEIGHT_KG = re.compile(r"(\d{3,6})\s*(?:кг|kg)\b")
_RE_VOLUME = re.compile(r"(\d{1,3})\s*(?:куб\w*|м3|m3|м³)")

# Raqam boshidan boshlanadi: "1.500.000 mln" dagi "500.000 mln" ni
# alohida o'qib, 500 mln qilib yubormaslik uchun (bunday xato bo'lgan).
_RE_MLN = re.compile(
    r"(?<![\d.,])(?<!\d )(\d{1,3}(?:[.,]\d{1,3})?)\s*(?:млн|mln|миллион\w*|mln\.)\s*"
    r"(?:сум|сўм|so'?m|sum|uzs)?"
)
# "1.500.000 mln", "1 500 000 млн" — to'liq summa, "mln" ortiqcha yozilgan
_RE_MLN_FULL = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[ .,]\d{3}){2,})\s*(?:млн|mln|миллион\w*)"
)
# Pul raqami: "4 200", "4.200", "4200", "4200.50" ni tanaydi, lekin
# "реф -18, 1800$" dagi ikki alohida sonni bitta raqamga qo'shib yubormaydi:
# ajratuvchi (probel/nuqta/vergul) faqat aynan 3 xonali guruh oldida qabul
# qilinadi. Bu xato ilgari 1800$ ni 181800$ ga aylantirgan.
_MONEY = r"\d{1,3}(?:[ .,]\d{3})+|\d+(?:[.,]\d{1,2})?"

_RE_USD = re.compile(
    rf"(?:\$\s*({_MONEY})|({_MONEY})\s*(?:\$|usd|долл\w*|у\.?е\.?\b|dollar))"
)
_RE_RUB = re.compile(rf"({_MONEY})\s*(?:руб\w*|rub\b|₽|р\.)")
_RE_KZT = re.compile(rf"({_MONEY})\s*(?:тг\b|тенге|kzt)")
_RE_UZS = re.compile(rf"({_MONEY})\s*(?:сум|сўм|so'?m|sum|uzs)")
_RE_PER_TON = re.compile(r"(?:за\s*тонн\w*|/\s*тн|/\s*т\b|per\s*ton|tonnasiga|бир\s*тонна)")

_RE_TEMP = re.compile(r"([+-]\s?\d{1,2})\s*(?:°|град\w*|℃|\bc\b|\bс\b)")
_RE_TEMP_MODE = re.compile(r"режим\s*([+-]?\s?\d{1,2})")
# "реф +5", "ref -18", "темп. +2" — belgidan keyin darhol kelgan son
_RE_TEMP_NEAR = re.compile(r"(?:реф\w*|ref\w*|режим|темп\w*|harorat)\D{0,4}([+-]\s?\d{1,2})")

_BODY_MAP = [
    ("ref", ["реф", "рефриж", "ref\\b", "refrij", "холодильник"]),
    ("izoterm", ["изотерм", "izoterm"]),
    ("tent", ["тент", "tent", "фура", "штор", "shtor"]),
    ("bort", ["борт", "bort", "площадк", "открыт"]),
    ("konteyner", ["контейнер", "konteyner", "container"]),
    ("tral", ["трал", "tral", "низкорам"]),
    ("samosval", ["самосвал", "samosval"]),
]

_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
    "yan": 1, "fev": 2, "mart": 3, "apr": 4, "may": 5, "iyun": 6, "iyul": 7,
    "avg": 8, "sen": 9, "okt": 10, "noy": 11, "dek": 12,
}
# Sana "12.05", "12/05/2026". Keyin o'lchov birligi kelsa — bu son, sana emas:
# "23.5 тонна" 23-may bo'lib qolardi (audit 2026-10-09: 12 soatda ~250 yuk)
_RE_DATE_NUM = re.compile(
    r"(?<!\d)(?<!\d[.,])(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d|[.,]\d)"
    r"(?!\s*(?:т\b|тн|тон|тонн|t\b|tn|ton|кг|kg|куб|м3|m3|м³|\$|usd|долл|млн|mln|%|"
    r"тыс|минг|ming|сум|so'm|руб|km|км))")
_RE_DATE_WORD = re.compile(r"(\d{1,2})\s*-?\s*(" + "|".join(_MONTHS) + r")\w*")

_RE_PHONE = re.compile(r"\+?\d[\d\s\-()]{7,18}\d")
_RE_USERNAME = re.compile(r"@([a-z0-9_]{4,32})")


def _num(s: str, decimal: bool = True) -> float | None:
    s = s.strip().replace(" ", "")
    if decimal and re.fullmatch(r"\d{1,3}[.,]\d{1,3}", s):
        return float(s.replace(",", "."))
    s = s.replace(".", "").replace(",", "")
    return float(s) if s.isdigit() else None


def _parse_weight(t: str) -> float | None:
    m = _RE_WEIGHT_T.search(t)
    if m:
        hi = m.group(2) or m.group(1)
        v = _num(hi)
        if v and 0.5 <= v <= 40:
            return v
    m = _RE_WEIGHT_KG.search(t)
    if m:
        v = _num(m.group(1), decimal=False)
        if v and 300 <= v <= 40000:
            return round(v / 1000, 2)
    return None


def _money(s: str) -> float | None:
    """Pul soni: "4 200" / "4.200" -> 4200; "200000.00" (tiyini bilan) -> 200000.

    Tiyin ".00" raqamga qo'shilib ketsa narx 100 barobar oshadi —
    "200000.00 KZT" 20 mln tenge ($41 000) bo'lib, AI uni eng foydali deb
    tavsiya qilgan.
    """
    s = s.strip().replace(" ", "")
    m = re.fullmatch(r"(\d+)[.,]\d{1,2}", s)
    if m:
        return float(m.group(1))
    return _num(s, decimal=False)


def _parse_rate(t: str) -> tuple[float | None, str | None, bool]:
    per_ton = bool(_RE_PER_TON.search(t))
    m = _RE_MLN_FULL.search(t)
    if m:
        v = _num(m.group(1), decimal=False)
        if v and v >= 100_000:
            return v, "UZS", per_ton
    m = _RE_MLN.search(t)
    if m:
        v = _num(m.group(1))
        if v:
            return v * 1_000_000, "UZS", per_ton
    m = _RE_USD.search(t)
    if m:
        v = _money(m.group(1) or m.group(2))
        if v and 50 <= v <= 100_000:
            return v, "USD", per_ton
    m = _RE_UZS.search(t)
    if m:
        v = _money(m.group(1))
        if v and v >= 100_000:
            return v, "UZS", per_ton
    m = _RE_RUB.search(t)
    if m:
        v = _money(m.group(1))
        if v and v >= 1000:
            return v, "RUB", per_ton
    m = _RE_KZT.search(t)
    if m:
        v = _money(m.group(1))
        if v and v >= 10_000:
            return v, "KZT", per_ton
    return None, None, per_ton


def _parse_body(t: str) -> str | None:
    for body, pats in _BODY_MAP:
        for p in pats:
            if re.search(p, t):
                return body
    return None


def _parse_temp(t: str) -> float | None:
    m = _RE_TEMP_MODE.search(t) or _RE_TEMP_NEAR.search(t) or _RE_TEMP.search(t)
    if m:
        v = m.group(1).replace(" ", "")
        try:
            val = float(v)
            if -30 <= val <= 30:
                return val
        except ValueError:
            pass
    if re.search(r"заморо|муз|frozen|глубок", t):
        return -18.0
    if re.search(r"охлажд|chilled|sovut", t):
        return 4.0
    return None


_RE_READY = re.compile(r"груз готов|готов к (?:загрузке|отгрузке)|загрузка готова|"
                       r"yuk tayyor|tayyor yuk|юк тай[её]р|тай[её]р юк")


def _parse_date(t: str, today: date | None = None) -> date | None:
    today = today or date.today()
    if re.search(r"сегодня|bugun", t):
        return today
    # "груз готов", "yuk tayyor" — yuk hozir tayyor, ya'ni bugun yuklanadi
    # (haqiqiy guruhlarda sanasiz e'lonlarning katta qismi shunday yoziladi)
    if _RE_READY.search(t):
        return today
    # "послезавтра" ichida "завтра" bor — avval uzunrog'ini tekshiramiz,
    # aks holda indingi yuk ertangi sana bilan yozilib qoladi.
    if re.search(r"послезавтра|indinga|indin\b", t):
        return today + timedelta(days=2)
    if re.search(r"завтра|ertaga", t):
        return today + timedelta(days=1)
    m = _RE_DATE_WORD.search(t)
    if m:
        day, mon = int(m.group(1)), _MONTHS[m.group(2)]
        return _safe_date(today.year, mon, day, today)
    for m in _RE_DATE_NUM.finditer(t):
        day, mon = int(m.group(1)), int(m.group(2))
        if 1 <= day <= 31 and 1 <= mon <= 12:
            year = int(m.group(3)) if m.group(3) else today.year
            if year < 100:
                year += 2000
            d = _safe_date(year, mon, day, today)
            if d:
                return d
    return None


def _safe_date(year: int, mon: int, day: int, today: date) -> date | None:
    """Yil ko'rsatilmagan bo'lsa — bugunga eng yaqin yilni tanlaydi.

    E'lonlar yangi bo'lgani uchun "12.05" dekabrda kelsa keyingi yilni,
    yanvarda kelsa joriy yilni bildiradi.
    """
    cands = []
    for y in (year - 1, year, year + 1):
        try:
            cands.append(date(y, mon, day))
        except ValueError:
            continue
    if not cands:
        return None
    # kelajakdagi sanalarga ustunlik beramiz (yuk kelajakda yuklanadi)
    return min(cands, key=lambda d: (abs((d - today).days), 0 if d >= today else 1))


def _parse_contact(text: str) -> tuple[str | None, str | None]:
    phone = None
    for m in _RE_PHONE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 9 <= len(digits) <= 13:
            phone = "+" + digits if not digits.startswith("+") else digits
            break
    um = _RE_USERNAME.search(text.lower())
    return phone, (um.group(1) if um else None)


# Yo'nalish belgilari: "Toshkentdan" (qayerdan), "Moskvaga" (qayerga),
# "из Москвы" / "в Ташкент". Qo'shimcha faqat so'z o'zi aniq shahar nomi
# bo'lmaganda hisoblanadi ("Kaluga" — "-ga" qo'shimchasi emas).
_FROM_SUFFIXES = ("dan", "дан")
_TO_SUFFIXES = ("gacha", "гача", "ga", "га", "ka", "ка", "qa")
_FROM_PREPS = {"из", "с", "со", "от", "iz"}
_TO_PREPS = {"в", "во", "до", "на"}
# Shablonli e'lonlar: "📍 Qayerdan: Tatariston ... 🏁 Qayerga: Toshkent shahri"
_FROM_LABELS = {"qayerdan", "откуда", "погрузка", "загрузка", "yuklash"}
_TO_LABELS = {"qayerga", "куда", "выгрузка", "разгрузка", "tushirish"}


def _route_role(words: list[str], i: int) -> str | None:
    """Shahar so'zining roli: "from" | "to" | None (belgisiz — tartib bo'yicha)."""
    w = words[i] if i < len(words) else ""
    if w and w not in geo._ALIAS_INDEX:
        if w.endswith(_FROM_SUFFIXES):
            return "from"
        if w.endswith(_TO_SUFFIXES):
            return "to"
    prev = words[i - 1] if i > 0 else ""
    if prev in _FROM_PREPS:
        return "from"
    if prev in _TO_PREPS:
        return "to"
    # Yorliq shahardan 1–2 so'z oldin: "Qayerga: Toshkent", "Загрузка: г. Москва"
    for back in words[max(0, i - 2):i]:
        if back in _FROM_LABELS:
            return "from"
        if back in _TO_LABELS:
            return "to"
    return None


def _parse_route(t: str) -> tuple[str | None, str | None, list[str]]:
    cities = geo.find_cities(t)
    frm, to, via = _route_from_cities(t, cities)
    if cities and (frm is None or to is None):
        frm, to = _country_side(t, cities, frm, to)
    return frm, to, via


def _country_side(t: str, cities, frm, to) -> tuple[str | None, str | None]:
    """Bir tomonida faqat davlat yozilgan: "Италия - Ташкент", "Samara ➡️ O'zbekiston".

    Yo'q tomon uchun davlatning asosiy shahri (`geo.COUNTRY_HUB`) olinadi:
    yuk ro'yxatda ko'rinadi, masofa taxminiy. Davlat ma'lum shahar bilan bir
    xil bo'lsa — bu shaharning izohi ("Ташкент (Узбекистан)"), olinmaydi.
    """
    known = to if frm is None else frm
    if known is None:
        return frm, to
    words = geo._norm(t).split()
    pos = next(p for p, name in cities if name == known)
    home = geo.CITIES[known].country

    def hub_in(indexes) -> str | None:
        for i in indexes:
            code = geo.country_of_word(words[i])
            if code and code != home and geo.COUNTRY_HUB.get(code) in geo.CITIES:
                return geo.COUNTRY_HUB[code]
        return None

    if frm is None:                       # "→ Ташкент": davlat shahardan oldin
        hub = hub_in(range(0, pos))
        return (hub, to) if hub else (frm, to)
    # Bitta shahar (belgisiz "qayerdan" deb olingan): davlat oldinda bo'lsa —
    # u jo'nash joyi ("Италия - Ташкент"), keyin bo'lsa — manzil ("Самара → Узбекистан")
    hub = hub_in(range(0, pos))
    if hub:
        return hub, frm
    hub = hub_in(range(pos + 1, len(words)))
    return (frm, hub) if hub else (frm, to)


SUBURB_KM = 30


def _merge_suburbs(names: list[str]) -> list[str]:
    """"Москва (Балашиха)" — bitta joy, ikki shahar emas: yonma-yon kelgan va
    30 km ichidagi shaharlardan kuratorlik qilingani (asosiysi) qoladi.
    Ikkalasi ham asosiy bo'lsa (Toshkent — Chirchiq) — bu haqiqiy yo'nalish."""
    out: list[str] = []
    for name in names:
        prev = out[-1] if out else None
        a, b = geo.CITIES.get(prev or ""), geo.CITIES.get(name)
        if a and b and (prev not in geo.CURATED or name not in geo.CURATED) \
                and geo.haversine_km(a, b) <= SUBURB_KM:
            if prev not in geo.CURATED and name in geo.CURATED:
                out[-1] = name
            continue
        out.append(name)
    return out


def _route_from_cities(t: str, cities) -> tuple[str | None, str | None, list[str]]:
    if not cities:
        return None, None, []
    words = geo._norm(t).split()
    roles = [(name, _route_role(words, pos)) for pos, name in cities]
    if any(role for _, role in roles):
        # Belgi bor — qo'shimcha/predlog tartibdan ustun
        frm = next((n for n, r in roles if r == "from"), None)
        to = next((n for n, r in roles if r == "to" and n != frm), None)
        rest = [n for n, r in roles if r is None and n not in (frm, to)]
        if frm is None and to is not None and rest:
            frm = rest.pop(0)
        if to is None and rest:
            to = rest.pop(0)
        return frm, to, []
    names = [c[1] for c in cities]
    # Shablon yo'nalishni ikki marta yozadi: "Namangan ➡️ Qarshi ... Yuk: Namangan ➡️ Qarshi"
    while len(names) >= 4 and names[:2] == names[2:4]:
        names = names[:2] + names[4:]
    names = _merge_suburbs(names)
    if len(names) == 1:
        return names[0], None, []
    if len(names) > 2:
        if names[0] == names[-1]:
            # "Toshkent-Moskva ... Toshkent-Almaty" — zanjir emas, ikki yuk:
            # birinchisini olamiz (parse_many ularni alohida ajratadi)
            return names[0], names[1], []
        # bitta e'londa zanjir: Moskva - Qozon - Toshkent
        return names[0], names[-1], names[1:-1]
    return names[0], names[1], []


# ---------------------------------------------------------------- main api

def parse(text: str, source: str = "", msg_id: int | None = None,
          posted_at: datetime | None = None, today: date | None = None) -> Cargo:
    """today — sanani hisoblash uchun tayanch kun (testlarda qotirish uchun)."""
    t = normalize(text)
    c = Cargo(raw_text=text.strip(), source=source, source_msg_id=msg_id,
              posted_at=posted_at)

    c.kind = classify(text)
    c.from_city, c.to_city, c.via = _parse_route(t)
    if c.from_city and c.from_city == c.to_city:
        c.to_city = None              # shahar ichidagi yoki noto'g'ri o'qilgan yo'nalish
    c.weight_t = _parse_weight(t)
    vol = _RE_VOLUME.search(t)
    c.volume_m3 = float(vol.group(1)) if vol else None
    c.body_type = _parse_body(t)
    c.temp_c = _parse_temp(t)
    if c.temp_c is not None and c.body_type in (None, "tent"):
        c.body_type = "ref"
    c.load_date = _parse_date(t, today)
    c.rate, c.currency, c.rate_per_ton = _parse_rate(t)
    c.phone, c.username = _parse_contact(text)

    if c.rate_per_ton and c.rate and c.weight_t:
        c.rate = c.rate * c.weight_t
        c.rate_per_ton = False

    c.confidence = _confidence(c)
    if c.kind == "unknown":
        c.kind = "cargo" if c.confidence >= 0.5 else "other"
    return c


# ---------------------------------------------------------------- ko'p yukli post

# Ro'yxat boshidagi belgilar: "1.", "2)", "•", "-", emoji
_RE_LIST_MARKER = re.compile(r"^\s*(?:\d{1,2}\s*[.)\]]|[-–—•*·◾▪▫✅➖🔹🔸📦🚛🔴🟢])\s*")
# Bitta qatorda bir nechta element bo'lsa: "1. ... 2. ..." — raqamdan oldin
# bo'lamiz. "12.05" kabi sanalar bo'linmaydi: raqamdan keyin harf kerak.
_RE_INLINE_ITEM = re.compile(r"(?<=\s)(?=\d{1,2}\s*[.)]\s*\D)")

# Bitta yukka tegishli, lekin shahar yozilmagan davom qatorlari
# ("20 тонн", "ставка 4000$") — oldingi blokka qo'shiladi.
_MIN_CITIES_FOR_SPLIT = 4


def _items(text: str) -> list[str]:
    """Matnni ro'yxat elementlariga ajratadi (qator + ichki raqamlar)."""
    out = []
    for line in text.splitlines():
        for part in _RE_INLINE_ITEM.split(line):
            part = _RE_LIST_MARKER.sub("", part).strip()
            if part:
                out.append(part)
    return out


def _city_count(text: str) -> int:
    return len(geo.find_cities(normalize(text)))


def _inherit(c: Cargo, head: str, whole_text: str, today: date | None) -> None:
    """Blokda yo'q umumiy maydonlarni sarlavha va butun matndan oladi.

    Telefon/username butun matndan olinadi — bitta postda kontakt bitta
    bo'ladi. Sana, vazn, kuzov esa faqat SARLAVHADAN: boshqa yukning
    sanasini bu yukka yopishtirib qo'ymaslik uchun.
    """
    if not c.phone or not c.username:
        phone, username = _parse_contact(whole_text)
        c.phone = c.phone or phone
        c.username = c.username or username

    if not head.strip():
        return
    h = normalize(head)
    if c.load_date is None:
        c.load_date = _parse_date(h, today)
    if c.weight_t is None:
        c.weight_t = _parse_weight(h)
    if c.body_type is None:
        c.body_type = _parse_body(h)
    if c.temp_c is None:
        c.temp_c = _parse_temp(h)
        if c.temp_c is not None and c.body_type in (None, "tent"):
            c.body_type = "ref"

    if c.rate_per_ton and c.rate and c.weight_t:
        c.rate = c.rate * c.weight_t
        c.rate_per_ton = False


def _parse_blocks(text: str, source: str, msg_id, posted_at, today) -> list[Cargo] | None:
    """Bo'sh qator bilan ajratilgan e'lonlar. Mos kelmasa — None (eski usul)."""
    blocks: list[str] = []
    for part in re.split(r"\n\s*\n", text):
        if not part.strip():
            continue
        if _city_count(part) >= 1 or not blocks:
            blocks.append(part)
        else:
            blocks[-1] += "\n" + part          # shaharsiz blok: telefon, "20 тонн"
    if sum(1 for b in blocks if _city_count(b) >= 1) < 2:
        return None
    out = []
    for block in blocks:
        if classify(block) == "truck":
            continue
        c = parse(block, source=source, msg_id=msg_id, posted_at=posted_at, today=today)
        _inherit(c, "", text, today)
        c.confidence = _confidence(c)
        if c.kind == "other":
            c.kind = "cargo" if c.confidence >= 0.5 else "other"
        if is_usable(c):
            out.append(c)
    # Hech bir blok o'zi yuk bo'lmadi ("Загрузка: Москва" \n\n "Выгрузка: Ташкент")
    # — bu bitta yukning qismlari, eski usulga qaytamiz
    return out or None


def parse_many(text: str, source: str = "", msg_id: int | None = None,
               posted_at: datetime | None = None,
               today: date | None = None) -> list[Cargo]:
    """Bitta xabardan bir nechta yukni ajratadi.

    Ekspeditorlar ko'pincha 5–10 ta yukni bitta postga ro'yxat qilib
    tashlaydi. Oddiy `parse()` bunday postdan birinchi va oxirgi shaharni
    olib, mavjud bo'lmagan yuk yasaydi — shuning uchun avval bo'lib
    ko'ramiz. Ro'yxat ajralmasa — bitta `parse()` natijasi qaytadi.
    """
    whole = parse(text, source=source, msg_id=msg_id, posted_at=posted_at, today=today)

    # Bo'sh qator bilan ajratilgan bloklar, har birida o'z yo'nalishi —
    # alohida yuklar ("🇩🇰Дания–Ташкент ... \n\n 🇺🇿Ферган–🇷🇺Москва").
    # Aks holda ular bitta "Toshkent → Farg'ona → Moskva" zanjiriga qo'shilib ketadi.
    by_blocks = _parse_blocks(text, source, msg_id, posted_at, today)
    if by_blocks is not None:
        return by_blocks

    # Bo'sh mashinalar ro'yxatini yuk deb bo'lib tashlamaymiz
    names = [n for _, n in geo.find_cities(normalize(text))]
    loop = len(names) >= 3 and names[0] == names[-1]
    if whole.kind == "truck" or (len(names) < _MIN_CITIES_FOR_SPLIT and not loop):
        return [whole]

    head: list[str] = []
    blocks: list[list[str]] = []
    for item in _items(text):
        if _city_count(item) >= 2:
            blocks.append([item])          # yangi yuk boshlandi
        elif blocks:
            blocks[-1].append(item)        # davom qatori: "20 тонн", "4000$"
        else:
            head.append(item)              # sarlavha: "БУГУНГИ ЮКЛАР"

    if len(blocks) < 2:
        return [whole]

    head_text = "\n".join(head)
    cargos: list[Cargo] = []
    for block in blocks:
        block_text = "\n".join(block)
        if classify(block_text) == "truck":
            continue
        c = parse(block_text, source=source, msg_id=msg_id,
                  posted_at=posted_at, today=today)
        _inherit(c, head_text, text, today)
        c.confidence = _confidence(c)
        if c.kind == "other":
            c.kind = "cargo" if c.confidence >= 0.5 else "other"
        if is_usable(c):
            cargos.append(c)

    # Ro'yxat sifatli ajralmadi — eski xulq-atvorga qaytamiz
    return cargos if len(cargos) >= 2 else [whole]


def _confidence(c: Cargo) -> float:
    score = 0.0
    if c.from_city:
        score += 0.25
    if c.to_city:
        score += 0.25
    if c.weight_t:
        score += 0.15
    if c.rate:
        score += 0.20
    if c.body_type:
        score += 0.08
    if c.load_date:
        score += 0.07
    return round(min(score, 1.0), 2)


def is_usable(c: Cargo) -> bool:
    """Bazaga yozishga arziydimi?"""
    # from == to: LLM bo'sh tomonga o'sha shaharni qo'yishi mumkin ("Jizzax → Jizzax")
    return (c.kind == "cargo" and bool(c.from_city and c.to_city)
            and c.from_city != c.to_city and c.confidence >= 0.45)
