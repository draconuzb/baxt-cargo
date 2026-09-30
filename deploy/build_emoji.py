"""
build_emoji.py — panel uchun animatsiyali emoji to'plamini tayyorlash.

    python deploy/build_emoji.py        # static/emoji/ ni qayta yig'adi

Manba: Google Noto Animated Emoji (CC BY 4.0) — fonts.gstatic.com.
Asl fayllar 512 px va og'ir (ba'zilari 1 MB+). Panelda ular 20–36 px
ko'rsatiladi, shuning uchun 72 px (retina uchun 2x) ga kichraytiriladi va
har ikkinchi kadr tashlanadi. Yoniga birinchi kadrdan statik PNG — "harakatni
kamaytirish" yoqilgan telefonlar uchun (`prefers-reduced-motion`).

Faqat dasturchi kompyuterida kerak (Pillow). Server tayyor fayllarni beradi.
"""
from __future__ import annotations

import io
import sys
import urllib.request
from pathlib import Path

from PIL import Image, ImageSequence

SIZE = 72                   # 36 px gacha ko'rsatish uchun 2x (retina)
FRAME_STEP = 2              # har 2-kadr: ravonlik deyarli o'zgarmaydi, hajm 2x kam
BASE = "https://fonts.gstatic.com/s/e/notoemoji/latest/{cp}/512.webp"
OUT = Path(__file__).resolve().parent.parent / "static" / "emoji"

# nom -> Unicode kod (web.anim("nom") shu nom bilan chaqiradi)
EMOJI = {
    "truck": "1f69a",       # 🚚 logo, fura
    "box": "1f4e6",         # 📦 aktiv yuklar
    "fire": "1f525",        # 🔥 bugun kelgan
    "check": "2705",        # ✅ olingan
    "sparkles": "2728",     # ✨ AI
    "robot": "1f916",       # 🤖 AI yordamchi
    "wave": "1f44b",        # 👋 salom / chiqish
    "eyes": "1f440",        # 👀 mos yuk qidirilmoqda
    "sleep": "1f634",       # 😴 o'chirilgan fura
    "snow": "2744_fe0f",    # ❄️ ref
    "flag": "1f3c1",        # 🏁 reys tugadi
    "party": "1f389",       # 🎉 reys olindi
    "sun": "1f31e",         # 🌞 kunduz
    "star": "1f31f",        # 🌟 kechqurun
    "chart": "1f4ca",       # 📊 statistika
    "brain": "1f9e0",       # 🧠 qoidalar
    "bell": "1f514",        # 🔔 kuzatuvlar
    "gear": "2699_fe0f",    # ⚙️ sozlamalar
    "money": "1f4b8",       # 💸 narx
    "alarm": "23f0",        # ⏰ keyinroq bo'shaydi
}


def fetch(cp: str) -> bytes:
    req = urllib.request.Request(BASE.format(cp=cp), headers={"User-Agent": "baxt-cargo/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


# Og'ir animatsiyalar uchun alohida sozlama: (o'lcham, kadr qadami)
OVERRIDES = {"snow": (40, 3), "gear": (60, 3), "flag": (48, 3)}
# 🚚 asl animatsiyada kadrdan chiqib ketib qaytadi — kartochkada va reys
# chizig'ida "yo'qolib qoladi". Faqat to'liq ko'rinib turgan kadrlar qoladi.
KEEP_WHOLE = {"truck"}


def build(name: str, cp: str) -> tuple[int, int]:
    size, step = OVERRIDES.get(name, (SIZE, FRAME_STEP))
    src = Image.open(io.BytesIO(fetch(cp)))
    frames, durations = [], []
    for i, frame in enumerate(ImageSequence.Iterator(src)):
        if i % step:
            if durations:
                durations[-1] += frame.info.get("duration", 40)   # vaqt saqlanadi
            continue
        img = frame.convert("RGBA")
        if name in KEEP_WHOLE:
            box = img.getchannel("A").getbbox()
            if box is None or box[2] - box[0] < img.width * 0.9:
                continue
        frames.append(img.resize((size, size), Image.LANCZOS))
        durations.append(frame.info.get("duration", 40))
    webp = OUT / f"{name}.webp"
    frames[0].save(webp, save_all=True, append_images=frames[1:], duration=durations,
                   loop=0, quality=80, method=4)
    frames[0].save(OUT / f"{name}.png", optimize=True)
    return webp.stat().st_size, len(frames)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[1:])
    for name, cp in EMOJI.items():
        if only and name not in only:
            continue
        size, n = build(name, cp)
        print(f"{name:10} {cp:10} {n:3} kadr  {size // 1024:4} KB", flush=True)
    (OUT / "LICENSE.txt").write_text(
        "Animated emoji: Google Noto Emoji Animation, CC BY 4.0\n"
        "https://googlefonts.github.io/noto-emoji-animation/\n", encoding="utf-8")


if __name__ == "__main__":
    main()
