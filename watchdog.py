"""
watchdog.py — nazoratchi: tizimning biror qismi jimgina to'xtab qolmasin.

    python main.py watchdog            # cron har 10 daqiqada ishga tushiradi

Tekshiradi:
  • servislar (listener, bot, panel) ishlayaptimi;
  • panel javob beryaptimi (/health);
  • yangi yuklar kelyaptimi (kunduzi 2 soat jimlik — listener uzilgan bo'lishi mumkin);
  • disk to'lib qolmadimi;
  • serverdagi kod qo'lda o'zgartirilmadimi (git).

Faqat HOLAT O'ZGARGANDA yozadi: muammo paydo bo'lsa — ogohlantirish, hal
bo'lsa — "tuzaldi". Har 10 daqiqada bir xil xabar kelib turmaydi.
Bot to'xtagan bo'lsa ham xabar ketadi — `notifier.send` Bot API'ga to'g'ridan.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import urllib.request
from datetime import datetime

import config
import db
import notifier

log = logging.getLogger("watchdog")

STATE_KEY = "watchdog_state"
SERVICES = ("baxt-listener", "baxt-bot", "baxt-web")
QUIET_HOURS_LIMIT = 2            # kunduzi shuncha soat yangi yuk bo'lmasa — shubhali
DAY_HOURS = range(7, 23)         # Toshkent vaqti: guruhlar faol payt
DISK_LIMIT_PCT = 90


# ---------------------------------------------------------------- tekshiruvlar

def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return out.returncode, (out.stdout or "").strip()
    except Exception as ex:
        return 1, str(ex)


def check_services() -> dict[str, str]:
    problems = {}
    for name in SERVICES:
        code, out = _run(["systemctl", "is-active", name])
        if out != "active":
            problems[f"service:{name}"] = f"{name} не работает ({out or code})"
    return problems


def check_web() -> dict[str, str]:
    port = os.getenv("WEB_PORT", "8080")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as r:
            if r.status == 200:
                return {}
            return {"web": f"Панель отвечает: HTTP {r.status}"}
    except Exception as ex:
        return {"web": f"Панель не отвечает: {type(ex).__name__}"}


def check_fresh_cargo(now_local: datetime | None = None) -> dict[str, str]:
    """Kunduzi guruhlardan yuk kelib turishi kerak. Jimlik — listener uzilgan."""
    import briefing
    now_local = now_local or briefing.local_now()
    if now_local.hour not in DAY_HOURS:
        return {}
    with db.connect() as conn:
        row = conn.execute("SELECT MAX(created_at) AS last FROM cargos "
                           "WHERE source <> 'demo_group'").fetchone()
    if not row or not row["last"]:
        return {}                       # hali hech narsa kelmagan (yangi o'rnatish)
    last = datetime.fromisoformat(row["last"][:19])
    hours = (db.utc_now() - last).total_seconds() / 3600
    if hours >= QUIET_HOURS_LIMIT:
        return {"fresh": f"Новых грузов нет уже {hours:.0f} ч — возможно, listener "
                         f"отключился"}
    return {}


def check_disk(path: str | None = None) -> dict[str, str]:
    usage = shutil.disk_usage(path or str(config.BASE_DIR))
    pct = usage.used * 100 // usage.total
    if pct >= DISK_LIMIT_PCT:
        return {"disk": f"Диск заполнен на {pct}% (свободно {usage.free // 2**30} ГБ)"}
    return {}


def check_code() -> dict[str, str]:
    """Serverdagi kod GitHub'dagidan farq qilsa — kimdir qo'lda o'zgartirgan."""
    code, out = _run(["git", "-C", str(config.BASE_DIR), "status", "--porcelain",
                      "--untracked-files=no"])
    if code != 0 or not out:
        return {}
    files = ", ".join(line[3:] for line in out.splitlines()[:5])
    return {"code": f"Код на сервере изменён вручную: {files}"}


CHECKS = (check_services, check_web, check_fresh_cargo, check_disk, check_code)


# ---------------------------------------------------------------- holat

def _load_state() -> dict[str, str]:
    try:
        return json.loads(db.get_setting(STATE_KEY) or "{}")
    except ValueError:
        return {}


def run(checks=CHECKS, send=None) -> dict[str, str]:
    """Hamma tekshiruv. Faqat o'zgarish bo'lsa xabar yuboradi. Joriy muammolarni qaytaradi."""
    send = send or notifier.send
    problems: dict[str, str] = {}
    for check in checks:
        try:
            problems.update(check())
        except Exception as ex:
            log.exception("Tekshiruv xatosi: %s", check.__name__)
            problems[f"check:{check.__name__}"] = f"{check.__name__} не сработал: {ex}"

    before = _load_state()
    new = {k: v for k, v in problems.items() if k not in before}
    fixed = [k for k in before if k not in problems]
    if new:
        send("🚨 <b>Контроль: проблема</b>\n"
             + "\n".join(f"• {notifier.escape(v)}" for v in new.values()))
    if fixed:
        send("✅ <b>Контроль: исправлено</b>\n"
             + "\n".join(f"• {notifier.escape(before[k])}" for k in fixed))
    if new or fixed:
        db.set_setting(STATE_KEY, json.dumps(problems, ensure_ascii=False))
    log.info("Nazoratchi: %s", "; ".join(problems.values()) or "hammasi joyida")
    return problems
