"""
web.py — veb-panel (TZ 5.7, 5-bosqich).

    python main.py web                  # http://127.0.0.1:8080

Sahifalar:
    /           6 mashina: qayerda, qachon bo'shaydi, top-3 taklif
    /cargos     yuklar jadvali, filtrlar, asl matn
    /cargo/N    bitta yuk: to'liq hisob, barcha mosliklar, qaytish yuki
    /trucks     mashinalarni tahrirlash, holatni qo'lda yangilash
    /map        xarita: mashinalar va aktiv yuklar (Leaflet + OSM)
    /history    olingan reyslar, prognoz va haqiqiy marja
    /settings   xarajat parametrlari (qayta ishga tushirishsiz)
    /chat       AI yordamchi: oddiy gap bilan qidirish, reja, qoida
    /rules      kompaniya qoidalari, o'rganilgan takliflar, AI xotirasi

Tanlovlar:
  • FastAPI + HTMX (TZ tavsiyasi). Qo'shimcha kutubxona yo'q: forma va HTML
    stdlib bilan yig'iladi — `python-multipart`/`jinja2` shart emas.
  • Har bir tugma oddiy HTML forma: JavaScript yuklanmasa ham ishlaydi.
    HTMX bo'lsa sahifa qayta yuklanmaydi — telefonda, yomon internetda
    bu sezilarli.
  • "Olaman" mantiqi `actions.py` da — bot bilan bir xil. Bitta yukni
    Telegramdan ham, paneldan ham ikki marta olib bo'lmaydi.

Xavfsizlik (TZ: "internetga ochiq qo'yilmasin"):
  • `WEB_PASSWORD` majburiy; sessiya — HMAC bilan imzolangan cookie.
  • Sukut bo'yicha faqat 127.0.0.1 da tinglaydi (VPN/SSH tunnel orqali).
  • `WEB_ALLOWED_IPS` — ruxsat etilgan IP'lar ro'yxati (ixtiyoriy).
  • Login urinishlari cheklangan; boshqa saytdan POST qabul qilinmaydi.
  • Telegram ichida (Web App) parolsiz kirish — lekin faqat Telegram
    imzosi to'g'ri va foydalanuvchi dispetcher ro'yxatida bo'lsa
    (`telegram_user`). Panelni hech qachon ochiq qoldirmang: unda
    sozlamalar va "Olaman" tugmasi bor.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import date, datetime
from html import escape
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import actions
import analytics
import config
import db
import geo
import notifier
import pipeline
import scoring
import settings

log = logging.getLogger("web")

SESSION_COOKIE = "baxt_session"
SESSION_DAYS = 7
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW_SEC = 600
PUBLIC_PATHS = {"/login", "/health", "/enter", "/tg-auth"}
TG_AUTH_MAX_AGE = 24 * 3600

BODY = {"ref": "Реф", "tent": "Тент", "izoterm": "Изотерм", "bort": "Борт",
        "konteyner": "Контейнер", "tral": "Трал", "samosval": "Самосвал"}
STATUS = {"new": "новый", "taken": "взят", "skipped": "пропущен",
          "expired": "устарел"}
DECISION = {"taken": "взят ✓", "skipped": "пропущен",
            "cancelled": "— отменён", "undone": "— отменён", None: ""}
POS_SOURCE = {"gps": "GPS", "manual": "вручную", "trip": "после рейса"}
DIRECTIONS = ["", "UZ", "RU", "KZ", "KG", "TJ", "TM", "BY", "TR", "CN"]


# ---------------------------------------------------------------- yordamchi

def e(v) -> str:
    """HTML ga xavfsiz chiqarish — e'lon matnida har narsa bo'lishi mumkin."""
    return escape("" if v is None else str(v))


def money(v) -> str:
    return notifier.money(v)


def ru(city) -> str:
    """Shahar nomi ruscha va HTML uchun xavfsiz ("Toshkent" -> "Ташкент").

    Panel ruscha; bazada kanonik nom qoladi (2-qoida) — bu faqat ko'rinish.
    """
    return e(geo.ru(city))


def plural(n, one: str, few: str, many: str) -> str:
    """Ruscha son bilan kelishik: 1 фура, 2 фуры, 5 фур."""
    n = abs(int(n or 0))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _details(row) -> dict:
    try:
        return json.loads(row["details"]) if row["details"] else {}
    except (ValueError, TypeError, KeyError, IndexError):
        return {}


def _score_class(score) -> str:
    if score is None:
        return "s0"
    return "s3" if score >= 85 else "s2" if score >= 70 else "s1" if score >= 55 else "s0"


def _rate_text(row) -> str:
    if not row["rate"] or not row["currency"]:
        return '<span class="muted">нет ставки</span>'
    if row["currency"] == "UZS":
        text = f"{row['rate'] / 1_000_000:.1f} млн сум"
    else:
        text = f"{row['rate']:,.0f} {row['currency']}".replace(",", " ")
    if row["currency"] != "USD" and row["rate_usd"]:
        text += f' <span class="muted">≈{money(row["rate_usd"])}</span>'
    return text


def _cargo_line(row) -> str:
    parts = []
    if row["weight_t"]:
        parts.append(f"{row['weight_t']:g} т")
    if row["body_type"]:
        parts.append(BODY.get(row["body_type"], row["body_type"]))
    if row["temp_c"] is not None:
        parts.append(f"{row['temp_c']:+.0f}°")
    return " · ".join(e(p) for p in parts) or '<span class="muted">—</span>'


def ago_text(timestamp) -> str:
    """"12 мин" — e'lon qachon kelgani. Baza UTC da yozadi."""
    if not timestamp:
        return ""
    try:
        when = datetime.fromisoformat(str(timestamp).replace("T", " ")[:19])
    except ValueError:
        return str(timestamp)[5:16]
    seconds = (db.utc_now() - when).total_seconds()
    if seconds < 90:
        return "сейчас"
    if seconds < 3600:
        return f"{int(seconds // 60)} мин"
    if seconds < 86400:
        return f"{int(seconds // 3600)} ч"
    return f"{int(seconds // 86400)} дн."


def ago_phrase(timestamp) -> str:
    """"5 мин назад" / "только что"."""
    text = ago_text(timestamp)
    if not text:
        return ""
    return "только что" if text == "сейчас" else f"{text} назад"


def _status_pill(status: str) -> str:
    kind = {"new": "ok", "taken": "", "skipped": "warn", "expired": "bad"}.get(status, "")
    return f'<span class="pill {kind}">{e(STATUS.get(status, status))}</span>'


async def _form(request: Request) -> dict[str, str]:
    """Forma maydonlari. Takrorlangan kalitda oxirgisi olinadi
    (checkbox uchun: yashirin "0" + belgilangan "1")."""
    body = (await request.body()).decode("utf-8", errors="replace")
    return {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _after_skip(match_id: int, status: str, request: Request) -> HTMLResponse | None:
    """"O'tkazish" dan keyin — o'sha joyga darhol keyingi taklif.

    Bosh sahifada butun fura kartochkasi, fura sahifasida takliflar bloki
    yangilanadi (HTMX `HX-Retarget`). Boshqa sahifalarda (qidiruv, yuk,
    AI) — oddiy "O'tkazildi" yozuvi.
    """
    if status not in ("ok", "decided"):
        return None
    m = db.get_match(match_id)
    truck = db.get_truck(m["truck_id"]) if m is not None else None
    if truck is None:
        return None
    path = urlparse(request.headers.get("hx-current-url", "")).path
    tid = truck["id"]
    if path == "/":
        html = _truck_card(truck, _best_offers(tid, 2) if truck["active"] else [],
                           db.active_trips(tid))
        target = f"#truck-{tid}"
    elif path == f"/truck/{tid}":
        html, target = _truck_offers(tid), f"#offers-{tid}"
    else:
        return None
    return HTMLResponse(html, headers={"HX-Retarget": target, "HX-Reswap": "outerHTML"})


def _back(request: Request, default: str) -> str:
    """Tugma bosilgan sahifaga qaytish (faqat o'z saytimiz ichida)."""
    ref = request.headers.get("referer") or ""
    parsed = urlparse(ref)
    if ref and parsed.netloc == request.headers.get("host", "") and parsed.path:
        return parsed.path + (f"?{parsed.query}" if parsed.query else "")
    return default


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ---------------------------------------------------------------- sessiya

def _password() -> str:
    return os.getenv("WEB_PASSWORD", "")


def _secret() -> bytes:
    raw = os.getenv("WEB_SECRET") or f"{_password()}|{config.BOT_TOKEN}|baxt"
    return hashlib.sha256(raw.encode()).digest()


def make_token(now: float | None = None) -> str:
    expires = int((now or time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(_secret(), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def token_valid(token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    expires_s, sig = token.split(".", 1)
    try:
        expires = int(expires_s)
    except ValueError:
        return False
    if expires < (now or time.time()):
        return False
    good = hmac.new(_secret(), expires_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)


def magic_valid(token: str | None, now: float | None = None) -> bool:
    """Bot yuborgan bir martalik kirish havolasi (parolsiz).

    Sessiya tokenidan farqli — imzo `m` prefiksi bilan (aralashib ketmasin)
    va qisqa muddat. Bot ham xuddi shu sirni ishlatadi (`bot._web_secret`).
    """
    if not token or "." not in token:
        return False
    expires_s, sig = token.split(".", 1)
    try:
        expires = int(expires_s)
    except ValueError:
        return False
    if expires < (now or time.time()):
        return False
    good = hmac.new(_secret(), b"m" + expires_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)


def telegram_user(init_data: str | None, now: float | None = None) -> dict | None:
    """Telegram Web App `initData` imzosini tekshiradi.

    Telegram hujjati bo'yicha: kalit = HMAC("WebAppData", bot_token),
    imzo = HMAC(kalit, "k=v" larning alifbo tartibidagi qatori, `hash` siz).
    To'g'ri va yangi (24 soat) bo'lsa — foydalanuvchi ma'lumoti, aks holda None.
    Bot tokenisiz buni soxtalashtirib bo'lmaydi.
    """
    if not init_data or not config.BOT_TOKEN:
        return None
    from urllib.parse import parse_qsl
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    sig = data.pop("hash", "")
    if not sig:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    key = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    good = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return None
    try:
        auth_date = int(data.get("auth_date", "0"))
        user = json.loads(data.get("user") or "{}")
        user_id = int(user["id"])
    except (ValueError, KeyError, TypeError):
        return None
    if (now or time.time()) - auth_date > TG_AUTH_MAX_AGE:
        return None
    return {**user, "id": user_id}


def telegram_allowed(user_id: int) -> bool:
    """Panelga Telegram orqali kim kira oladi: botga /start yozgan dispetcherlar.

    Shaxsiy chatda chat id = foydalanuvchi id. Ro'yxat bo'sh bo'lsa —
    hech kim (botdagidan farqli: panelda sozlamalar bor).
    """
    extra = [x.strip() for x in os.getenv("WEB_TG_USERS", "").split(",") if x.strip()]
    allowed = set(extra) | set(db.dispatcher_chats())
    if config.DISPATCHER_CHAT_ID:
        allowed.add(str(config.DISPATCHER_CHAT_ID))
    return str(user_id) in allowed


def _set_session(resp: Response, cross_site: bool = False) -> Response:
    """Sessiya cookie. Telegram Web (brauzer) Web App'ni iframe ichida ochadi —
    u yerda cookie faqat `SameSite=None; Secure` bilan saqlanadi."""
    secure = cross_site or os.getenv("WEB_SECURE_COOKIE") == "1"
    resp.set_cookie(SESSION_COOKIE, make_token(), max_age=SESSION_DAYS * 86400,
                    httponly=True, samesite="none" if cross_site else "lax",
                    secure=secure)
    return resp


_login_fails: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _locked_out(ip: str) -> bool:
    now = time.time()
    fails = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW_SEC]
    _login_fails[ip] = fails
    return len(fails) >= LOGIN_MAX_FAILS


def _ip_allowed(ip: str) -> bool:
    allow = [x.strip() for x in os.getenv("WEB_ALLOWED_IPS", "").split(",") if x.strip()]
    return not allow or ip in allow


def _same_origin(request: Request) -> bool:
    """Boshqa saytdan yuborilgan POST'ni rad etamiz (CSRF)."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True
    return urlparse(origin).netloc == request.headers.get("host", "")


# ---------------------------------------------------------------- layout

CSS = """
/* ================= iOS uslubidagi dizayn (BAXT) ================= */
/* Yorug' rejim — iOS "grouped" fon (systemGroupedBackground) */
:root{
--brand:#007aff;--brand-soft:rgba(0,122,255,.12);--on-brand:#fff;
--bg:#f2f2f7;--surface:#fff;--surface-2:#f2f2f7;--surface-3:#e7e7ee;
--border:rgba(60,60,67,.13);--border-2:rgba(60,60,67,.24);
--text:#1c1c1e;--muted:#8a8a8e;
--ok:#34c759;--ok-bg:rgba(52,199,89,.15);--warn:#ff9500;--warn-bg:rgba(255,149,0,.16);
--bad:#ff3b30;--bad-bg:rgba(255,59,48,.14);--info-bg:rgba(0,122,255,.10);--track:#e5e5ea;
--nav-bg:rgba(255,255,255,.72);--nav-brd:rgba(60,60,67,.14);
--shadow:0 1px 2px rgba(15,23,42,.06),0 6px 20px rgba(15,23,42,.05);
--shadow-lg:0 12px 40px rgba(15,23,42,.14);
--r:20px;--r-sm:13px;--nav:250px;
color-scheme:light}
/* Qorong'i rejim — iOS true-black grouped (tizim sozlamasi bo'yicha) */
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--brand:#0a84ff;--brand-soft:rgba(10,132,255,.24);
--bg:#000;--surface:#1c1c1e;--surface-2:#2c2c2e;--surface-3:#3a3a3c;
--border:rgba(84,84,88,.4);--border-2:rgba(84,84,88,.6);
--text:#f5f5f7;--muted:#98989f;
--ok:#30d158;--ok-bg:rgba(48,209,88,.18);--warn:#ff9f0a;--warn-bg:rgba(255,159,10,.18);
--bad:#ff453a;--bad-bg:rgba(255,69,58,.18);--info-bg:rgba(10,132,255,.16);--track:#3a3a3c;
--nav-bg:rgba(28,28,30,.7);--nav-brd:rgba(84,84,88,.5);
--shadow:0 1px 2px rgba(0,0,0,.5);--shadow-lg:0 14px 44px rgba(0,0,0,.6);
color-scheme:dark}}
:root[data-theme="dark"]{
--brand:#0a84ff;--brand-soft:rgba(10,132,255,.24);
--bg:#000;--surface:#1c1c1e;--surface-2:#2c2c2e;--surface-3:#3a3a3c;
--border:rgba(84,84,88,.4);--border-2:rgba(84,84,88,.6);
--text:#f5f5f7;--muted:#98989f;
--ok:#30d158;--ok-bg:rgba(48,209,88,.18);--warn:#ff9f0a;--warn-bg:rgba(255,159,10,.18);
--bad:#ff453a;--bad-bg:rgba(255,69,58,.18);--info-bg:rgba(10,132,255,.16);--track:#3a3a3c;
--nav-bg:rgba(28,28,30,.7);--nav-brd:rgba(84,84,88,.5);
--shadow:0 1px 2px rgba(0,0,0,.5);--shadow-lg:0 14px 44px rgba(0,0,0,.6);
color-scheme:dark}

*{box-sizing:border-box}
html{height:100%;-webkit-text-size-adjust:100%}
body{margin:0;min-height:100%;background:var(--bg);color:var(--text);
font:16px/1.47 -apple-system,BlinkMacSystemFont,"SF Pro Text","SF Pro Display",
"Segoe UI",Roboto,system-ui,sans-serif;letter-spacing:-.01em;
-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
display:grid;grid-template-columns:var(--nav) minmax(0,1fr)}
a{color:var(--brand);text-decoration:none}
a:active{opacity:.55}
h1{font-size:30px;font-weight:700;letter-spacing:-.022em;margin:0 0 3px}
h2{font-size:14px;font-weight:600;letter-spacing:.02em;text-transform:uppercase;
color:var(--muted);margin:28px 4px 10px}
.sub{color:var(--muted);font-size:15px;margin-bottom:20px}
.num{font-variant-numeric:tabular-nums}

/* --- yon menyu (frosted, iOS sidebar) --- */
.side{background:var(--nav-bg);-webkit-backdrop-filter:blur(22px) saturate(180%);
backdrop-filter:blur(22px) saturate(180%);border-right:.5px solid var(--nav-brd);
padding:calc(18px + env(safe-area-inset-top,0px)) 12px 18px;display:flex;flex-direction:column;
gap:3px;position:sticky;top:0;height:100vh;z-index:20}
.side .brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:19px;
letter-spacing:-.02em;padding:6px 12px 16px}
.side a{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:12px;
color:var(--text);font-size:15px;font-weight:500;transition:background .15s,transform .1s}
.side a .ic{color:var(--muted);transition:color .15s}
.side a:active{transform:scale(.97)}
.side a.on{background:var(--brand-soft);color:var(--brand);font-weight:600}
.side a.on .ic{color:var(--brand)}
.side .foot{margin-top:auto;display:flex;gap:8px;padding-top:14px}
.side .foot .btn{flex:1;justify-content:center}
main{padding:26px 30px calc(46px + env(safe-area-inset-bottom,0px));max-width:1340px;
width:100%;min-width:0}
.tabbar{display:none}

/* --- kartochka --- */
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fill,minmax(400px,1fr))}
.card{background:var(--surface);border-radius:var(--r);padding:18px;box-shadow:var(--shadow);
border:.5px solid var(--border)}
.card>header{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:12px}
.muted{color:var(--muted);font-size:14px}
.truck{position:relative;overflow:hidden}
.truck:before{content:"";position:absolute;inset:0 auto 0 0;width:4px;
background:linear-gradient(var(--ok),#2aa84a)}
.truck.off:before{background:var(--border-2)}
.tnum{display:inline-flex;align-items:center;justify-content:center;min-width:36px;height:28px;
padding:0 9px;border-radius:9px;background:var(--surface-3);font-weight:700;font-size:14px;
color:var(--text);text-decoration:none;font-variant-numeric:tabular-nums}
a.tnum{background:var(--brand-soft);color:var(--brand)}
a.tnum:active{opacity:.6}

/* --- ko'rsatkich plitalari --- */
.stats{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));margin-bottom:24px}
.stat{background:var(--surface);border-radius:var(--r);padding:16px 18px;box-shadow:var(--shadow);
border:.5px solid var(--border)}
.stat .k{font-size:13px;font-weight:500;color:var(--muted);display:flex;align-items:center;gap:7px}
.stat .v{font-size:30px;font-weight:700;margin-top:8px;font-variant-numeric:tabular-nums;
line-height:1.05;letter-spacing:-.02em}
.stat small{color:var(--muted);font-size:13px}

/* --- taklif qatori --- */
.offer{display:flex;align-items:center;gap:13px;padding:13px 0;
border-top:.5px solid var(--border);flex-wrap:wrap}
.offer .act{margin-left:auto;display:flex;gap:8px;align-items:center}
.offer:first-of-type{margin-top:4px}
.offer.done{color:var(--muted);justify-content:center;font-size:14px;padding:16px 0}
.grow{flex:1 1 190px;min-width:190px}
.route{font-weight:600;letter-spacing:-.015em;display:block;font-size:16px}
.ring{width:48px;height:48px;border-radius:50%;flex:none;display:grid;place-items:center;
background:conic-gradient(var(--c) calc(var(--v)*3.6deg),var(--track) 0)}
.ring span{width:38px;height:38px;border-radius:50%;background:var(--surface);
display:grid;place-items:center;font-weight:700;font-size:14px;font-variant-numeric:tabular-nums}
.ring.sm{width:36px;height:36px}.ring.sm span{width:28px;height:28px;font-size:12px}

/* --- tugmalar (iOS pill) --- */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:none;
background:var(--surface-3);color:var(--text);border-radius:999px;padding:9px 17px;
font:600 15px/1.1 inherit;letter-spacing:-.01em;cursor:pointer;
transition:transform .1s,filter .15s,background .15s;white-space:nowrap;
-webkit-tap-highlight-color:transparent}
.btn:active{transform:scale(.96);filter:brightness(.96)}
.btn.primary{background:var(--brand);color:var(--on-brand)}
.btn.ok{background:var(--ok);color:#fff}
.btn.ghost{background:transparent;color:var(--brand);padding:8px 10px}
.btn.ghost:active{background:var(--surface-3)}
.btn.danger{background:var(--bad-bg);color:var(--bad)}
.btn.sm{padding:7px 13px;font-size:14px}
:focus-visible{outline:2.5px solid var(--brand);outline-offset:2px}
form.inline{display:inline-flex}

/* --- segmentli boshqaruv (iOS segmented control) --- */
.seg{display:inline-flex;background:var(--surface-3);border-radius:11px;padding:2px;gap:2px}
.seg a{padding:6px 15px;border-radius:9px;font-size:14px;font-weight:600;color:var(--text)}
.seg a:active{opacity:.6}
.seg a.on{background:var(--surface);color:var(--text);box-shadow:0 1px 4px rgba(0,0,0,.14)}
:root[data-theme="dark"] .seg a.on,
:root:not([data-theme="light"]) .seg a.on{background:#636366}

/* --- iOS switch --- */
.switch{position:relative;display:inline-flex;align-items:center;gap:10px;cursor:pointer;
font-size:15px;font-weight:500}
.switch input{position:absolute;opacity:0;width:0;height:0}
.switch .track{width:51px;height:31px;border-radius:999px;background:var(--track);
transition:background .2s;flex:none;position:relative}
.switch .track:after{content:"";position:absolute;top:2px;left:2px;width:27px;height:27px;
border-radius:50%;background:#fff;box-shadow:0 2px 5px rgba(0,0,0,.25);transition:transform .22s}
.switch input:checked + .track{background:var(--ok)}
.switch input:checked + .track:after{transform:translateX(20px)}

/* --- jadval (iOS inset grouped list) --- */
.table-wrap{background:var(--surface);border-radius:var(--r);box-shadow:var(--shadow);
border:.5px solid var(--border);overflow:auto;max-width:100%}
table{border-collapse:separate;border-spacing:0;width:100%;font-size:15px}
th{position:sticky;top:0;z-index:1;background:var(--surface);color:var(--muted);
font:600 12px/1.4 inherit;letter-spacing:.02em;text-transform:uppercase;
padding:13px 14px;text-align:left;border-bottom:.5px solid var(--border);white-space:nowrap}
td{padding:13px 14px;border-bottom:.5px solid var(--border);vertical-align:middle;white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
tbody tr:active td{background:var(--surface-2)}
@media(hover:hover){tbody tr:hover td{background:var(--surface-2)}}
td.wrap{white-space:normal;min-width:220px}
td.r{text-align:right;font-variant-numeric:tabular-nums}
.empty{padding:44px 18px;text-align:center;color:var(--muted);font-size:15px}
.empty .ico{display:block;margin:0 auto 12px;opacity:.5;color:var(--muted)}

/* --- forma --- */
input,select{font:inherit;font-size:16px;color:var(--text);background:var(--surface);
border:.5px solid var(--border-2);border-radius:var(--r-sm);padding:11px 13px;max-width:100%;
transition:border-color .15s,box-shadow .15s;-webkit-appearance:none;appearance:none}
select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8' fill='none' stroke='%238a8a8e' stroke-width='2' stroke-linecap='round'%3E%3Cpath d='M1 1.5 6 6.5 11 1.5'/%3E%3C/svg%3E");
background-repeat:no-repeat;background-position:right 13px center;padding-right:34px}
input:focus,select:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 4px var(--brand-soft)}
.filters{display:flex;flex-wrap:wrap;gap:12px;align-items:end;margin-bottom:18px;
background:var(--surface);border-radius:var(--r);padding:16px;box-shadow:var(--shadow);
border:.5px solid var(--border)}
.filters label,.field label{display:block;font-size:13px;color:var(--muted);margin-bottom:6px}
.field{margin-bottom:14px}
.row{display:flex;flex-wrap:wrap;gap:14px}
.row .field{flex:1;min-width:140px}
pre{white-space:pre-wrap;word-break:break-word;background:var(--surface-2);
border-radius:var(--r-sm);padding:14px;font-size:14px;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace;margin:0}

/* --- kichik elementlar --- */
.note{padding:14px 16px;border-radius:var(--r-sm);margin-bottom:18px;font-size:15px;
display:flex;gap:10px;align-items:flex-start;line-height:1.4}
.note .ic{flex:none;margin-top:1px}
.note.ok{background:var(--ok-bg);color:var(--text)}.note.ok .ic{color:var(--ok)}
.note.err{background:var(--bad-bg);color:var(--text)}.note.err .ic{color:var(--bad)}
.note.info{background:var(--info-bg);color:var(--text)}.note.info .ic{color:var(--brand)}
.pill{display:inline-block;font-size:12px;font-weight:600;padding:3px 10px;border-radius:999px;
background:var(--surface-3);color:var(--muted)}
.pill.ok{background:var(--ok-bg);color:var(--ok)}
.pill.warn{background:var(--warn-bg);color:var(--warn)}
.pill.bad{background:var(--bad-bg);color:var(--bad)}
.pill.info{background:var(--info-bg);color:var(--brand)}
/* animatsiyali emoji (rasm) */
.em{display:inline-flex;flex:none;vertical-align:middle;line-height:0}
.em img{display:block}
h1 .em{margin-left:6px;vertical-align:-4px}
.kpi{position:relative}
.kpi .em{position:absolute;right:10px;top:10px}
.li .badge.em-badge{background:none;width:34px;height:34px}
.road{position:relative;margin-top:22px}
.road .bar{margin-top:0}
.road .rider{position:absolute;top:-24px;transform:scaleX(-1)}
.empty-row .em{margin-right:6px}
/* boshqa fura tanlash */
.alts:empty{display:none}
.offer .alts{flex-basis:100%}
.alts{margin-top:10px;border-radius:14px;background:var(--surface-2);padding:4px 12px}
.alt{display:flex;align-items:center;gap:10px;padding:9px 0;border-top:.5px solid var(--border);
font-size:14.5px}
.alt:first-child{border-top:0}
.alt.off{color:var(--muted);font-size:13.5px}
.deal.taken{background:var(--ok-bg)}
/* qisqa xabar */
#toast{position:fixed;left:50%;top:calc(12px + env(safe-area-inset-top,0px));
transform:translateX(-50%);z-index:60;pointer-events:none;width:max-content;max-width:92vw}
.toast-msg{background:var(--surface);color:var(--text);border:.5px solid var(--border-2);
box-shadow:var(--shadow-lg);border-radius:16px;padding:10px 16px;font-weight:600;
animation:toastin .25s ease-out,toastout .5s ease-in 4.5s forwards}
@keyframes toastin{from{opacity:0;transform:translateY(-10px)}to{opacity:1;transform:none}}
@keyframes toastout{to{opacity:0;transform:translateY(-10px)}}
.money{font-weight:600;font-variant-numeric:tabular-nums}
.money.plus{color:var(--ok)}
#map{height:calc(100vh - 210px);min-height:440px;border-radius:var(--r);
border:.5px solid var(--border);box-shadow:var(--shadow)}
.legend{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:14px;margin-bottom:14px}
.legend i{display:inline-block;width:18px;height:3px;border-radius:2px;vertical-align:middle;margin-right:6px}
.chips{display:flex;gap:8px;flex-wrap:wrap}

/* --- ikonkalar va harakat --- */
.ic{fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;
stroke-linejoin:round;flex:none;vertical-align:-3px}
.side .brand .ic{stroke-width:2.2;color:var(--brand)}
.stat .k .ic{color:var(--brand)}
.card.truck{transition:box-shadow .2s,transform .12s}
.card.truck:active{transform:scale(.99)}
@media(hover:hover){.card.truck:hover{box-shadow:var(--shadow-lg)}
.stat{transition:box-shadow .2s,transform .15s}
.stat:hover{transform:translateY(-2px);box-shadow:var(--shadow-lg)}
.offer:hover .ring{transform:scale(1.06)}}
.ring{transition:transform .2s}
.offer{transition:background .15s}
/* jonli holat */
.live{display:inline-flex;align-items:center;gap:8px;font-size:13px;color:var(--muted);margin-bottom:6px}
.live b{position:relative;width:9px;height:9px;border-radius:50%;background:var(--ok)}
.live b:after{content:"";position:absolute;inset:-4px;border-radius:50%;
border:2px solid var(--ok);opacity:.7;animation:ping 2s ease-out infinite}
@keyframes ping{0%{transform:scale(.6);opacity:.7}70%{transform:scale(1.6);opacity:0}100%{opacity:0}}
.htmx-request{opacity:.5;pointer-events:none}
.htmx-request .ic.spin,.spin{animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.fade{animation:fade .3s cubic-bezier(.22,1,.36,1)}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.offer.done{animation:fade .3s cubic-bezier(.22,1,.36,1)}
/* --- AI suhbat (iMessage uslubi) --- */
.chat{display:flex;flex-direction:column;gap:10px;max-width:780px;padding-bottom:12px}
.bubble{max-width:86%;padding:10px 14px;border-radius:20px;white-space:pre-wrap;
line-height:1.45;font-size:15.5px;overflow-wrap:anywhere}
.bubble.me{align-self:flex-end;background:var(--brand);color:var(--on-brand);
border-bottom-right-radius:6px}
.bubble.ai{align-self:flex-start;background:var(--surface);border:.5px solid var(--border);
box-shadow:var(--shadow);border-bottom-left-radius:6px}
.bubble.ai code{background:var(--surface-3);padding:1px 5px;border-radius:6px}
.bubble .offers{margin:6px 0;white-space:normal}
.bubble.ai p{margin:0}
.bubble .offer{padding:9px 0}
.dock{position:sticky;bottom:0;max-width:780px;padding:8px 0 12px;
background:linear-gradient(transparent,var(--bg) 18px)}
.dock .chips{margin-bottom:8px}
.composer{display:flex;gap:8px;align-items:flex-end}
.composer textarea{flex:1;font:inherit;font-size:16px;color:var(--text);resize:none;
background:var(--surface);border:.5px solid var(--border-2);border-radius:22px;
padding:11px 16px;min-height:46px;max-height:160px}
.composer textarea:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 4px var(--brand-soft)}
.composer .btn{border-radius:50%;width:46px;height:46px;padding:0;flex:none}
.composer textarea::placeholder{color:var(--muted)}
.typing{display:none;align-self:flex-start;gap:5px;padding:14px 16px}
.htmx-request.typing,.htmx-request .typing{display:flex}
.typing i{width:8px;height:8px;border-radius:50%;background:var(--muted);
animation:blink 1.2s infinite ease-in-out}
.typing i:nth-child(2){animation-delay:.2s}.typing i:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.25}40%{opacity:1}}
.chip{display:inline-flex;align-items:center;gap:6px;border:.5px solid var(--border-2);
background:var(--surface);color:var(--text);border-radius:999px;padding:7px 13px;
font:inherit;font-size:14px;cursor:pointer}
.chip:active{transform:scale(.96)}
.rule{display:flex;gap:12px;align-items:flex-start;padding:13px 0;
border-bottom:.5px solid var(--border)}
.rule:last-child{border-bottom:none}
.rule .grow{flex:1}
@media (prefers-reduced-motion:reduce){
*,*:before,*:after{animation-duration:.001ms!important;transition-duration:.001ms!important}}

/* ================= v2: tushunarli tuzilma (iPhone birinchi) ================= */
.top{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin:2px 0 16px}
.top h1{font-size:34px;font-weight:800;letter-spacing:-.03em;margin:0;line-height:1.1}
.top .sub{margin:4px 0 0}
.top .right{flex:none;padding-bottom:4px}
.back{display:inline-flex;align-items:center;gap:1px;font-size:17px;font-weight:500;
margin:0 0 4px -6px;padding:6px}
.back .ic{stroke-width:2.4}
h2.sec{display:flex;align-items:baseline;justify-content:space-between;gap:4px 10px;
flex-wrap:wrap;margin:26px 4px 10px}
h2.sec small{font-size:12px;text-transform:none;letter-spacing:0;font-weight:500}
.kpis{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;max-width:760px}
.kpi{background:var(--surface);border-radius:16px;padding:12px 14px;border:.5px solid var(--border);
box-shadow:var(--shadow);color:var(--text);display:block}
.kpi:active{transform:scale(.97)}
.kpi b{display:block;font-size:28px;font-weight:700;letter-spacing:-.02em;line-height:1.15;
font-variant-numeric:tabular-nums}
.kpi span{font-size:12.5px;color:var(--muted);font-weight:500}
/* iOS "inset grouped" ro'yxat */
.list{background:var(--surface);border-radius:var(--r);border:.5px solid var(--border);
box-shadow:var(--shadow);overflow:hidden}
.list>*+*{border-top:.5px solid var(--border)}
.li{display:flex;align-items:center;gap:13px;padding:12px 16px;min-height:54px;color:var(--text);
background:none;border:0;width:100%;font:inherit;text-align:left;cursor:pointer;
-webkit-tap-highlight-color:transparent}
a.li:active,button.li:active{background:var(--surface-2)}
.li .badge{width:30px;height:30px;border-radius:8px;display:grid;place-items:center;color:#fff;flex:none}
.li .main{flex:1;min-width:0}
.li .t{font-weight:600;font-size:16px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
display:block;color:var(--text)}
.li .s{color:var(--muted);font-size:13.5px;line-height:1.35}
.li .clamp{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
margin-top:3px;font-size:13px}
.li .end{margin-left:auto;display:flex;align-items:center;gap:8px;color:var(--muted);
font-size:15px;flex:none}
.li .chev{color:var(--border-2)}
.li.red,.li.red .t{color:var(--bad)}
.kv .li{min-height:48px}
.kv .li .k{color:var(--muted);flex:none;min-width:110px;font-size:15px}
.kv .li .v{margin-left:auto;text-align:right;font-weight:500}
/* fura kartochkasi va taklif */
.tcard{padding:0;overflow:hidden}
.tnum.big{min-width:46px;height:36px;font-size:15px;border-radius:11px}
.deal{padding:14px 16px;border-top:.5px solid var(--border)}
.deal .row1{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.deal .route{font-size:17px;display:inline}
.deal .pd{font-size:21px;font-weight:700;color:var(--ok);white-space:nowrap;
font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.deal .pd small{font-size:13px;font-weight:600}
.deal .pd.none{color:var(--muted);font-size:15px}
.deal .meta{color:var(--muted);font-size:14px;margin-top:3px}
.deal .acts{display:flex;gap:8px;margin-top:11px}
.deal .acts form:first-child{flex:1}
.deal .acts form:first-child .btn{width:100%;padding:11px 16px;font-size:16px}
.deal .acts .btn{padding:11px 16px}
.list>.deal:first-child,.tcard>.deal:first-child{border-top:0}
.empty-row{padding:13px 16px;border-top:.5px solid var(--border);display:flex;align-items:center;
justify-content:space-between;gap:10px;color:var(--muted);font-size:14.5px;flex-wrap:wrap}
.offer.done{border-top:.5px solid var(--border);padding:16px}
/* narxi yozilmagan yuk: "shuncha so'rang" */
.deal .pd.ask{color:var(--warn);font-size:18px}
.deal .pd.ask small{font-weight:600}
/* yo'ldagi reys */
.trip{padding:14px 16px;border-top:.5px solid var(--border);background:var(--info-bg)}
.trip .row1{display:flex;align-items:center;justify-content:space-between;gap:10px}
.trip .route{font-size:17px}
.trip .meta{color:var(--muted);font-size:14px;margin-top:3px}
.trip .acts{display:flex;gap:8px;margin-top:11px;flex-wrap:wrap}
.bar{height:6px;border-radius:99px;background:var(--track);margin-top:10px;overflow:hidden}
.bar i{display:block;height:100%;border-radius:99px;background:var(--brand)}
.sub-h{padding:12px 16px 2px;font-size:12.5px;font-weight:600;color:var(--muted);
text-transform:uppercase;letter-spacing:.03em;border-top:.5px solid var(--border)}
.tstate{display:inline-flex;align-items:center;gap:5px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;background:var(--muted)}
.dot.s-free{background:var(--ok)}.dot.s-trip{background:var(--brand)}.dot.s-later{background:var(--warn)}
.truck.ontrip:before{background:linear-gradient(var(--brand),#5e5ce6)}
/* qidiruv maydoni */
.searchbar{display:flex;gap:8px;margin-bottom:12px}
.sfield{position:relative;flex:1;min-width:0}
.sfield .ic{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--muted)}
.sfield input{width:100%;padding-left:38px;background:var(--surface-3);border-color:transparent;
border-radius:12px}
.seg.full{display:flex;width:100%;max-width:520px;margin:0 0 16px}
.seg.full a{flex:1;text-align:center}
details.fdet{margin-bottom:14px}
details.fdet>summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:6px}
details.fdet>summary::-webkit-details-marker{display:none}
details.fdet[open]>summary{margin-bottom:10px}
details.card>summary{list-style:none;cursor:pointer;font-weight:600;display:flex;
align-items:center;justify-content:space-between}
details.card>summary::-webkit-details-marker{display:none}
details.card[open]>summary{margin-bottom:14px}
.side .grp{font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;
letter-spacing:.03em;padding:16px 12px 4px}
.foot-note{text-align:center;color:var(--muted);font-size:13px;margin:26px 0 6px}

/* ================= mobil: iOS pastki tab-panel ================= */
@media (max-width:900px){
body{grid-template-columns:minmax(0,1fr)}
.side{display:none}
.tabbar{display:flex;position:fixed;left:0;right:0;bottom:0;z-index:30;align-items:flex-end;
justify-content:space-around;padding:6px 4px calc(6px + env(safe-area-inset-bottom,0px));
background:var(--nav-bg);-webkit-backdrop-filter:blur(22px) saturate(180%);
backdrop-filter:blur(22px) saturate(180%);border-top:.5px solid var(--nav-brd)}
.tabbar a{flex:1 1 0;min-width:0;display:flex;flex-direction:column;align-items:center;gap:2px;
padding:3px 0;color:var(--muted);font-size:10.5px;font-weight:600;
-webkit-tap-highlight-color:transparent}
.tabbar a.on{color:var(--brand)}
.tabbar a:active{opacity:.6}
.tabbar .orb{width:56px;height:56px;margin-top:-30px;border-radius:50%;display:grid;
place-items:center;color:#fff;background:linear-gradient(140deg,#0a84ff,#5e5ce6);
border:4px solid var(--bg);box-shadow:0 8px 20px rgba(10,132,255,.38)}
.tabbar a.ai{color:var(--text)}
.tabbar a.ai.on{color:var(--brand)}
main{padding:calc(10px + env(safe-area-inset-top,0px)) 16px
     calc(100px + env(safe-area-inset-bottom,0px));max-width:100%}
.top h1{font-size:31px}
.grid{grid-template-columns:1fr;gap:14px}
.card{padding:16px}
.tcard{padding:0}
.kpi{padding:11px 12px}.kpi b{font-size:24px}
.stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.stat{padding:14px}.stat .v{font-size:26px}
.grow{flex-basis:150px;min-width:150px}
.dock{position:fixed;left:0;right:0;bottom:calc(70px + env(safe-area-inset-bottom,0px));
z-index:25;padding:8px 16px 10px;background:var(--nav-bg);-webkit-backdrop-filter:blur(22px);
backdrop-filter:blur(22px);border-top:.5px solid var(--nav-brd)}
.chat{padding-bottom:130px}
.chips.scroll{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;margin-right:-16px;
padding-right:16px}
.chips.scroll::-webkit-scrollbar{display:none}
.chips.scroll>*{flex:none}
.hide-m{display:none!important}
td,th{padding:11px 12px}
#map{height:calc(100vh - 290px);min-height:360px}
.filters{padding:14px}
.filters>div{flex:1 1 140px}
.filters input,.filters select{width:100%}
}
"""

# ---------------------------------------------------------------- ikonkalar
#
# Emoji o'rniga SVG: har qurilmada bir xil ko'rinadi, rangni matndan oladi
# (qorong'i rejimga o'zi moslashadi) va kattalashtirilganda buzilmaydi.
# Kutubxona ulanmaydi — bir marta sahifaga qo'yiladi, keyin `<use>` bilan.
_ICON_PATHS = {
    "dashboard": '<rect x="3" y="3" width="7" height="9" rx="1"/>'
                 '<rect x="14" y="3" width="7" height="5" rx="1"/>'
                 '<rect x="14" y="12" width="7" height="9" rx="1"/>'
                 '<rect x="3" y="16" width="7" height="5" rx="1"/>',
    "box": '<path d="M21 8v8a2 2 0 0 1-1 1.7l-7 4a2 2 0 0 1-2 0l-7-4A2 2 0 0 1 3 16V8a2 2 0 0 1 1-1.7l7-4a2 2 0 0 1 2 0l7 4A2 2 0 0 1 21 8Z"/>'
           '<path d="m3.3 7 8.7 5 8.7-5M12 22V12"/>',
    "truck": '<path d="M14 18V6a1 1 0 0 0-1-1H2a1 1 0 0 0-1 1v11a1 1 0 0 0 1 1h2"/>'
             '<path d="M14 9h4l4 4v4a1 1 0 0 1-1 1h-1"/>'
             '<circle cx="7" cy="18" r="2"/><circle cx="17" cy="18" r="2"/>',
    "map": '<path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3V6Z"/><path d="M9 3v15M15 6v15"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "settings": '<circle cx="12" cy="12" r="3"/>'
                '<path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 9 19.4a1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 4.6 9a1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1Z"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "bell": '<path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/>'
            '<path d="M13.7 21a2 2 0 0 1-3.4 0"/>',
    "check": '<path d="m20 6-11 11-5-5"/>',
    "skip": '<path d="m5 4 10 8-10 8V4ZM19 5v14"/>',
    "phone": '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.2a2 2 0 0 1 2.1-.5c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2Z"/>',
    "pin": '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/>',
    "chart": '<path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 5-6"/>',
    "money": '<circle cx="12" cy="12" r="9"/><path d="M15 9a3 3 0 0 0-3-1.5c-1.7 0-3 .9-3 2.2 0 3 6 1.6 6 4.6 0 1.3-1.3 2.2-3 2.2A3 3 0 0 1 9 15M12 6v1.5M12 16.5V18"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "trash": '<path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/>',
    "theme": '<circle cx="12" cy="12" r="9"/><path d="M12 3v18" fill="currentColor"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5M21 12H9"/>',
    "filter": '<path d="M3 4h18l-7 8v7l-4 2v-9L3 4Z"/>',
    "warning": '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>'
               '<path d="M12 9v4M12 17h.01"/>',
    "route": '<circle cx="6" cy="19" r="3"/><circle cx="18" cy="5" r="3"/>'
             '<path d="M9 19h6a4 4 0 0 0 0-8H9a4 4 0 0 1 0-8h3"/>',
    "fuel": '<path d="M3 22V4a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v18M2 22h12"/>'
            '<path d="M13 9h3a2 2 0 0 1 2 2v6a2 2 0 0 0 3 1.7"/><path d="M6 8h4"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
    "inbox": '<path d="M22 12h-6l-2 3h-4l-2-3H2"/>'
             '<path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.7 4H7.3a2 2 0 0 0-1.8 1.1Z"/>',
    "receipt": '<path d="M4 2v20l2-1 2 1 2-1 2 1 2-1 2 1 2-1V2l-2 1-2-1-2 1-2-1-2 1-2-1Z"/>'
               '<path d="M8 8h8M8 12h8M8 16h5"/>',
    "ban": '<circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/>',
    "spark": '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9Z"/>'
             '<path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8Z"/>',
    "rules": '<path d="M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6l-8-3Z"/>'
             '<path d="m9 12 2 2 4-4"/>',
    "send": '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4 20-7Z"/>',
    "brain": '<circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3'
             'M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/>',
    "home": '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1v-9.5Z"/>',
    "more": '<circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/>'
            '<circle cx="19" cy="12" r="1.6"/>',
    "chevron": '<path d="m9 5 7 7-7 7"/>',
    "back": '<path d="m15 5-7 7 7 7"/>',
    "satellite": '<path d="M13 7 9 3 5 7l4 4M17 11l4 4-4 4-4-4"/>'
                 '<path d="m8 12 4 4M16 8l-4-4"/><path d="M3 21a6 6 0 0 0 6-6"/>',
}

ICON_SPRITE = ('<svg xmlns="http://www.w3.org/2000/svg" style="display:none">'
               + "".join(f'<symbol id="i-{name}" viewBox="0 0 24 24">{path}</symbol>'
                         for name, path in _ICON_PATHS.items())
               + "</svg>")


EMOJI_DIR = config.BASE_DIR / "static" / "emoji"
_EMOJI_CACHE: dict[str, bytes] = {}


def anim(name: str, size: int = 24, cls: str = "") -> str:
    """Animatsiyali emoji — RASM (Noto, o'z serverimizdan), shrift belgisi emas.

    Shrift emoji har telefonda har xil chiqadi (17-qoida); rasm hamma joyda
    bir xil. "Harakatni kamaytirish" yoqilgan bo'lsa — qimirlamaydigan PNG.
    """
    return (f'<picture class="em {cls}"><source srcset="/static/emoji/{name}.png" '
            f'media="(prefers-reduced-motion: reduce)">'
            f'<img src="/static/emoji/{name}.webp" width="{size}" height="{size}" alt="" '
            f'decoding="async"></picture>')


def ic(name: str, size: int = 18, cls: str = "") -> str:
    """Ikonka. Rangi matn rangidan olinadi, o'lchami pikselda."""
    return (f'<svg class="ic {cls}" width="{size}" height="{size}" aria-hidden="true">'
            f'<use href="#i-{name}"/></svg>')


# Pastki menyu — 5 ta bo'lim (iOS tab-bar). Qolgan sahifalar shularning ichida:
# Yuklar = ro'yxat + qidiruv + xarita, Ko'proq = statistika, tarix, qoidalar...
TABS = [("/", "Сегодня", "home"), ("/cargos", "Грузы", "box"), ("/chat", "AI", "spark"),
        ("/trucks", "Парк", "truck"), ("/more", "Ещё", "more")]
MORE = [("/stats", "Статистика", "chart", "#0a84ff"),
        ("/rules", "Правила", "rules", "#af52de"), ("/watches", "Отслеживание", "bell", "#ff3b30"),
        ("/settings", "Настройки", "settings", "#8e8e93")]
_SECTION = {"/search": "/cargos", "/map": "/cargos", "/cargo": "/cargos", "/truck": "/trucks",
            "/stats": "/more", "/history": "/trucks", "/rules": "/more", "/watches": "/more",
            "/settings": "/more"}
# Eski nom — testlar va tashqi havolalar uchun
NAV = [(href, label, icon) for href, label, icon in TABS] + \
      [(href, label, icon) for href, label, icon, _ in MORE]


def _section(active: str) -> str:
    """Sahifa qaysi tab ichida (masalan /stats -> Ko'proq)."""
    if any(active == href for href, _, _ in TABS):
        return active
    for prefix, section in _SECTION.items():
        if active.startswith(prefix):
            return section
    return active


# Rejim tanlovi brauzerda saqlanadi; sahifa chizilishidan oldin qo'llanadi,
# aks holda yorug'dan qorong'iga "sakrash" ko'rinadi.
THEME_JS = """<script>
(function(){try{var t=localStorage.getItem('baxt-theme');
if(t)document.documentElement.dataset.theme=t}catch(e){}})();
function baxtTheme(){var r=document.documentElement;
var cur=r.dataset.theme||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');
var next=cur==='dark'?'light':'dark';r.dataset.theme=next;
try{localStorage.setItem('baxt-theme',next)}catch(e){}}
</script>"""

# Telegram ichida ochilganda: to'liq ekran va Telegram mavzusiga moslashish
TG_JS = """<script src="https://telegram.org/js/telegram-web-app.js" defer></script>
<script>addEventListener('DOMContentLoaded',function(){var w=window.Telegram&&Telegram.WebApp;
if(w&&w.initData){w.ready();w.expand();}});</script>"""

import urllib.parse as _up

FAVICON = "data:image/svg+xml," + _up.quote(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="%232563eb" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    + _ICON_PATHS["truck"] + '</svg>')


def page(title: str, body: str, active: str = "", head: str = "",
         status_code: int = 200, nav: bool = True) -> HTMLResponse:
    section = _section(active)
    side_links = "".join(
        f'<a href="{href}" class="{"on" if href == section else ""}">'
        f'{ic(icon)}<span>{"AI-помощник" if href == "/chat" else label}</span></a>'
        for href, label, icon in TABS if href != "/more")
    side_more = "".join(
        f'<a href="{href}" class="{"on" if href == active else ""}">{ic(icon)}<span>{label}</span></a>'
        for href, label, icon, _ in MORE)
    menu = (f'<aside class="side">'
            f'<div class="brand">{anim("truck", 28)}<span>BAXT</span></div>{side_links}'
            f'<div class="grp">Ещё</div>{side_more}'
            f'<div class="foot">'
            f'<button class="btn ghost" onclick="baxtTheme()" '
            f'title="Светлая/тёмная тема">{ic("theme")}</button>'
            f'<form method="post" action="/logout">'
            f'<button class="btn ghost" title="Выйти">{ic("logout")}</button></form>'
            f'</div></aside>') if nav else ""
    # Mobil: 5 ta tab, AI o'rtada katta tugma
    tabs = []
    for href, label, icon in TABS:
        on = " on" if href == section else ""
        if href == "/chat":
            tabs.append(f'<a href="{href}" class="ai{on}"><span class="orb">{anim("sparkles", 30)}</span>'
                        f'<span>{label}</span></a>')
        else:
            tabs.append(f'<a href="{href}" class="{on.strip()}">{ic(icon, 24)}'
                        f'<span>{label}</span></a>')
    tabbar = f'<nav class="tabbar">{"".join(tabs)}</nav>' if nav else ""
    layout = "" if nav else "<style>body{grid-template-columns:1fr}</style>"
    html = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#f2f2f7" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>{e(title)} — BAXT</title><link rel="icon" href="{FAVICON}">
<style>{CSS}</style>{layout}{THEME_JS}
<script src="https://unpkg.com/htmx.org@1.9.12" defer></script>{TG_JS}{head}</head>
<body>{ICON_SPRITE}{menu}<main>{body}</main>{tabbar}
<div id="toast" aria-live="polite"></div></body></html>"""
    return HTMLResponse(html, status_code=status_code)


def top(title: str, subtitle: str = "", back: tuple[str, str] | None = None,
        right: str = "") -> str:
    """Sahifa sarlavhasi (iOS "large title"). `back` — ichki sahifadan qaytish."""
    back_html = (f'<a class="back" href="{back[0]}">{ic("back", 20)}{e(back[1])}</a>'
                 if back else "")
    sub = f'<div class="sub">{subtitle}</div>' if subtitle else ""
    right_html = f'<div class="right">{right}</div>' if right else ""
    return f'{back_html}<div class="top"><div><h1>{title}</h1>{sub}</div>{right_html}</div>'


BACK_MORE = ("/more", "Ещё")


TG_LOGIN_JS = """<script>
addEventListener('DOMContentLoaded',function(){
  var w=window.Telegram&&Telegram.WebApp; if(!w||!w.initData)return;
  var box=document.getElementById('tg-login'); box.style.display='block';
  fetch('/tg-auth',{method:'POST',credentials:'include',
    headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:'init_data='+encodeURIComponent(w.initData)})
  .then(function(r){return r.json().then(function(j){return [r.ok,j]})})
  .then(function(x){
    if(x[0]){location.replace('/');return}
    box.textContent=x[1].error==='not_dispatcher'
      ?'Сначала нажмите /start в боте, затем откройте панель снова.'
      :'Не удалось войти через Telegram — войдите по паролю.';
  }).catch(function(){box.textContent='Ошибка сети — войдите по паролю.'});
});
</script>"""


def head(title: str, subtitle: str = "") -> str:
    return top(title, subtitle)


def _note(kind: str, text: str) -> str:
    name = {"ok": "check", "err": "warning", "info": "info"}.get(kind, "info")
    return f'<div class="note {kind}">{ic(name)} <span>{text}</span></div>'


def _empty(text: str, icon: str = "inbox", colspan: int | None = None) -> str:
    block = f'<div class="empty"><span class="ico">{ic(icon, 30)}</span>{text}</div>'
    if colspan:
        return f'<tr><td colspan="{colspan}" style="padding:0">{block}</td></tr>'
    return block


# ---------------------------------------------------------------- fragmentlar

def _ring(score, small: bool = False) -> str:
    """Ballni halqa ko'rinishida — raqamdan tezroq o'qiladi."""
    if score is None:
        return '<div class="ring sm" style="--v:0;--c:var(--track)"><span>—</span></div>'
    color = {"s3": "var(--ok)", "s2": "#65a30d", "s1": "var(--warn)",
             "s0": "var(--muted)"}[_score_class(score)]
    cls = "ring sm" if small else "ring"
    return (f'<div class="{cls}" style="--v:{score:.0f};--c:{color}" '
            f'title="{score:.0f}/100"><span>{score:.0f}</span></div>')


def _decision_buttons(match_id: int, alts: bool = True) -> str:
    """"Olaman · №04" (qaysi fura olishi tugmaning o'zida), "Boshqa fura", "O'tkazish"."""
    target = f'hx-target="#m{match_id}" hx-swap="outerHTML"'
    m = db.get_match(match_id)
    truck = f" · №{e(m['truck_id'])}" if m is not None else ""
    other = (f'<button type="button" class="btn sm" title="Выбрать другую фуру" '
             f'style="margin-left:6px" hx-get="/match/{match_id}/alt" '
             f'hx-target="#alt-{match_id}" hx-swap="innerHTML">{ic("truck", 15)}</button>'
             if alts else "")
    return (f'<form method="post" action="/match/{match_id}/take" class="inline" '
            f'hx-post="/match/{match_id}/take" {target}>'
            f'<button class="btn ok sm" title="Беру на эту фуру">{ic("check", 15)} '
            f'Беру{truck}</button></form>{other}'
            f'<form method="post" action="/match/{match_id}/skip" class="inline" '
            f'style="margin-left:6px" hx-post="/match/{match_id}/skip" {target}>'
            f'<button class="btn sm" title="Пропустить">{ic("skip", 15)}</button></form>')


def _alt_trucks(match_id: int) -> str:
    """"Boshqa fura" ro'yxati: har fura uchun hisob va o'z "Olaman" tugmasi.

    Mos kelmaydigan fura ham sababi bilan ko'rinadi — dispetcher nega
    yo'qligini bilsin ("ref kerak", "bo'sh probeg juda uzoq").
    """
    import ai_tools
    m = db.get_match(match_id)
    if m is None:
        return '<div class="muted">Предложение не найдено</div>'
    row = db.get_cargo(m["cargo_id"])
    if row is None or row["status"] != "new":
        return '<div class="muted">Груз уже взят</div>'
    cargo = {k: row[k] for k in row.keys()}
    skipped = ai_tools._skipped_pairs()
    fit, unfit = [], []
    for t in db.get_trucks():
        if t["id"] == m["truck_id"]:
            continue
        if (cargo["id"], str(t["id"])) in skipped:
            unfit.append((t["id"], "вы пропустили"))
            continue
        r = scoring.evaluate(cargo, t)
        if not r["ok"]:
            unfit.append((t["id"], (r["reasons"] or ["не подходит"])[0]))
            continue
        mid = ai_tools._ensure_match(cargo, t["id"], r)
        if mid:
            fit.append((r.get("margin_usd") or 0, t, r, mid))
    fit.sort(key=lambda x: x[0], reverse=True)
    rows = []
    for margin, t, r, mid in fit:
        rows.append(
            f'<div class="alt"><div class="grow"><b>№{e(t["id"])}</b> · {ru(t["current_city"] or "—")}'
            f'<div class="muted">пустой {r["empty_km"]} км · {r["trip_days"]} дн.'
            f'{" · маржа " + money(r["margin_usd"]) if r.get("margin_usd") is not None else ""}'
            f'</div></div><form method="post" action="/match/{mid}/take" class="inline" '
            f'hx-post="/match/{mid}/take" hx-target="#m{match_id}" hx-swap="outerHTML">'
            f'<button class="btn ok sm">{ic("check", 15)} №{e(t["id"])}</button></form></div>')
    for tid, why in unfit:
        rows.append(f'<div class="alt off"><b>№{e(tid)}</b> <span class="muted">— {e(why)}</span></div>')
    return "".join(rows) or '<div class="muted">Других фур нет</div>'


def _taken_card(res, driver_ok: bool) -> str:
    """Olgandan keyin — o'sha joyda aniq tasdiq: qaysi fura, qachon bo'shaydi, bekor qilish."""
    c, truck_id, mid = res.cargo, res.match["truck_id"], res.match["id"]
    driver = ("водителю отправлено в Telegram" if driver_ok else
              "водитель не подключён к боту — передайте рейс сами")
    return f"""<div class="deal taken" id="m{mid}">
  <div class="row1"><b>{anim("party", 22)} Груз взят — фура №{e(truck_id)}</b>
  <span class="pill info">В пути</span></div>
  <div class="meta">#{c['id']} {ru(c['from_city'])} → {ru(c['to_city'])} ·
  погрузка {e(_day_text(c.get('load_date')))} · освободится <b>{e(_day_text(res.free_date))}</b></div>
  <div class="meta">{e(driver)}</div>
  <div class="acts"><form method="post" action="/trip/{mid}/undo" class="inline"
    onsubmit="return confirm('Отменить рейс?')">
    <button class="btn sm">{ic("refresh", 15)} Отменить</button></form>
    <a class="btn sm" href="/truck/{e(truck_id)}">{ic("truck", 15)} Фура №{e(truck_id)}</a></div>
</div>"""


def _toast(text: str) -> str:
    """Tepada qisqa xabar (HTMX out-of-band) — javob qaysi joyga borsa ham ko'rinadi."""
    return f'<div id="toast" hx-swap-oob="true"><div class="toast-msg">{text}</div></div>'


_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря"]
_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _as_day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _day_text(value) -> str:
    """Sana odamcha: "сегодня", "завтра", "30 сентября"."""
    d = _as_day(value)
    if d is None:
        return "без даты"
    delta = (d - date.today()).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    if delta == -1:
        return "вчера"
    return f"{d.day} {_MONTHS[d.month - 1]}"


def _free_text(value) -> str:
    """Fura qachon bo'sh: o'tgan sana = hozir bo'sh."""
    d = _as_day(value)
    if d is None:
        return "дата освобождения не указана"
    if d <= date.today():
        return "свободна сейчас"
    return f"освободится {_day_text(d)}"


def _price_short(row) -> str | None:
    """Yuk narxi asl valyutada, qisqa: "$5 000", "55 млн сум", "400 000 ₽"."""
    rate, cur = row["rate"], row["currency"]
    if not rate or not cur:
        return None
    if cur == "USD":
        return money(rate)
    if cur == "UZS":
        return f"{rate / 1_000_000:g} млн сум"
    if cur == "RUB":
        return f"{rate:,.0f} ₽".replace(",", " ")
    return f"{rate:,.0f} {cur}".replace(",", " ")


def _price_big(row) -> str:
    """Taklifning o'ng tomonidagi katta yashil raqam — yuk narxi (buyurtmachi talabi).

    Narx yozilmagan bo'lsa — qancha so'rash kerakligi (to'q sariq)."""
    price = _price_short(row)
    if price:
        return f'<div class="pd">{e(price)}</div>'
    ask = _ask_price(_details(row)) if "details" in row.keys() else None
    if ask:
        return f'<div class="pd ask"><small>просить </small>от {money(ask)}</div>'
    return '<div class="pd none">нет цены</div>'


def _per_day(value) -> str:
    """Eski nom — endi marja (kunlik emas) ko'rsatiladi."""
    if value is None:
        return '<div class="pd none">нет цены</div>'
    return f'<div class="pd">{money(value)}</div>'


def _ask_price(d: dict) -> int | None:
    """Narxi yozilmagan yuk: bizga kamida shuncha kerak (xarajat + maqsadli marja)."""
    cost, days = d.get("total_cost"), d.get("trip_days")
    if cost is None or not days:
        return None
    target = cost + settings.current_costs().target_margin_per_day * float(days)
    return int(-(-target // 50) * 50)


def _deal(match_id, cargo_id, route_from, route_to, per_day, meta: str,
          actions: str, right: str | None = None) -> str:
    """Bitta taklif: yo'nalish, kuniga qancha, tafsilot va tugmalar.

    `id="m.."` — HTMX "Olaman/O'tkazish" javobi shu blokni almashtiradi.
    """
    return f"""<div class="deal" id="m{match_id}">
  <div class="row1"><a class="route" href="/cargo/{cargo_id}">{ru(route_from)} → {ru(route_to)}</a>
  {right if right is not None else _per_day(per_day)}</div>
  <div class="meta">{meta}</div>
  {f'<div class="acts">{actions}</div>' if actions else ''}
  <div class="alts" id="alt-{match_id}"></div>
</div>"""


def _offer(row) -> str:
    """Taklif: o'ngda yuk NARXI (yashil); pastda marja, bo'sh probeg, sana, kunlar."""
    d = _details(row)
    when = e(_day_text(row["load_date"]))
    empty = f"пустой {row['empty_km']:.0f} км"
    days = d.get("trip_days", "—")
    if row["margin_usd"] is None:
        meta = f"цена не указана · {empty} · {when} · {days} дн."
    else:
        meta = f"маржа {money(row['margin_usd'])} · {empty} · {when} · {days} дн."
    return _deal(row["id"], row["cargo_id"], row["from_city"], row["to_city"],
                 None, meta, _decision_buttons(row["id"]), right=_price_big(row))


def _rank_offers(rows) -> list:
    import search
    return search.rank_offers(rows)


def _best_offers(truck_id: str, n: int) -> list:
    import search
    return search.best_offers(truck_id, n)


def _ask_ai_button(question: str, label: str = "Спросить AI") -> str:
    return (f'<a class="btn sm" href="/chat?{urlencode({"q": question})}">'
            f'{ic("spark", 14)} {label}</a>')


_STATE_EMOJI = {"free": "truck", "trip": "truck", "later": "alarm", "off": "sleep"}


def _truck_state(t, trips) -> tuple[str, str]:
    """(holat, matn): free | trip | later | off — kartochkada rangli nuqta bilan."""
    if not t["active"]:
        return "off", "отключена"
    if trips:
        return "trip", f"В пути · освободится {_day_text(t['free_date'])}"
    d = _as_day(t["free_date"])
    if d is None or d <= date.today():
        return "free", "Свободна"
    return "later", f"Освободится {_day_text(d)}"


def _trip_free_date(trip) -> str:
    return actions.free_date_after(trip, _details(trip))


def _trip_block(trip, show_truck: bool = False) -> str:
    """Yo'ldagi reys: qayerdan-qayerga, qachon bo'shaydi, [Tugadi] [Bekor]."""
    free = _trip_free_date(trip)
    start = _as_day(trip["decided_at"] or trip["created_at"]) or date.today()
    end = _as_day(free) or date.today()
    total = max((end - start).days, 1)
    done = min(max((date.today() - start).days, 0), total)
    truck = f'<span class="tnum">№{e(trip["truck_id"])}</span> ' if show_truck else ""
    actual = (f" · факт {money(trip['actual_margin_usd'])}"
              if trip["actual_margin_usd"] is not None else "")
    finish = (f'<form method="post" action="/trip/{trip["id"]}/finish" class="inline">'
              f'<button class="btn sm ok">{ic("check", 15)} Рейс завершён</button></form>')
    undo = (f'<form method="post" action="/trip/{trip["id"]}/undo" class="inline" '
            f'onsubmit="return confirm(\'Отменить рейс? Груз снова станет свободным, '
            f'фура вернётся на прежнее место.\')">'
            f'<button class="btn sm">{ic("refresh", 15)} Отменить</button></form>')
    return f"""<div class="trip">
  <div class="row1"><span>{truck}<a class="route" href="/cargo/{trip['cargo_id']}">{ru(trip['from_city'])} → {ru(trip['to_city'])}</a></span>
  <span class="pill info">В пути</span></div>
  <div class="meta">груз #{trip['cargo_id']} · погрузка {e(_day_text(trip['load_date']))} ·
  освободится <b>{e(_day_text(free))}</b> · прогноз {money(trip['margin_usd'])}{actual}</div>
  <div class="road" title="{done}/{total} дн."><div class="bar"><i style="width:{done * 100 // total}%"></i></div>
  <span class="rider" style="left:max(0px, calc({done * 100 // total}% - 28px))">{anim("truck", 28)}</span></div>
  <div class="acts">{finish}{undo}</div>
</div>"""


def _truck_card(t, offers, trips=()) -> str:
    body = BODY.get(t["body_type"], t["body_type"] or "—")
    temp = ""
    if t["temp_min"] is not None and t["temp_max"] is not None:
        temp = f" {t['temp_min']:g}…{t['temp_max']:g}°"
    state, label = _truck_state(t, trips)
    where = t["current_city"] or "—"
    title = f"{ru(trips[-1]['from_city'])} → {ru(where)}" if trips else ru(where)
    snow = anim("snow", 16) if t["body_type"] == "ref" else ""
    header = f"""<a class="li" href="/truck/{e(t['id'])}"><span class="tnum big">№{e(t['id'])}</span>
  <div class="main"><span class="t">{title}</span>
  <div class="s"><span class="tstate"><span class="dot s-{state}"></span>{e(label)}</span>
  · {snow}{e(body)}{e(temp)} · {t['capacity_t'] or 0:g} т</div></div>
  <span class="end">{anim(_STATE_EMOJI[state], 32)}{ic("chevron", 16, "chev")}</span></a>"""
    parts = [header] + [_trip_block(tr) for tr in trips]
    if not t["active"]:
        return (f'<section class="card tcard truck off" id="truck-{e(t["id"])}">'
                f'{"".join(parts)}</section>')
    if trips:
        parts.append(f'<div class="sub-h">Следующий груз — из г. {ru(where)}</div>')
    if offers:
        parts += [_offer(o) for o in offers]
    else:
        ask = f"Найди груз для фуры {t['id']}"
        parts.append(f'<div class="empty-row"><span>{anim("eyes", 22)}Пока подходящих грузов нет</span>'
                     f'{_ask_ai_button(ask)}</div>')
    cls = "card tcard truck ontrip" if trips else "card tcard truck"
    return f'<section class="{cls}" id="truck-{e(t["id"])}">{"".join(parts)}</section>'


def _truck_row(t, trips=()) -> str:
    """Park ro'yxatidagi qator: holat birinchi o'rinda."""
    body = BODY.get(t["body_type"], t["body_type"] or "—")
    state, label = _truck_state(t, trips)
    where = t["current_city"] or "—"
    title = f"{ru(trips[-1]['from_city'])} → {ru(where)}" if trips else ru(where)
    driver = f" · {e(t['driver'])}" if t["driver"] else ""
    return f"""<a class="li" href="/truck/{e(t['id'])}"><span class="tnum big">№{e(t['id'])}</span>
  <div class="main"><span class="t">{title}</span>
  <div class="s"><span class="tstate"><span class="dot s-{state}"></span>{e(label)}</span>
  · {e(body)} {t['capacity_t'] or 0:g} т{driver}</div></div>
  <span class="end">{ic("chevron", 16, "chev")}</span></a>"""


def _park_tabs(active: str) -> str:
    items = [("/trucks", "Фуры"), ("/history", "Рейсы")]
    return '<div class="seg full">' + "".join(
        f'<a class="{"on" if href == active else ""}" href="{href}">{label}</a>'
        for href, label in items) + "</div>"


def _cargo_tabs(active: str) -> str:
    """"Грузы" ichidagi iOS segment: Список | Поиск | Карта."""
    items = [("/cargos", "Список"), ("/search", "Поиск"), ("/map", "Карта")]
    return '<div class="seg full">' + "".join(
        f'<a class="{"on" if href == active else ""}" href="{href}">{label}</a>'
        for href, label in items) + "</div>"


# ---------------------------------------------------------------- ilova

def create_app() -> FastAPI:
    app = FastAPI(title="BAXT TRANSPORT", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        ip = _client_ip(request)
        path = request.url.path
        if path != "/health" and not _ip_allowed(ip):
            return Response("Доступ запрещён", status_code=403)
        if request.method == "POST" and not _same_origin(request):
            return Response("Запрос с другого сайта отклонён", status_code=403)
        if path not in PUBLIC_PATHS and not path.startswith("/static/emoji/") \
                and not token_valid(request.cookies.get(SESSION_COOKIE)):
            if _is_htmx(request):
                return Response(status_code=401, headers={"HX-Redirect": "/login"})
            return _redirect("/login")
        return await call_next(request)

    # ---------------------------------------------------------- kirish

    @app.get("/static/emoji/{fname}")
    async def emoji_file(fname: str):
        """Animatsiyali emoji rasmlari — login sahifasida ham kerak, shuning uchun ochiq."""
        import re as _re
        if not _re.fullmatch(r"[a-z]+\.(webp|png)", fname):
            return Response(status_code=404)
        data = _EMOJI_CACHE.get(fname)
        if data is None:
            path = EMOJI_DIR / fname
            if not path.is_file():
                return Response(status_code=404)
            data = _EMOJI_CACHE[fname] = path.read_bytes()
        kind = "image/webp" if fname.endswith(".webp") else "image/png"
        return Response(data, media_type=kind,
                        headers={"Cache-Control": "public, max-age=2592000"})

    @app.get("/health")
    async def health():
        try:
            c = db.counters()
            return JSONResponse({"ok": True, "active_cargos": c["active"]})
        except Exception as ex:
            return JSONResponse({"ok": False, "error": str(ex)}, status_code=500)

    @app.get("/login")
    async def login_form(error: str = ""):
        msg = _note("err", e(error)) if error else ""
        if not _password():
            msg = _note("err", "WEB_PASSWORD не задан — укажите его в .env")
        body = f"""<div style="min-height:100vh;display:grid;place-items:center;padding:20px">
<div class="card" style="max-width:380px;width:100%;box-shadow:var(--shadow-lg)">
  <div style="text-align:center;margin-bottom:20px">
    <div>{anim("truck", 64)}</div>
    <h1 style="margin-top:8px">BAXT TRANSPORT</h1>
    <div class="sub" style="margin:0">Панель диспетчера</div>
  </div>{msg}
  <div id="tg-login" class="muted" style="text-align:center;margin-bottom:14px;display:none">
    Вход через Telegram…</div>
  <form method="post" action="/login">
    <div class="field"><label>Пароль</label>
    <input type="password" name="password" autofocus required style="width:100%"></div>
    <button class="btn primary" style="width:100%;justify-content:center">Войти</button>
  </form>
</div></div>"""
        return page("Вход", body + TG_LOGIN_JS, nav=False)

    @app.post("/tg-auth")
    async def tg_auth(request: Request):
        """Telegram Web App ichidan parolsiz kirish (imzo tekshiriladi)."""
        ip = _client_ip(request)
        if _locked_out(ip):
            return JSONResponse({"ok": False, "error": "locked"}, status_code=429)
        form = await _form(request)
        user = telegram_user(form.get("init_data"))
        if user is None:
            _login_fails.setdefault(ip, []).append(time.time())
            return JSONResponse({"ok": False, "error": "bad_signature"}, status_code=403)
        if not telegram_allowed(user["id"]):
            log.warning("Panelga ruxsatsiz Telegram foydalanuvchi: %s", user["id"])
            return JSONResponse({"ok": False, "error": "not_dispatcher"}, status_code=403)
        return _set_session(JSONResponse({"ok": True}), cross_site=True)

    @app.post("/login")
    async def login(request: Request):
        ip = _client_ip(request)
        if _locked_out(ip):
            return page("Вход", _note("err", "Слишком много неудачных попыток. "
                                               "Повторите через 10 минут."),
                        nav=False, status_code=429)
        form = await _form(request)
        password = _password()
        if password and hmac.compare_digest(form.get("password", ""), password):
            _login_fails.pop(ip, None)
            return _set_session(_redirect("/"))
        _login_fails.setdefault(ip, []).append(time.time())
        log.warning("Panelga noto'g'ri parol: %s", ip)
        return _redirect("/login?" + urlencode({"error": "Неверный пароль"}))

    @app.post("/logout")
    async def logout():
        resp = _redirect("/login")
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    @app.get("/enter")
    async def enter(t: str = ""):
        """Bot yuborgan parolsiz kirish havolasi.

        Havola qisqa muddatli (15 daqiqa). To'g'ri bo'lsa — 7 kunlik sessiya
        cookie beriladi, dispetcher panelga to'g'ridan-to'g'ri kiradi.
        """
        if not magic_valid(t):
            return _redirect("/login?" + urlencode({"error": "Ссылка устарела. "
                                                    "Откройте панель через бота ещё раз."}))
        return _set_session(_redirect("/"))

    # ---------------------------------------------------------- bosh sahifa

    @app.get("/fragment/fleet")
    async def fleet_fragment():
        """Faqat mashina kartochkalari — sahifa har 45 soniyada shuni yangilaydi.

        Dispetcher sahifani qayta yuklab o'tirmasin: yangi e'lonlar o'zi
        paydo bo'ladi.
        """
        return HTMLResponse(_fleet_cards())

    @app.get("/")
    async def dashboard():
        today = date.today()
        c = db.counters()
        kpis = [(c["active"], "активных грузов", "/cargos", "box"),
                (c["today"], "пришло сегодня", "/cargos?status=", "fire"),
                (c["taken_week"], "взято · 7 дн.", "/history", "check")]
        kpi_html = '<div class="kpis">' + "".join(
            f'<a class="kpi" href="{href}">{anim(em, 26)}<b>{value}</b><span>{label}</span></a>'
            for value, label, href, em in kpis) + "</div>"
        import briefing
        hour = briefing.local_now().hour
        greet = anim("sun" if 6 <= hour < 18 else "star", 34)
        subtitle = f"{_WEEKDAYS[today.weekday()].capitalize()}, {today.day} {_MONTHS[today.month - 1]}"
        return page("Сегодня",
                    top(f"Сегодня{greet}", subtitle, right=f'<a class="btn sm" href="/chat?'
                        f'{urlencode({"q": "Спланируй груз и обратный груз для каждой фуры"})}">'
                        f'{ic("spark", 14)} План</a>')
                    + kpi_html
                    + '<h2 class="sec">Фуры <small>самые выгодные сверху</small></h2>'
                    + f"""<div class="grid fade" id="fleet"
     hx-get="/fragment/fleet" hx-trigger="every 45s"
     hx-swap="innerHTML">{_fleet_cards()}</div>""", active="/")

    # ---------------------------------------------------------- qarorlar

    @app.post("/match/{match_id}/take")
    async def take(match_id: int, request: Request):
        res = actions.take_match(match_id)
        if not res.ok:
            text = {"not_found": "Предложение не найдено",
                    "cargo_missing": "Груз не найден",
                    "stale": "Фура уже взяла другой рейс — предложение устарело",
                    "already_taken": f"Уже взят — фура №{e(res.holder or '—')}"
                    }.get(res.reason, "Ошибка")
            if _is_htmx(request):
                return HTMLResponse(f'<div class="offer done" id="m{match_id}">{text}</div>')
            cargo_id = res.cargo["id"] if res.cargo else None
            return _redirect(f"/cargo/{cargo_id}" if cargo_id else "/")

        # Telegram chati ham bilsin — dispetcherlardan biri telefonda bo'lishi mumkin
        truck_id = res.match["truck_id"]
        driver_ok = False
        try:
            driver_ok = notifier.notify_driver(
                truck_id, notifier.format_driver_trip(
                    res.cargo, dict(db.get_truck(truck_id) or {"id": truck_id}), res.free_date))
            notifier.send(notifier.format_taken(res.cargo, res.truck or {}, res.free_date)
                          + "\n<i>(панель)</i>"
                          + ("\n📨 Водителю отправлено." if driver_ok else ""))
        except Exception:
            log.exception("Telegramga tasdiq yuborilmadi")

        url = f"/cargo/{res.cargo['id']}?taken=1&driver={1 if driver_ok else 0}"
        if not _is_htmx(request):
            return _redirect(url)
        toast = _toast(f'{anim("party", 20)} Фура №{e(truck_id)} взяла: '
                       f'{ru(res.cargo["from_city"])} → {ru(res.cargo["to_city"])}')
        path = urlparse(request.headers.get("hx-current-url", "")).path
        if path == "/":
            # Bosh sahifa: butun park yangilanadi — fura "Yo'lda" bo'lib ko'rinadi
            return HTMLResponse(_fleet_cards() + toast,
                                headers={"HX-Retarget": "#fleet", "HX-Reswap": "innerHTML"})
        if path.startswith("/cargo/"):
            return HTMLResponse("", headers={"HX-Redirect": url})
        if path.startswith("/truck/"):
            return HTMLResponse("", headers={"HX-Refresh": "true"})
        # AI chat, qidiruv, yuklar — o'sha joyda aniq tasdiq
        return HTMLResponse(_taken_card(res, driver_ok) + toast)

    @app.get("/match/{match_id}/alt")
    async def match_alternatives(match_id: int):
        from starlette.concurrency import run_in_threadpool
        return HTMLResponse(await run_in_threadpool(_alt_trucks, match_id))

    @app.post("/match/{match_id}/skip")
    async def skip(match_id: int, request: Request):
        status = actions.skip_match(match_id)
        text = {"ok": "Пропущено", "decided": "Решение уже принято",
                "not_found": "Предложение не найдено"}[status]
        if _is_htmx(request):
            refreshed = _after_skip(match_id, status, request)
            if refreshed is not None:
                return refreshed
            return HTMLResponse(f'<div class="offer done" id="m{match_id}">{text}</div>')
        return _redirect(request.headers.get("referer") or "/")

    @app.post("/match/{match_id}/actual")
    async def actual(match_id: int, request: Request):
        form = await _form(request)
        try:
            value = float(form.get("margin", "").replace(",", ".").replace(" ", ""))
        except ValueError:
            return _redirect("/history?" + urlencode({"error": "Маржа должна быть числом"}))
        m = db.get_match(match_id)
        if m is None:
            return _redirect("/history?" + urlencode({"error": "Предложение не найдено"}))
        if m["decision"] == "taken":
            actions.finish_trip(match_id, value)     # haqiqiy marja = reys tugadi
        else:
            db.set_actual_margin(match_id, value)
        return _redirect("/history?saved=1")

    # ---------------------------------------------------------- qidiruv

    @app.get("/search")
    async def search_page(request: Request):
        """"Toshkent Moskva yuk bormi?" — park bo'yicha javob.

        Botdagi qidiruv bilan bir xil mantiq (`search.py`), faqat
        ko'rinishi boshqacha.
        """
        import search as search_mod
        text = (request.query_params.get("q") or "").strip()
        notes = ""
        if request.query_params.get("watched"):
            notes = _note("ok", "Добавлено в отслеживание. Как только появится такой "
                                "груз — сразу придёт сообщение в Telegram.")

        chips = "".join(
            f'<a class="chip" href="/search?{urlencode({"q": ex})}">{e(ex)}</a>'
            for ex in ("Ташкент Москва", "Москва", "Ташкент Алматы реф",
                       "Самарканд Казань"))
        form = f"""<form class="searchbar" method="get" action="/search">
  <div class="sfield">{ic("search", 17)}<input name="q" value="{e(text)}" list="cities"
    placeholder="Ташкент Москва реф" aria-label="Откуда куда"></div>
  <button class="btn primary">Найти</button>
</form>{_city_datalist()}
<div class="chips scroll" style="margin:0 0 18px">{chips}</div>"""

        body = (top("Грузы", "Поиск по нашему парку: какая фура возьмёт и сколько заработает")
                + _cargo_tabs("/search"))
        if not text:
            return page("Поиск", body + notes + form + _watch_block(), active="/search")

        query = search_mod.parse_query(text)
        if query.is_empty:
            return page("Поиск", body + notes + form
                        + _note("err", f"«{e(text)}» — непонятно. Напишите "
                                       f"города, например: <b>Ташкент Москва</b>")
                        + _watch_block(), active="/search")

        found = search_mod.find(query, top=20)
        return page("Поиск", body + notes + form
                    + _search_results(found, text) + _watch_block(),
                    active="/search")

    @app.post("/watch")
    async def watch_add(request: Request):
        import search as search_mod
        form = await _form(request)
        text = (form.get("q") or "").strip()
        query = search_mod.parse_query(text)
        if query.is_empty:
            return _redirect("/search")
        for w in db.active_watches():
            if (w["from_city"], w["to_city"], w["body_type"]) == \
                    (query.from_city, query.to_city, query.body_type):
                return _redirect("/search?" + urlencode({"q": text}))
        db.add_watch(query.from_city, query.to_city, query.body_type,
                     query=text, chat_id=config.DISPATCHER_CHAT_ID or None)
        return _redirect("/search?" + urlencode({"q": text, "watched": "1"}))

    @app.post("/watch/{watch_id}/delete")
    async def watch_delete(watch_id: int):
        db.delete_watch(watch_id)
        return _redirect("/search")

    # ---------------------------------------------------------- yuklar

    @app.get("/cargos")
    async def cargos(request: Request):
        q = request.query_params
        notes = []

        def city(param: str) -> str | None:
            raw = (q.get(param) or "").strip()
            if not raw:
                return None
            found = geo.lookup(raw)
            if not found:
                notes.append(_note("err", f"Город «{e(raw)}» не найден"))
                return raw           # kanonik emas — hech narsa topilmaydi
            return found

        from_city, to_city = city("from"), city("to")
        body_type = q.get("body") or None
        status = q.get("status", "new") or None
        try:
            min_score = float(q["min_score"]) if q.get("min_score") else None
        except ValueError:
            min_score = None
        search = (q.get("q") or "").strip() or None

        rows = db.search_cargos(from_city=from_city, to_city=to_city,
                                body_type=body_type, status=status,
                                min_score=min_score, q=search, limit=300)

        def opt(items, current):
            return "".join(f'<option value="{k}" {"selected" if k == current else ""}>'
                           f'{v}</option>' for k, v in items)

        active_filters = sum(1 for k in ("from", "to", "body", "min_score", "q") if q.get(k))
        filters = f"""<details class="fdet"{' open' if active_filters else ''}>
<summary class="btn sm">{ic("filter", 15)} Фильтр{f' · {active_filters}' if active_filters else ''}</summary>
<form class="filters" method="get">
  <div><label>Откуда</label><input name="from" value="{e(q.get('from', ''))}" list="cities" size="12"></div>
  <div><label>Куда</label><input name="to" value="{e(q.get('to', ''))}" list="cities" size="12"></div>
  <div><label>Кузов</label><select name="body">{opt([('', 'все')] + list(BODY.items()), body_type or '')}</select></div>
  <div><label>Статус</label><select name="status">{opt([('', 'все')] + list(STATUS.items()), status or '')}</select></div>
  <div><label>Балл ≥</label><input name="min_score" type="number" min="0" max="100" value="{e(q.get('min_score', ''))}" style="width:80px"></div>
  <div><label>Поиск по тексту</label><input name="q" value="{e(search or '')}" size="14"></div>
  <button class="btn primary">Показать</button> <a class="btn" href="/cargos">Сбросить</a>
</form></details>{_city_datalist()}"""

        items = []
        for r in rows:
            items.append(f"""<div class="li">{_ring(r['best_score'], small=True)}
  <div class="main"><a class="t route" href="/cargo/{r['id']}">{ru(r['from_city'])} → {ru(r['to_city'])}</a>
    <div class="s">{_cargo_line(r)} · {_rate_text(r)} · {e(_day_text(r['load_date']))}</div>
    <div class="s">{e(r['source'] or '')} · {e(ago_phrase(r['created_at']))}</div>
    <div class="s clamp">{e(r['raw_text'])}</div></div>
  <span class="end">{_status_pill(r['status'])}</span></div>""")
        listing = (f'<div class="list">{"".join(items)}</div>' if items else
                   _empty("Таких грузов нет. Расширьте фильтр "
                          "или выберите статус «все».", "search"))
        return page("Грузы",
                    top("Грузы", f"{len(rows)} {plural(len(rows), 'груз', 'груза', 'грузов')}"
                                 f" · собираются из групп автоматически")
                    + _cargo_tabs("/cargos")
                    + "".join(notes) + filters + listing, active="/cargos")

    @app.get("/cargo/{cargo_id}")
    async def cargo_detail(cargo_id: int, taken: str = "", driver: str = ""):
        c = db.get_cargo(cargo_id)
        if c is None:
            return page("Не найдено", _note("err", f"Груз #{cargo_id} не найден"),
                        status_code=404)
        matches = db.matches_for_cargo(cargo_id)
        notes = ""
        if taken:
            holder = db.taken_truck_for_cargo(cargo_id)
            said = (f"Назначен на фуру №{e(holder)}. " if holder else "") + \
                ("Водителю отправлено в Telegram." if driver == "1" else
                    "Водитель не подключён к боту — передайте рейс сами (Парк → фура → "
                    "в строке Telegram написано, как подключить).")
            notes = _note("ok", f'{anim("party", 24)} Рейс назначен. {said}')

        via = ""
        try:
            via_list = json.loads(c["via"]) if c["via"] else []
            if via_list:
                via = f'<div class="muted">через: {e(", ".join(geo.ru(v) for v in via_list))}</div>'
        except ValueError:
            pass

        contact = " · ".join(filter(None, [
            f'<a href="tel:{e(c["phone"])}">{e(c["phone"])}</a>' if c["phone"] else "",
            f'<a href="https://t.me/{e(c["username"])}">@{e(c["username"])}</a>'
            if c["username"] else ""])) or '<span class="muted">нет</span>'

        info = f"""<a class="back" href="/cargos">{ic("back", 20)}Грузы</a><div class="card">
  <header>
    <div><h1 style="margin:0">{ru(c['from_city'])} → {ru(c['to_city'])}</h1>
      <div class="sub" style="margin:2px 0 0">Груз #{c['id']} ·
        {e(ago_phrase(c['created_at']))} · {e(c['source'] or '?')}</div>{via}</div>
    {_status_pill(c['status'])}
  </header>
  <div class="row" style="margin-top:10px">
    <div class="field"><label>Груз</label>{_cargo_line(c)}</div>
    <div class="field"><label>Ставка</label>{_rate_text(c)}</div>
    <div class="field"><label>Погрузка</label>{e(c['load_date'] or '—')}</div>
    <div class="field"><label>Контакт</label>{contact}</div>
    <div class="field"><label>Уверенность разбора</label>
      <span class="num">{c['confidence'] or 0:.2f}</span></div>
  </div>
  <h2>Исходное объявление</h2><pre>{e(c['raw_text'])}</pre>
</div>"""

        deals = []
        for m in matches:
            d = _details(m)
            action = _decision_buttons(m["id"], alts=False) if c["status"] == "new" and not m["decision"] \
                else f'<span class="pill">{e(DECISION.get(m["decision"], m["decision"] or ""))}</span>'
            meta = (f'<span class="tnum">№{e(m["truck_id"])}</span> · пустой {m["empty_km"]:.0f} + '
                    f'гружёный {m["loaded_km"]:.0f} км · {d.get("trip_days", "—")} дн.<br>'
                    f'расходы {money(d.get("total_cost"))} · маржа '
                    f'<span class="money">{money(m["margin_usd"])}</span> · балл {m["score"]:.0f}')
            deals.append(f"""<div class="deal" id="m{m['id']}">
  <div class="row1"><b>Фура №{e(m['truck_id'])}</b>{_per_day(m['margin_usd'])}</div>
  <div class="meta">{meta}</div><div class="acts">{action}</div></div>""")
        match_table = (f'<h2 class="sec">Расчёт по фурам '
                       f'<small>маржа — прибыль</small></h2>'
                       + (f'<div class="list">{"".join(deals)}</div>' if deals else
                          _empty("Ни одна фура не может взять этот груз", "ban")))

        return page(f"Груз #{cargo_id}",
                    notes + info + match_table + _return_block(c, matches),
                    active="/cargos")

    # ---------------------------------------------------------- mashinalar

    @app.get("/trucks")
    async def trucks_page():
        trucks = db.get_trucks(active_only=False)
        active_n = sum(1 for t in trucks if t["active"])
        listing = (f'<div class="list">{"".join(_truck_row(t, db.active_trips(t["id"])) for t in trucks)}</div>'
                   if trucks else _note("info", "Парк пуст — загрузите trucks.json командой "
                                                "<code>python main.py init</code>."))
        add_btn = f'<a class="btn sm primary" href="/trucks/new">{ic("plus", 15)} Фура</a>'
        count = f"{len(trucks)} {plural(len(trucks), 'фура', 'фуры', 'фур')} · активных: {active_n}"
        return page("Парк",
                    top("Парк", count, right=add_btn)
                    + _park_tabs("/trucks") + listing
                    + '<p class="muted" style="margin:12px 4px">Нажмите на фуру — местоположение, '
                      'предложения, история и редактирование.</p>', active="/trucks")

    @app.get("/trucks/new")
    async def truck_new_form():
        return page("Новая фура", _truck_new_page(), active="/trucks")

    @app.post("/trucks/new")
    async def truck_new(request: Request):
        from starlette.concurrency import run_in_threadpool
        form = await _form(request)
        truck, errors = _parse_new_truck(form)
        if errors:
            return page("Новая фура", _truck_new_page(form, errors), active="/trucks",
                        status_code=400)
        db.upsert_truck(truck)
        # Yangi fura uchun mavjud yuklar ham hisoblansin (bildirishnomasiz)
        await run_in_threadpool(pipeline.rematch_all)
        return _redirect(f"/truck/{truck['id']}?saved=1")

    @app.post("/trucks/{truck_id}")
    async def truck_save(truck_id: str, request: Request):
        truck = db.get_truck(truck_id)
        if truck is None:
            return page("Не найдено", _note("err", "Фура не найдена"), status_code=404)
        form = await _form(request)
        errors, fields = [], {}

        city = None
        raw_city = form.get("current_city", "").strip()
        if raw_city:
            city = geo.lookup(raw_city)
            if not city:
                found = geo.find_cities(raw_city)
                city = found[0][1] if found else None
            if not city:
                errors.append(f"Город «{e(raw_city)}» не найден")

        free_date = form.get("free_date", "").strip() or None
        if free_date:
            try:
                free_date = date.fromisoformat(free_date).isoformat()
            except ValueError:
                errors.append("Неверная дата")

        for name, label, lo, hi in (("capacity_t", "Грузоподъёмность", 1, 40),
                                    ("fuel_l_100km", "Расход топлива", 10, 70)):
            raw = form.get(name, "").strip().replace(",", ".")
            if raw:
                try:
                    value = float(raw)
                    if not lo <= value <= hi:
                        raise ValueError
                    fields[name] = value
                except ValueError:
                    errors.append(f"{label}: введите число от {lo} до {hi}")

        direction = form.get("preferred_dir", "")
        if direction in DIRECTIONS:
            fields["preferred_dir"] = direction
        if form.get("body_type") in BODY:
            fields["body_type"] = form["body_type"]
            temps, temp_errors = _parse_temps(form, fields["body_type"])
            errors += temp_errors
            fields.update(temps)
        for name in ("driver", "driver_phone", "plate"):
            if name in form:
                fields[name] = form[name].strip()[:60]
        fields["active"] = 1 if form.get("active") == "1" else 0

        if errors:
            return _truck_page(truck_id, note=_note("err", "<br>".join(errors)),
                               open_edit=True, status_code=400)

        db.update_truck(truck_id, **fields)
        # Holat faqat o'zgargan bo'lsa yangilanadi: aks holda oddiy "saqlash"
        # GPS'ni 24 soatga bloklab qo'yardi (qo'lda kiritilgan holat ustun).
        city_changed = city and city != truck["current_city"]
        date_changed = free_date and free_date != truck["free_date"]
        if city_changed or date_changed:
            db.set_truck_position(truck_id,
                                  city=city if city_changed else None,
                                  free_date=free_date if date_changed else None,
                                  source="manual")
        return _redirect(f"/truck/{truck_id}?saved=1")

    # ---------------------------------------------------------- xarita

    @app.get("/map")
    async def map_page():
        libs = ('<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">'
                '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>')
        legend = f"""<div class="legend">
  <span>{ic("truck", 15)} фуры</span>
  <span><i style="background:var(--ok)"></i>балл ≥ 85</span>
  <span><i style="background:#ca8a04"></i>65–84</span>
  <span><i style="background:#9ca3af"></i>низкий</span>
</div>"""
        body = (top("Грузы", "Карта: фуры и активные грузы за последние 24 часа")
                + _cargo_tabs("/map") + legend + '<div id="map"></div>' + MAP_JS)
        return page("Карта", body, active="/map", head=libs)

    @app.get("/api/map")
    async def map_data():
        return JSONResponse(map_payload())

    # ---------------------------------------------------------- statistika

    @app.get("/stats")
    async def stats_page(days: int = 30):
        days = max(1, min(int(days or 30), 365))
        report = analytics.report(days=days)
        t = report["totals"]

        tiles = [("box", "Грузы", t["cargos"], f"за {days} дн."),
                 ("check", "Взято", t["taken"], "рейсов"),
                 ("clock", "Устарело", t["expired"], "без ответа"),
                 ("bell", "Уведомления", t["notified"], f"из {t['matches']} совпадений"),
                 ("money", "Средняя маржа", money(t["avg_taken_margin"]),
                  "по взятым рейсам")]
        stats = '<div class="stats">' + "".join(
            f'<div class="stat"><div class="k">{ic(name, 14)} {label}</div>'
            f'<div class="v">{value}</div><small>{note}</small></div>'
            for name, label, value, note in tiles) + "</div>"

        groups = "".join(f"""<tr>
  <td>{e(g['source'])}</td><td class="num">{g['cargos']}</td>
  <td class="num">{g['taken']}</td><td class="num">{g['skipped']}</td>
  <td class="r">{g['take_rate']}%</td>
  <td class="r">{g['avg_score'] if g['avg_score'] is not None else '—'}</td>
</tr>""" for g in report["groups"])
        groups_table = f"""<h2>{ic("satellite", 14)} Группы</h2>
<div class="table-wrap"><table>
<thead><tr><th>Группа</th><th>Грузы</th><th>Взято</th><th>Пропущено</th>
<th style="text-align:right">Доля взятых</th>
<th style="text-align:right">Ср. балл</th></tr></thead>
<tbody>{groups or _empty("Пока нет данных", "inbox", colspan=6)}</tbody>
</table></div>
<p class="muted">Группу с низкой долей взятых можно перестать отслеживать —
удалите её из <code>sources.json</code>.</p>"""

        few = '<span class="muted">мало данных</span>'
        route_rows = []
        for r in report["routes"]:
            per_km = f"{r['rate_per_km']:.2f} $/км" if r["rate_per_km"] else few
            km = f"{r['km']:.0f} км" if r["km"] else "—"
            route_rows.append(f"""<tr>
  <td><b>{ru(r['from_city'])} → {ru(r['to_city'])}</b></td>
  <td class="num">{r['count']}</td>
  <td class="num">{km}</td>
  <td class="r">{money(r['avg_rate_usd'])}</td>
  <td class="r">{per_km}</td>
</tr>""")
        routes = "".join(route_rows)
        routes_table = f"""<h2>{ic("route", 14)} Направления</h2>
<div class="table-wrap"><table>
<thead><tr><th>Направление</th><th>Объявления</th><th>Расстояние</th>
<th style="text-align:right">Ср. ставка</th>
<th style="text-align:right">Рынок $/км</th></tr></thead>
<tbody>{routes or _empty("Пока нет данных", "inbox", colspan=5)}</tbody>
</table></div>
<p class="muted">Когда по направлению набирается больше 3 объявлений, в расчёте
балла используется его собственная ставка.</p>"""

        s, m = report["score"], report["margin"]
        verdicts = f"""<div class="stats">
  <div class="stat"><div class="k">{ic("chart", 14)} Верен ли балл</div>
    <div style="margin-top:6px">{e(s['verdict'])}</div>
    <small>взятые {s['taken']['avg_score'] if s['taken'] else '—'} ·
      пропущенные {s['skipped']['avg_score'] if s['skipped'] else '—'}</small></div>
  <div class="stat"><div class="k">{ic("money", 14)} Точность прогноза</div>
    <div style="margin-top:6px">{e(m['verdict'])}</div>
    <small>по {m['n']} {plural(m['n'], 'рейсу', 'рейсам', 'рейсам')}</small></div>
</div>"""

        picker = '<div class="seg">' + "".join(
            f'<a class="{"on" if d == days else ""}" href="/stats?days={d}">{d} дн.</a>'
            for d in (7, 30, 90)) + "</div>"
        return page("Статистика",
                    top("Статистика", back=BACK_MORE, subtitle=
                         "Какие группы полезны, верно ли работает балл, "
                         "сбывается ли прогноз")
                    + f'<div style="margin:-8px 0 20px">{picker}</div>'
                    + stats + verdicts + groups_table + routes_table, active="/stats")

    # ---------------------------------------------------------- mashina

    @app.get("/truck/{truck_id}")
    async def truck_page(truck_id: str, saved: str = ""):
        note = _note("ok", "Сохранено") if saved else ""
        return _truck_page(truck_id, note=note)

    # ---------------------------------------------------------- tarix

    @app.get("/history")
    async def history(saved: str = "", error: str = ""):
        notes = (_note("ok", "Сохранено") if saved else "") + \
            (_note("err", e(error)) if error else "")
        active_trips = db.active_trips()
        done = db.finished_trips(days=180)

        on_road = "".join(_trip_block(t, show_truck=True) for t in active_trips)
        road_html = (f'<div class="card tcard">{on_road}</div>' if on_road else
                     '<div class="muted" style="margin:0 4px">Сейчас рейсов в пути нет.</div>')

        rows = []
        for r in done:
            actual_v = r["actual_margin_usd"]
            if actual_v is None:
                result = (f'<form method="post" action="/match/{r["id"]}/actual" class="inline">'
                          f'<input name="margin" type="number" step="1" placeholder="факт $"'
                          f' style="width:110px" aria-label="Фактическая маржа">'
                          f'<button class="btn sm" style="margin-left:6px">OK</button></form>')
            else:
                pct = ""
                if r["margin_usd"]:
                    diff = (actual_v - r["margin_usd"]) / abs(r["margin_usd"]) * 100
                    pct = (f' <span class="pill {"ok" if abs(diff) <= 15 else "bad"}">'
                           f'{diff:+.0f}%</span>')
                result = f'<span class="money">{money(actual_v)}</span>{pct}'
            rows.append(f"""<div class="li">
  <span class="tnum">№{e(r['truck_id'])}</span>
  <div class="main"><a class="t route" href="/cargo/{r['cargo_id']}">{ru(r['from_city'])} → {ru(r['to_city'])}</a>
  <div class="s">завершён {e((r['finished_at'] or '')[:10])} · прогноз {money(r['margin_usd'])}</div></div>
  <span class="end">{result}</span></div>""")
        done_html = (f'<div class="list">{"".join(rows)}</div>' if rows else
                     _empty("Взятых рейсов пока нет. Они появятся здесь после "
                            "нажатия «Беру» на карточке.", "receipt"))
        return page("Рейсы",
                    top("Рейсы", "В пути и завершённые")
                    + _park_tabs("/history") + notes
                    + f'<h2 class="sec"><span>{anim("truck", 22)} В пути</span> <small>{len(active_trips)}</small></h2>'
                    + road_html
                    + f'<h2 class="sec"><span>{anim("flag", 22)} Завершённые</span> <small>введите фактическую маржу — '
                      'по ней уточняется прогноз</small></h2>'
                    + done_html, active="/history")

    @app.post("/trip/{match_id}/finish")
    async def trip_finish(match_id: int, request: Request):
        actions.finish_trip(match_id)
        return _redirect(_back(request, "/history"))

    @app.post("/trip/{match_id}/undo")
    async def trip_undo(match_id: int, request: Request):
        from starlette.concurrency import run_in_threadpool
        m = db.get_match(match_id)
        status = actions.undo_take(match_id)
        if status == "ok" and m is not None:
            cargo = db.get_cargo(m["cargo_id"])
            if cargo is not None:
                notifier.notify_driver(m["truck_id"], notifier.format_driver_cancel(
                    dict(cargo), {"id": m["truck_id"]}))
            # yuk va fura yana taklif qilinsin
            await run_in_threadpool(pipeline.rematch_cargo, m["cargo_id"])
            await run_in_threadpool(pipeline.rematch_all, 48, [m["truck_id"]])
        elif status == "not_latest":
            return _redirect("/history?" + urlencode(
                {"error": "Сначала отмените следующий рейс этой фуры"}))
        return _redirect(_back(request, "/history"))

    # ---------------------------------------------------------- sozlamalar

    @app.get("/settings")
    async def settings_page(saved: str = ""):
        note = _note("ok", "Сохранено. Новые значения применятся во всех процессах "
                           "в течение 30 секунд — перезапуск не нужен.") \
            if saved else ""
        return page("Настройки",
                    top("Настройки", back=BACK_MORE, subtitle=
                        "Основа расчёта маржи и балла. Изменения доходят "
                        "до всех процессов за 30 секунд")
                    + note + _settings_form(), active="/settings")

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await _form(request)
        errors = settings.save(form)
        if errors:
            return page("Настройки",
                        top("Настройки", back=BACK_MORE) + _note("err", "<br>".join(
                            e(v) for v in errors.values())) + _settings_form(form),
                        active="/settings", status_code=400)
        return _redirect("/settings?saved=1")

    @app.post("/settings/reset")
    async def settings_reset():
        settings.reset()
        return _redirect("/settings?saved=1")

    # ---------------------------------------------------------- AI yordamchi

    @app.get("/chat")
    async def chat_page():
        import brain
        note = "" if brain.enabled() else _note(
            "info", "AI не подключён — укажите в <code>.env</code> <code>MISTRAL_API_KEY</code> "
                    "или <code>GROQ_API_KEY</code>. Пока работает обычный поиск.")
        history = "".join(
            (f'<div class="bubble ai fade">'
             f'{_merge_offers(h["content"], _history_offer_ids(h["content"]))}</div>')
            if h["role"] == "assistant" else _bubble(h["role"], h["content"])
            for h in db.ai_history(WEB_CHAT_ID, limit=30, hours=72))
        if not history:
            import brain
            history = (f'<div class="bubble ai fade">{anim("wave", 28)} '
                       f'{brain.to_telegram_html(CHAT_WELCOME)}</div>')
        chips = "".join(
            f'<button type="button" class="chip" onclick="baxtAsk(this.textContent)">'
            f'{e(t)}</button>' for t in CHAT_SUGGESTIONS)
        new_btn = (f'<form method="post" action="/chat/new" class="inline">'
                   f'<button class="btn sm" title="Начать разговор заново">'
                   f'{ic("refresh", 14)} Новый</button></form>')
        body = f"""{top(f"AI-помощник{anim('robot', 34)}", "Пишите обычными словами", right=new_btn)}{note}
<div class="chat" id="thread">{history}</div>
<div class="bubble ai typing" id="typing"><i></i><i></i><i></i></div>
<div class="dock"><div class="chips scroll">{chips}</div>
<form class="composer" id="composer" method="post" action="/chat/send"
  hx-post="/chat/send" hx-target="#thread" hx-swap="beforeend" hx-indicator="#typing"
  hx-on::before-request="baxtEcho()" hx-on::after-request="this.reset();baxtScroll()">
  <textarea name="text" rows="1" placeholder="Напишите вопрос или задачу…"
    required onkeydown="baxtKey(event,this)" oninput="baxtGrow(this)"></textarea>
  <button class="btn primary" title="Отправить" aria-label="Отправить">{ic("send", 20)}</button>
</form></div>{CHAT_JS}"""
        return page("AI-помощник", body, active="/chat")

    @app.post("/chat/send")
    async def chat_send(request: Request):
        from starlette.concurrency import run_in_threadpool
        form = await _form(request)
        text = (form.get("text") or "").strip()[:2000]
        if not text:
            return HTMLResponse("")
        # Model 5–30 soniya o'ylaydi — server boshqa so'rovlarni kutib qolmasin
        html_out = await run_in_threadpool(_chat_answer, text)
        if not _is_htmx(request):
            return _redirect("/chat")
        return HTMLResponse(html_out)

    @app.post("/chat/new")
    async def chat_new():
        db.clear_ai_history(WEB_CHAT_ID)
        return _redirect("/chat")

    # ---------------------------------------------------------- Ko'proq

    @app.get("/more")
    async def more_page():
        import rules
        counts = {"/rules": sum(1 for r in rules.list_rules() if r["status"] == "active"),
                  "/watches": len(db.active_watches())}

        more_emoji = {"/stats": "chart", "/rules": "brain", "/watches": "bell",
                      "/settings": "gear"}

        def row(href, label, icon, color):
            n = counts.get(href)
            badge = f'<span>{n}</span>' if n else ""
            return (f'<a class="li" href="{href}"><span class="badge em-badge">'
                    f'{anim(more_emoji.get(href, "sparkles"), 30)}</span>'
                    f'<div class="main"><span class="t">{label}</span></div>'
                    f'<span class="end">{badge}{ic("chevron", 16, "chev")}</span></a>')

        groups = [("Анализ", MORE[:1]), ("AI и отслеживание", MORE[1:3]), ("Система", MORE[3:])]
        body = top("Ещё")
        for title, items in groups:
            extra = ""
            if title == "Система":
                extra = (f'<button class="li" onclick="baxtTheme()"><span class="badge em-badge">'
                         f'{anim("sun", 30)}</span><div class="main">'
                         f'<span class="t">Светлая / тёмная тема</span></div></button>'
                         f'<form method="post" action="/logout"><button class="li red">'
                         f'<span class="badge em-badge">{anim("wave", 30)}'
                         f'</span><div class="main"><span class="t">Выйти</span></div>'
                         f'</button></form>')
            body += (f'<h2 class="sec">{title}</h2><div class="list">'
                     + "".join(row(*item) for item in items) + extra + "</div>")
        body += '<div class="foot-note">BAXT TRANSPORT · панель диспетчера</div>'
        return page("Ещё", body, active="/more")

    @app.get("/watches")
    async def watches_page():
        block = _watch_block() or _empty(
            "Отслеживаний нет. Найдите направление в Грузы → Поиск и "
            "нажмите «Сообщить, когда появится такой груз».", "bell")
        return page("Отслеживание",
                    top("Отслеживание", "Как только появится такой груз — сразу сообщение в Telegram",
                        back=BACK_MORE) + block, active="/watches")

    # ---------------------------------------------------------- qoidalar

    @app.get("/rules")
    async def rules_page(msg: str = "", err: str = ""):
        note = (_note("ok", e(msg)) if msg else "") + (_note("err", e(err)) if err else "")
        return page("Правила", top("Правила", "Правила компании: автоматически применяются "
                                              "к расчёту каждого груза", back=BACK_MORE)
                    + note + _rules_block(), active="/rules")

    @app.post("/rules")
    async def rules_add(request: Request):
        import ai_tools
        import rules
        form = await _form(request)
        scope = {k: form.get(k) for k in rules.SCOPE_KEYS if form.get(k)}
        out = ai_tools.add_rule(ai_tools.Ctx(), effect=form.get("effect"), scope=scope,
                                points=form.get("points") or None,
                                min_rate=form.get("min_rate") or None,
                                currency=form.get("currency") or "USD",
                                text="")
        if "error" in out:
            return _redirect("/rules?" + urlencode({"err": out["error"]}))
        return _redirect("/rules?" + urlencode({"msg": f"Правило #{out['rule_id']} добавлено"}))

    @app.post("/rules/{rule_id}/status")
    async def rules_status(rule_id: int, request: Request):
        import rules
        form = await _form(request)
        status = form.get("status", "")
        if status == "delete":
            rules.delete(rule_id)
        else:
            rules.set_status(rule_id, status)
        return _redirect("/rules")

    @app.post("/rules/learn")
    async def rules_learn():
        import learn
        items = learn.propose()
        msg = (f"Новых предложений: {len(items)} — подтвердите ниже" if items
               else "Новых закономерностей нет — нужно больше решений «Беру/Пропустить»")
        return _redirect("/rules?" + urlencode({"msg": msg}))

    @app.post("/memory/{memory_id}/delete")
    async def memory_delete(memory_id: int):
        db.delete_memory(memory_id)
        return _redirect("/rules")

    return app


# ---------------------------------------------------------------- sahifa qismlari

def _fleet_cards() -> str:
    """Park kartochkalari (bosh sahifa va jonli yangilanish uchun bir xil)."""
    trucks = db.get_trucks(active_only=False)
    if not trucks:
        return _note("info", "Парк пуст — загрузите trucks.json командой "
                             "<code>python main.py init</code>.")
    return "".join(
        _truck_card(t, _best_offers(t["id"], 2) if t["active"] else [],
                    db.active_trips(t["id"]))
        for t in trucks)


def _search_results(found: dict, text: str) -> str:
    """Qidiruv natijasi. Har bir yuk yonida — qaysi fura va kuniga qancha."""
    query = found["query"]
    if not found["results"]:
        history = found["history"]
        hint = (f"За последние 7 дней такой груз появлялся <b>{history}</b> раз — "
                f"направление живое, есть смысл подождать."
                if history else
                "За последние 7 дней таких грузов тоже не было.")
        if not found["trucks"]:
            hint += " Внимание: нет активных фур."
        return (_empty(f"<b>{e(query.describe())}</b> — сейчас подходящих грузов нет.<br>"
                       f"<span class='muted'>{hint}</span><br><br>"
                       + _watch_button(text), "search"))

    deals = []
    for r in found["results"]:
        c = r["cargo"]
        match = db.find_match(c["id"], r["truck_id"])
        actions = (_decision_buttons(match["id"]) if match
                   else f'<a class="btn sm" href="/cargo/{c["id"]}">Смотреть</a>')
        meta = (f'<span class="tnum">№{e(r["truck_id"])}</span> · пустой {r["empty_km"]:.0f} км · '
                f'маржа {money(r["margin_usd"])} · {e(_day_text(c.get("load_date")))}'
                f'<br>{_cargo_line(c)}')
        deals.append(_deal(match["id"] if match else f"c{c['id']}", c["id"],
                           c["from_city"], c["to_city"], None, meta, actions,
                           right=_price_big(c)))
    return f"""<h2 class="sec">Найдено: {len(deals)}
<small>самые выгодные сверху · из {found['scanned']} грузов, фур: {found['trucks']}</small></h2>
<div class="list">{''.join(deals)}</div>
<p>{_watch_button(text)}</p>"""


def _watch_button(text: str) -> str:
    return (f'<form method="post" action="/watch" class="inline">'
            f'<input type="hidden" name="q" value="{e(text)}">'
            f'<button class="btn">{ic("bell", 15)} Сообщить, когда появится такой груз'
            f'</button></form>')


def _watch_block() -> str:
    """Kuzatilayotgan yo'nalishlar — botdan ham, paneldan ham qo'shiladi."""
    watches = db.active_watches()
    if not watches:
        return ""
    items = []
    for w in watches:
        route = " → ".join(geo.ru(x) for x in (w["from_city"], w["to_city"]) if x) or "—"
        body = f" · {BODY.get(w['body_type'], w['body_type'])}" if w["body_type"] else ""
        items.append(f"""<tr>
  <td><b>{e(route)}</b>{e(body)}</td>
  <td class="num">{w['hits']}</td>
  <td class="muted">{e((w['expires_at'] or '')[:10])}</td>
  <td style="text-align:right">
    <form method="post" action="/watch/{w['id']}/delete" class="inline">
      <button class="btn sm danger" title="Удалить">{ic("trash", 15)}</button>
    </form></td>
</tr>""")
    return f"""<h2>{ic("bell", 14)} Отслеживаемые направления</h2>
<div class="table-wrap"><table>
<thead><tr><th>Направление</th><th>Найдено</th><th>До</th><th></th></tr></thead>
<tbody>{''.join(items)}</tbody></table></div>
<p class="muted">Когда появится такой груз, сообщение придёт в Telegram независимо
от порога балла. По истечении срока отслеживание отключится само.</p>"""


def _city_datalist() -> str:
    options = "".join(f'<option value="{e(name)}">' for name in sorted(geo.RU.values()))
    return f'<datalist id="cities">{options}</datalist>'


def _return_block(cargo, matches) -> str:
    """Olingan yuk sahifasida: mashina manzilga yetgach qaytish yuki."""
    if cargo["status"] != "taken":
        return ""
    taken = next((m for m in matches if m["decision"] == "taken"), None)
    truck = db.get_truck(taken["truck_id"]) if taken else None
    if truck is None or truck["current_city"] != cargo["to_city"]:
        return ""
    best = scoring.best_cargos(truck, db.active_cargos(hours=48), top=5)
    if not best:
        return (f"<h2>Обратный груз</h2>" + _note(
            "info", f"Из г. {ru(cargo['to_city'])} подходящих грузов пока нет. "
                    f"Когда появятся новые объявления, бот сообщит сам."))
    lines = []
    for r in best:
        back = db.get_cargo(r["cargo_id"])
        lines.append(f"""<tr>
  <td>{_ring(r['score'], small=True)}</td>
  <td><a class="route" href="/cargo/{back['id']}">{ru(back['from_city'])} → {ru(back['to_city'])}</a></td>
  <td class="num">{r['empty_km']} км</td>
  <td class="r"><span class="money plus">{money(r['margin_usd'])}</span></td>
  <td class="r">{e(_price_short(back) or '—')}</td>
  <td>{e(back['load_date'] or '—')}</td>
</tr>""")
    return f"""<h2>Обратный груз · фура №{e(truck['id'])} · из г. {ru(truck['current_city'])}
(освободится {e(_day_text(truck['free_date']))})</h2>
<div class="table-wrap"><table>
<thead><tr><th>Балл</th><th>Направление</th><th>Пустой</th>
<th style="text-align:right">Маржа</th><th style="text-align:right">Цена</th>
<th>Погрузка</th></tr></thead>
<tbody>{''.join(lines)}</tbody></table></div>"""


def _truck_form(t) -> str:
    """Bitta furani tahrirlash formasi (fura sahifasida)."""
    dirs = "".join(f'<option value="{d}" {"selected" if (t["preferred_dir"] or "") == d else ""}>'
                   f'{d or "—"}</option>' for d in DIRECTIONS)
    return f"""<form method="post" action="/trucks/{e(t['id'])}">
  <div class="row">
    <div class="field"><label>Где сейчас</label>
      <input name="current_city" value="{ru(t['current_city'] or '')}" list="cities"></div>
    <div class="field"><label>Когда освободится</label>
      <input name="free_date" type="date" value="{e(t['free_date'] or '')}"></div>
  </div>
  <div class="row">
    <div class="field"><label>Госномер</label><input name="plate" value="{e(t['plate'] or '')}"></div>
    <div class="field"><label>Водитель</label><input name="driver" value="{e(t['driver'] or '')}"></div>
    <div class="field"><label>Телефон</label><input name="driver_phone" value="{e(t['driver_phone'] or '')}" inputmode="tel"></div>
  </div>
  <div class="row">
    <div class="field"><label>Кузов</label><select name="body_type">{_body_options(t['body_type'])}</select></div>
    <div class="field"><label>Реф: мин. °C</label>
      <input name="temp_min" value="{'' if t['temp_min'] is None else format(t['temp_min'], 'g')}" inputmode="decimal"></div>
    <div class="field"><label>Реф: макс. °C</label>
      <input name="temp_max" value="{'' if t['temp_max'] is None else format(t['temp_max'], 'g')}" inputmode="decimal"></div>
  </div>
  <div class="row">
    <div class="field"><label>Грузоподъёмность, т</label><input name="capacity_t" value="{t['capacity_t'] or ''}" inputmode="decimal"></div>
    <div class="field"><label>Топливо, л/100 км</label><input name="fuel_l_100km" value="{t['fuel_l_100km'] or ''}" inputmode="decimal"></div>
    <div class="field"><label>Направление</label><select name="preferred_dir">{dirs}</select></div>
  </div>
  <div class="field"><input type="hidden" name="active" value="0">
    <label class="switch"><input type="checkbox" name="active" value="1"
    {"checked" if t['active'] else ""}><span class="track"></span>
    <span>Активна — участвует в предложениях</span></label></div>
  <button class="btn primary">Сохранить</button>
</form>{_city_datalist()}"""


TRUCK_BODIES = [("ref", "Реф (холодильник)"), ("tent", "Тент"), ("izoterm", "Изотерм")]


def _parse_temps(form: dict, body: str) -> tuple[dict, list[str]]:
    """Ref uchun harorat oralig'i; boshqa kuzovda — bo'sh."""
    if body != "ref":
        return {"temp_min": None, "temp_max": None}, []
    out, errors = {}, []
    for name, label in (("temp_min", "Мин. температура"), ("temp_max", "Макс. температура")):
        raw = (form.get(name) or "").strip().replace(",", ".")
        try:
            value = float(raw)
            if not -30 <= value <= 30:
                raise ValueError
            out[name] = value
        except ValueError:
            errors.append(f"{label}: введите число от −30 до 30 °C")
    if not errors and out["temp_min"] > out["temp_max"]:
        errors.append("Мин. температура не должна быть выше макс.")
    return out, errors


def _next_truck_id() -> str:
    nums = [int(t["id"]) for t in db.get_trucks(active_only=False) if str(t["id"]).isdigit()]
    return f"{(max(nums) + 1) if nums else 1:02d}"


def _parse_new_truck(form: dict) -> tuple[dict, list[str]]:
    errors = []
    truck_id = (form.get("id") or "").strip().lstrip("№#")
    if truck_id.isdigit():
        truck_id = truck_id.zfill(2)
    if not truck_id or len(truck_id) > 8 or not truck_id.replace("-", "").isalnum():
        errors.append("Номер фуры: например 07")
    elif db.get_truck(truck_id) is not None:
        errors.append(f"№{e(truck_id)} уже есть")
    body = form.get("body_type") if form.get("body_type") in BODY else None
    if body is None:
        errors.append("Выберите тип кузова")
    temps, temp_errors = _parse_temps(form, body or "")
    errors += temp_errors
    city = geo.lookup((form.get("current_city") or "").strip()) \
        if (form.get("current_city") or "").strip() else None
    if not city:
        errors.append("Укажите, где стоит фура (город)")
    numbers = {}
    for name, label, lo, hi, default in (("capacity_t", "Грузоподъёмность", 1, 40, 20),
                                         ("fuel_l_100km", "Расход топлива", 10, 70, 33)):
        raw = (form.get(name) or "").strip().replace(",", ".")
        try:
            value = float(raw) if raw else float(default)
            if not lo <= value <= hi:
                raise ValueError
            numbers[name] = value
        except ValueError:
            errors.append(f"{label}: число от {lo} до {hi}")
    free = (form.get("free_date") or "").strip() or date.today().isoformat()
    try:
        free = date.fromisoformat(free).isoformat()
    except ValueError:
        errors.append("Неверная дата")
    truck = {"id": truck_id, "body_type": body, **temps, **numbers,
             "current_city": city, "free_date": free,
             "plate": (form.get("plate") or "").strip()[:20],
             "driver": (form.get("driver") or "").strip()[:60],
             "driver_phone": (form.get("driver_phone") or "").strip()[:30],
             "preferred_dir": form.get("preferred_dir") if form.get("preferred_dir")
             in DIRECTIONS else "", "active": 1}
    return truck, errors


def _body_options(current: str | None) -> str:
    return "".join(f'<option value="{k}" {"selected" if k == current else ""}>{v}</option>'
                   for k, v in TRUCK_BODIES)


def _truck_new_page(form: dict | None = None, errors: list[str] | None = None) -> str:
    f = form or {}
    note = _note("err", "<br>".join(errors)) if errors else ""
    dirs = "".join(f'<option value="{d}" {"selected" if f.get("preferred_dir") == d else ""}>'
                   f'{d or "—"}</option>' for d in DIRECTIONS)
    return (top("Новая фура", "Обязательно: номер, кузов, где стоит", back=("/trucks", "Парк"))
            + note + f"""<form class="card" method="post" action="/trucks/new">
  <div class="row">
    <div class="field"><label>Номер фуры</label>
      <input name="id" value="{e(f.get('id') or _next_truck_id())}" inputmode="numeric"></div>
    <div class="field"><label>Кузов</label>
      <select name="body_type">{_body_options(f.get('body_type') or 'tent')}</select></div>
    <div class="field"><label>Грузоподъёмность, т</label>
      <input name="capacity_t" value="{e(f.get('capacity_t') or '20')}" inputmode="decimal"></div>
  </div>
  <div class="row">
    <div class="field"><label>Реф: мин. °C</label>
      <input name="temp_min" value="{e(f.get('temp_min') or '-20')}" inputmode="decimal"></div>
    <div class="field"><label>Реф: макс. °C</label>
      <input name="temp_max" value="{e(f.get('temp_max') or '15')}" inputmode="decimal"></div>
  </div>
  <div class="row">
    <div class="field"><label>Где сейчас</label>
      <input name="current_city" value="{e(f.get('current_city') or '')}" list="cities"
        placeholder="Ташкент"></div>
    <div class="field"><label>Когда освободится</label>
      <input name="free_date" type="date" value="{e(f.get('free_date') or date.today().isoformat())}"></div>
  </div>
  <div class="row">
    <div class="field"><label>Госномер</label><input name="plate" value="{e(f.get('plate') or '')}"></div>
    <div class="field"><label>Водитель</label><input name="driver" value="{e(f.get('driver') or '')}"></div>
    <div class="field"><label>Телефон</label>
      <input name="driver_phone" value="{e(f.get('driver_phone') or '')}" inputmode="tel"></div>
  </div>
  <div class="row">
    <div class="field"><label>Топливо, л/100 км</label>
      <input name="fuel_l_100km" value="{e(f.get('fuel_l_100km') or '33')}" inputmode="decimal"></div>
    <div class="field"><label>Направление</label><select name="preferred_dir">{dirs}</select></div>
  </div>
  <button class="btn primary">{ic("plus", 16)} Добавить</button>
  <p class="muted">После добавления активные грузы из базы пересчитаются и для этой фуры.</p>
</form>{_city_datalist()}""")


def _truck_offers(truck_id: str, n: int = 5) -> str:
    """Fura sahifasidagi takliflar bloki (o'tkazilganda shu blok yangilanadi)."""
    offers = "".join(_offer(o) for o in _best_offers(truck_id, n))
    if not offers:
        offers = (f'<div class="empty-row" style="border-top:0"><span>{anim("eyes", 22)}Пока подходящих грузов нет</span>'
                  f'{_ask_ai_button(f"Найди груз и обратный груз для фуры {truck_id}")}</div>')
    return f'<div class="card tcard fade" id="offers-{e(truck_id)}">{offers}</div>'


def _truck_page(truck_id: str, note: str = "", open_edit: bool = False,
                status_code: int = 200) -> HTMLResponse:
    """Fura sahifasi: holat, takliflar, tarix, tahrirlash — hammasi bir joyda."""
    truck = db.get_truck(truck_id)
    if truck is None:
        return page("Не найдено", _note("err", f"Фура №{e(truck_id)} не найдена"),
                    active="/trucks", status_code=404)

    gps_row = db.last_gps(truck_id)
    if gps_row:
        city = geo.nearest_city(gps_row["lat"], gps_row["lon"])
        gps_txt = (f"последний сигнал {e(ago_phrase(gps_row['recorded_at'] or gps_row['created_at']))}"
                   f" · {e(geo.ru(city) if city else 'город не определён')}")
    else:
        gps_txt = "Нет сигнала GPS"

    body = BODY.get(truck["body_type"], truck["body_type"] or "—")
    src = POS_SOURCE.get(truck["pos_source"] or "", "")
    src_pill = f' <span class="pill">{e(src)}</span>' if src else ""
    phone = (f'<a href="tel:{e(truck["driver_phone"])}">{e(truck["driver_phone"])}</a>'
             if truck["driver_phone"] else "—")
    on_trip = db.active_trips(truck_id)
    state, label = _truck_state(truck, on_trip)
    facts = [("Статус", f'<span class="tstate"><span class="dot s-{state}"></span>{e(label)}</span>'),
             ("Сейчас" if not on_trip else "Едет в",
              f'<b>{ru(truck["current_city"] or "—")}</b>{src_pill}'),
             ("Водитель", e(truck["driver"] or "—")),
             ("Телефон", phone),
             ("Telegram", "подключён — рейсы приходят сами" if truck["tg_user_id"] else
              (f'не подключён · пусть водитель напишет боту: <code>/link {e(truck_id)} '
               f'{e(truck["plate"])}</code>' if truck["plate"] else
               "не подключён · сначала укажите госномер")),
             ("GPS", gps_txt),
             ("Топливо", f'{truck["fuel_l_100km"] or 0:g} л/100 км'),
             ("Направление", e(truck["preferred_dir"] or "—"))]
    facts_html = '<div class="list kv">' + "".join(
        f'<div class="li"><span class="k">{k}</span><span class="v">{v}</span></div>'
        for k, v in facts) + "</div>"

    offers = _truck_offers(truck_id)

    trips = "".join(f"""<a class="li" href="/cargo/{r['cargo_id']}">
  <div class="main"><span class="t">{ru(r['from_city'])} → {ru(r['to_city'])}</span>
  <div class="s">{e((r['decided_at'] or r['created_at'] or '')[:10])} · прогноз {money(r['margin_usd'])}
  · факт {money(r['actual_margin_usd']) if r['actual_margin_usd'] is not None else '—'}</div></div>
  <span class="end">{ic("chevron", 16, "chev")}</span></a>""" for r in db.truck_history(truck_id))

    plan_btn = _ask_ai_button(f"Спланируй груз и обратный груз для фуры {truck_id}", "План")
    html = (top(f"Фура №{e(truck_id)}",
                " · ".join(x for x in (e(truck["plate"] or ""),
                                       f"{e(body)} {truck['capacity_t'] or 0:g} т") if x),
                back=("/trucks", "Парк"), right=plan_btn)
            + note + facts_html
            + ('<h2 class="sec">Текущий рейс</h2><div class="card tcard">'
               + "".join(_trip_block(t) for t in on_trip) + '</div>' if on_trip else '')
            + f'<h2 class="sec">{"Следующий груз" if on_trip else "Предложения"} '
              f'<small>самые выгодные сверху</small></h2>'
            + offers
            + '<h2 class="sec">История рейсов</h2>'
            + (f'<div class="list">{trips}</div>' if trips else _empty("Рейсов пока нет", "receipt"))
            + '<h2 class="sec">Редактирование</h2>'
            + f'<details class="card"{" open" if open_edit else ""}><summary>'
              f'Местоположение, водитель, грузоподъёмность {ic("chevron", 16, "chev")}</summary>'
            + _truck_form(truck) + "</details>")
    return page(f"Фура №{truck_id}", html, active="/trucks", status_code=status_code)


def _settings_form(raw: dict | None = None) -> str:
    values = settings.current_values(pipeline.NOTIFY_THRESHOLD)
    overridden = settings.overrides()
    rows = []
    for name, label, unit, lo, hi in settings.FIELDS + settings.EXTRA_FIELDS:
        value = (raw or {}).get(name, values.get(name))
        default = {"notify_threshold": pipeline.NOTIFY_THRESHOLD,
                   "briefing_hour": settings.BRIEFING_HOUR_DEFAULT}.get(name)
        if default is None:
            default = getattr(config.COSTS, name)
        mark = ' <span class="pill">изменено</span>' if name in overridden else ""
        rows.append(f"""<tr><td>{e(label)}{mark}</td>
  <td><input name="{name}" value="{e(value)}" inputmode="decimal" style="width:110px"></td>
  <td class="muted">{e(unit)}</td><td class="muted num">{default:g}</td>
  <td class="muted num">{lo:g}–{hi:g}</td></tr>""")
    return f"""<form method="post" action="/settings"><div class="table-wrap"><table>
<thead><tr><th>Параметр</th><th>Значение</th><th>Ед.</th><th>По умолчанию</th>
<th>Диапазон</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<p style="display:flex;gap:10px;align-items:center">
<button class="btn primary">Сохранить</button>
<span class="muted">Значения по умолчанию — в <code>config.py</code>, он не меняется.</span>
</p></form>
<form method="post" action="/settings/reset"
  onsubmit="return confirm('Все значения вернутся к исходным. Продолжить?')">
<button class="btn danger">Вернуть значения по умолчанию</button></form>"""


# ---------------------------------------------------------------- AI suhbat

WEB_CHAT_ID = "web"
CHAT_WELCOME = ("Здравствуйте! Я AI-помощник диспетчера BAXT.\n\n"
                "Напишите, например:\n• 01 в Москве, найди хороший груз рядом\n"
                "• Не бери груз из России дешевле 30 млн — запомню как правило\n"
                "• Спланируй груз и обратный груз для каждой фуры")
CHAT_SUGGESTIONS = ["План: груз + обратный для каждой фуры", "Состояние парка",
                    "Самые выгодные грузы сегодня", "Какие у нас правила?"]
CHAT_JS = """<script>
function baxtScroll(){var t=document.getElementById('thread');
 var last=t.lastElementChild; if(last)last.scrollIntoView({block:'end',behavior:'smooth'});}
function baxtEcho(){var f=document.getElementById('composer');var v=f.text.value.trim();
 if(!v)return; var d=document.createElement('div');d.className='bubble me';d.textContent=v;
 document.getElementById('thread').appendChild(d);baxtScroll();}
function baxtAsk(t){var f=document.getElementById('composer');f.text.value=t;f.requestSubmit();}
function baxtGrow(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,140)+'px';}
function baxtKey(ev,el){if(ev.key==='Enter'&&!ev.shiftKey){ev.preventDefault();
 el.form.requestSubmit();}}
addEventListener('DOMContentLoaded',function(){baxtScroll();
 var q=new URLSearchParams(location.search).get('q');
 if(q){history.replaceState(null,'','/chat');setTimeout(function(){baxtAsk(q)},60);}});
</script>"""


def _history_offer_ids(content: str) -> list[int]:
    """Tarixdagi AI javobi uchun "Olaman" tugmalari (sahifa qayta ochilganda ham).

    Javobda tilga olingan yuklar (#904) — faqat hali bo'sh va qaror
    qilinmagan takliflari bo'lsa. Yangi moslik yaratilmaydi: tarix sahifasi
    faqat mavjudini ko'rsatadi.
    """
    import brain
    import search
    rows = []
    for raw in dict.fromkeys(brain._CARGO_REF.findall(content)):
        cargo = db.get_cargo(int(raw))
        if cargo is None or cargo["status"] != "new":
            continue
        open_ = [m for m in db.matches_for_cargo(int(raw)) if m["decision"] is None]
        if open_:
            rows.append(search.rank_offers(open_)[0]["id"])
        if len(rows) >= 5:
            break
    return rows


# AI javobidagi yuk qatori: "#904 — ...", "**#904** — ...", "1. #904 ...", "• #904 ..."
_CARGO_LINE = re.compile(r"^\s*(?:\d+[.)]\s*|[-•*]\s*)?(?:\*\*|<b>)?#(\d+)")


def _merge_offers(raw: str, match_ids: list[int]) -> str:
    """AI matni + kartochkalar BITTA ro'yxat bo'lib.

    Kartochkasi bor yukning matndagi qatori (va uning davomi) olib tashlanadi,
    kartochkalar o'sha joyga qo'yiladi — ro'yxat ikki marta chiqmaydi.
    """
    import brain
    cards: dict[int, str] = {}
    for mid in match_ids:
        m = db.get_match(mid)
        if m is not None and m["cargo_id"] not in cards:
            cards[m["cargo_id"]] = _ai_offer(mid)
    if not cards:
        return brain.to_telegram_html(raw)

    kept, placed, skipping = [], False, False
    for line in raw.splitlines():
        hit = _CARGO_LINE.match(line)
        if hit and int(hit.group(1)) in cards:
            if not placed:
                kept.append("@@CARDS@@")
                placed = True
            skipping = True
            continue
        if skipping and line.strip() and (line[:1].isspace() or line.lstrip()[:1] in "•-"):
            continue                        # yuk qatorining davomi ("   Yuk: 18t ...")
        skipping = False
        kept.append(line)
    if not placed:
        kept.append("@@CARDS@@")
    html = brain.to_telegram_html("\n".join(kept))
    block = f'</p><div class="offers">{"".join(cards.values())}</div><p>'
    html = html.replace("@@CARDS@@", block)
    html = re.sub(r"\n*</p>", "</p>", re.sub(r"<p>\n*", "<p>", f"<p>{html}</p>"))
    return html.replace("<p></p>", "")


def _bubble(role: str, content: str, extra: str = "") -> str:
    """Suhbat pufakchasi. AI matni Telegram HTML (b/i/code) — xavfsiz
    ko'rinishga `brain.to_telegram_html` keltiradi; foydalanuvchi matni ekranlanadi."""
    import brain
    if role == "user":
        return f'<div class="bubble me">{e(content)}</div>'
    return f'<div class="bubble ai fade">{brain.to_telegram_html(content)}{extra}</div>'


def _chat_answer(text: str) -> str:
    """Bitta savolga javob (pufakcha + taklif tugmalari). Foydalanuvchi pufakchasini
    brauzer o'zi qo'shadi (`baxtEcho`) — kutish paytida ham ko'rinib tursin."""
    import brain
    import search
    res = None
    try:
        res = brain.reply(WEB_CHAT_ID, text)
    except Exception:
        log.exception("AI javobida xato")
    if res is None:
        # AI yo'q yoki ishlamadi — oddiy qidiruv (bot bilan bir xil manba)
        query = search.parse_query(text)
        if query.is_empty:
            return _bubble("assistant", "AI сейчас не смог ответить. Попробуйте написать "
                                        "города: Ташкент Москва")
        return f'<div class="bubble ai fade">{_search_results(search.find(query), text)}</div>'
    ids = [int(row[0]["callback_data"].split(":")[1])
           for row in (res.keyboard or {}).get("inline_keyboard", [])
           if row[0].get("callback_data", "").startswith("take:")]
    return f'<div class="bubble ai fade">{_merge_offers(res.raw or res.text, ids)}</div>'


def _ai_offer(match_id: int) -> str:
    m = db.get_match(match_id)
    if m is None:
        return ""
    c = db.get_cargo(m["cargo_id"])
    if c is None:
        return ""
    price = _price_short(c)
    price_txt = (f'<span class="money plus">{e(price)}</span>' if price
                 else "цена не указана")
    margin = f" · маржа {money(m['margin_usd'])}" if m["margin_usd"] is not None else ""
    d = _details(m)
    body = BODY.get(c["body_type"], c["body_type"] or "")
    load = " ".join(x for x in (f"{c['weight_t']:g} т" if c["weight_t"] else "", body) if x)
    facts = " · ".join(x for x in (
        load, f"пустой {m['empty_km']:.0f} км" if m["empty_km"] is not None else "",
        f"{d['trip_days']} дн." if d.get("trip_days") else "",
        _day_text(c["load_date"]) if c["load_date"] else "") if x)
    return f"""<div class="offer" id="m{match_id}">
  <div class="grow"><a class="route" href="/cargo/{c['id']}">#{c['id']} {ru(c['from_city'])} → {ru(c['to_city'])}</a>
  <div class="muted">{price_txt} · №{e(m['truck_id'])}{margin}</div>
  <div class="muted">{e(facts)}</div></div>
  <div class="act">{_decision_buttons(match_id)}</div>
  <div class="alts" id="alt-{match_id}"></div></div>"""


_EFFECT_LOOK = {"block": ("ban", "bad"), "penalty": ("warning", "warn"), "boost": ("check", "ok")}


def _rule_row(r: dict, buttons: str) -> str:
    import rules
    src = ('<span class="pill ok">из решений</span>' if r["source"] == "learned"
           else '<span class="pill">руководитель</span>')
    icon, cls = _EFFECT_LOOK.get(r["effect"], ("info", ""))
    said = f'<div class="muted">«{e(r["text"])}»</div>' if r["text"] else ""
    return f"""<div class="rule"><span class="pill {cls}">{ic(icon, 14)}</span>
  <div class="grow"><b>#{r['id']}</b> {e(rules.describe(r))} {src}{said}</div>
  <div class="act" style="display:flex;gap:6px">{buttons}</div></div>"""


def _rule_btn(rule_id: int, status: str, label: str, cls: str = "") -> str:
    return (f'<form method="post" action="/rules/{rule_id}/status" class="inline">'
            f'<input type="hidden" name="status" value="{status}">'
            f'<button class="btn sm {cls}">{label}</button></form>')


def _rules_block() -> str:
    import rules
    items = rules.list_rules()
    active = [r for r in items if r["status"] == "active"]
    proposed = [r for r in items if r["status"] == "proposed"]

    active_html = "".join(_rule_row(r, _rule_btn(r["id"], "delete", ic("trash", 15), "danger"))
                          for r in active) or _empty(
        "Правил нет. Напишите AI-помощнику: «Не бери груз из России дешевле 30 млн»",
        "rules")
    proposed_html = "".join(
        _rule_row(r, _rule_btn(r["id"], "active", "Применить", "ok")
                  + _rule_btn(r["id"], "rejected", "Нет"))
        for r in proposed) or ('<div class="muted">Предложений пока нет. Когда программа '
                               'найдёт закономерность в ваших решениях «Беру/Пропустить», '
                               'она появится здесь.</div>')
    learn_form = ('<form method="post" action="/rules/learn" class="inline">'
                  f'<button class="btn sm">{ic("brain", 15)} Учиться на решениях</button></form>')
    proposed_card = f"""<section class="card" style="margin-top:16px">
<header><h2>Предложения из решений</h2>{learn_form}</header>{proposed_html}</section>"""

    countries = "".join(f'<option value="{c}">{c}</option>' for c in DIRECTIONS)
    bodies = '<option value=""></option>' + "".join(
        f'<option value="{k}">{e(v)}</option>' for k, v in BODY.items())
    currencies = "".join(f"<option>{c}</option>" for c in config.RATES_TO_USD)
    form = f"""<section class="card" style="margin-top:16px"><header><h2>Правило вручную</h2></header>
<form method="post" action="/rules"><div class="row">
<div class="field"><label>Действие</label><select name="effect">
<option value="block">Не берём (запрет)</option><option value="penalty">Не нравится (−балл)</option>
<option value="boost">Нравится (+балл)</option></select></div>
<div class="field"><label>Откуда (страна)</label><select name="from_country">{countries}</select></div>
<div class="field"><label>Куда (страна)</label><select name="to_country">{countries}</select></div>
<div class="field"><label>Откуда (город)</label><input name="from_city" list="cities"></div>
<div class="field"><label>Куда (город)</label><input name="to_city" list="cities"></div>
<div class="field"><label>Кузов</label><select name="body_type">{bodies}</select></div>
<div class="field"><label>Фура №</label><input name="truck_id"></div>
<div class="field"><label>Мин. ставка</label><input name="min_rate" inputmode="decimal"
  placeholder="30000000"></div>
<div class="field"><label>Валюта</label><select name="currency">{currencies}</select></div>
<div class="field"><label>Балл (±)</label><input name="points" value="15"></div>
</div><button class="btn primary">{ic("plus", 16)} Добавить</button></form>{_city_datalist()}
</section>"""

    notes = db.memories(limit=50)
    notes_html = "".join(
        f'<div class="rule"><div class="grow">{e(n["note"])}</div>'
        f'<form method="post" action="/memory/{n["id"]}/delete" class="inline">'
        f'<button class="btn sm danger">{ic("trash", 15)}</button></form></div>'
        for n in notes) or '<div class="muted">AI пока ничего не запомнил.</div>'
    memory_card = f"""<section class="card" style="margin-top:16px">
<header><h2>Память AI</h2></header>{notes_html}</section>"""

    return (f'<section class="card"><header><h2>Действующие правила</h2>'
            f'<span class="pill">{len(active)}</span></header>{active_html}</section>'
            + proposed_card + form + memory_card)


def map_payload() -> dict:
    """Xarita uchun ma'lumot: mashinalar va aktiv yuklar."""
    trucks_out, seen = [], {}
    for t in db.get_trucks(active_only=False):
        city = geo.CITIES.get(t["current_city"] or "")
        if not city:
            continue
        # bir shahardagi mashinalar ustma-ust tushmasin
        n = seen.get(city.name, 0)
        seen[city.name] = n + 1
        trucks_out.append({
            "id": t["id"], "city": geo.ru(city.name), "lat": city.lat + n * 0.05,
            "lon": city.lon + n * 0.05, "free_date": t["free_date"],
            "body": BODY.get(t["body_type"], t["body_type"]), "active": bool(t["active"]),
        })

    cargos_out = []
    for c in db.search_cargos(status="new", hours=24, limit=150):
        a, b = geo.CITIES.get(c["from_city"] or ""), geo.CITIES.get(c["to_city"] or "")
        if not a or not b:
            continue
        cargos_out.append({
            "id": c["id"], "from": [a.lat, a.lon], "to": [b.lat, b.lon],
            "label": f"{geo.ru(c['from_city'])} → {geo.ru(c['to_city'])}",
            "rate_usd": c["rate_usd"], "score": c["best_score"],
        })
    return {"trucks": trucks_out, "cargos": cargos_out}


MAP_JS = """<script>
document.addEventListener('DOMContentLoaded', async () => {
  if (!window.L) { document.getElementById('map').innerText = 'Библиотека карты не загрузилась (интернет?)'; return; }
  const map = L.map('map').setView([47, 60], 4);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    {maxZoom: 12, attribution: '&copy; OpenStreetMap'}).addTo(map);
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const data = await (await fetch('/api/map')).json();
  const bounds = [];
  for (const c of data.cargos) {
    const color = c.score >= 85 ? '#15803d' : c.score >= 65 ? '#ca8a04' : '#9ca3af';
    L.polyline([c.from, c.to], {color, weight: 2, opacity: .7}).addTo(map)
      .bindPopup(`<a href="/cargo/${c.id}">#${c.id} ${esc(c.label)}</a><br>` +
                 `${c.rate_usd ? '$' + Math.round(c.rate_usd) : 'нет ставки'}` +
                 `${c.score != null ? ' · балл ' + Math.round(c.score) : ''}`);
    bounds.push(c.from, c.to);
  }
  for (const t of data.trucks) {
    const icon = L.divIcon({className: '', html: `<div style="background:${t.active ? '#1f6feb' : '#6b7280'};color:#fff;border-radius:8px;padding:2px 6px;font:600 12px sans-serif;white-space:nowrap">${esc(t.id)}</div>`});
    L.marker([t.lat, t.lon], {icon}).addTo(map)
      .bindPopup(`<b>№${esc(t.id)}</b> ${esc(t.body)}<br>${esc(t.city)}<br>освободится: ${esc(t.free_date || '—')}`);
    bounds.push([t.lat, t.lon]);
  }
  if (bounds.length) map.fitBounds(bounds, {padding: [30, 30], maxZoom: 7});
});
</script>"""


def run(host: str = "127.0.0.1", port: int = 8080) -> None:
    if not _password():
        raise SystemExit("WEB_PASSWORD sozlanmagan — panelni parolsiz ochib bo'lmaydi")
    if host not in ("127.0.0.1", "localhost") and not os.getenv("WEB_ALLOWED_IPS"):
        log.warning("Panel %s da ochiq va IP cheklovi yo'q. Internetga ochiq "
                    "qo'ymang — VPN yoki WEB_ALLOWED_IPS ishlating.", host)
    import uvicorn
    db.init()
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
