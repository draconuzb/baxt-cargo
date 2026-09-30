"""
translit.py — o'zbek lotin → kirill (AI javobi uchun).

Sinovda hamma modellar kirilldagi o'zbekcha savolga ("01 учун юк топ")
lotinda javob berdi — ko'rsatma yetmadi. Transliteratsiya qat'iy qoidali,
shuning uchun buni dastur o'zi qiladi: 100% ishonchli, tokensiz.

Tegilmaydi: HTML teglar, <code> ichidagi matn (yuk egasiga tayyor xabar),
raqam aralash so'zlar (20t, #4), qisqartmalar (USD, UZS, GPS).
Shahar nomlari lug'atdagi kirill shaklda (Almaty → Алматы, harfma-harf
"Алматй" emas).
"""
from __future__ import annotations

import re

import geo

APOS = "'ʻʼ‘’`"
_SINGLE = {"a": "а", "b": "б", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "ҳ", "i": "и",
           "j": "ж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "қ",
           "r": "р", "s": "с", "t": "т", "u": "у", "v": "в", "x": "х", "y": "й", "z": "з",
           "c": "с", "w": "в"}
_Y_PAIRS = {"yo": "ё", "yu": "ю", "ya": "я", "ye": "е"}
_WORD = re.compile(rf"[A-Za-z{APOS}]+")
_CYRL = re.compile(r"[А-Яа-яЁёЎўҚқҒғҲҳ]")
_LATN = re.compile(r"[A-Za-z]")


def _case(src: str, dst: str) -> str:
    return dst[:1].upper() + dst[1:] if src[:1].isupper() else dst


def word(w: str) -> str:
    """Bitta lotin so'zni kirillga."""
    if len(w) > 1 and w.isupper():
        return w                                  # USD, GPS, RU
    low = w.lower()
    out, i = [], 0
    while i < len(low):
        ch, nxt = low[i], low[i + 1] if i + 1 < len(low) else ""
        after = low[i + 2] if i + 2 < len(low) else ""
        if ch in "og" and nxt and nxt in APOS:
            rep, step = ("ў" if ch == "o" else "ғ"), 2
        elif low[i:i + 2] in ("sh", "ch"):
            rep, step = ("ш" if low[i:i + 2] == "sh" else "ч"), 2
        elif low[i:i + 2] in _Y_PAIRS and not (low[i:i + 2] == "yo" and after in APOS):
            rep, step = _Y_PAIRS[low[i:i + 2]], 2
        elif ch == "e" and i == 0:
            rep, step = "э", 1
        elif ch in APOS:
            rep, step = ("ъ" if 0 < i < len(low) - 1 else ""), 1
        else:
            rep, step = _SINGLE.get(ch, w[i]), 1
        piece = w[i:i + step]
        out.append(rep.upper() if piece[:1].isupper() and rep else rep)
        i += step
    return "".join(out)


_CITY_CACHE: dict[str, str] = {}


def _cities() -> dict[str, str]:
    """Kanonik shahar nomi → kirill shakli."""
    if not _CITY_CACHE:
        for name, _lat, _lon, _country, aliases in geo._RAW:
            cyr = [a for a in aliases.split("|") if _CYRL.search(a)]
            guess = word(name)
            if cyr and guess.lower() not in cyr:
                guess = cyr[0][:1].upper() + cyr[0][1:]
            _CITY_CACHE[name] = guess
    return _CITY_CACHE


def to_cyrillic(text: str) -> str:
    """Matnni kirillga o'giradi (teglar va <code> tegilmaydi)."""
    cities = _cities()
    parts = re.split(r"(<code>.*?</code>|<[^>]+>)", text, flags=re.S)
    out = []
    for part in parts:
        if not part or part.startswith("<"):
            out.append(part)
            continue
        # Avval shahar nomlari (bir nechta so'zli ham bo'lishi mumkin)
        for name in sorted(cities, key=len, reverse=True):
            if name in part:
                part = re.sub(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", cities[name], part)
        out.append(_WORD.sub(lambda m: m.group(0) if _near_digit(part, m) else word(m.group(0)),
                             part))
    return "".join(out)


def _near_digit(text: str, m: re.Match) -> bool:
    """"20t", "4-yuk" kabi raqamga yopishgan so'z — qisqartma, tegmaymiz."""
    before = text[m.start() - 1] if m.start() > 0 else ""
    after = text[m.end()] if m.end() < len(text) else ""
    return before.isdigit() or after.isdigit()


def is_latin(text: str) -> bool:
    plain = re.sub(r"<[^>]+>", "", text)
    return len(_LATN.findall(plain)) > len(_CYRL.findall(plain))
