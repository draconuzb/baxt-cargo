"""
dedup.py — bir xil yuk turli guruhlarda va turli so'zlar bilan qayta-qayta
chiqadi. Uch bosqichda filtrlaymiz:

1. fingerprint  — yo'nalish + sana + vazn + stavka bo'yicha qat'iy kalit
2. telefon      — bir xil raqam + bir xil yo'nalish = bir xil yuk
3. matn o'xshashligi — 90%+ o'xshash matn (forward qilingan e'lon)
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

import db

try:
    from rapidfuzz import fuzz

    def _sim(a: str, b: str) -> float:
        return fuzz.token_set_ratio(a, b) / 100.0
except ImportError:
    from difflib import SequenceMatcher

    def _sim(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()


def fingerprint(c) -> str:
    """Yuk uchun qat'iy kalit. Stavka 50$ aniqlikda yaxlitlanadi."""
    rate_bucket = ""
    if c.rate and c.currency:
        import config
        usd = config.to_usd(c.rate, c.currency) or 0
        rate_bucket = str(int(usd // 50))
    parts = [
        (c.from_city or "?").lower(),
        (c.to_city or "?").lower(),
        c.load_date.isoformat() if c.load_date else "?",
        str(int(c.weight_t)) if c.weight_t else "?",
        c.body_type or "?",
        rate_bucket,
    ]
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:20]


def _clean(text: str) -> str:
    t = re.sub(r"@\w+|https?://\S+", " ", text.lower())
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _same_day(cargo_date, row_date: str | None) -> bool:
    """Sanalar to'qnashmaydimi.

    Ikkalasi ham ma'lum va boshqa bo'lsa — bu boshqa yuk (bir ekspeditor
    bir yo'nalishga har hafta yuk chiqaradi). Biri noma'lum bo'lsa —
    to'qnashmadi deb hisoblaymiz, aks holda dublni o'tkazib yuboramiz.
    """
    if not cargo_date or not row_date:
        return True
    return cargo_date.isoformat() == str(row_date)[:10]


def is_duplicate(c, window_hours: int = 36, sim_threshold: float = 0.90) -> tuple[bool, int | None]:
    """(dubl_mi, mavjud_yuk_id) qaytaradi."""
    fp = fingerprint(c)
    rows = db.recent_cargos(hours=window_hours)

    for r in rows:
        if r["fingerprint"] == fp:
            return True, r["id"]

    # bir xil telefon + bir xil yo'nalish + bir xil kun
    if c.phone:
        for r in rows:
            if r["phone"] == c.phone and r["from_city"] == c.from_city \
                    and r["to_city"] == c.to_city \
                    and _same_day(c.load_date, r["load_date"]):
                return True, r["id"]

    # forward qilingan / ozgina o'zgartirilgan matn
    mine = _clean(c.raw_text)
    if len(mine) > 40:
        for r in rows:
            if r["from_city"] != c.from_city or r["to_city"] != c.to_city:
                continue
            if not _same_day(c.load_date, r["load_date"]):
                continue
            if _sim(mine, _clean(r["raw_text"] or "")) >= sim_threshold:
                return True, r["id"]

    return False, None
