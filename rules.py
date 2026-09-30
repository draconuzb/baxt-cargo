"""
rules.py — kompaniya qoidalari: "Rossiyadan 30 mln dan arzon yuk olma".

Rahbar qoidani oddiy gap bilan aytadi, AI (`brain.py`) uni shu moduldagi
tuzilmaga aylantiradi. Keyin qoida **tokensiz**, har bir yuk hisobida
(`scoring.evaluate`) aniq qo'llanadi — model har safar so'ralmaydi.

Qoida ikki qismdan iborat:
  scope   — qaysi yuklarga tegishli (qayerdan/qayerga, davlat, kuzov, mashina);
  require — shart (eng kam stavka, marja, bo'sh probeg...). Shart berilsa,
            qoida faqat shart BAJARILMAGAN yuklarga ta'sir qiladi.

Ta'siri (`effect`):
  block   — yuk bu mashinaga umuman taklif qilinmaydi;
  penalty — ball `points` ga tushadi (yoqmaydi, lekin juda foydali bo'lsa chiqadi);
  boost   — ball `points` ga oshadi (bizga yoqadigan yo'nalish).

Qoidalar ikki yo'l bilan paydo bo'ladi:
  source=user    — rahbar aytgan, darhol ishlaydi;
  source=learned — dastur qarorlardan o'rgangan (`learn.py`), rahbar
                   tasdiqlamaguncha `proposed` holatida turadi.

Asosiy tamoyil saqlanadi: qoida ballni o'zgartiradi, lekin marja
hisobiga tegmaydi — kunlik marja hamon asosiy mezon.
"""
from __future__ import annotations

import json
import logging
import time

import config
import db
import geo

log = logging.getLogger("rules")

CACHE_TTL_SEC = 30
MAX_POINTS = 50.0

EFFECTS = ("block", "penalty", "boost")
STATUSES = ("active", "proposed", "rejected", "off")

SCOPE_KEYS = ("from_country", "to_country", "from_city", "to_city",
              "body_type", "truck_id")
REQUIRE_KEYS = ("min_rate_usd", "min_rate_per_km", "min_margin_usd",
                "min_margin_per_day", "max_empty_km", "max_trip_days",
                "max_weight_t")

BODY_TYPES = ("ref", "tent", "izoterm", "bort", "konteyner", "tral", "samosval")

# Model va odam davlatni har xil yozadi — hammasi ikki harfli kodga
_COUNTRY_ALIASES = {
    "RU": ("ru", "rus", "russia", "россия", "рф", "rossiya", "росия", "русия"),
    "UZ": ("uz", "uzb", "uzbekistan", "узбекистан", "o'zbekiston", "ozbekiston",
           "ўзбекистон", "узбекистон"),
    "KZ": ("kz", "kaz", "kazakhstan", "казахстан", "qozog'iston", "qozogiston",
           "қозоғистон", "казакстан"),
    "KG": ("kg", "kyrgyzstan", "киргизия", "кыргызстан", "qirg'iziston", "qirgiziston"),
    "TJ": ("tj", "tajikistan", "таджикистан", "tojikiston"),
    "TM": ("tm", "turkmenistan", "туркменистан", "turkmaniston"),
    "BY": ("by", "belarus", "беларусь", "belorussiya"),
    "TR": ("tr", "turkey", "турция", "turkiya"),
    "CN": ("cn", "china", "китай", "xitoy"),
    "AF": ("af", "afghanistan", "афганистан", "afg'oniston"),
}
_COUNTRY_INDEX = {alias: code for code, aliases in _COUNTRY_ALIASES.items()
                  for alias in aliases}
_COUNTRY_NAME = {"RU": "Россия", "UZ": "Узбекистан", "KZ": "Казахстан",
                 "KG": "Кыргызстан", "TJ": "Таджикистан", "TM": "Туркменистан",
                 "BY": "Беларусь", "TR": "Турция", "CN": "Китай",
                 "AF": "Афганистан"}


def country_code(value) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if raw.upper() in _COUNTRY_NAME:
        return raw.upper()
    return _COUNTRY_INDEX.get(raw.lower())


def city_country(city: str | None) -> str | None:
    info = geo.CITIES.get(city or "")
    return info.country if info else None


# ---------------------------------------------------------------- tekshirish

def normalize(effect: str, scope: dict | None, require: dict | None,
              points: float | None = None) -> tuple[dict | None, str]:
    """Kiruvchi qoidani tekshiradi va kanonik ko'rinishga keltiradi.

    Qaytaradi: (qoida, xato matni). Model noto'g'ri narsa bergan bo'lsa —
    bazaga yozilmaydi, xato modelga qaytadi va u qayta urinadi.
    """
    effect = (effect or "").strip().lower()
    if effect not in EFFECTS:
        return None, f"effect must be one of {EFFECTS}"

    clean_scope: dict = {}
    for key, value in (scope or {}).items():
        if value in (None, "", []):
            continue
        if key in ("from_country", "to_country"):
            code = country_code(value)
            if not code:
                return None, f"unknown country: {value}"
            clean_scope[key] = code
        elif key in ("from_city", "to_city"):
            city = geo.lookup(str(value))
            if not city:
                return None, f"unknown city: {value}"
            clean_scope[key] = city
        elif key == "body_type":
            if str(value).lower() not in BODY_TYPES:
                return None, f"body_type must be one of {BODY_TYPES}"
            clean_scope[key] = str(value).lower()
        elif key == "truck_id":
            truck_id = str(value).lstrip("№#").strip()
            if truck_id.isdigit():
                truck_id = truck_id.zfill(2)
            if db.get_truck(truck_id) is None:
                return None, f"unknown truck: {value}"
            clean_scope[key] = truck_id
        else:
            return None, f"unknown scope key: {key}"

    clean_require: dict = {}
    for key, value in (require or {}).items():
        if value in (None, ""):
            continue
        if key not in REQUIRE_KEYS:
            return None, f"unknown require key: {key}"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None, f"{key} must be a number"
        if number < 0:
            return None, f"{key} must be >= 0"
        clean_require[key] = number

    if effect == "block" and not clean_scope and not clean_require:
        return None, "block rule without scope or requirement would block everything"

    pts = 0.0
    if effect in ("penalty", "boost"):
        try:
            pts = float(points if points is not None else 15)
        except (TypeError, ValueError):
            return None, "points must be a number"
        pts = max(1.0, min(MAX_POINTS, abs(pts)))

    return {"effect": effect, "scope": clean_scope, "require": clean_require,
            "points": pts}, ""


# ---------------------------------------------------------------- baza

def add(effect: str, scope: dict | None = None, require: dict | None = None,
        points: float | None = None, text: str = "", source: str = "user",
        status: str = "active") -> tuple[int | None, str]:
    """Qoidani saqlaydi. Qaytaradi: (id, xato). Bir xil qoida takrorlanmaydi."""
    rule, error = normalize(effect, scope, require, points)
    if rule is None:
        return None, error
    scope_json = json.dumps(rule["scope"], sort_keys=True, ensure_ascii=False)
    require_json = json.dumps(rule["require"], sort_keys=True)
    with db.connect() as conn:
        same = conn.execute(
            "SELECT id, status FROM rules WHERE effect=? AND scope=? AND require=?",
            (rule["effect"], scope_json, require_json)).fetchone()
        if same is not None:
            if source == "user" and same["status"] != "active":
                conn.execute("UPDATE rules SET status='active', source='user'"
                             " WHERE id=?", (same["id"],))
                reset_cache()
            return same["id"], ""
        cur = conn.execute(
            "INSERT INTO rules (effect, scope, require, points, text, source, status)"
            " VALUES (?,?,?,?,?,?,?)",
            (rule["effect"], scope_json, require_json, rule["points"],
             text.strip()[:300], source, status))
        rule_id = cur.lastrowid
    reset_cache()
    log.info("Qoida #%s (%s): %s", rule_id, source, describe(get(rule_id)))
    return rule_id, ""


def _decode(row) -> dict:
    out = {k: row[k] for k in row.keys()}
    out["scope"] = json.loads(out["scope"] or "{}")
    out["require"] = json.loads(out["require"] or "{}")
    return out


def get(rule_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
    return _decode(row) if row else None


def list_rules(status: str | None = None) -> list[dict]:
    q, params = "SELECT * FROM rules", []
    if status:
        q += " WHERE status=?"
        params.append(status)
    q += " ORDER BY id"
    with db.connect() as conn:
        return [_decode(r) for r in conn.execute(q, params).fetchall()]


def set_status(rule_id: int, status: str) -> bool:
    if status not in STATUSES:
        return False
    with db.connect() as conn:
        ok = conn.execute("UPDATE rules SET status=? WHERE id=?",
                          (status, rule_id)).rowcount > 0
    reset_cache()
    return ok


def delete(rule_id: int) -> bool:
    with db.connect() as conn:
        ok = conn.execute("DELETE FROM rules WHERE id=?", (rule_id,)).rowcount > 0
    reset_cache()
    return ok


_cache: dict[str, tuple[float, list[dict]]] = {}


def reset_cache() -> None:
    _cache.clear()


def active() -> list[dict]:
    """Amaldagi qoidalar (30 s kesh — har bir yuk uchun bazaga bormaymiz)."""
    key = str(config.DB_PATH)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_SEC:
        return hit[1]
    try:
        rules = list_rules("active")
    except Exception:
        # jadval hali yo'q (eski baza) — qoidasiz ishlaymiz
        log.debug("Qoidalar o'qilmadi", exc_info=True)
        rules = []
    _cache[key] = (time.time(), rules)
    return rules


# ---------------------------------------------------------------- qo'llash

def _in_scope(scope: dict, c: dict, t: dict) -> bool:
    if scope.get("from_city") and c.get("from_city") != scope["from_city"]:
        return False
    if scope.get("to_city") and c.get("to_city") != scope["to_city"]:
        return False
    if scope.get("from_country") and city_country(c.get("from_city")) != scope["from_country"]:
        return False
    if scope.get("to_country") and city_country(c.get("to_city")) != scope["to_country"]:
        return False
    if scope.get("body_type"):
        body = c.get("body_type") or ("ref" if c.get("temp_c") is not None else None)
        if body != scope["body_type"]:
            return False
    if scope.get("truck_id") and str(t.get("id")) != scope["truck_id"]:
        return False
    return True


def _values(c: dict, res: dict) -> dict:
    """Shartlar solishtiriladigan qiymatlar (hisob natijasidan)."""
    rate_usd = res.get("revenue_usd")
    if rate_usd is None:
        rate_usd = c.get("rate_usd")
    return {
        "min_rate_usd": rate_usd,
        "min_rate_per_km": res.get("rate_per_km"),
        "min_margin_usd": res.get("margin_usd"),
        "min_margin_per_day": res.get("margin_per_day"),
        "max_empty_km": res.get("empty_km"),
        "max_trip_days": res.get("trip_days"),
        "max_weight_t": c.get("weight_t"),
    }


def _violations(require: dict, values: dict) -> list[str]:
    """Bajarilmagan shartlar. Qiymat noma'lum bo'lsa (stavka yozilmagan) —
    buzilgan deb hisoblamaymiz: taxmin bilan yukni yashirmaymiz."""
    out = []
    for key, limit in require.items():
        value = values.get(key)
        if value is None:
            continue
        if key.startswith("min_") and value < limit:
            out.append(key)
        elif key.startswith("max_") and value > limit:
            out.append(key)
    return out


def apply(c: dict, t: dict, res: dict,
          rules: list[dict] | None = None) -> tuple[list[str], float, list[str]]:
    """Yuk+mashina juftligiga qoidalarni qo'llaydi.

    Qaytaradi: (to'sish sabablari, ballga qo'shimcha, izohlar).
    """
    rules = active() if rules is None else rules
    fails: list[str] = []
    delta = 0.0
    notes: list[str] = []
    if not rules:
        return fails, delta, notes

    values = _values(c, res)
    for rule in rules:
        if not _in_scope(rule["scope"], c, t):
            continue
        if rule["require"]:
            if not _violations(rule["require"], values):
                continue          # shart bajarilgan — qoida ta'sir qilmaydi
        label = f"правило #{rule['id']}: {describe(rule)}"
        if rule["effect"] == "block":
            fails.append(label)
        elif rule["effect"] == "penalty":
            delta -= rule["points"]
            notes.append(f"−{rule['points']:g} {label}")
        elif rule["effect"] == "boost":
            delta += rule["points"]
            notes.append(f"+{rule['points']:g} {label}")
    return fails, delta, notes


# ---------------------------------------------------------------- matn

_REQ_TEXT = {
    "min_rate_usd": "ставка от ${:,.0f}",
    "min_rate_per_km": "ставка от ${:g}/км",
    "min_margin_usd": "маржа от ${:,.0f}",
    "min_margin_per_day": "маржа от ${:,.0f}/день",
    "max_empty_km": "пустой пробег до {:,.0f} км",
    "max_trip_days": "рейс до {:g} дн.",
    "max_weight_t": "вес до {:g} т",
}


def scope_text(s: dict) -> str:
    """Qoida qaysi yuklarga tegishli — ruscha qisqa matn."""
    where = []
    src = geo.ru(s.get("from_city")) or _COUNTRY_NAME.get(s.get("from_country", ""),
                                                           s.get("from_country"))
    dst = geo.ru(s.get("to_city")) or _COUNTRY_NAME.get(s.get("to_country", ""),
                                                         s.get("to_country"))
    if src and dst:
        where.append(f"{src} → {dst}")
    elif src:
        where.append(f"из {src}")
    elif dst:
        where.append(f"в {dst}")
    if s.get("body_type"):
        where.append(s["body_type"])
    if s.get("truck_id"):
        where.append(f"машина №{s['truck_id']}")
    return ", ".join(where) or "все грузы"


def describe(rule: dict | None) -> str:
    """Qoidani odam o'qiydigan ko'rinishda (ruscha — bot shu matnni ko'rsatadi)."""
    if not rule:
        return "—"
    scope_txt = scope_text(rule["scope"])

    req = [_REQ_TEXT[k].format(v).replace(",", " ")
           for k, v in rule["require"].items() if k in _REQ_TEXT]
    req_txt = " и ".join(req)

    if rule["effect"] == "block":
        if req_txt:
            return f"{scope_txt}: брать только если {req_txt}"
        return f"{scope_txt}: не брать"
    sign = "−" if rule["effect"] == "penalty" else "+"
    verb = "не нравится" if rule["effect"] == "penalty" else "предпочитаем"
    cond = f" (если не выполнено: {req_txt})" if req_txt else ""
    return f"{scope_txt}: {verb}{cond}, {sign}{rule['points']:g} балл."
