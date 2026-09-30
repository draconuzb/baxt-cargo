"""
ai_eval.py — AI sifatini o'lchash: qaysi model va ko'rsatma yaxshiroq ishlaydi.

    python main.py ai-eval                     # har bir sozlangan model alohida
    python main.py ai-eval --limit 8 --pause 5

Rahbar yozishi mumkin bo'lgan savollar (o'zbek lotin/kirill, rus, xato yozuv)
har bir modelga beriladi va javob to'rt mezon bo'yicha baholanadi:

  asbob    — kerakli asbobni chaqirdimi (masalan narx savoliga `price_advice`)
  til      — rahbar yozgan alifboda javob berdimi (lotin → lotin)
  toza     — soxta tugma/havola, asbob nomlari, JSON yo'qmi
  asosli   — javobdagi har bir katta raqam asbob natijasida bormi
             (model raqam o'ylab topmaganini tekshiradi)

Ishchi bazaga TEGILMAYDI: sinov bazaning vaqtinchalik nusxasida o'tadi
(qoida qo'shish, fura joyini o'zgartirish kabi savollar ham bor).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field

import config

log = logging.getLogger("ai_eval")


@dataclass
class Case:
    q: str
    tools: set[str]                # qabul qilinadigan asboblar; bo'sh — asbobsiz javob ham to'g'ri
    no_tool_ok: bool = False


# Tartib muhim: bazani o'zgartiradigan savollar (qoida, joy, xotira) oxirida.
CASES: list[Case] = [
    Case("Nechta furamiz bor va qayerda turibdi?", {"fleet"}, no_tool_ok=True),
    Case("01 uchun eng foydali yuk qaysi?", {"find_cargo", "plan_truck"}),
    Case("01 учун энг фойдали юк қайси?", {"find_cargo", "plan_truck"}),
    Case("Какой самый выгодный груз для машины 01?", {"find_cargo", "plan_truck"}),
    Case("02 Qozonda turibdi, atrofidan yaxshi yuk top",
         {"find_cargo", "plan_truck"}),
    Case("Har furaga yuk va qaytish yukini rejalab ber", {"plan_fleet", "plan_truck"}),
    Case("Составь план на все машины: груз и обратный груз",
         {"plan_fleet", "plan_truck"}),
    Case("Ҳар бир фурага юк топ, қайтиш юкини ҳам", {"plan_fleet", "plan_truck"}),
    Case("Toshkent Moskva yuk bormi?", {"find_cargo"}),
    Case("Есть грузы из Москвы в Ташкент?", {"find_cargo"}),
    Case("toshkentdan maskvaga ref yuk bormi", {"find_cargo"}),
    Case("Qanday qoidalarimiz bor?", {"list_rules"}, no_tool_ok=True),
    Case("4-yukka qancha so'rash kerak?", {"price_advice"}),
    Case("Сколько просить за груз #2?", {"price_advice"}),
    Case("Тошкент Москва 20 тонна тентга қанча нарх сўрайлик?", {"price_advice"}),
    Case("Toshkent Almaty yo'nalishida bozor narxi qancha?",
         {"market", "price_advice"}),
    Case("Oxirgi 7 kunda nechta yuk keldi va nechtasi olindi?", {"stats"}),
    Case("#3 yukning to'liq e'lon matnini ko'rsat", {"cargo_info"}),
    Case("salom", set(), no_tool_ok=True),
    Case("Toshkent Novosibirsk yuk chiqsa xabar ber", {"watch_route"}),
    Case("Rossiyadan 30 mln dan arzon yuk olmagin", {"add_rule"}),
    Case("Не бери грузы в Казахстан", {"add_rule"}),
    Case("Esda tut: 02 haydovchisi Sardor, u Qozog'istonga bormaydi",
         {"remember", "add_rule"}),
    Case("01 fura endi Samarqandda", {"set_truck_position"}),
]

_LATN = re.compile(r"[A-Za-zʻʼ'‘’]")
_CYRL = re.compile(r"[А-Яа-яЁёЎўҚқҒғҲҳ]")
_UZ_LETTERS = re.compile(r"[ЎўҚқҒғҲҳ]")
_BAD = re.compile(r"&lt;/?[a-z]+|\b(find_cargo|plan_truck|plan_fleet|price_advice|"
                  r"set_truck_position|add_rule|list_rules|watch_route|cargo_info|"
                  r"margin_per_day|tool_call)\b|\{\"")
# Minglik ajratgich har xil bo'ladi: oddiy, qotgan (U+00A0) va tor (U+202F) bo'shliq,
# vergul. `[^\S\n]` — qatordan tashqari har qanday bo'shliq.
_NUM = re.compile(r"\d(?:(?:[^\S\n]|,)?\d)*")


def _script(text: str) -> str:
    """Javob tili: "ru" (kerakli), "uz" (o'zbek kirill) yoki "latn"."""
    plain = re.sub(r"<[^>]+>", "", text)
    lat, cyr = len(_LATN.findall(plain)), len(_CYRL.findall(plain))
    if cyr <= lat:
        return "latn"
    return "uz" if _UZ_LETTERS.search(plain) else "ru"


def _numbers(text: str, minimum: int = 100) -> set[int]:
    out = set()
    for token in _NUM.findall(text):
        digits = re.sub(r"\D", "", token)
        if digits.isdigit() and int(digits) >= minimum:
            out.add(int(digits))
    return out


def _grounded(answer: str, corpus: str) -> tuple[bool, list[int]]:
    """Javobdagi katta raqamlar asbob natijalari/kontekstda bormi (±1 yaxlitlash)."""
    known = set(_numbers(corpus, minimum=0))       # "10 000 USD" kabi ajratilganlar
    for token in re.findall(r"-?\d+(?:\.\d+)?", corpus):
        value = float(token)
        known.update({int(value), round(value), abs(int(value)), abs(round(value))})
    missing = [n for n in _numbers(answer)
               if not any(abs(n - k) <= 1 for k in known) and not 2020 <= n <= 2030]
    return not missing, missing


@dataclass
class Score:
    model: str
    rows: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        done = [r for r in self.rows if r["ok_reply"]]
        n = len(done) or 1
        return {"model": self.model, "cases": len(self.rows), "answered": len(done),
                "tool": round(sum(r["tool"] for r in done) / n * 100),
                "lang": round(sum(r["lang"] for r in done) / n * 100),
                "clean": round(sum(r["clean"] for r in done) / n * 100),
                "grounded": round(sum(r["grounded"] for r in done) / n * 100),
                "avg_sec": round(sum(r["sec"] for r in done) / n, 1)}


def _isolate_db() -> str:
    """Ishchi bazaning nusxasi — sinov unga yozadi."""
    import db
    import rules
    import settings
    tmp = tempfile.NamedTemporaryFile(prefix="ai_eval_", suffix=".db", delete=False).name
    shutil.copy(config.DB_PATH, tmp)
    config.DB_PATH = tmp
    settings.reset_cache()
    rules.reset_cache()
    db.init()
    return tmp


def run(models: list[dict], cases: list[Case], pause: float = 8.0,
        isolate: bool = True) -> list[Score]:
    import brain
    import db
    import notifier
    if isolate:
        log.info("Sinov bazasi: %s", _isolate_db())
    notifier.send = lambda *a, **k: None          # sinovdan Telegramga xabar ketmasin

    scores = []
    for p in models:
        score = Score(model=f"{p['name']}:{p['model']}")
        for i, case in enumerate(cases):
            chat = f"eval-{p['model']}-{i}"
            db.clear_ai_history(chat)
            res = brain.reply(chat, case.q, providers=[p])
            if res is None and pause:
                time.sleep(max(pause * 4, 30))    # daqiqalik limit — kutib, bir marta qayta
                res = brain.reply(chat, case.q, providers=[p])
            row = {"q": case.q, "ok_reply": res is not None}
            if res is not None:
                used = set(res.tools_used)
                row["tool"] = bool(used & case.tools) or (case.no_tool_ok and not used)
                row["lang"] = _script(res.raw) == "ru"          # javob doim ruscha
                row["clean"] = not _BAD.search(res.text)
                corpus = " ".join(res.tool_outputs) + " " + brain._context() + " " + case.q
                row["grounded"], row["missing"] = _grounded(res.raw, corpus)
                row.update(sec=res.seconds, tools=sorted(used), answer=res.raw[:600])
            score.rows.append(row)
            log.info("%s #%d %s", score.model, i, {k: row.get(k) for k in
                                                     ("tool", "lang", "clean", "grounded")})
            if pause:
                time.sleep(pause)
        scores.append(score)
    return scores


def report(scores: list[Score]) -> str:
    lines = [f"{'model':42} {'javob':>6} {'asbob':>6} {'til':>5} {'toza':>5} "
             f"{'asosli':>7} {'sek':>5}"]
    for s in scores:
        m = s.summary()
        lines.append(f"{m['model']:42} {m['answered']:>3}/{m['cases']:<2} {m['tool']:>5}% "
                     f"{m['lang']:>4}% {m['clean']:>4}% {m['grounded']:>6}% {m['avg_sec']:>5}")
    for s in scores:
        bad = [r for r in s.rows if r["ok_reply"] and not
               (r["tool"] and r["lang"] and r["clean"] and r["grounded"])]
        failed = [r for r in s.rows if not r["ok_reply"]]
        if bad or failed:
            lines.append(f"\n--- {s.model}: xatolar")
            for r in failed:
                lines.append(f"  [javob yo'q] {r['q']}")
            for r in bad:
                flags = [k for k in ("tool", "lang", "clean", "grounded") if not r[k]]
                extra = f" missing={r['missing']}" if r.get("missing") else ""
                lines.append(f"  [{','.join(flags)}] {r['q']}  -> tools={r['tools']}{extra}")
    return "\n".join(lines)


def save(scores: list[Score], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([{"summary": s.summary(), "rows": s.rows} for s in scores], f,
                  ensure_ascii=False, indent=1)
