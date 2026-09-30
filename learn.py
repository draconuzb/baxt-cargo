"""
learn.py — dastur rahbarning qarorlaridan o'rganadi.

Har bir "✅ Беру" va "⏭ Пропустить" — bu kompaniya didi haqida ma'lumot.
Masalan, Qozog'istonga ketadigan yuklar 8 marta o'tkazib yuborilgan va
bittasi ham olinmagan bo'lsa — bu tasodif emas. Dastur shunday naqshni
topib, **qoida taklif qiladi**:

    "Qozog'istonga ketadigan yuklarni doim o'tkazib yuborasiz (0 / 8).
     Ballini pasaytiraymi?"   [✅ Да] [❌ Нет]

Taklif rahbar tasdiqlamaguncha ishlamaydi (`rules.status = proposed`).
Rad etilgan taklif qayta so'ralmaydi — `rejected` bo'lib bazada qoladi.

Model ishlatilmaydi — oddiy sanoq. Bepul va tushunarli: har bir taklif
yonida "nega" (nechta olindi / o'tkazildi) yoziladi.
"""
from __future__ import annotations

import json
import logging

import db
import rules

log = logging.getLogger("learn")

WINDOW_DAYS = 60
MIN_SKIPS = 6           # shuncha o'tkazish bo'lmasa — xulosa chiqarmaymiz
MAX_TAKE_RATE = 0.1     # olinganlar ulushi shundan past bo'lsa — "yoqmaydi"
MIN_TAKES = 4
MIN_PREFER_RATE = 0.6   # olinganlar ulushi shundan yuqori bo'lsa — "yoqadi"
PENALTY_POINTS = 15
BOOST_POINTS = 10


def _decisions(days: int = WINDOW_DAYS) -> list[dict]:
    """Dispetcher qarorlari (tizim tozalashi — `cancelled` — hisobga kirmaydi)."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT m.decision, m.truck_id, c.from_city, c.to_city, c.body_type,
                      c.temp_c, c.rate_usd
               FROM matches m JOIN cargos c ON c.id = m.cargo_id
               WHERE m.decision IN ('taken', 'skipped') AND m.created_at >= ?""",
            (db._ago(days=days),)).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


def _segments(d: dict) -> list[dict]:
    """Bitta qaror qaysi guruhlarga tushadi: yo'nalish, davlat, kuzov."""
    src = rules.city_country(d["from_city"])
    dst = rules.city_country(d["to_city"])
    out = []
    if d["from_city"] and d["to_city"]:
        out.append({"from_city": d["from_city"], "to_city": d["to_city"]})
    if dst:
        out.append({"to_country": dst})
    if src and src != "UZ":
        out.append({"from_country": src})
    if src and dst and src != dst:
        out.append({"from_country": src, "to_country": dst})
    body = d["body_type"] or ("ref" if d["temp_c"] is not None else None)
    if body:
        out.append({"body_type": body})
    return out


def _key(scope: dict) -> str:
    return json.dumps(scope, sort_keys=True, ensure_ascii=False)


def analyze(days: int = WINDOW_DAYS) -> list[dict]:
    """Naqshlarni topadi. Qaytaradi: [{effect, scope, points, taken, skipped, why}]."""
    stats: dict[str, dict] = {}
    for d in _decisions(days):
        for scope in _segments(d):
            s = stats.setdefault(_key(scope), {"scope": scope, "taken": 0, "skipped": 0})
            s[d["decision"]] += 1

    found = []
    for s in stats.values():
        total = s["taken"] + s["skipped"]
        if not total:
            continue
        rate = s["taken"] / total
        if s["skipped"] >= MIN_SKIPS and rate <= MAX_TAKE_RATE:
            found.append({**s, "effect": "penalty", "points": PENALTY_POINTS})
        elif s["taken"] >= MIN_TAKES and rate >= MIN_PREFER_RATE and total >= MIN_TAKES + 1:
            found.append({**s, "effect": "boost", "points": BOOST_POINTS})

    # Aniqroq naqsh (yo'nalish) umumiyroqni (davlat) qamrab olsa — ikkalasini
    # taklif qilmaymiz: eng ko'p qarorga asoslanganini qoldiramiz.
    found.sort(key=lambda f: (f["taken"] + f["skipped"]), reverse=True)
    for f in found:
        f["why"] = f"взято {f['taken']} из {f['taken'] + f['skipped']}"
    return found


def propose(days: int = WINDOW_DAYS, limit: int = 3) -> list[dict]:
    """Yangi takliflarni `proposed` qoida sifatida yozadi va qaytaradi.

    Allaqachon bor (faol, taklif qilingan yoki rad etilgan) qoida qayta
    taklif qilinmaydi.
    """
    existing = {(r["effect"], _key(r["scope"])) for r in rules.list_rules()}
    # teskari ta'sirli qoida bo'lsa ham taklif qilmaymiz (rahbar o'zi hal qilgan)
    touched = {_key(r["scope"]) for r in rules.list_rules()}
    out = []
    for f in analyze(days):
        rule, _ = rules.normalize(f["effect"], f["scope"], None, f["points"])
        if rule is None:
            continue
        key = _key(rule["scope"])
        if (f["effect"], key) in existing or key in touched:
            continue
        rule_id, error = rules.add(f["effect"], f["scope"], None, f["points"],
                                   text=f"Выучено: {f['why']}", source="learned",
                                   status="proposed")
        if rule_id is None:
            log.debug("Taklif yozilmadi: %s", error)
            continue
        out.append({"rule": rules.get(rule_id), "why": f["why"]})
        touched.add(key)
        if len(out) >= limit:
            break
    if out:
        log.info("%d ta yangi qoida taklif qilindi", len(out))
    return out


def format_proposal(item: dict) -> str:
    rule = item["rule"]
    action = "понижать балл" if rule["effect"] == "penalty" else "повышать балл"
    habit = ("обычно пропускаете" if rule["effect"] == "penalty"
             else "обычно берёте")
    return (f"🧠 <b>Я заметил закономерность</b>\n\n"
            f"{rules.scope_text(rule['scope'])} — "
            f"вы {habit} ({item['why']}).\n\n"
            f"Предлагаю правило: {action} на {rule['points']:g}.\n"
            f"<i>{rules.describe(rule)}</i>")


def proposal_keyboard(rule_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Да, применить", "callback_data": f"rule_ok:{rule_id}"},
        {"text": "❌ Нет", "callback_data": f"rule_no:{rule_id}"},
    ]]}
