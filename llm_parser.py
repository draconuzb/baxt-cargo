"""
llm_parser.py — IXTIYORIY. Regex tushunmagan chalkash e'lonlar uchun.

Guruhlarda ba'zi e'lonlar juda erkin yoziladi ("bratishka Moskvaga sovutgich
kerak edi, 20 ga yaqin, kelishamiz"). Bunday xabarlarni model tushunadi.

**Provayder almashtirilishi mumkin.** Hammasi OpenAI-mos `/chat/completions`
API orqali, shuning uchun qo'shimcha kutubxona kerak emas (oddiy HTTP):

    LLM_PROVIDER=ollama       # o'z serveringizda, mutlaqo bepul
    LLM_PROVIDER=mistral      # bepul/arzon reja bor
    LLM_PROVIDER=groq         # bepul reja bor, juda tez
    LLM_PROVIDER=openrouter   # ":free" modellari bor
    LLM_PROVIDER=anthropic    # eski sozlama (ANTHROPIC_API_KEY)
    LLM_PROVIDER=none         # o'chirilgan (sukut)

Xarajatni ushlab turadigan uchta narsa:
  1. Model faqat regex uddalay olmagan xabarlar uchun chaqiriladi (~5%).
  2. Javob keshlanadi — bir xil e'lon 5 ta guruhda chiqsa ham bir marta
     so'raladi (dubl filtri bu bosqichdan keyin ishlaydi).
  3. Kunlik chegara: `LLM_MAX_CALLS_PER_DAY` (sukut 300).

Model javob bermasa yoki xato qaytarsa — tizim regex natijasi bilan
ishlayveradi, hech narsa to'xtamaydi.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import date, datetime

import config
import geo
import parser as ad_parser

log = logging.getLogger("llm")

# Provayder sozlamalari. `model` nomlari vaqt o'tishi bilan o'zgaradi —
# provayder hujjatidan tekshirib, kerak bo'lsa `LLM_MODEL` bilan almashtiring.
PROVIDERS = {
    "ollama": {
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b-instruct",
        "needs_key": False,
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-small-latest",
        "needs_key": True,
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-120b",
        "needs_key": True,
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "needs_key": True,
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-6",
        "needs_key": True,
    },
}

DEFAULT_TIMEOUT = 20
MAX_CACHE = 500

SYSTEM = """Sen yuk tashish e'lonlarini tahlil qiluvchi yordamchisan.
Foydalanuvchi Telegram guruhidan olingan xom matn beradi.
Faqat JSON qaytar, boshqa hech narsa yozma. Format:
{"kind":"cargo|truck|other","from_city":"","to_city":"","load_date":"YYYY-MM-DD",
 "weight_t":0,"body_type":"ref|tent|izoterm|bort|konteyner|tral","temp_c":0,
 "rate":0,"currency":"USD|UZS|RUB|KZT","phone":"","comment":""}
Qoidalar:
- kind="cargo" faqat YUK taklif qilinayotgan bo'lsa (mashina qidirilyapti).
- kind="truck" agar bo'sh MASHINA taklif qilinayotgan bo'lsa (yuk qidirilyapti).
- Shahar nomlarini to'liq rus tilida yoz (Ташкент, Москва, Казань).
- Noma'lum maydonni null qoldir, taxmin qilma.
- "42 млн" = 42000000 UZS. "4.2к" yoki "4200" $ belgisi bilan = USD."""


# ---------------------------------------------------------------- sozlama

def _provider() -> dict | None:
    """Sozlangan provayder yoki None.

    `config.py` tahrirlanmaydi — hamma qiymat `.env` dan.
    """
    name = os.getenv("LLM_PROVIDER", "").strip().lower()
    key = os.getenv("LLM_API_KEY", "").strip()

    if not name:
        # Eski sozlama bilan moslik: faqat ANTHROPIC_API_KEY berilgan bo'lsa
        if config.ANTHROPIC_API_KEY:
            name, key = "anthropic", config.ANTHROPIC_API_KEY
        else:
            return None
    if name in ("none", "off", "0"):
        return None

    preset = PROVIDERS.get(name)
    if preset is None:
        # Notanish nom — OpenAI-mos server deb qaraymiz (LLM_BASE_URL majburiy)
        preset = {"base_url": "", "model": "", "needs_key": False}
    if name == "anthropic" and not key:
        key = config.ANTHROPIC_API_KEY

    base_url = os.getenv("LLM_BASE_URL", "").strip() or preset["base_url"]
    model = os.getenv("LLM_MODEL", "").strip() or preset["model"]
    if not base_url or not model:
        log.warning("LLM_PROVIDER=%s uchun LLM_BASE_URL va LLM_MODEL kerak", name)
        return None
    if preset["needs_key"] and not key:
        log.warning("LLM_PROVIDER=%s uchun LLM_API_KEY berilmagan", name)
        return None

    return {"name": name, "base_url": base_url.rstrip("/"), "model": model,
            "key": key, "timeout": int(os.getenv("LLM_TIMEOUT", DEFAULT_TIMEOUT))}


def enabled() -> bool:
    return _provider() is not None


def describe() -> str:
    p = _provider()
    if p is None:
        return "LLM o'chirilgan (LLM_PROVIDER sozlanmagan)"
    return f"{p['name']} · {p['model']} · {p['base_url']}"


# ---------------------------------------------------------------- byudjet

_calls = {"day": None, "count": 0}
_cache: dict[str, dict] = {}


def _budget_left() -> bool:
    limit = int(os.getenv("LLM_MAX_CALLS_PER_DAY", "300"))
    today = date.today().isoformat()
    if _calls["day"] != today:
        _calls["day"], _calls["count"] = today, 0
    if _calls["count"] >= limit:
        return False
    return True


def usage() -> dict:
    return {"day": _calls["day"], "calls": _calls["count"],
            "cached": len(_cache),
            "limit": int(os.getenv("LLM_MAX_CALLS_PER_DAY", "300"))}


def reset_state() -> None:
    """Testlar va qayta ishga tushirish uchun."""
    _calls["day"], _calls["count"] = None, 0
    _cache.clear()


# ---------------------------------------------------------------- so'rov

def _http_json(url: str, payload: dict, headers: dict, timeout: int) -> dict | None:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "baxt-cargo/1.0", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as ex:
        body = ex.read().decode(errors="replace")[:300]
        log.warning("LLM xatosi %s: %s", ex.code, body)
        # Ba'zi serverlar `response_format` ni qo'llamaydi — usiz qayta urinamiz
        if ex.code in (400, 404, 422) and "response_format" in payload:
            payload.pop("response_format")
            return _http_json(url, payload, headers, timeout)
    except Exception as ex:
        log.warning("LLM so'rovi bajarilmadi: %s", ex)
    return None


def _extract_json(raw: str) -> dict | None:
    """Model javobidan JSON ajratadi (markdown ichida bo'lsa ham)."""
    if not raw:
        return None
    text = raw.replace("```json", "```").strip()
    if "```" in text:
        parts = [p for p in text.split("```") if p.strip()]
        text = parts[0] if parts else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
        return value if isinstance(value, dict) else None
    except ValueError:
        log.debug("LLM javobi JSON emas: %s", raw[:200])
        return None


def _ask_openai_compatible(p: dict, text: str, system: str) -> dict | None:
    headers = {}
    if p["key"]:
        headers["Authorization"] = f"Bearer {p['key']}"
    payload = {
        "model": p["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": text}],
        "max_tokens": 500,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    data = _http_json(f"{p['base_url']}/chat/completions", payload, headers, p["timeout"])
    if not data:
        return None
    try:
        return _extract_json(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        log.warning("LLM javobi kutilmagan ko'rinishda: %s", str(data)[:200])
        return None


def _ask_anthropic(p: dict, text: str, system: str) -> dict | None:
    headers = {"x-api-key": p["key"], "anthropic-version": "2023-06-01"}
    payload = {"model": p["model"], "max_tokens": 500, "system": system,
               "messages": [{"role": "user", "content": text}]}
    data = _http_json(f"{p['base_url']}/messages", payload, headers, p["timeout"])
    if not data:
        return None
    try:
        raw = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        return _extract_json(raw)
    except (KeyError, TypeError):
        log.warning("LLM javobi kutilmagan ko'rinishda: %s", str(data)[:200])
        return None


def ask(text: str, today: date | None = None) -> dict | None:
    """Matnni modelga yuboradi. Kesh va kunlik chegara shu yerda."""
    p = _provider()
    if p is None or not text.strip():
        return None

    key = hashlib.md5(f"{p['model']}|{text.strip()}".encode()).hexdigest()
    if key in _cache:
        log.debug("LLM javobi keshdan olindi")
        return _cache[key]
    if not _budget_left():
        log.warning("LLM kunlik chegarasi to'ldi (%s) — faqat regex ishlaydi",
                    os.getenv("LLM_MAX_CALLS_PER_DAY", "300"))
        return None

    system = SYSTEM + f"\nBugungi sana: {(today or date.today()).isoformat()}"
    _calls["count"] += 1
    if p["name"] == "anthropic":
        result = _ask_anthropic(p, text[:2000], system)
    else:
        result = _ask_openai_compatible(p, text[:2000], system)

    if result is not None:
        if len(_cache) >= MAX_CACHE:
            _cache.clear()
        _cache[key] = result
    return result


# ---------------------------------------------------------------- to'ldirish

def enrich(cargo: ad_parser.Cargo, today: date | None = None) -> ad_parser.Cargo:
    """Regex natijasini model javobi bilan to'ldiradi (faqat bo'sh maydonlarni).

    Regex topgan qiymat ustun turadi: u aniq qoidaga asoslangan, model esa
    taxmin qiladi.
    """
    data = ask(cargo.raw_text, today)
    if not data:
        return cargo

    # E'lon turi: matnda aniq belgi bo'lsa ("mashina kerak", "ищу груз") —
    # parser ustun. 8B model "mashina kerak" ni bo'sh fura deb adashgan.
    explicit = ad_parser.classify(cargo.raw_text)
    if data.get("kind") in ("cargo", "truck", "other") and explicit == "unknown":
        cargo.kind = data["kind"]
    # Shahar nomi lug'atdan o'tkaziladi — bazaga faqat kanonik nom tushadi
    if not cargo.from_city and data.get("from_city"):
        cargo.from_city = geo.lookup(str(data["from_city"])) or cargo.from_city
    if not cargo.to_city and data.get("to_city"):
        cargo.to_city = geo.lookup(str(data["to_city"])) or cargo.to_city
    if not cargo.weight_t and data.get("weight_t"):
        cargo.weight_t = _number(data["weight_t"], 0.5, 40)
    if not cargo.body_type and data.get("body_type") in (
            "ref", "tent", "izoterm", "bort", "konteyner", "tral", "samosval"):
        cargo.body_type = data["body_type"]
    if cargo.temp_c is None and data.get("temp_c") is not None:
        cargo.temp_c = _number(data["temp_c"], -30, 30)
    if not cargo.rate and data.get("rate"):
        currency = str(data.get("currency") or "").upper()
        rate = _number(data["rate"], 1, 10_000_000_000)
        if rate and currency in config.RATES_TO_USD:
            cargo.rate, cargo.currency = rate, currency
    # Sana faqat matnda sana izi bo'lsa — aks holda model "bugun" ni o'ylab topadi
    if not cargo.load_date and data.get("load_date") and _DATE_HINT.search(cargo.raw_text):
        try:
            cargo.load_date = datetime.fromisoformat(str(data["load_date"])[:10]).date()
        except ValueError:
            pass
    if not cargo.phone and data.get("phone"):
        digits = "".join(ch for ch in str(data["phone"]) if ch.isdigit())
        if 9 <= len(digits) <= 13:
            cargo.phone = "+" + digits

    cargo.confidence = ad_parser._confidence(cargo)
    return cargo


_DATE_HINT = re.compile(
    r"\d{1,2}\s*[./-]\s*\d{1,2}|\d{1,2}\s*-?\s*(?:янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|"
    r"дек|yan|fev|mart|apr|may|iyun|iyul|avg|sen|okt|noy|dek)"
    r"|\d{1,2}\s*-\s*(?:sida|si|chi|числа|го|е)|числа", re.I)


def _number(value, lo: float, hi: float) -> float | None:
    """Model ba'zan satr yoki aql bovar qilmas son qaytaradi — tekshiramiz."""
    try:
        number = float(str(value).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return None
    return number if lo <= number <= hi else None
