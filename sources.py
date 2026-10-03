"""
sources.py — kuzatiladigan Telegram guruhlari (bazada, panel va botdan boshqariladi).

Ilgari ro'yxat `sources.json` da edi: yangi guruh uchun serverda faylni
tahrirlab, listener'ni qayta ishga tushirish kerak edi. Endi:

  panel / bot  ──>  sources jadvali (status=pending)
  listener     ──>  har 30 s: kutayotganini topadi (kerak bo'lsa guruhga
                    o'zi qo'shiladi), `active` qiladi, tinglash ro'yxatini
                    yangilaydi; akkaunt a'zo bo'lgan guruhlarni `tg_dialogs` ga yozadi.

Telethon faqat listener'da (bitta sessiya — bitta dastur). Bu modul faqat
baza bilan ishlaydi: panel va bot uni bemalol chaqiradi.
"""
from __future__ import annotations

import logging
import re

import db

log = logging.getLogger("sources")

STATUSES = ("pending", "active", "error", "removed")
HEARTBEAT_KEY = "listener_seen"

# t.me/+HASH, t.me/joinchat/HASH — yopiq guruhga taklif
_RE_INVITE = re.compile(r"(?:https?://)?(?:t|telegram)\.me/(?:\+|joinchat/)([\w-]{10,})", re.I)
# t.me/username yoki t.me/username/123 (xabar havolasi) yoki @username
_RE_LINK = re.compile(r"(?:https?://)?(?:t|telegram)\.me/([a-z][\w]{3,31})(?:/\d+)?/?$", re.I)
_RE_USER = re.compile(r"@?([a-z][\w]{3,31})$", re.I)
_RE_ID = re.compile(r"-?\d{5,16}$")


def normalize_ref(text: str) -> str | None:
    """Foydalanuvchi yozganini yagona ko'rinishga: "invite:HASH" | "user:name" | "id:-100…".

    Tanib bo'lmasa — None.
    """
    t = (text or "").strip()
    if not t:
        return None
    m = _RE_INVITE.search(t)
    if m:
        return f"invite:{m.group(1)}"
    m = _RE_LINK.match(t)
    if m:
        name = m.group(1).lower()
        return None if name in ("joinchat", "addlist", "share") else f"user:{name}"
    if _RE_ID.match(t):
        return f"id:{t}"
    m = _RE_USER.match(t)
    if m and (t.startswith("@") or "_" in t or any(ch.isdigit() for ch in t) or len(t) >= 5):
        return f"user:{m.group(1).lower()}"
    return None


def describe_ref(ref: str) -> str:
    kind, _, value = ref.partition(":")
    if kind == "user":
        return f"@{value}"
    if kind == "invite":
        return "приглашение t.me/+…"
    return value


# ---------------------------------------------------------------- jadval

def add(text: str, backfill: bool = True) -> tuple[int | None, str]:
    """Guruhni ro'yxatga qo'shadi (listener uni ulaydi). Qaytaradi: (id, xato)."""
    ref = normalize_ref(text)
    if ref is None:
        return None, ("Не понял ссылку. Пример: @logistika_uz, https://t.me/logistika_uz "
                      "или приглашение https://t.me/+AbCdEf…")
    with db.connect() as conn:
        row = conn.execute("SELECT id, status FROM sources WHERE ref=?", (ref,)).fetchone()
        if row is not None:
            if row["status"] in ("removed", "error"):
                conn.execute("UPDATE sources SET status='pending', error=NULL,"
                             " updated_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
            return row["id"], ""
        if ref.startswith("id:"):
            same = conn.execute("SELECT id, status FROM sources WHERE chat_id=?",
                                (ref[3:],)).fetchone()
            if same is not None:
                if same["status"] in ("removed", "error"):
                    conn.execute("UPDATE sources SET status='pending', error=NULL,"
                                 " updated_at=CURRENT_TIMESTAMP WHERE id=?", (same["id"],))
                return same["id"], ""
        cur = conn.execute("INSERT INTO sources (ref, backfill) VALUES (?, ?)",
                           (ref, 1 if backfill else 0))
        log.info("Guruh qo'shildi: %s", ref)
        return cur.lastrowid, ""


def get(source_id: int):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()


def list_sources(include_removed: bool = False) -> list:
    q = "SELECT * FROM sources"
    if not include_removed:
        q += " WHERE status <> 'removed'"
    q += " ORDER BY CASE status WHEN 'error' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, id"
    with db.connect() as conn:
        return conn.execute(q).fetchall()


def pending(limit: int = 3) -> list:
    with db.connect() as conn:
        return conn.execute("SELECT * FROM sources WHERE status='pending' ORDER BY id LIMIT ?",
                            (limit,)).fetchall()


def active_chat_ids() -> list[int]:
    with db.connect() as conn:
        rows = conn.execute("SELECT chat_id FROM sources WHERE status='active'"
                            " AND chat_id IS NOT NULL").fetchall()
    out = []
    for r in rows:
        try:
            out.append(int(r["chat_id"]))
        except (TypeError, ValueError):
            pass
    return out


def set_active(source_id: int, chat_id: int | str, title: str | None,
               username: str | None) -> bool:
    """Listener guruhni topdi. Shu chat boshqa qatorda allaqachon faol bo'lsa —
    bu qator takror (o'chiriladi). Qaytaradi: yangi faol guruhmi."""
    chat_id = str(chat_id)
    with db.connect() as conn:
        dup = conn.execute("SELECT id FROM sources WHERE chat_id=? AND status='active' AND id<>?",
                           (chat_id, source_id)).fetchone()
        if dup is not None:
            conn.execute("UPDATE sources SET status='removed', error='уже отслеживается',"
                         " updated_at=CURRENT_TIMESTAMP WHERE id=?", (source_id,))
            return False
        conn.execute("UPDATE sources SET status='active', chat_id=?, title=?, username=?,"
                     " error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     (chat_id, title, (username or None), source_id))
    return True


def set_error(source_id: int, message: str, keep_pending: bool = False) -> None:
    status = "pending" if keep_pending else "error"
    with db.connect() as conn:
        conn.execute("UPDATE sources SET status=?, error=?, updated_at=CURRENT_TIMESTAMP"
                     " WHERE id=?", (status, message[:200], source_id))


def done_backfill(source_id: int) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE sources SET backfill=0 WHERE id=?", (source_id,))


def remove(source_id: int) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE sources SET status='removed', updated_at=CURRENT_TIMESTAMP"
                     " WHERE id=?", (source_id,))


def migrate_from_file(refs: list) -> int:
    """`sources.json` dagi guruhlar — bir marta, jadval bo'sh bo'lsa. Eski
    xabarlari bazada bor, shuning uchun qayta o'qilmaydi (backfill=0)."""
    with db.connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]:
            return 0
    n = 0
    for r in refs or []:
        sid, err = add(str(r), backfill=False)
        if sid and not err:
            n += 1
    if n:
        log.info("sources.json dan %d ta guruh bazaga ko'chirildi", n)
    return n


# ---------------------------------------------------------------- akkaunt guruhlari

def save_dialogs(items: list[dict]) -> None:
    """Akkaunt a'zo bo'lgan guruh/kanallar (listener yozadi). Eski ro'yxat almashtiriladi."""
    with db.connect() as conn:
        conn.execute("DELETE FROM tg_dialogs")
        conn.executemany(
            "INSERT OR REPLACE INTO tg_dialogs (chat_id, title, username, members)"
            " VALUES (?,?,?,?)",
            [(str(d["chat_id"]), d.get("title"), d.get("username") or None, d.get("members"))
             for d in items])


def dialogs(exclude_watched: bool = True) -> list:
    q = "SELECT * FROM tg_dialogs"
    if exclude_watched:
        q += (" WHERE chat_id NOT IN (SELECT chat_id FROM sources WHERE status IN"
              " ('active','pending') AND chat_id IS NOT NULL)")
    q += " ORDER BY COALESCE(members, 0) DESC, title"
    with db.connect() as conn:
        return conn.execute(q).fetchall()


# ---------------------------------------------------------------- holat

def heartbeat() -> None:
    db.set_setting(HEARTBEAT_KEY, db.utc_now().isoformat(" ", "seconds"))


def last_seen_seconds() -> float | None:
    """Listener oxirgi marta qachon "tirik" edi (soniya). Hech qachon — None."""
    from datetime import datetime
    raw = db.get_setting(HEARTBEAT_KEY)
    if not raw:
        return None
    try:
        return (db.utc_now() - datetime.fromisoformat(raw)).total_seconds()
    except ValueError:
        return None


def cargo_stats(days: int = 7) -> dict[str, dict]:
    """Manba nomi (username yoki sarlavha) -> {n: yuklar soni, last: oxirgisi}."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT source, COUNT(*) AS n, MAX(created_at) AS last FROM cargos"
            " WHERE created_at >= ? GROUP BY source", (db._ago(days=days),)).fetchall()
    return {r["source"]: {"n": r["n"], "last": r["last"]} for r in rows}
