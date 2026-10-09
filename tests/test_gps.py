"""
test_gps.py — GPS integratsiyasi (TZ 5.5) va `geo.nearest_city`.

Asosiy qoidalar shu yerda qotirilgan:
  • holat avtomatik yangilanadi;
  • 2 soatdan beri signal yo'q bo'lsa — ogohlantirish;
  • qo'lda kiritilgan holat GPS'dan 24 soat ustun turadi.
"""
from __future__ import annotations

import json
from datetime import timedelta, timezone

import pytest

import db
import geo
import gps


class FakeSource:
    """Test uchun GPS manbai."""

    name = "fake"

    def __init__(self, positions):
        self._positions = positions

    def positions(self):
        return self._positions


@pytest.fixture
def park(clean_db, truck_tent, truck_ref):
    for t in (truck_tent, truck_ref):
        clean_db.upsert_truck(t)
    return clean_db


# ---------------------------------------------------------------- nearest_city

def test_nearest_city_exact():
    assert geo.nearest_city(55.7887, 49.1221) == "Qozon"


def test_nearest_city_nearby():
    """Qozondan 40 km narida — baribir Qozon."""
    assert geo.nearest_city(55.95, 49.40) == "Qozon"


def test_nearest_city_too_far_returns_none():
    """Okean o'rtasida shahar yo'q — taxmin qilmaymiz."""
    assert geo.nearest_city(0.0, 0.0) is None


def test_nearest_city_respects_limit():
    """Dashtdagi nuqta: 150 km ichida shahar yo'q."""
    far = geo.nearest_city(46.5, 58.5, max_km=150)        # Ustyurt / Orol bo'yi
    near = geo.nearest_city(46.5, 58.5, max_km=1000)
    assert far is None
    assert near is not None


def test_nearest_city_bad_input():
    assert geo.nearest_city(None, None) is None
    assert geo.nearest_city("x", "y") is None


# ---------------------------------------------------------------- manbalar

def test_telegram_source_reads_last_point(park):
    park.save_gps_position("01", 55.7887, 49.1221, source="telegram")
    park.save_gps_position("01", 41.3111, 69.2797, source="telegram")

    positions = gps.TelegramLiveLocation().positions()
    assert positions["01"][0] == pytest.approx(41.3111)


def test_file_source(tmp_path):
    path = tmp_path / "positions.json"
    path.write_text(json.dumps(
        {"01": {"lat": 55.7887, "lon": 49.1221, "at": "2026-09-20 14:30"}}),
        encoding="utf-8")
    positions = gps.FileSource(path).positions()
    assert positions["01"][0] == pytest.approx(55.7887)


def test_file_source_missing_file(tmp_path):
    assert gps.FileSource(tmp_path / "yoq.json").positions() == {}


def test_file_source_broken_json(tmp_path):
    path = tmp_path / "positions.json"
    path.write_text("{buzuq", encoding="utf-8")
    assert gps.FileSource(path).positions() == {}     # xato — lekin crash emas


def test_wialon_without_token_is_silent(monkeypatch):
    monkeypatch.delenv("WIALON_TOKEN", raising=False)
    assert gps.WialonAdapter(token="").positions() == {}


def test_collect_prefers_newest(park):
    old = gps.db.utc_now() - timedelta(hours=3)
    new = gps.db.utc_now()
    merged = gps.collect([
        FakeSource({"01": (41.3111, 69.2797, old)}),
        FakeSource({"01": (55.7887, 49.1221, new)}),
    ])
    assert merged["01"][0] == pytest.approx(55.7887)


def test_broken_source_does_not_stop_others(park):
    class Broken:
        name = "broken"

        def positions(self):
            raise RuntimeError("treker yiqildi")

    merged = gps.collect([Broken(),
                          FakeSource({"01": (55.7887, 49.1221, gps.db.utc_now())})])
    assert "01" in merged


# ---------------------------------------------------------------- sync

def test_sync_updates_city(park):
    source = FakeSource({"01": (55.7887, 49.1221, db.utc_now())})
    result = gps.sync([source])

    assert result["updated"] == [{"truck_id": "01", "city": "Qozon",
                                  "was": "Toshkent"}]
    truck = db.get_truck("01")
    assert truck["current_city"] == "Qozon"
    assert truck["pos_source"] == "gps"


def test_sync_ignores_unchanged_city(park):
    """Mashina o'sha shaharda — bazaga tegmaymiz."""
    source = FakeSource({"01": (41.3111, 69.2797, db.utc_now())})
    assert gps.sync([source])["updated"] == []
    assert db.get_truck("01")["pos_source"] is None


def test_sync_warns_about_stale_signal(park):
    """TZ mezoni: 2 soatdan ko'p signal yo'q — dispetcherga ogohlantirish."""
    stale = db.utc_now() - timedelta(hours=5)
    result = gps.sync([FakeSource({"01": (55.7887, 49.1221, stale)})])

    assert result["stale"] and result["stale"][0]["truck_id"] == "01"
    assert result["updated"] == []
    assert db.get_truck("01")["current_city"] == "Toshkent"   # eski signalga ishonmaymiz


def test_sync_reports_unknown_area(park):
    """Yaqin atrofda shahar yo'q — holat o'zgarmaydi, lekin xabar qilinadi."""
    result = gps.sync([FakeSource({"01": (0.0, 0.0, db.utc_now())})])
    assert result["unknown"] and result["unknown"][0]["truck_id"] == "01"
    assert db.get_truck("01")["current_city"] == "Toshkent"


def test_manual_position_beats_gps(park):
    """TZ mezoni: qo'lda kiritilgan holat GPS'dan ustun (24 soat)."""
    db.set_truck_position("01", city="Almaty", source="manual")
    result = gps.sync([FakeSource({"01": (55.7887, 49.1221, db.utc_now())})])

    assert result["skipped_manual"] == ["01"]
    assert db.get_truck("01")["current_city"] == "Almaty"


def test_manual_position_expires_after_24h(park):
    """Bir kundan keyin GPS yana ishlaydi."""
    db.set_truck_position("01", city="Almaty", source="manual")
    old = (db.utc_now() - timedelta(hours=30)).isoformat(" ", "seconds")
    with db.connect() as conn:
        conn.execute("UPDATE trucks SET pos_updated_at=? WHERE id='01'", (old,))

    gps.sync([FakeSource({"01": (55.7887, 49.1221, db.utc_now())})])
    assert db.get_truck("01")["current_city"] == "Qozon"


def test_trip_position_is_not_protected(park):
    """Reysdan keyingi holat — taxmin; GPS uni to'g'rilashi kerak."""
    db.set_truck_position("01", city="Moskva", source="trip")
    gps.sync([FakeSource({"01": (55.7887, 49.1221, db.utc_now())})])
    assert db.get_truck("01")["current_city"] == "Qozon"


def test_sync_without_positions_is_noop(park):
    result = gps.sync([FakeSource({})])
    assert result["updated"] == [] and result["stale"] == []


def test_format_report_is_readable(park):
    result = gps.sync([FakeSource({"01": (55.7887, 49.1221, db.utc_now())})])
    report = gps.format_report(result)
    assert "Машина №01" in report and "Qozon" in report


def test_old_points_are_trimmed(park):
    park.save_gps_position("01", 55.7887, 49.1221)
    with db.connect() as conn:
        conn.execute("UPDATE gps_positions SET created_at='2020-01-01 00:00:00'")
    assert db.trim_gps_positions(keep_days=14) == 1
    assert db.latest_gps_positions() == {}


# ---------------------------------------------------------------- Wialon Local

class FakeWialon(gps.WialonAdapter):
    """Tarmoqqa chiqmaydigan Wialon: `_call` o'rniga tayyor javoblar."""

    def __init__(self, items=None, error=None, **kw):
        kw.setdefault("token", "test-token")
        super().__init__(**kw)
        self.items = items or []
        self.error = error
        self.calls = []

    def _call(self, svc, params, sid=None):
        self.calls.append({"svc": svc, "params": params, "sid": sid})
        if self.error:
            raise gps.WialonError(self.error, svc)
        if svc == "token/login":
            return {"eid": "sid-123"}
        return {"items": self.items}


def unit(uid, name, lat=55.7887, lon=49.1221, t=None):
    import time as _t
    return {"id": uid, "nm": name, "pos": {"y": lat, "x": lon,
                                           "t": t or int(_t.time())}}


def test_wialon_url_from_plain_host():
    """Buyurtmachi "gpsmonitor.uz" deb beradi — API yo'li o'zi qo'shiladi."""
    assert gps.wialon_api_url("gpsmonitor.uz") == "https://gpsmonitor.uz/wialon/ajax.html"
    assert gps.wialon_api_url("https://gpsmonitor.uz/") == \
        "https://gpsmonitor.uz/wialon/ajax.html"
    assert gps.wialon_api_url("https://gpsmonitor.uz/wialon/ajax.html") == \
        "https://gpsmonitor.uz/wialon/ajax.html"
    assert gps.wialon_api_url("") == ""


def test_wialon_local_url_from_env(monkeypatch):
    monkeypatch.setenv("WIALON_URL", "gpsmonitor.uz")
    monkeypatch.setenv("WIALON_TOKEN", "t")
    assert gps.WialonAdapter().base_url == "https://gpsmonitor.uz/wialon/ajax.html"


def test_wialon_lists_units(park):
    w = FakeWialon(items=[unit("12345", "01 A 111 AA")])
    units = w.units()
    assert units[0]["name"] == "01 A 111 AA"
    assert units[0]["lat"] == pytest.approx(55.7887)
    # nom va joylashuv uchun kerakli flag so'raladi
    assert w.calls[1]["params"]["flags"] == 1025


def test_wialon_matches_truck_by_plate(park):
    """Treker nomi davlat raqami bo'lsa — qo'lda xarita kerak emas."""
    w = FakeWialon(items=[unit("12345", "01A111AA")])
    positions = w.positions()
    assert "01" in positions


def test_wialon_matches_by_truck_number(park):
    w = FakeWialon(items=[unit("999", "02")])
    assert "02" in w.positions()


def test_wialon_explicit_mapping_wins(park):
    w = FakeWialon(items=[unit("12345", "01 A 111 AA")], units={"12345": "02"})
    assert list(w.positions()) == ["02"]


def test_wialon_unknown_unit_is_skipped(park):
    w = FakeWialon(items=[unit("777", "Direktor mashinasi")])
    assert w.positions() == {}


def test_wialon_error_does_not_crash(park):
    """Wialon xatoni HTTP 200 bilan qaytaradi — uni sezishimiz kerak."""
    w = FakeWialon(error=7)
    assert w.positions() == {}


def test_wialon_error_message_is_readable():
    assert "ruxsat yo'q" in str(gps.WialonError(7, "core/search_items"))
    assert "1011" in str(gps.WialonError(1011))


def test_wialon_time_is_utc(park):
    """Wialon Unix vaqt beradi — UTC da olinishi shart, aks holda
    "signal yo'q" ogohlantirishi noto'g'ri ishlaydi."""
    now = db.utc_now()
    w = FakeWialon(items=[unit("12345", "01A111AA",
                               t=int(now.replace(tzinfo=timezone.utc).timestamp()))])
    when = w.positions()["01"][2]
    assert abs((when - now).total_seconds()) < 5


def test_wialon_feeds_sync(park):
    """To'liq zanjir: treker -> shahar -> baza."""
    w = FakeWialon(items=[unit("12345", "01A111AA")])
    result = gps.sync([w])
    assert result["updated"] == [{"truck_id": "01", "city": "Qozon",
                                  "was": "Toshkent"}]
    assert db.get_truck("01")["pos_source"] == "gps"


def test_wialon_without_token_is_silent(monkeypatch, park):
    monkeypatch.delenv("WIALON_TOKEN", raising=False)
    assert gps.WialonAdapter(token="").positions() == {}
