"""
conftest.py — testlar uchun umumiy sozlama.

Har bir test alohida vaqtinchalik bazada ishlaydi: ishchi `cargo.db` ga
hech qachon tegilmaydi.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import db  # noqa: E402

ADS_FILE = Path(__file__).parent / "ads.jsonl"

# Parser aniqligini yig'ib borish uchun (oxirida konsolga chiqadi)
ACCURACY = {"ads": 0, "ads_ok": 0, "fields": 0, "fields_ok": 0, "misses": []}


def load_ads() -> list[dict]:
    """ads.jsonl — real e'lon matnlari va kutilgan natijalar."""
    ads = []
    for line in ADS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            ads.append(json.loads(line))
    return ads


def ad_today(ad: dict) -> date:
    """E'lon uchun tayanch sana — sanali testlar kalendarga bog'lanmasin."""
    return date.fromisoformat(ad["today"]) if ad.get("today") else date(2026, 9, 20)


@pytest.fixture(autouse=True)
def isolate_db(tmp_path, monkeypatch):
    """Hamma testlar vaqtinchalik bazada ishlaydi.

    Bu majburiy: `scoring` bozor stavkasini `analytics` orqali bazadan
    oladi, demak ishchi `cargo.db` bor-yo'qligi test natijasini
    o'zgartirib yuborishi mumkin edi.
    """
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    try:
        import analytics
        analytics.reset_cache()
    except ImportError:
        pass
    import settings
    settings.reset_cache()
    import rules
    rules.reset_cache()
    # Haqiqiy AI kalitlari (.env dan) testlarga o'tmasin: aks holda oddiy bot
    # testi pullik/limitli tashqi so'rov yuborib yuboradi. AI testlari
    # kalitni o'zi qo'yadi va modelni soxtasi bilan almashtiradi.
    for key in ("MISTRAL_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
                "AI_PROVIDERS", "AI_MAX_CALLS_PER_DAY"):
        monkeypatch.delenv(key, raising=False)
    import brain
    brain.reset_state()
    # bildirishnoma hisoblagichlari modul darajasida — testlar orasida
    # oqib o'tmasin (aks holda soatlik chegara boshqa testda to'lib qoladi)
    import notifier
    notifier.reset_limits()
    monkeypatch.setattr(notifier, "MIN_SEND_INTERVAL", 0.0)


@pytest.fixture
def clean_db(isolate_db):
    """Bo'sh vaqtinchalik baza. `db` moduli o'zini qaytaradi."""
    db.init()
    return db


@pytest.fixture
def costs():
    """Testlar uchun qotirilgan xarajat parametrlari.

    config.Costs qiymatlari .env orqali o'zgarishi mumkin — testlar shunga
    bog'lanib qolmasligi kerak.
    """
    return config.Costs()


@pytest.fixture
def truck_tent():
    return {
        "id": "01", "plate": "01 A 111 AA", "driver": "Test",
        "body_type": "tent", "capacity_t": 22.0,
        "temp_min": None, "temp_max": None,
        "current_city": "Toshkent", "free_date": "2026-09-20",
        "preferred_dir": "", "fuel_l_100km": 33.0, "active": 1,
    }


@pytest.fixture
def truck_ref():
    return {
        "id": "02", "plate": "01 B 222 BB", "driver": "Test",
        "body_type": "ref", "capacity_t": 20.0,
        "temp_min": -22.0, "temp_max": 12.0,
        "current_city": "Toshkent", "free_date": "2026-09-20",
        "preferred_dir": "", "fuel_l_100km": 35.0, "active": 1,
    }


def pytest_terminal_summary(terminalreporter):
    """Parser aniqligini hisobot qilib chiqaradi (TZ 5.3 talabi)."""
    a = ACCURACY
    if not a["ads"]:
        return
    tr = terminalreporter
    tr.write_sep("=", "PARSER ANIQLIGI")
    tr.write_line(
        f"E'lonlar: {a['ads_ok']}/{a['ads']} to'liq to'g'ri "
        f"({a['ads_ok'] / a['ads'] * 100:.0f}%)"
    )
    tr.write_line(
        f"Maydonlar: {a['fields_ok']}/{a['fields']} to'g'ri "
        f"({a['fields_ok'] / max(a['fields'], 1) * 100:.1f}%)"
    )
    for m in a["misses"]:
        tr.write_line(f"  ✗ {m}")
