"""
brain.py — AI yurak: rahbar botga oddiy gap yozadi, AI tushunadi va ishlaydi.

    "01 Moskvada, atrofidan yaxshi yuk top"      → find_cargo(truck 01, near Moskva)
    "Rossiyadan 30 mln dan arzon yuk olmagin"     → add_rule(block, from RU, min 30 mln UZS)
    "Har furaga yuk va qaytish yukini rejalab ber" → plan_fleet()

Qanday ishlaydi: model (Groq / Mistral — bepul reja) `ai_tools` dagi
asboblarni chaqiradi, natijani o'qib, qisqa javob yozadi. Hisob-kitobning
hammasi asboblarda — model raqam o'ylab topmaydi.

Token tejash:
  • model faqat odam yozganda ishlaydi (guruhdagi har bir e'lon uchun emas);
  • tarixda faqat savol va yakuniy javob saqlanadi (asbob natijalari emas);
  • asbob javoblari qisqa JSON;
  • kunlik chegara: `AI_MAX_CALLS_PER_DAY`.

Provayder ishlamasa (limit, tarmoq) — keyingisiga o'tadi. Hammasi ishlamasa
`None` qaytadi va bot oddiy qidiruvga (`search.py`) qaytadi.

Sozlama (.env):
    AI_PROVIDERS=mistral,groq        # tartib = ustuvorlik
    MISTRAL_API_KEY=...   GROQ_API_KEY=...
    AI_MODEL_MISTRAL=...  AI_MODEL_GROQ=...   # ixtiyoriy
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date

import ai_tools
import db
import rules

log = logging.getLogger("brain")

PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1",
             "model": "openai/gpt-oss-120b", "key_env": "GROQ_API_KEY"},
    "mistral": {"base_url": "https://api.mistral.ai/v1",
                "model": "mistral-medium-latest", "key_env": "MISTRAL_API_KEY"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1",
                   "model": "meta-llama/llama-3.3-70b-instruct:free",
                   "key_env": "OPENROUTER_API_KEY"},
}
# Groq Cloudflare ortida: Python'ning standart "Python-urllib" belgisini
# bloklaydi (HTTP 403, "error code: 1010") — o'z nomimiz bilan boramiz.
USER_AGENT = "baxt-cargo/1.0"
MAX_STEPS = 4
RETRY_MAX_WAIT = 12          # limit "N soniyadan keyin" desa — shuncha kutishga rozimiz
HISTORY_TURNS = 6            # bepul reja: bitta so'rov 8k tokendan oshmasin

_CYRL = re.compile(r"[А-Яа-яЁёЎўҚқҒғҲҳ]")
_LATN = re.compile(r"[A-Za-z]")
_UZ_CYRL = re.compile(r"[ЎўҚқҒғҲҳ]|\b(учун|юк|қайси|менга|бор|керак|топ)\b", re.I)


def _script_hint(text: str) -> str:
    """Foydalanuvchi qaysi til/alifboda yozgan — javob shunda bo'lsin."""
    cyr, lat = len(_CYRL.findall(text)), len(_LATN.findall(text))
    if cyr > lat:
        if _UZ_CYRL.search(text):
            return ("The owner wrote in UZBEK CYRILLIC. Answer in Uzbek using the Cyrillic "
                    "alphabet (ўзбек кирилл: ў, қ, ғ, ҳ). City names may stay as given.")
        return "The owner wrote in RUSSIAN. Answer in Russian."
    return ("The owner wrote in UZBEK LATIN. Answer in Uzbek using the Latin alphabet "
            "(o', g', sh, ch). Do not switch to Cyrillic.")
TIMEOUT = 40

SYSTEM = """You are the AI dispatcher of BAXT TRANSPORT, a trucking company from Uzbekistan \
(routes Uzbekistan–Russia–Kazakhstan and around). You talk to the owner, who is also the dispatcher.

Your job: find the most profitable cargo for OUR trucks, plan trips (cargo + return cargo), \
and follow the company rules.

Core principle: the best cargo is NOT the most expensive one. It is the one with the highest \
net margin PER DAY for a specific truck (empty run, fuel, borders and days all count). \
The tools already compute this — rank and recommend by margin_per_day.

Rules of work:
- Use tools for any data. Never invent cargo, prices, distances or numbers.
- Be economical: usually ONE tool call is enough (find_cargo, plan_truck or plan_fleet), \
at most two. Do not repeat a search with slightly different filters. The truck list is \
already below — don't call fleet or set_truck_position unless something changed.
- When the owner states a policy or preference ("don't take cargo from Russia cheaper than \
30 mln", "we like Kazan", "truck 02 never goes to Kazakhstan"), save it with add_rule, \
then repeat the rule back in one short sentence. "mln" means million UZS unless said otherwise. \
Hard "never/don't" = block; "better not / don't like" = penalty; "we like / prefer" = boost.
- Facts that are not rules (driver names, habits, clients) — save with remember.
- If the owner says a truck is somewhere else than listed below, call set_truck_position. \
Pass free_date ONLY if the owner said the date — never guess it.
- You cannot take a cargo yourself. Offers you mention get "✅ Беру" buttons under your \
message automatically — tell the owner to press them. Refer to cargos as #<cargo_id>. \
Never write links, URLs or button text yourself.
- Questions about price ("qancha so'rash kerak", "сколько просить", "narxi to'g'rimi") → \
price_advice. Give ask price and the lowest acceptable price, and quote its ready message \
to the cargo owner inside <code>…</code> so it is easy to copy.
- If nothing fits, say so and offer to notify when such cargo appears.
- Never show tool names or JSON field names to the owner.
- Mention a rule or remembered fact ONLY if it is in the lists below. Never invent rules, \
facts, cargos or trucks. Don't write "✅ Беру" yourself.
- Reply in the user's language AND alphabet: Uzbek Latin -> Uzbek Latin, Uzbek Cyrillic -> \
Uzbek Cyrillic, Russian -> Russian. Be short and \
practical: at most ~5 cargos, each on 2–3 lines: route, date, truck, empty km, margin and \
margin/day, rate. Telegram HTML only: <b>, <i>, <code>. No markdown, no tables."""


# ---------------------------------------------------------------- sozlama

def _providers() -> list[dict]:
    order = os.getenv("AI_PROVIDERS", "mistral,groq")
    out = []
    for name in [x.strip().lower() for x in order.split(",") if x.strip()]:
        preset = PROVIDERS.get(name)
        if preset is None:
            continue
        key = os.getenv(preset["key_env"], "").strip()
        if not key:
            continue
        models = os.getenv(f"AI_MODEL_{name.upper()}", "").strip() or preset["model"]
        # Bir nechta model vergul bilan: bepul limit har model uchun alohida,
        # shuning uchun biri to'lsa — keyingisi ishlaydi.
        for model in [m.strip() for m in models.split(",") if m.strip()]:
            out.append({"name": name, "base_url": preset["base_url"], "model": model,
                        "key": key})
    return out


def enabled() -> bool:
    return os.getenv("AI_ENABLED", "1") != "0" and bool(_providers())


def describe() -> str:
    ps = _providers()
    if not ps:
        return "AI o'chirilgan (GROQ_API_KEY / MISTRAL_API_KEY yo'q)"
    return " → ".join(f"{p['name']}:{p['model']}" for p in ps)


_calls = {"day": None, "count": 0}


def _budget_ok() -> bool:
    limit = int(os.getenv("AI_MAX_CALLS_PER_DAY", "600"))
    today = date.today().isoformat()
    if _calls["day"] != today:
        _calls["day"], _calls["count"] = today, 0
    return _calls["count"] < limit


def reset_state() -> None:
    _calls["day"], _calls["count"] = None, 0


# ---------------------------------------------------------------- HTTP

class ProviderError(Exception):
    def __init__(self, message: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(ex, body: str) -> float | None:
    """429 javobidan "qancha kutish kerak" ni oladi (sarlavha yoki matn)."""
    if getattr(ex, "code", None) != 429:
        return None
    header = ex.headers.get("retry-after") if getattr(ex, "headers", None) else None
    try:
        if header:
            return float(header)
    except ValueError:
        pass
    m = re.search(r"try again in ([\d.]+)s", body)
    return float(m.group(1)) if m else None


def _post(p: dict, payload: dict) -> dict:
    """Bitta so'rov. Xato — `ProviderError` (keyingi provayderga o'tamiz)."""
    if not _budget_ok():
        raise ProviderError("daily AI limit reached")
    _calls["count"] += 1
    req = urllib.request.Request(
        f"{p['base_url']}/chat/completions", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT,
                 "Authorization": f"Bearer {p['key']}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as ex:
        body = ex.read().decode(errors="replace")[:400]
        raise ProviderError(f"{p['name']} HTTP {ex.code}: {body}",
                            retry_after=_retry_after(ex, body)) from None
    except Exception as ex:
        raise ProviderError(f"{p['name']}: {ex}") from None


# Tashqi dunyo shu funksiya orqali — testlarda almashtiriladi
def chat_completion(p: dict, messages: list[dict], tools: bool = True) -> dict:
    payload = {"model": p["model"], "messages": messages,
               "temperature": 0.2, "max_tokens": 1200}
    if tools:
        payload["tools"] = ai_tools.SCHEMAS
        payload["tool_choice"] = "auto"
    else:
        payload["max_tokens"] = 400
    if "gpt-oss" in p["model"]:
        # Fikrlash tokenlari ham bepul limitdan yeyiladi — qisqa o'ylasin
        payload["reasoning_effort"] = "low"
    try:
        data = _post(p, payload)
    except ProviderError as ex:
        # Daqiqalik limit: bir necha soniya kutib, bir marta qayta urinamiz
        if ex.retry_after is None or ex.retry_after > RETRY_MAX_WAIT:
            raise
        log.info("AI limit: %.1f s kutamiz (%s)", ex.retry_after, p["model"])
        time.sleep(ex.retry_after + 0.5)
        data = _post(p, payload)
    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise ProviderError(f"{p['name']}: unexpected response {str(data)[:200]}") from None


# ---------------------------------------------------------------- ovoz → matn

STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
STT_MODEL = "whisper-large-v3-turbo"
# Whisper'ga "lug'at" — shahar va soha so'zlarini to'g'ri eshitsin
STT_PROMPT = ("Yuk, fura, ref, tent, reys, Toshkent, Samarqand, Moskva, Qozon, Almaty, "
              "Rossiya, Qozog'iston. Груз, фура, реф, тент, Ташкент, Москва, Казань, "
              "Алматы, млн сум.")
MAX_AUDIO_BYTES = 10 * 1024 * 1024


def _multipart(fields: dict, file_field: str, filename: str, data: bytes) -> tuple[bytes, str]:
    import uuid
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                     f'\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
                 f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                 .encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def _post_audio(key: str, body: bytes, boundary: str) -> dict:
    """Bitta HTTP so'rov (testlarda almashtiriladi)."""
    req = urllib.request.Request(STT_URL, data=body, headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": f"Bearer {key}", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as ex:
        text = ex.read().decode(errors="replace")[:300]
        raise ProviderError(f"stt HTTP {ex.code}: {text}",
                            retry_after=_retry_after(ex, text)) from None
    except Exception as ex:
        raise ProviderError(f"stt: {ex}") from None


def transcribe(audio: bytes, filename: str = "voice.ogg") -> str | None:
    """Ovozli xabar → matn (Groq Whisper, bepul). Ishlamasa None."""
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key or not audio or len(audio) > MAX_AUDIO_BYTES or not _budget_ok():
        return None
    _calls["count"] += 1
    body, boundary = _multipart(
        {"model": os.getenv("AI_STT_MODEL", STT_MODEL), "prompt": STT_PROMPT,
         "response_format": "json", "temperature": "0"}, "file", filename, audio)
    try:
        try:
            data = _post_audio(key, body, boundary)
        except ProviderError as ex:
            if ex.retry_after is None or ex.retry_after > RETRY_MAX_WAIT:
                raise
            time.sleep(ex.retry_after + 0.5)
            data = _post_audio(key, body, boundary)
    except ProviderError as ex:
        log.warning("Ovozni matnga aylantirib bo'lmadi: %s", ex)
        return None
    text = str(data.get("text") or "").strip()
    if _stt_noise(text):
        return None
    return text or None


# Whisper jim yoki shovqinli audioda shunday "gap"larni o'ylab topadi
_STT_NOISE = {"thank you", "thanks for watching", "спасибо", "спасибо за внимание",
              "продолжение следует", "субтитры сделал dimatorzok", "you", "bye",
              "rahmat", "subtitles by the amara.org community"}


def _stt_noise(text: str) -> bool:
    key = re.sub(r"[^\w\s]", "", text.lower()).strip()
    return not key or key in _STT_NOISE


ADVISE_SYSTEM = """You are the dispatcher AI of BAXT TRANSPORT. You get today's plan for our \
trucks as JSON (already calculated by the program — trust the numbers, never invent new ones). \
Write ONE short practical tip for the owner (1–2 sentences, Russian, no greeting, no lists): \
what to do first today — e.g. which truck to book right now, which truck has no cargo and \
where to look, or a warning. Plain text only."""


def advise(data: dict) -> str | None:
    """Bitta qisqa maslahat (asbobsiz, bitta so'rov — arzon). Ishlamasa None."""
    if not enabled():
        return None
    messages = [{"role": "system", "content": ADVISE_SYSTEM},
                {"role": "user", "content": ai_tools.dumps(data, limit=3000)}]
    for p in _providers():
        try:
            msg = chat_completion(p, messages, tools=False)
        except ProviderError as ex:
            log.warning("AI maslahat bermadi (%s): %s", p["model"], ex)
            continue
        text = (msg.get("content") or "").strip()
        if text:
            # Faqat oddiy matn: HTML/markdown qoldiqlari tozalanadi
            return re.sub(r"<[^>]+>|\*\*", "", text)[:400]
    return None


# ---------------------------------------------------------------- kontekst

def _context() -> str:
    """Har savolga qo'shiladigan qisqa holat: park, qoidalar, xotira.

    Shu tufayli oddiy savollarga ("nechta mashinamiz bor?") asbobsiz,
    bitta so'rov bilan javob beriladi.
    """
    lines = [f"Today: {date.today().isoformat()}", "", "Our trucks:"]
    trucks = db.get_trucks(active_only=False)
    for t in trucks:
        off = "" if t["active"] else " (inactive)"
        lines.append(f"- #{t['id']}{off} {t['body_type']} {t['capacity_t'] or 0:g}t, "
                     f"at {t['current_city'] or '?'}, free {t['free_date'] or '?'}"
                     + (f", driver {t['driver']}" if t["driver"] else ""))
    if not trucks:
        lines.append("- (no trucks yet)")
    # Bo'sh ro'yxat ham aniq yoziladi: kichik modellar "yo'q" ni ko'rmasa,
    # qoida o'ylab topadi (sinovda "02 Qozonga qaytmasin" deb to'qigan).
    active_rules = [r for r in rules.list_rules() if r["status"] == "active"]
    lines += ["", "Company rules (already applied by the tools):"]
    lines += [f"- rule {r['id']}: {rules.describe(r)}" for r in active_rules] or ["- none"]
    notes = db.memories(limit=20)
    lines += ["", "Remembered facts:"]
    lines += [f"- [{m['id']}] {m['note']}" for m in reversed(notes)] or ["- none"]
    return "\n".join(lines)


# ---------------------------------------------------------------- javob

@dataclass
class Reply:
    text: str
    keyboard: dict | None = None
    provider: str = ""
    tools_used: list[str] = field(default_factory=list)
    model: str = ""
    seconds: float = 0.0
    raw: str = ""                                          # model yozgan asl matn
    tool_outputs: list[str] = field(default_factory=list)  # sifat sinovi uchun


def _run(p: dict, messages: list[dict], ctx: ai_tools.Ctx) -> tuple[str, list[str]]:
    """Asbob chaqiruvlari tsikli. Yakuniy matn va ishlatilgan asboblar."""
    used: list[str] = []
    msgs = list(messages)
    for step in range(MAX_STEPS):
        msg = chat_completion(p, msgs)
        calls = msg.get("tool_calls") or []
        if not calls:
            return (msg.get("content") or "").strip(), used
        msgs.append({"role": "assistant", "content": msg.get("content") or "",
                     "tool_calls": calls})
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = None
            result = ai_tools.call(ctx, name, args if args is not None else "bad json")
            used.append(name)
            log.info("AI asbob: %s(%s) -> %s", name, fn.get("arguments"),
                     "error" if "error" in result else "ok")
            dumped = ai_tools.dumps(result)
            ctx.outputs.append(dumped)
            msgs.append({"role": "tool", "tool_call_id": call.get("id", ""),
                         "name": name, "content": dumped})
    # Qadamlar tugadi — oxirgi marta asbobsiz javob so'raymiz
    msgs.append({"role": "user", "content": "Answer now with what you have."})
    msg = chat_completion({**p}, msgs)
    return (msg.get("content") or "").strip(), used


def reply(chat_id, text: str, providers: list[dict] | None = None) -> Reply | None:
    """Foydalanuvchi xabariga AI javobi. Ishlamasa — None (bot eski qidiruvga qaytadi).

    `providers` — faqat sifat sinovi uchun (bitta modelni alohida o'lchash).
    """
    providers = providers if providers is not None else _providers()
    if not providers or not text.strip():
        return None
    messages = [{"role": "system", "content": SYSTEM + "\n\n" + _context()}]
    for h in db.ai_history(chat_id, limit=HISTORY_TURNS):
        messages.append({"role": h["role"], "content": h["content"]})
    # Alifboni dastur aniqlaydi — sinovda hamma modellar kirilldagi o'zbekcha
    # savolga lotinda javob berdi. Aniq ko'rsatma buni tuzatadi.
    messages.append({"role": "system", "content": _script_hint(text)})
    messages.append({"role": "user", "content": text.strip()[:2000]})

    for p in providers:
        ctx = ai_tools.Ctx(chat_id=str(chat_id))
        started = time.time()
        try:
            answer, used = _run(p, messages, ctx)
        except ProviderError as ex:
            log.warning("AI provayder ishlamadi (%s): %s", p["model"], ex)
            continue
        if not answer:
            continue
        if "UZBEK CYRILLIC" in _script_hint(text):
            import translit
            if translit.is_latin(answer):
                answer = translit.to_cyrillic(answer)
        log.info("AI javobi (%s, %.1fs, asboblar: %s)", p["name"],
                 time.time() - started, ",".join(used) or "-")
        db.add_ai_message(chat_id, "user", text.strip()[:2000])
        db.add_ai_message(chat_id, "assistant", answer)
        return Reply(text=to_telegram_html(answer), keyboard=_keyboard(ctx, answer),
                     provider=p["name"], tools_used=used, model=p["model"],
                     seconds=round(time.time() - started, 1), raw=answer,
                     tool_outputs=list(ctx.outputs))
    return None


# ---------------------------------------------------------------- ko'rinish

_ALLOWED_TAGS = re.compile(r"&lt;(/?)(b|i|u|s|code|pre)&gt;")


def to_telegram_html(text: str) -> str:
    """Model javobini Telegram HTML ga keltiradi.

    Model ba'zan markdown (`**qalin**`) yoki ruxsat etilmagan teg yozadi —
    Telegram bunday xabarni umuman yubormaydi. Shuning uchun hammasi
    ekranlanadi, faqat oddiy teglar qaytariladi.
    """
    text = html.escape(text, quote=False)
    text = _ALLOWED_TAGS.sub(r"<\1\2>", text)
    # Model ba'zan o'zi "Беру" havolasini yozadi — tugmalar baribir pastda,
    # shuning uchun havola olib tashlanadi, faqat so'zi qoladi.
    text = re.sub(r"&lt;a\b.*?&gt;(.*?)&lt;/a&gt;", r"\1", text)
    # Boshqa "teglar" (<button>, <br> ...) — model o'ylab topgan bezak, olib tashlanadi
    text = re.sub(r"&lt;/?[a-zA-Z][a-zA-Z0-9]*(\s[^&]*?)?/?&gt;", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    # Model o'zi yozgan "✅ Беру" tugma-qatori — haqiqiy tugma baribir pastda
    text = re.sub(r"(?m)^[^\w\n]*(?:<b>)?[^\w\n]*Беру[^\w\n]*(?:</b>)?[^\n]{0,60}$\n?",
                  "", text)
    text = re.sub(r"\s*\(✅ Беру\)|\s*✅ Беру(?=[\s,.;)]|$)", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # Markdown sarlavha "## Nom" — lekin "#4 yuk" (yuk raqami) sarlavha emas
    text = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "• ", text)
    return text.strip()[:4000]


_CARGO_REF = re.compile(r"#([1-9]\d{0,6})\b")        # #1182 — yuk
_TRUCK_REF = re.compile(r"(?:№\s?|#)(0\d)\b")          # №03 / #03 — fura


def _offers_for_mentioned(ctx: ai_tools.Ctx, answer: str) -> None:
    """Javobda tilga olingan har bir yuk uchun "✅ Беру" bo'lsin.

    AI oldingi javobni takrorlaganda asbob chaqirmaydi ("1182 ga tugma ber")
    — ilgari bunda tugma chiqmasdi. Endi moslik yozuvini dastur o'zi topadi.
    Javobda bitta fura aniq aytilgan bo'lsa — o'sha fura uchun.
    """
    have = {o["cargo_id"] for o in ctx.offers}
    trucks = {t for t in _TRUCK_REF.findall(answer)}
    truck_id = trucks.pop() if len(trucks) == 1 else None
    for raw in _CARGO_REF.findall(answer)[:5]:
        cargo_id = int(raw)
        if cargo_id not in have:
            try:
                ai_tools.offer_for_cargo(ctx, cargo_id, truck_id)
            except Exception:
                log.exception("Yuk #%s uchun tugma tayyorlanmadi", cargo_id)


def _keyboard(ctx: ai_tools.Ctx, answer: str) -> dict | None:
    """Javob ostidagi tugmalar: taklif qilingan yuklar, yangi qoidalar."""
    rows = []
    _offers_for_mentioned(ctx, answer)
    mentioned = [o for o in ctx.offers if f"#{o['cargo_id']}" in answer]
    offers = mentioned or ctx.offers[:3]
    for o in offers[:5]:
        rows.append([{"text": f"✅ Беру #{o['cargo_id']} → №{o['truck_id']} ({o['label']})"[:60],
                      "callback_data": f"take:{o['match_id']}"}])
    for rule_id in ctx.new_rules[:3]:
        rows.append([{"text": f"↩️ Отменить правило #{rule_id}",
                      "callback_data": f"rule_del:{rule_id}"}])
    for item in ctx.proposals[:3]:
        rid = item["rule"]["id"]
        rows.append([{"text": f"✅ Применить #{rid}", "callback_data": f"rule_ok:{rid}"},
                     {"text": "❌ Нет", "callback_data": f"rule_no:{rid}"}])
    return {"inline_keyboard": rows} if rows else None
