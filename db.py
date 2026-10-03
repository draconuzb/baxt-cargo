"""
db.py — SQLite baza. Bosqich 1–3 uchun yetarli; keyin PostgreSQL ga
ko'chirish oson (SQL deyarli bir xil).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS cargos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint   TEXT UNIQUE,
    from_city     TEXT,
    to_city       TEXT,
    via           TEXT,
    load_date     TEXT,
    weight_t      REAL,
    body_type     TEXT,
    temp_c        REAL,
    rate          REAL,
    currency      TEXT,
    rate_usd      REAL,
    phone         TEXT,
    username      TEXT,
    source        TEXT,
    source_msg_id INTEGER,
    posted_at     TEXT,
    confidence    REAL,
    raw_text      TEXT,
    status        TEXT DEFAULT 'new',   -- new | taken | skipped | expired
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cargo_route ON cargos(from_city, to_city, load_date);
CREATE INDEX IF NOT EXISTS idx_cargo_status ON cargos(status, created_at);

CREATE TABLE IF NOT EXISTS trucks (
    id            TEXT PRIMARY KEY,     -- "01", "02" ...
    plate         TEXT,
    driver        TEXT,
    driver_phone  TEXT,
    body_type     TEXT,                 -- ref | tent | izoterm
    capacity_t    REAL,
    temp_min      REAL,
    temp_max      REAL,
    current_city  TEXT,
    free_date     TEXT,
    preferred_dir TEXT,                 -- "RU", "KZ", "UZ" yoki bo'sh
    fuel_l_100km  REAL,
    active        INTEGER DEFAULT 1,
    updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

-- GPS nuqtalari: haydovchi "Live Location" yuborganda yoki treker API'sidan
CREATE TABLE IF NOT EXISTS gps_positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    truck_id    TEXT,
    lat         REAL,
    lon         REAL,
    source      TEXT,                 -- telegram | wialon | file
    recorded_at TEXT,                 -- signal vaqti
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_gps_truck ON gps_positions(truck_id, id);

-- Dispetcher so'ragan yo'nalishlar: "Toshkent-Moskva yuk topib ber".
-- Shunday yuk chiqsa, ball chegarasidan qat'i nazar darhol xabar beriladi.
CREATE TABLE IF NOT EXISTS watches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_city   TEXT,
    to_city     TEXT,
    body_type   TEXT,
    query       TEXT,                 -- dispetcher yozgan asl matn
    chat_id     TEXT,
    active      INTEGER DEFAULT 1,
    hits        INTEGER DEFAULT 0,    -- nechta yuk topib berildi
    expires_at  TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Panelda o'zgartirilgan sozlamalar (config.py ustidan qo'yiladi)
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Kompaniya qoidalari (rules.py): "Rossiyadan 30 mln dan arzon olma"
CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    effect      TEXT,                 -- block | penalty | boost
    scope       TEXT,                 -- JSON: qaysi yuklarga tegishli
    require     TEXT,                 -- JSON: shart (bajarilmasa qoida ishlaydi)
    points      REAL DEFAULT 0,
    text        TEXT,                 -- rahbar aytgan asl gap
    source      TEXT DEFAULT 'user',  -- user | learned
    status      TEXT DEFAULT 'active',-- active | proposed | rejected | off
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

-- AI xotirasi: qoidaga sig'maydigan faktlar ("01 haydovchisi Qozog'istonga bormaydi")
CREATE TABLE IF NOT EXISTS memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    note        TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

-- AI suhbat tarixi (faqat savol va yakuniy javob — token tejaladi)
CREATE TABLE IF NOT EXISTS ai_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT,
    role        TEXT,                 -- user | assistant
    content     TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ai_chat ON ai_messages(chat_id, id);

CREATE TABLE IF NOT EXISTS matches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cargo_id    INTEGER,
    truck_id    TEXT,
    score       REAL,
    empty_km    REAL,
    loaded_km   REAL,
    margin_usd  REAL,
    details     TEXT,
    notified    INTEGER DEFAULT 0,
    decision    TEXT,                   -- taken | skipped | NULL
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(cargo_id, truck_id)
);
"""


def utc_now() -> datetime:
    """Baza vaqti bilan bir xil vaqt (UTC).

    SQLite'ning `CURRENT_TIMESTAMP` i UTC da yozadi. Agar Python tomonda
    mahalliy vaqt bilan solishtirilsa, Toshkentda (UTC+5) barcha vaqt
    oynalari 5 soatga qisqarib qoladi: 36 soatlik dubl filtri 31 soat,
    24 soatlik eskirish esa 19 soat bo'lib ishlaydi. Shuning uchun
    hamma taqqoslash shu funksiya orqali.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ago(hours: float = 0, days: float = 0) -> str:
    return (utc_now() - timedelta(hours=hours, days=days)).isoformat(" ", "seconds")


class _Conn(sqlite3.Connection):
    """`with connect() as conn:` — commit/rollback VA yopish.

    Oddiy `sqlite3.Connection` ning `with` bloki faqat commit qiladi, ulanishni
    yopmaydi: fayl deskriptori axlat yig'uvchi kelguncha ochiq qoladi. Listener
    kuniga minglab xabar o'qiydi — 1024 chegarasiga yetib, "unable to open
    database file" bilan yuklar yo'qolardi (2026-09-30 da serverda ko'rildi).
    """

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


# Uchta process (listener, bot, panel) bitta bazaga yozadi — band bo'lsa kutamiz
BUSY_TIMEOUT_SEC = 15


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=BUSY_TIMEOUT_SEC, factory=_Conn)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with connect() as conn:
        # WAL: o'qish yozishni kutmaydi — bir necha process uchun qulay
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Eski bazaga yangi ustunlarni qo'shadi.

    Ishlab turgan tizimda bazani qayta yaratib bo'lmaydi — `cargos.raw_text`
    yo'qoladi. Shuning uchun har bir yangi ustun shu yerga qo'shiladi.
    """
    extra = {
        "trucks": {
            "pos_source": "TEXT",          # manual | gps | trip
            "pos_updated_at": "TEXT",      # holat oxirgi marta qachon yangilandi
            "tg_user_id": "INTEGER",       # haydovchining Telegram id'si (Live Location)
        },
        "matches": {
            "actual_margin_usd": "REAL",   # reys tugagandagi haqiqiy marja
            "decided_at": "TEXT",
            # Reys: olingan paytdagi fura holati (bekor qilinsa — qaytariladi)
            "prev_city": "TEXT",
            "prev_free_date": "TEXT",
            "finished_at": "TEXT",         # reys tugadi (NULL — hali yo'lda)
        },
    }
    for table, columns in extra.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, coltype in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")


# ---------------------------------------------------------------- cargos

def insert_cargo(cargo, fingerprint: str) -> int | None:
    """Yangi yukni yozadi. Dubl bo'lsa None qaytaradi."""
    rate_usd = config.to_usd(cargo.rate, cargo.currency)
    with connect() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO cargos (fingerprint, from_city, to_city, via, load_date,
                   weight_t, body_type, temp_c, rate, currency, rate_usd, phone, username,
                   source, source_msg_id, posted_at, confidence, raw_text)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fingerprint, cargo.from_city, cargo.to_city, json.dumps(cargo.via),
                 cargo.load_date.isoformat() if cargo.load_date else None,
                 cargo.weight_t, cargo.body_type, cargo.temp_c, cargo.rate,
                 cargo.currency, rate_usd, cargo.phone, cargo.username,
                 cargo.source, cargo.source_msg_id,
                 cargo.posted_at.isoformat() if cargo.posted_at else None,
                 cargo.confidence, cargo.raw_text),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def recent_cargos(hours: int = 48) -> list[sqlite3.Row]:
    since = _ago(hours=hours)
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM cargos WHERE created_at >= ? ORDER BY id DESC", (since,)
        ).fetchall()


def active_cargos(hours: int = 24) -> list[sqlite3.Row]:
    since = _ago(hours=hours)
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM cargos WHERE status='new' AND created_at >= ? ORDER BY id DESC",
            (since,),
        ).fetchall()


def get_cargo(cargo_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM cargos WHERE id=?", (cargo_id,)).fetchone()


def set_cargo_status(cargo_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE cargos SET status=? WHERE id=?", (status, cargo_id))


def claim_cargo(cargo_id: int) -> bool:
    """Yukni "olingan" deb belgilaydi. Bitta yuk ikki marta olinmasin:
    holat faqat `new` bo'lsa o'zgaradi, aks holda False qaytadi.

    Ikki dispetcher bir vaqtda tugmani bossa ham faqat bittasi yutadi —
    shart SQL ichida tekshiriladi.
    """
    with connect() as conn:
        cur = conn.execute(
            "UPDATE cargos SET status='taken' WHERE id=? AND status='new'", (cargo_id,))
        return cur.rowcount > 0


def expire_old_cargos(hours: int = 24) -> int:
    """Eskirgan e'lonlarni `expired` ga o'tkazadi (TZ 5.8.3).

    Yuk e'loni 10–20 daqiqa yashaydi; bir kundan keyin u deyarli aniq
    olingan. Bunday yuklar taklif qilinmasin.
    """
    until = _ago(hours=hours)
    with connect() as conn:
        cur = conn.execute(
            "UPDATE cargos SET status='expired' WHERE status='new' AND created_at < ?",
            (until,))
        return cur.rowcount


# ---------------------------------------------------------------- trucks

def upsert_truck(t: dict) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO trucks (id, plate, driver, driver_phone, body_type, capacity_t,
               temp_min, temp_max, current_city, free_date, preferred_dir, fuel_l_100km, active)
               VALUES (:id,:plate,:driver,:driver_phone,:body_type,:capacity_t,:temp_min,
                       :temp_max,:current_city,:free_date,:preferred_dir,:fuel_l_100km,
                       COALESCE(:active,1))
               ON CONFLICT(id) DO UPDATE SET
                 plate=excluded.plate, driver=excluded.driver,
                 driver_phone=excluded.driver_phone, body_type=excluded.body_type,
                 capacity_t=excluded.capacity_t, temp_min=excluded.temp_min,
                 temp_max=excluded.temp_max, current_city=excluded.current_city,
                 free_date=excluded.free_date, preferred_dir=excluded.preferred_dir,
                 fuel_l_100km=excluded.fuel_l_100km, active=excluded.active,
                 updated_at=CURRENT_TIMESTAMP""",
            {**{k: None for k in ("plate", "driver", "driver_phone", "preferred_dir",
                                  "temp_min", "temp_max", "active")}, **t},
        )


def get_trucks(active_only: bool = True) -> list[sqlite3.Row]:
    q = "SELECT * FROM trucks" + (" WHERE active=1" if active_only else "")
    with connect() as conn:
        return conn.execute(q).fetchall()


def get_truck(truck_id: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM trucks WHERE id=?", (truck_id,)).fetchone()


def set_truck_position(truck_id: str, city: str | None = None,
                       free_date: str | None = None,
                       source: str = "manual") -> None:
    """Mashina holatini yangilaydi.

    `source` — kim yangiladi: `manual` (dispetcher), `gps`, `trip`
    (reys olingandan keyin avtomatik). GPS qo'lda kiritilgan holatni
    darhol bosib ketmasligi uchun kerak (TZ 5.5).
    """
    sets, vals = [], []
    if city is not None:
        sets.append("current_city=?")
        vals.append(city)
    if free_date is not None:
        sets.append("free_date=?")
        vals.append(free_date)
    if not sets:
        return
    sets += ["pos_source=?", "pos_updated_at=CURRENT_TIMESTAMP",
             "updated_at=CURRENT_TIMESTAMP"]
    vals.append(source)
    vals.append(truck_id)
    with connect() as conn:
        conn.execute(f"UPDATE trucks SET {', '.join(sets)} WHERE id=?", vals)


# Panelda tahrirlanadigan maydonlar. Ro'yxat ataylab qat'iy: ustun nomi
# SQL ga to'g'ridan-to'g'ri qo'yiladi, shuning uchun faqat shu kalitlar.
TRUCK_EDITABLE = ("plate", "driver", "driver_phone", "capacity_t", "body_type",
                  "temp_min", "temp_max", "preferred_dir", "fuel_l_100km", "active")


def update_truck(truck_id: str, **fields) -> None:
    """Mashina ma'lumotlarini yangilaydi (holatdan tashqari — uni
    `set_truck_position` qiladi, chunki u GPS ustunligini belgilaydi)."""
    fields = {k: v for k, v in fields.items() if k in TRUCK_EDITABLE}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE trucks SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     [*fields.values(), truck_id])


# ---------------------------------------------------------------- GPS

def link_truck_tg_user(truck_id: str, tg_user_id: int) -> bool:
    """Haydovchining Telegram akkauntini mashinaga bog'laydi."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE trucks SET tg_user_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (tg_user_id, truck_id))
        return cur.rowcount > 0


def truck_by_tg_user(tg_user_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM trucks WHERE tg_user_id=?",
                            (tg_user_id,)).fetchone()


def save_gps_position(truck_id: str, lat: float, lon: float,
                      source: str = "telegram",
                      recorded_at: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO gps_positions (truck_id, lat, lon, source, recorded_at)"
            " VALUES (?,?,?,?,?)",
            (truck_id, lat, lon, source,
             recorded_at or utc_now().isoformat(" ", "seconds")))


def latest_gps_positions() -> dict[str, sqlite3.Row]:
    """Har bir mashina uchun eng so'nggi nuqta."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT g.* FROM gps_positions g
               JOIN (SELECT truck_id, MAX(id) AS last_id
                     FROM gps_positions GROUP BY truck_id) t
                 ON g.id = t.last_id""").fetchall()
    return {r["truck_id"]: r for r in rows}


def trim_gps_positions(keep_days: int = 14) -> int:
    """Eski nuqtalarni tozalaydi — baza cheksiz o'smasin."""
    until = _ago(days=keep_days)
    with connect() as conn:
        return conn.execute("DELETE FROM gps_positions WHERE created_at < ?",
                            (until,)).rowcount


# ---------------------------------------------------------------- matches

def save_match(cargo_id: int, truck_id: str, result: dict) -> int | None:
    with connect() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO matches (cargo_id, truck_id, score, empty_km, loaded_km,
                   margin_usd, details) VALUES (?,?,?,?,?,?,?)""",
                (cargo_id, truck_id, result["score"], result["empty_km"],
                 result["loaded_km"], result["margin_usd"], json.dumps(result, default=str)),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def mark_notified(match_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE matches SET notified=1 WHERE id=?", (match_id,))


def set_decision(match_id: int, decision: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE matches SET decision=?, decided_at=CURRENT_TIMESTAMP WHERE id=?",
            (decision, match_id))


def set_actual_margin(match_id: int, margin_usd: float) -> None:
    """Reys tugagach dispetcher haqiqiy marjani kiritadi — prognozni
    shu bilan solishtiramiz (TZ 5.6.4)."""
    with connect() as conn:
        conn.execute("UPDATE matches SET actual_margin_usd=? WHERE id=?",
                     (margin_usd, match_id))


def set_trip_prev(match_id: int, city: str | None, free_date: str | None) -> None:
    with connect() as conn:
        conn.execute("UPDATE matches SET prev_city=?, prev_free_date=? WHERE id=?",
                     (city, free_date, match_id))


_TRIP_SELECT = """SELECT m.*, c.from_city, c.to_city, c.load_date, c.rate, c.currency,
                         c.rate_usd, c.weight_t, c.body_type, c.phone, c.username, c.source
                  FROM matches m JOIN cargos c ON c.id = m.cargo_id"""


def active_trips(truck_id: str | None = None) -> list[sqlite3.Row]:
    """Yo'ldagi reyslar (olingan, hali tugamagan) — eskisidan yangisiga."""
    q = _TRIP_SELECT + " WHERE m.decision='taken' AND m.finished_at IS NULL"
    params: list = []
    if truck_id:
        q += " AND m.truck_id=?"
        params.append(truck_id)
    q += " ORDER BY COALESCE(m.decided_at, m.created_at)"
    with connect() as conn:
        return conn.execute(q, params).fetchall()


def finished_trips(days: int = 180, limit: int = 300) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            _TRIP_SELECT + " WHERE m.decision='taken' AND m.finished_at IS NOT NULL"
            " AND m.finished_at >= ? ORDER BY m.finished_at DESC LIMIT ?",
            (_ago(days=days), limit)).fetchall()


def finish_trip(match_id: int, actual_margin: float | None = None) -> None:
    with connect() as conn:
        conn.execute("UPDATE matches SET finished_at=CURRENT_TIMESTAMP,"
                     " actual_margin_usd=COALESCE(?, actual_margin_usd) WHERE id=?",
                     (actual_margin, match_id))


def get_match(match_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()


def taken_truck_for_cargo(cargo_id: int) -> str | None:
    """Bu yukni qaysi mashina olgan (ikkinchi marta bosilganda kerak)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT truck_id FROM matches WHERE cargo_id=? AND decision='taken'",
            (cargo_id,)).fetchone()
        return row["truck_id"] if row else None


def top_matches(limit: int = 10, min_score: float = 0.0,
                truck_id: str | None = None) -> list[sqlite3.Row]:
    """Qaror qabul qilinmagan eng yaxshi mosliklar (bot /list, veb-panel)."""
    q = """SELECT m.*, c.from_city, c.to_city, c.rate, c.currency, c.rate_usd,
                  c.load_date, c.body_type, c.temp_c, c.weight_t, c.phone,
                  c.username, c.source, c.status
           FROM matches m JOIN cargos c ON c.id = m.cargo_id
           WHERE c.status='new' AND m.decision IS NULL AND m.score >= ?"""
    params: list = [min_score]
    if truck_id:
        q += " AND m.truck_id = ?"
        params.append(truck_id)
    q += " ORDER BY m.score DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        return conn.execute(q, params).fetchall()


def cancel_other_matches(cargo_id: int, keep_match_id: int) -> int:
    """Yuk olingandan keyin qolgan mashinalarning mosligini bekor qiladi.

    `cancelled` ataylab `skipped` dan ajratilgan: `skipped` — dispetcherning
    ongli qarori (statistika uchun qimmatli), `cancelled` — tizimning
    avtomatik tozalashi.
    """
    with connect() as conn:
        cur = conn.execute(
            "UPDATE matches SET decision='cancelled'"
            " WHERE cargo_id=? AND id<>? AND decision IS NULL",
            (cargo_id, keep_match_id))
        return cur.rowcount


# ---------------------------------------------------------------- panel

def search_cargos(from_city: str | None = None, to_city: str | None = None,
                  body_type: str | None = None, status: str | None = None,
                  min_score: float | None = None, q: str | None = None,
                  hours: int | None = None, limit: int = 200) -> list[sqlite3.Row]:
    """Yuklar jadvali uchun filtrlangan ro'yxat (eng yaxshi ball bilan)."""
    where, params = ["1=1"], []
    if from_city:
        where.append("c.from_city = ?")
        params.append(from_city)
    if to_city:
        where.append("c.to_city = ?")
        params.append(to_city)
    if body_type:
        where.append("c.body_type = ?")
        params.append(body_type)
    if status:
        where.append("c.status = ?")
        params.append(status)
    if q:
        where.append("c.raw_text LIKE ?")
        params.append(f"%{q}%")
    if hours:
        where.append("c.created_at >= ?")
        params.append(_ago(hours=hours))

    sql = f"""SELECT c.*, MAX(m.score) AS best_score, COUNT(m.id) AS match_count
              FROM cargos c LEFT JOIN matches m ON m.cargo_id = c.id
              WHERE {' AND '.join(where)}
              GROUP BY c.id"""
    if min_score is not None:
        sql += " HAVING best_score >= ?"
        params.append(min_score)
    sql += " ORDER BY c.id DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def matches_for_cargo(cargo_id: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM matches WHERE cargo_id=? ORDER BY score DESC",
            (cargo_id,)).fetchall()


def taken_matches(days: int = 90, limit: int = 300) -> list[sqlite3.Row]:
    """Olingan reyslar — prognoz va haqiqiy marja bilan (/history)."""
    with connect() as conn:
        return conn.execute(
            """SELECT m.*, c.from_city, c.to_city, c.load_date, c.rate,
                      c.currency, c.rate_usd, c.source
               FROM matches m JOIN cargos c ON c.id = m.cargo_id
               WHERE m.decision='taken' AND m.created_at >= ?
               ORDER BY COALESCE(m.decided_at, m.created_at) DESC LIMIT ?""",
            (_ago(days=days), limit)).fetchall()


def counters() -> dict:
    """Bosh sahifadagi qisqa ko'rsatkichlar."""
    with connect() as conn:
        row = conn.execute(
            """SELECT
                 (SELECT COUNT(*) FROM cargos WHERE status='new'
                    AND created_at >= ?) AS active,
                 (SELECT COUNT(*) FROM cargos WHERE created_at >= ?) AS today,
                 (SELECT COUNT(*) FROM matches WHERE notified=1
                    AND created_at >= ?) AS notified_today,
                 (SELECT COUNT(*) FROM matches WHERE decision='taken'
                    AND created_at >= ?) AS taken_week""",
            (_ago(hours=24), _ago(hours=24), _ago(hours=24), _ago(days=7))).fetchone()
    return {k: row[k] or 0 for k in row.keys()}


# ---------------------------------------------------------------- kuzatuvlar

WATCH_DAYS = 7


def add_watch(from_city: str | None, to_city: str | None, body_type: str | None,
              query: str = "", chat_id: str | None = None,
              days: int = WATCH_DAYS) -> int:
    """Dispetcher so'ragan yo'nalishni kuzatuvga qo'yadi.

    Muddat qo'yiladi: unutilgan so'rov oylab xabar yuborib turmasin.
    """
    expires = (utc_now() + timedelta(days=days)).isoformat(" ", "seconds")
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO watches (from_city, to_city, body_type, query,
                                    chat_id, expires_at)
               VALUES (?,?,?,?,?,?)""",
            (from_city, to_city, body_type, query, str(chat_id) if chat_id else None,
             expires))
        return cur.lastrowid


def active_watches() -> list[sqlite3.Row]:
    now = utc_now().isoformat(" ", "seconds")
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM watches WHERE active=1 AND (expires_at IS NULL"
            " OR expires_at > ?) ORDER BY id", (now,)).fetchall()


def delete_watch(watch_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM watches WHERE id=?",
                            (watch_id,)).rowcount > 0


def touch_watch(watch_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE watches SET hits = hits + 1 WHERE id=?", (watch_id,))


def cancel_truck_matches(truck_id: str, keep_match_id: int) -> int:
    """Fura reys olgach — uning boshqa ochiq takliflari eskirdi (ular furaning
    ESKI joyi va bo'shash sanasi bo'yicha hisoblangan). Bitta fura ikki reysga
    biriktirilib qolmasligi uchun ular `cancelled` bo'ladi."""
    with connect() as conn:
        return conn.execute(
            "UPDATE matches SET decision='cancelled'"
            " WHERE truck_id=? AND id<>? AND decision IS NULL",
            (truck_id, keep_match_id)).rowcount


def revive_match(cargo_id: int, truck_id: str, result: dict) -> int | None:
    """Tizim bekor qilgan taklifni YANGI hisob bilan qayta ochadi (AI qayta
    taklif qilganda). Dispetcher qarori (`taken`/`skipped`) hech qachon tiklanmaydi."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM matches WHERE cargo_id=? AND truck_id=? AND decision='cancelled'",
            (cargo_id, truck_id)).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE matches SET decision=NULL, decided_at=NULL, notified=0, score=?,"
            " empty_km=?, loaded_km=?, margin_usd=?, details=?,"
            " created_at=CURRENT_TIMESTAMP WHERE id=?",
            (result["score"], result["empty_km"], result["loaded_km"], result["margin_usd"],
             json.dumps(result, default=str), row["id"]))
        return row["id"]


def update_match_calc(match_id: int, result: dict) -> None:
    """Ochiq taklifning hisobini yangilaydi (hisob qoidasi o'zgarganda — `rescore`)."""
    with connect() as conn:
        conn.execute(
            "UPDATE matches SET score=?, empty_km=?, loaded_km=?, margin_usd=?, details=?"
            " WHERE id=? AND decision IS NULL",
            (result["score"], result["empty_km"], result["loaded_km"], result["margin_usd"],
             json.dumps(result, default=str), match_id))


def find_match(cargo_id: int, truck_id: str) -> sqlite3.Row | None:
    """Shu yuk + mashina uchun qaror qabul qilinmagan moslik (panel tugmalari)."""
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM matches WHERE cargo_id=? AND truck_id=? AND decision IS NULL",
            (cargo_id, truck_id)).fetchone()


def truck_history(truck_id: str, limit: int = 20) -> list[sqlite3.Row]:
    """Mashinaning oxirgi reyslari."""
    with connect() as conn:
        return conn.execute(
            """SELECT m.*, c.from_city, c.to_city, c.load_date, c.rate_usd
               FROM matches m JOIN cargos c ON c.id = m.cargo_id
               WHERE m.truck_id = ? AND m.decision = 'taken'
               ORDER BY COALESCE(m.decided_at, m.created_at) DESC LIMIT ?""",
            (truck_id, limit)).fetchall()


def last_gps(truck_id: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM gps_positions WHERE truck_id=? ORDER BY id DESC LIMIT 1",
            (truck_id,)).fetchone()


# ---------------------------------------------------------------- dispetcher chatlari

def dispatcher_chats() -> list[str]:
    """Bildirishnoma boradigan chatlar. /start bosilganda avtomatik qo'shiladi —
    chat id ni qo'lda qidirish shart emas."""
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='dispatcher_chats'").fetchone()
    if not row or not row["value"]:
        return []
    try:
        return [str(c) for c in json.loads(row["value"])]
    except (ValueError, TypeError):
        return []


def add_dispatcher_chat(chat_id) -> bool:
    """Chatni ro'yxatga qo'shadi. Yangi qo'shilgani True qaytaradi."""
    chats = dispatcher_chats()
    if str(chat_id) in chats:
        return False
    chats.append(str(chat_id))
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('dispatcher_chats',?,CURRENT_TIMESTAMP)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (json.dumps(chats),))
    return True


def remove_dispatcher_chat(chat_id) -> None:
    chats = [c for c in dispatcher_chats() if c != str(chat_id)]
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('dispatcher_chats',?,CURRENT_TIMESTAMP)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (json.dumps(chats),))


# ---------------------------------------------------------------- AI xotirasi

def add_memory(note: str) -> int:
    with connect() as conn:
        return conn.execute("INSERT INTO memory (note) VALUES (?)",
                            (note.strip()[:500],)).lastrowid


def memories(limit: int = 50) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM memory ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()


def delete_memory(memory_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM memory WHERE id=?",
                            (memory_id,)).rowcount > 0


def add_ai_message(chat_id, role: str, content: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO ai_messages (chat_id, role, content) VALUES (?,?,?)",
                     (str(chat_id), role, content[:4000]))


def ai_history(chat_id, limit: int = 8, hours: float = 12) -> list[sqlite3.Row]:
    """Oxirgi suhbat (eskisi kerak emas — kontekst va token isrof bo'lmasin)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM ai_messages WHERE chat_id=? AND created_at >= ?"
            " ORDER BY id DESC LIMIT ?", (str(chat_id), _ago(hours=hours), limit)).fetchall()
    return list(reversed(rows))


def clear_ai_history(chat_id) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM ai_messages WHERE chat_id=?", (str(chat_id),))


# ---------------------------------------------------------------- oddiy kalit-qiymat

def get_setting(key: str) -> str | None:
    """Matnli holat (masalan, ertalabki reja oxirgi marta qachon yuborilgan)."""
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,CURRENT_TIMESTAMP)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (key, str(value)))


def day_summary(hours: int = 24) -> dict:
    """Oxirgi sutka: nechta yuk keldi, nechta reys olindi, qancha marja."""
    since = _ago(hours=hours)
    with connect() as conn:
        row = conn.execute(
            """SELECT
                 (SELECT COUNT(*) FROM cargos WHERE created_at >= ?) AS cargos,
                 (SELECT COUNT(*) FROM cargos WHERE status='new') AS active,
                 (SELECT COUNT(*) FROM matches WHERE decision='taken'
                    AND COALESCE(decided_at, created_at) >= ?) AS taken,
                 (SELECT SUM(margin_usd) FROM matches WHERE decision='taken'
                    AND COALESCE(decided_at, created_at) >= ?) AS margin""",
            (since, since, since)).fetchone()
    return {k: row[k] or 0 for k in row.keys()}
