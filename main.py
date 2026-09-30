#!/usr/bin/env python3
"""
main.py — boshqaruv nuqtasi.

    python main.py init                 # baza + mashinalarni yuklash
    python main.py demo                 # Telegramsiz sinov (namunaviy e'lonlar)
    python main.py backfill --limit 300 # guruhlardagi eski xabarlarni o'qish
    python main.py listen               # jonli rejim
    python main.py report --truck 01    # bitta mashina uchun eng yaxshi yuklar
    python main.py roundtrip --cargo 5 --truck 01
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import config
import db
import notifier
import pipeline
import scoring


def _setup_console() -> None:
    """Windows konsoli sukut bo'yicha cp1252 — kirill harf yoki emoji
    chiqarilganda dastur qulab tushardi. Chiqishni UTF-8 ga o'tkazamiz,
    imkoni bo'lmasa xato o'rniga '?' qo'yiladi."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _setup_logging() -> None:
    """Konsolga va faylga yozadi (TZ 5.8.7).

    Fayl 5 MB dan oshsa aylanadi, 5 tagacha eski nusxa saqlanadi —
    VPS diski to'lib qolmasin. Fayl yozilmasa (huquq yo'q) tizim
    to'xtamaydi, faqat konsolga yozadi.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_dir = Path(os.getenv("LOG_DIR", str(config.BASE_DIR / "logs")))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(
            log_dir / "baxt.log", maxBytes=5_000_000, backupCount=5,
            encoding="utf-8"))
    except OSError as e:
        print(f"Log fayli yozilmadi ({e}) — faqat konsolga yoziladi")

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers)


_setup_console()
_setup_logging()
log = logging.getLogger("main")

DEMO_ADS = [
    "🔴 ЕСТЬ ГРУЗ\nКазань → Ташкент\n20 тонн, реф +5\n42 млн сум\nзагрузка завтра\nТел: +998 90 123 45 67",
    "Москва-Ташкент тент 20т 4200$ послезавтра @uzlogistic",
    "Свободная машина реф 20т в Алматы, ищу груз на Ташкент, 87011234567",
    "Казань → Ташкент, реф +5, 20 тонн, 42 млн сум, завтра, тел 998901234567",  # dubl
    "нужна машина Самарканд - Екатеринбург 18 тонн тент 55 млн сум завтра",
    "Ташкент → Алматы реф -18 20т 1800$ завтра @coldchain_uz",
    # bitta postda uchta yuk — parse_many sinovi
    "📦 БУГУНГИ ЮКЛАР\n1. Бухара - Москва, 20т тент, 4100$\n"
    "2. Наманган - Казань, 18т реф +2, 3900$\n"
    "3. Карши - Алматы, 20т тент, 1700$\nТел: +998901234567",
]


def cmd_init(args):
    db.init()
    path = Path(args.trucks or config.TRUCKS_FILE)
    if not path.exists():
        # demo va birinchi ishga tushirish to'xtab qolmasin — namunaga tushamiz
        example = config.BASE_DIR / "trucks.example.json"
        if example.exists():
            log.warning("%s topilmadi — namunaviy parkka (%s) o'tildi. "
                        "Real ma'lumot uchun trucks.json yarating.",
                        path.name, example.name)
            path = example
        else:
            log.warning("%s topilmadi — mashina yuklanmadi", path)
            return
    trucks = json.loads(path.read_text(encoding="utf-8"))
    for t in trucks:
        db.upsert_truck(t)
    log.info("%d ta mashina yuklandi (%s)", len(trucks), path.name)


def cmd_demo(args):
    db.init()
    if not db.get_trucks():
        cmd_init(args)
    for ad in DEMO_ADS:
        pipeline.handle_message(ad, source="demo_group")
    print("\n=== BAZADAGI YUKLAR ===")
    for r in db.active_cargos():
        print(f"#{r['id']:3} {r['from_city']} -> {r['to_city']:16} "
              f"{r['weight_t'] or '?'}t {r['body_type'] or '?':8} "
              f"{r['rate_usd'] or '?'}$ {r['load_date'] or '?'} [{r['source']}]")


def cmd_backfill(args):
    import listener
    db.init()
    asyncio.run(listener.backfill(limit=args.limit))


def cmd_listen(args):
    import listener
    db.init()
    asyncio.run(listener.run())


def cmd_bot(args):
    """Dispetcher boti — kartochka tugmalari va buyruqlar.

    `listen` bilan bir vaqtda, alohida process'da ishlaydi.
    """
    import bot
    db.init()
    bot.run()


def cmd_llm(args):
    """LLM provayderini sinash: ulanganmi va chalkash e'lonni tushunadimi.

        python main.py llm
        python main.py llm --text "bratishka Moskvaga sovutgich kerak edi"
    """
    import llm_parser
    import parser as ad_parser

    print(f"\nProvayder: {llm_parser.describe()}")
    if not llm_parser.enabled():
        print("\nUlash uchun .env ga yozing, masalan:\n"
              "   LLM_PROVIDER=ollama      # o'z serveringizda, bepul\n"
              "   LLM_PROVIDER=groq\n   LLM_API_KEY=...\n")
        return

    text = args.text or ("bratishka Moskvaga sovutgich kerak edi, 20 ga yaqin, "
                         "12-sida yuklanadi, kelishamiz 998901234567")
    print(f"\nMatn: {text}\n")

    before = ad_parser.parse(text)
    print(f"Regex:  {before.from_city} -> {before.to_city} | {before.weight_t}t "
          f"{before.body_type} | {before.rate} {before.currency} | "
          f"ishonch {before.confidence}")

    after = llm_parser.enrich(ad_parser.parse(text))
    print(f"LLM:    {after.from_city} -> {after.to_city} | {after.weight_t}t "
          f"{after.body_type} | {after.rate} {after.currency} | "
          f"ishonch {after.confidence}")
    if not llm_parser.enabled() or after.confidence == before.confidence:
        print("\n(model qo'shimcha ma'lumot topmadi yoki javob bermadi — "
              "loglarga qarang)")
    print(f"\nSo'rovlar: {llm_parser.usage()}")


def cmd_web(args):
    """Veb-panel (FastAPI). Sukut bo'yicha faqat shu kompyuterdan ochiladi."""
    try:
        import web
    except ImportError as ex:
        raise SystemExit(f"Veb-panel uchun kutubxona yo'q ({ex.name}): "
                         "pip install fastapi uvicorn")
    web.run(host=args.host, port=args.port)


def cmd_expire(args):
    db.init()
    log.info("%d ta eskirgan e'lon yopildi", db.expire_old_cargos(hours=args.hours))


def cmd_stats(args):
    """Statistika: qaysi guruh foydali, ball to'g'rimi, prognoz aniqmi."""
    import analytics
    db.init()
    analytics.print_report(analytics.report(days=args.days))


def cmd_gps(args):
    """Mashinalar holatini GPS bo'yicha yangilash.

    `--loop` bilan fon vazifasi sifatida ishlaydi; `bot` ham buni
    30 daqiqada bir o'zi bajaradi, shuning uchun alohida process shart emas.
    """
    import gps
    db.init()
    if args.loop:
        gps.run(interval=args.interval)
        return
    report = gps.format_report(gps.sync())
    print(report or "GPS bo'yicha o'zgarish yo'q")


def cmd_wialon(args):
    """Wialon (gpsmonitor.uz) ulanishini tekshirish va trekerlarni ko'rish.

        WIALON_URL=https://gpsmonitor.uz WIALON_TOKEN=... python main.py wialon

    Ro'yxatdagi "mashina" ustuni bo'sh bo'lsa — treker nomi biror mashinaning
    davlat raqamiga mos kelmagan. Uni `.env` dagi WIALON_UNITS ga qo'shing:
        WIALON_UNITS={"12345": "01"}
    """
    import gps
    db.init()
    adapter = gps.WialonAdapter()
    print(f"\nServer: {adapter.base_url}")
    if not adapter.token:
        print("WIALON_TOKEN berilmagan — .env ga yozing")
        return
    try:
        units = adapter.units()
    except gps.WialonError as ex:
        print(f"Ulanmadi: {ex}")
        return
    except Exception as ex:
        print(f"Ulanmadi: {ex}")
        return
    if not units:
        print("Ulanish bor, lekin obyekt topilmadi (token huquqlarini tekshiring)")
        return

    trucks = db.get_trucks(active_only=False)
    print(f"Topildi: {len(units)} ta obyekt\n")
    print(f"{'id':10} {'nomi':24} {'koordinata':22} {'signal vaqti':18} mashina")
    for u in units:
        truck_id = adapter.match_truck(u, trucks)
        coords = (f"{u['lat']:.4f}, {u['lon']:.4f}"
                  if u["lat"] is not None else "—")
        city = geo_nearest(u) if u["lat"] is not None else ""
        link = f"№{truck_id}" if truck_id else "— bog'lanmagan"
        print(f"{u['id']:10} {u['name'][:24]:24} {coords:22} "
              f"{str(u['at'] or '—')[:16]:18} {link} {city}")
    print("\nBog'lanmaganlar uchun: WIALON_UNITS={\"<id>\": \"<mashina>\"}")


def geo_nearest(unit: dict) -> str:
    import geo
    city = geo.nearest_city(unit["lat"], unit["lon"])
    return f"({city})" if city else ""


def cmd_calibrate(args):
    """Real reyslar bo'yicha masofa koeffitsientini qayta hisoblash (TZ 5.4-A).

    Kirish fayli (trips.json):
        [{"from": "Qozon", "to": "Toshkent", "km": 3100}, ...]
    """
    import geo
    path = Path(args.trips)
    if not path.exists():
        print(f"{path} topilmadi. Namuna:\n"
              '[{"from": "Qozon", "to": "Toshkent", "km": 3100}]')
        return
    trips = json.loads(path.read_text(encoding="utf-8"))
    factors = geo.calibrate(trips)
    if not factors:
        print("Koeffitsient hisoblanmadi — shahar nomlari yoki km tekshiring")
        return

    print(f"\n{len(trips)} ta reys bo'yicha yangi koeffitsientlar:\n")
    for key, value in sorted(factors.items()):
        old = geo.road_factor_for(*key.split("-"))
        print(f"   {key}: {old:.3f} -> {value:.3f}")

    out = config.BASE_DIR / "road_factors.json"
    existing = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    existing.update(factors)
    out.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    geo.reload_road_factors()
    print(f"\n{out.name} yangilandi. Marja hisobi shu koeffitsientlar bilan ketadi.")


def cmd_report(args):
    db.init()
    trucks = db.get_trucks()
    if args.truck:
        trucks = [t for t in trucks if t["id"] == args.truck]
    cargos = db.active_cargos(hours=args.hours)
    if not cargos:
        print("Aktiv yuk yo'q. Avval `demo` yoki `backfill` ni ishga tushiring.")
        return
    for t in trucks:
        print(f"\n🚛 МАШИНА №{t['id']} — {t['body_type']}, {t['current_city']}, "
              f"свободна {t['free_date']}")
        best = scoring.best_cargos(t, cargos, top=args.top)
        if not best:
            print("   mos yuk topilmadi")
            continue
        for r in best:
            c = db.get_cargo(r["cargo_id"])
            print(f"   {r['score']:5.1f} | {c['from_city']} → {c['to_city']:14} | "
                  f"пустой {r['empty_km']:4} км | маржа {notifier.money(r['margin_usd'])}"
                  f" ({notifier.money(r['margin_per_day'])}/дн)")


def cmd_roundtrip(args):
    db.init()
    cargo = db.get_cargo(args.cargo)
    trucks = {t["id"]: t for t in db.get_trucks()}
    truck = trucks.get(args.truck)
    if not cargo or not truck:
        print("Yuk yoki mashina topilmadi")
        return
    chains = scoring.best_roundtrip(cargo, truck, db.active_cargos(hours=args.hours))
    if not chains:
        print("Qaytish yuki topilmadi")
        return
    print(f"\n🔁 ZANJIR: {cargo['from_city']} → {cargo['to_city']} + qaytish\n")
    for ch in chains:
        b = ch["back_cargo"]
        print(f"   {b['from_city']} → {b['to_city']:14} | jami marja "
              f"{notifier.money(ch['total_margin_usd'])} | {ch['total_days']} kun | "
              f"{notifier.money(ch['margin_per_day'])}/kun | bo'sh {ch['empty_km']} km")


def cmd_ai(args):
    """AI yurakni terminaldan sinash: `python main.py ai "01 Moskvada, yuk top"`.

    Kalitlar to'g'ri ulanganini tekshirish uchun ham qulay.
    """
    import brain
    db.init()
    print("AI:", brain.describe())
    if not args.text:
        return
    res = brain.reply(args.chat or "cli", " ".join(args.text))
    if res is None:
        print("Javob yo'q (kalit, limit yoki tarmoqni tekshiring — logga qarang)")
        return
    tools = ", ".join(res.tools_used) or "-"
    print(f"[{res.provider} · asboblar: {tools}]\n")
    print(res.text)
    if res.keyboard:
        for row in res.keyboard["inline_keyboard"]:
            print("  [" + "] [".join(b["text"] for b in row) + "]")


def cmd_ai_eval(args):
    """AI sifat sinovi: har bir model alohida, bazaning nusxasida."""
    import ai_eval
    import brain
    db.init()
    models = brain._providers()
    if args.models:
        wanted = [m.strip() for m in args.models.split(",") if m.strip()]
        models = [p for p in models if p["model"] in wanted or p["name"] in wanted]
    if not models:
        print("Model yo'q — .env da GROQ_API_KEY / MISTRAL_API_KEY ni tekshiring")
        return
    cases = ai_eval.CASES[:args.limit] if args.limit else ai_eval.CASES
    scores = ai_eval.run(models, cases, pause=args.pause)
    print(ai_eval.report(scores))
    if args.out:
        ai_eval.save(scores, args.out)
        print(f"\nBatafsil: {args.out}")


def cmd_watchdog(args):
    """Nazoratchi (cron har 10 daqiqada): muammo paydo bo'lsa yoki tuzalsa — Telegram."""
    import watchdog
    db.init()
    problems = watchdog.run()
    print("\n".join(problems.values()) or "hammasi joyida")


def cmd_learn(args):
    """Qarorlardan naqsh qidirish va qoida taklif qilish."""
    import learn
    import rules
    db.init()
    items = learn.propose()
    if not items:
        print("Yangi naqsh topilmadi.")
    for item in items:
        print(f"#{item['rule']['id']} {rules.describe(item['rule'])}  ({item['why']})")


def main():
    ap = argparse.ArgumentParser(description="BAXT TRANSPORT — Telegram Cargo Finder")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init"); p.add_argument("--trucks"); p.set_defaults(func=cmd_init)
    p = sub.add_parser("demo"); p.add_argument("--trucks"); p.set_defaults(func=cmd_demo)
    p = sub.add_parser("backfill"); p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_backfill)
    p = sub.add_parser("listen"); p.set_defaults(func=cmd_listen)
    p = sub.add_parser("bot"); p.set_defaults(func=cmd_bot)
    p = sub.add_parser("llm"); p.add_argument("--text"); p.set_defaults(func=cmd_llm)
    p = sub.add_parser("web"); p.add_argument("--host", default=os.getenv("WEB_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.getenv("WEB_PORT", "8080")))
    p.set_defaults(func=cmd_web)
    p = sub.add_parser("expire"); p.add_argument("--hours", type=int, default=24)
    p.set_defaults(func=cmd_expire)
    p = sub.add_parser("calibrate"); p.add_argument("--trips", default="trips.json")
    p.set_defaults(func=cmd_calibrate)
    p = sub.add_parser("stats"); p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=cmd_stats)
    p = sub.add_parser("wialon"); p.set_defaults(func=cmd_wialon)
    p = sub.add_parser("gps"); p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=int, default=1800); p.set_defaults(func=cmd_gps)
    p = sub.add_parser("report"); p.add_argument("--truck"); p.add_argument("--top", type=int, default=5)
    p.add_argument("--hours", type=int, default=24); p.set_defaults(func=cmd_report)
    p = sub.add_parser("ai"); p.add_argument("text", nargs="*")
    p.add_argument("--chat"); p.set_defaults(func=cmd_ai)
    p = sub.add_parser("learn"); p.set_defaults(func=cmd_learn)
    p = sub.add_parser("watchdog"); p.set_defaults(func=cmd_watchdog)
    p = sub.add_parser("ai-eval"); p.add_argument("--models")
    p.add_argument("--limit", type=int); p.add_argument("--pause", type=float, default=8.0)
    p.add_argument("--out"); p.set_defaults(func=cmd_ai_eval)
    p = sub.add_parser("roundtrip"); p.add_argument("--cargo", type=int, required=True)
    p.add_argument("--truck", required=True); p.add_argument("--hours", type=int, default=48)
    p.set_defaults(func=cmd_roundtrip)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
