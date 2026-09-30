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
]


def classify(text: str) -> str:
    t = normalize(text)
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

_RE_MLN = re.compile(
    r"(\d{1,3}(?:[.,]\d{1,3})?)\s*(?:млн|mln|миллион\w*|mln\.)\s*"
    r"(?:сум|сўм|so'?m|sum|uzs)?"
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
_RE_DATE_NUM = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)")
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


def _parse_rate(t: str) -> tuple[float | None, str | None, bool]:
    per_ton = bool(_RE_PER_TON.search(t))
    m = _RE_MLN.search(t)
    if m:
        v = _num(m.group(1))
        if v:
            return v * 1_000_000, "UZS", per_ton
    m = _RE_USD.search(t)
    if m:
        v = _num(m.group(1) or m.group(2), decimal=False)
        if v and 50 <= v <= 100_000:
            return v, "USD", per_ton
    m = _RE_UZS.search(t)
    if m:
        v = _num(m.group(1), decimal=False)
        if v and v >= 100_000:
            return v, "UZS", per_ton
    m = _RE_RUB.search(t)
    if m:
        v = _num(m.group(1), decimal=False)
        if v and v >= 1000:
            return v, "RUB", per_ton
    m = _RE_KZT.search(t)
    if m:
        v = _num(m.group(1), decimal=False)
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


def _parse_route(t: str) -> tuple[str | None, str | None, list[str]]:
    cities = geo.find_cities(t)
    if not cities:
        return None, None, []
    names = [c[1] for c in cities]
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
    return c.kind == "cargo" and bool(c.from_city and c.to_city) and c.confidence >= 0.45
