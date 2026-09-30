"""
test_web.py — veb-panel (TZ 5.7).

Asosiy tekshiruvlar: parolsiz kirib bo'lmaydi, "Olaman" bot bilan bir
xil ishlaydi (bitta yuk ikki marta olinmaydi), e'lon matnidagi HTML
bajarilmaydi, tahrirlangan shahar kanonik nomga keltiriladi.
"""
from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import actions  # noqa: E402
import db  # noqa: E402
import pipeline  # noqa: E402
import settings  # noqa: E402
import web  # noqa: E402

PASSWORD = "sir-parol"


@pytest.fixture
def app_env(clean_db, truck_tent, truck_ref, monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", PASSWORD)
    monkeypatch.delenv("WEB_ALLOWED_IPS", raising=False)
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)
    web._login_fails.clear()
    for t in (truck_tent, truck_ref):
        clean_db.upsert_truck(t)
    ids = pipeline.handle_message(
        "Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09, тел +998901234567",
        source="grp1")
    return {"cargo_id": ids[0]}


@pytest.fixture
def anon(app_env):
    return TestClient(web.create_app())


@pytest.fixture
def client(anon):
    r = anon.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303
    return anon


def best_match(cargo_id):
    return db.matches_for_cargo(cargo_id)[0]


# ---------------------------------------------------------------- kirish

def test_requires_login(anon):
    r = anon.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_every_page_is_protected(anon):
    for path in ("/cargos", "/cargo/1", "/trucks", "/map", "/api/map",
                 "/history", "/settings"):
        assert anon.get(path, follow_redirects=False).status_code == 303, path


def test_post_is_protected(anon, app_env):
    m = best_match(app_env["cargo_id"])
    r = anon.post(f"/match/{m['id']}/take", follow_redirects=False)
    assert r.status_code == 303
    assert db.get_cargo(app_env["cargo_id"])["status"] == "new"


def test_wrong_password(anon):
    r = anon.post("/login", data={"password": "xato"}, follow_redirects=False)
    assert "error" in r.headers["location"]
    assert web.SESSION_COOKIE not in anon.cookies


def test_login_lockout(anon):
    """Parolni tanlab topishga urinish to'xtatiladi."""
    for _ in range(web.LOGIN_MAX_FAILS):
        anon.post("/login", data={"password": "xato"})
    r = anon.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 429


def test_health_is_public(anon):
    r = anon.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_htmx_unauthorized(anon):
    r = anon.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"


def test_token_tampering(app_env, monkeypatch):
    token = web.make_token()
    expires, sig = token.split(".")
    assert web.token_valid(token)
    assert not web.token_valid(f"{int(expires) + 999}.{sig}")   # muddat soxtalashtirildi
    assert not web.token_valid("abc")
    assert not web.token_valid(web.make_token(now=time.time() - 30 * 86400))


def test_password_change_invalidates_sessions(app_env, monkeypatch):
    token = web.make_token()
    monkeypatch.setenv("WEB_PASSWORD", "yangi-parol")
    assert not web.token_valid(token)


def test_cross_site_post_rejected(client, app_env):
    m = best_match(app_env["cargo_id"])
    r = client.post(f"/match/{m['id']}/take",
                    headers={"Origin": "https://yomon-sayt.com"}, follow_redirects=False)
    assert r.status_code == 403
    assert db.get_cargo(app_env["cargo_id"])["status"] == "new"


def test_ip_allowlist(app_env, monkeypatch):
    monkeypatch.setenv("WEB_ALLOWED_IPS", "10.8.0.2")
    c = TestClient(web.create_app())
    assert c.get("/login").status_code == 403


def test_logout(client):
    client.post("/logout")
    assert client.get("/", follow_redirects=False).status_code == 303


# ---------------------------------------------------------------- bosh sahifa

def test_dashboard_shows_trucks_and_offers(client):
    html = client.get("/").text
    assert "№01" in html and "№02" in html
    assert "Toshkent → Moskva" in html
    assert "Olaman" in html


def test_dashboard_without_trucks(client, clean_db):
    with clean_db.connect() as conn:
        conn.execute("DELETE FROM trucks")
    assert "Park bo'sh" in client.get("/").text


# ---------------------------------------------------------------- qarorlar

def test_take_from_panel(client, app_env):
    m = best_match(app_env["cargo_id"])
    r = client.post(f"/match/{m['id']}/take", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/cargo/{app_env['cargo_id']}?taken=1&driver=0"

    assert db.get_cargo(app_env["cargo_id"])["status"] == "taken"
    truck = db.get_truck(m["truck_id"])
    assert truck["current_city"] == "Moskva" and truck["pos_source"] == "trip"


def test_take_htmx(client, app_env):
    m = best_match(app_env["cargo_id"])
    r = client.post(f"/match/{m['id']}/take", headers={"HX-Request": "true"})
    assert r.headers["HX-Redirect"].startswith("/cargo/")
    assert "Olindi" in r.text


def test_panel_and_bot_share_one_claim(client, app_env):
    """Bot orqali olingan yukni paneldan qayta olib bo'lmaydi."""
    matches = db.matches_for_cargo(app_env["cargo_id"])
    assert actions.take_match(matches[0]["id"]).ok          # "bot" oldi

    r = client.post(f"/match/{matches[1]['id']}/take", headers={"HX-Request": "true"})
    assert "Allaqachon olingan" in r.text
    assert db.taken_truck_for_cargo(app_env["cargo_id"]) == matches[0]["truck_id"]


def test_taken_cargo_page_suggests_return(client, app_env, monkeypatch):
    pipeline.handle_message("Есть груз Москва → Ташкент, 20т тент, 3800$, 05.10",
                            source="grp1")
    m = best_match(app_env["cargo_id"])
    client.post(f"/match/{m['id']}/take")
    html = client.get(f"/cargo/{app_env['cargo_id']}?taken=1").text
    assert "Reys biriktirildi" in html
    assert "Qaytish yuki" in html and "Moskva → Toshkent" in html


def test_skip_htmx(client, app_env):
    m = best_match(app_env["cargo_id"])
    r = client.post(f"/match/{m['id']}/skip", headers={"HX-Request": "true"})
    assert "O'tkazildi" in r.text
    assert db.get_match(m["id"])["decision"] == "skipped"


def test_actual_margin(client, app_env):
    m = best_match(app_env["cargo_id"])
    client.post(f"/match/{m['id']}/take")
    r = client.post(f"/match/{m['id']}/actual", data={"margin": "1 850"},
                    follow_redirects=False)
    assert r.headers["location"] == "/history?saved=1"
    assert db.get_match(m["id"])["actual_margin_usd"] == 1850.0
    assert "$1 850" in client.get("/history").text


def test_actual_margin_rejects_text(client, app_env):
    m = best_match(app_env["cargo_id"])
    r = client.post(f"/match/{m['id']}/actual", data={"margin": "ko'p"},
                    follow_redirects=False)
    assert "error" in r.headers["location"]


# ---------------------------------------------------------------- yuklar

def test_cargos_list(client):
    html = client.get("/cargos").text
    assert "Toshkent → Moskva" in html
    assert "Есть груз Ташкент" in html            # asl matn


def test_cargos_filter_canonical_city(client):
    """Filtrda "Казань" deb yozilsa ham kanonik nom bo'yicha qidiriladi."""
    assert "Toshkent → Moskva" in client.get("/cargos?from=Ташкент").text
    other = client.get("/cargos?from=Казань").text
    assert "Toshkent → Moskva" not in other
    assert "topilmadi" in other


def test_cargos_unknown_city_note(client):
    assert "topilmadi" in client.get("/cargos?from=Кукуево").text


def test_raw_text_is_escaped(client):
    """E'lon matnidagi HTML/JS panelda bajarilmasligi kerak (XSS)."""
    ids = pipeline.handle_message(
        "Есть груз Бухара → Казань, 20т тент, 4100$, 23.09 <script>alert(1)</script>",
        source="<b>grp</b>")
    html = client.get(f"/cargo/{ids[0]}").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>grp</b>" not in client.get("/cargos").text


def test_cargo_detail(client, app_env):
    html = client.get(f"/cargo/{app_env['cargo_id']}").text
    assert "Furalar bo'yicha hisob" in html
    assert "+998901234567" in html


def test_cargo_404(client):
    assert client.get("/cargo/99999").status_code == 404


# ---------------------------------------------------------------- mashinalar

def truck_form(**over) -> dict:
    base = {"current_city": "Toshkent", "free_date": "2026-09-20",
            "plate": "01 A 111 AA", "driver": "Test", "driver_phone": "",
            "capacity_t": "22", "fuel_l_100km": "33", "preferred_dir": "",
            "active": "1"}
    base.update(over)
    return base


def test_truck_edit_canonicalizes_city(client):
    r = client.post("/trucks/01", data=truck_form(current_city="Казань",
                                                  free_date="2026-09-25"),
                    follow_redirects=False)
    assert r.status_code == 303
    t = db.get_truck("01")
    assert t["current_city"] == "Qozon"            # bazaga xom matn yozilmaydi
    assert t["free_date"] == "2026-09-25"
    assert t["pos_source"] == "manual"             # GPS'dan 24 soat ustun


def test_truck_edit_unknown_city(client):
    r = client.post("/trucks/01", data=truck_form(current_city="Кукуево"))
    assert r.status_code == 400
    assert db.get_truck("01")["current_city"] == "Toshkent"


def test_truck_save_without_position_change_keeps_gps(client):
    """Faqat haydovchi nomi o'zgarsa — GPS bloklanmasligi kerak."""
    client.post("/trucks/01", data=truck_form(driver="Yangi haydovchi"))
    t = db.get_truck("01")
    assert t["driver"] == "Yangi haydovchi"
    assert t["pos_source"] is None


def test_truck_deactivate(client):
    client.post("/trucks/02", data=truck_form(active="0", capacity_t="20"))
    assert db.get_truck("02")["active"] == 0


def test_truck_bad_numbers(client):
    r = client.post("/trucks/01", data=truck_form(capacity_t="500"))
    assert r.status_code == 400
    assert db.get_truck("01")["capacity_t"] == 22.0


# ---------------------------------------------------------------- xarita, tarix

def test_map_payload(client):
    data = client.get("/api/map").json()
    assert {t["id"] for t in data["trucks"]} == {"01", "02"}
    assert data["cargos"][0]["label"] == "Toshkent → Moskva"


def test_map_page(client):
    assert "leaflet" in client.get("/map").text


def test_history_empty(client):
    assert "Hali olingan reys" in client.get("/history").text


# ---------------------------------------------------------------- sozlamalar

def test_settings_page(client):
    html = client.get("/settings").text
    assert "Dizel narxi" in html and "Bildirishnoma chegarasi" in html


def test_settings_save(client):
    r = client.post("/settings", data={"fuel_price_usd": "1.10"}, follow_redirects=False)
    assert r.status_code == 303
    assert settings.current_costs().fuel_price_usd == 1.10


def test_settings_invalid(client):
    r = client.post("/settings", data={"fuel_price_usd": "95"})
    assert r.status_code == 400
    assert "0.3–3" in r.text                       # ruxsat etilgan oraliq ko'rsatildi
    assert settings.current_costs().fuel_price_usd != 95


def test_settings_reset(client):
    client.post("/settings", data={"fuel_price_usd": "1.10"})
    client.post("/settings/reset")
    assert settings.overrides() == {}


# ---------------------------------------------------------------- qidiruv sahifasi

def test_search_page_empty(client):
    html = client.get("/search").text
    assert "Qidirish" in html


def test_search_finds_cargo(client):
    html = client.get("/search?q=Ташкент Москва").text
    assert "Toshkent → Moskva" in html
    assert "№01" in html or "№02" in html          # park bo'yicha hisoblangan
    assert "4 000" in html and "/kun" not in html          # narx yashil, kunlik marja yo'q


def test_search_nothing_found_offers_watch(client):
    html = client.get("/search?q=Бухара Казань").text
    assert "mos yuk yo'q" in html
    assert "xabar bering" in html


def test_search_unknown_query(client):
    assert "tushunarsiz" in client.get("/search?q=салом жигар").text


def test_search_result_has_take_button(client, app_env):
    """Qidiruvdan turib darhol olish mumkin."""
    m = best_match(app_env["cargo_id"])
    html = client.get("/search?q=Ташкент Москва").text
    assert f"/match/{m['id']}/take" in html


def test_watch_create_and_delete(client):
    r = client.post("/watch", data={"q": "Бухара Казань реф"}, follow_redirects=False)
    assert r.status_code == 303
    watches = db.active_watches()
    assert (watches[0]["from_city"], watches[0]["to_city"],
            watches[0]["body_type"]) == ("Buxoro", "Qozon", "ref")

    assert "Buxoro → Qozon" in client.get("/search").text
    client.post(f"/watch/{watches[0]['id']}/delete")
    assert db.active_watches() == []


def test_watch_is_not_duplicated_from_panel(client):
    client.post("/watch", data={"q": "Бухара Казань"})
    client.post("/watch", data={"q": "Бухара Казань"})
    assert len(db.active_watches()) == 1


def test_watch_ignores_empty_query(client):
    client.post("/watch", data={"q": "салом"})
    assert db.active_watches() == []


# ---------------------------------------------------------------- statistika

def test_stats_page(client, app_env):
    html = client.get("/stats").text
    assert "Statistika" in html
    assert "Guruhlar" in html and "grp1" in html
    assert "Yo'nalishlar" in html and "Toshkent → Moskva" in html


def test_stats_period(client):
    assert "7 kun" in client.get("/stats?days=7").text


def test_stats_handles_absurd_period(client):
    assert client.get("/stats?days=99999").status_code == 200


def test_stats_on_empty_db(clean_db, monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", PASSWORD)
    c = TestClient(web.create_app())
    c.post("/login", data={"password": PASSWORD})
    assert c.get("/stats").status_code == 200


# ---------------------------------------------------------------- mashina sahifasi

def test_truck_page(client):
    html = client.get("/truck/01").text
    assert "Mashina №01" in html
    assert "Toshkent" in html
    assert "Reyslar tarixi" in html


def test_truck_page_shows_offers(client):
    assert "Toshkent → Moskva" in client.get("/truck/01").text


def test_truck_page_shows_gps_state(client, clean_db):
    clean_db.save_gps_position("01", 41.31, 69.28, source="telegram")
    assert "oxirgi signal" in client.get("/truck/01").text


def test_truck_page_history(client, app_env):
    m = best_match(app_env["cargo_id"])
    client.post(f"/match/{m['id']}/take")
    html = client.get(f"/truck/{m['truck_id']}").text
    assert "Toshkent → Moskva" in html


def test_truck_page_404(client):
    assert client.get("/truck/99").status_code == 404


# ---------------------------------------------------------------- jonli yangilanish

def test_fleet_fragment(client):
    html = client.get("/fragment/fleet").text
    assert "№01" in html
    assert "<html" not in html                     # faqat bo'lak, to'liq sahifa emas


def test_fleet_fragment_is_protected(anon):
    assert anon.get("/fragment/fleet", follow_redirects=False).status_code == 303


def test_dashboard_polls_fragment(client):
    html = client.get("/").text
    assert 'hx-get="/fragment/fleet"' in html
    assert "every 45s" in html


# ---------------------------------------------------------------- ikonkalar

def test_icons_are_svg_not_emoji(client):
    html = client.get("/").text
    assert 'id="i-truck"' in html                  # sprite sahifada
    assert '<use href="#i-box"/>' in html
    for emoji in ("📦", "🚛", "✅", "⏭", "🔔"):
        assert emoji not in html, f"emoji qolib ketdi: {emoji}"


def test_icons_inherit_color(client):
    assert "stroke:currentColor" in client.get("/").text


# ---------------------------------------------------------------- Telegram Web App

def _init_data(user_id: int, token: str = "test-token", auth_date: int | None = None,
               tamper: bool = False) -> str:
    """Telegram imzolagandek initData yasaydi (hujjatdagi algoritm)."""
    import hashlib
    import hmac
    import json as _json
    from urllib.parse import urlencode
    data = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAH",
            "user": _json.dumps({"id": user_id, "first_name": "Rahbar"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        data["user"] = _json.dumps({"id": 1, "first_name": "Boshqa"})
    return urlencode(data)


@pytest.fixture
def tg_env(app_env, monkeypatch):
    import config
    monkeypatch.setattr(config, "BOT_TOKEN", "test-token")
    monkeypatch.setattr(config, "DISPATCHER_CHAT_ID", "")
    db.add_dispatcher_chat(555)


def test_telegram_signature_checked(tg_env):
    assert web.telegram_user(_init_data(555))["id"] == 555
    assert web.telegram_user(_init_data(555, tamper=True)) is None
    assert web.telegram_user(_init_data(555, token="boshqa-bot")) is None
    old = int(time.time()) - 2 * 86400
    assert web.telegram_user(_init_data(555, auth_date=old)) is None
    assert web.telegram_user("") is None


def test_tg_auth_logs_in_dispatcher(app_env, tg_env):
    # Secure cookie faqat HTTPS da qaytariladi (server Cloudflare ortida)
    anon = TestClient(web.create_app(), base_url="https://testserver")
    r = anon.post("/tg-auth", data={"init_data": _init_data(555)})
    assert r.status_code == 200 and r.json()["ok"]
    cookie = r.headers["set-cookie"].lower()
    assert "samesite=none" in cookie and "secure" in cookie
    assert anon.get("/", follow_redirects=False).status_code == 200


def test_tg_auth_rejects_stranger(anon, tg_env):
    r = anon.post("/tg-auth", data={"init_data": _init_data(999)})
    assert r.status_code == 403 and r.json()["error"] == "not_dispatcher"
    assert anon.get("/", follow_redirects=False).status_code == 303


def test_tg_auth_rejects_forgery(anon, tg_env):
    r = anon.post("/tg-auth", data={"init_data": _init_data(555, token="soxta")})
    assert r.status_code == 403
    assert anon.get("/", follow_redirects=False).status_code == 303


def test_panel_never_open_without_login(anon):
    """Panel parolsiz ochiq qolmasin (sozlamalar va "Olaman" bor)."""
    for path in ("/chat", "/rules", "/settings"):
        assert anon.get(path, follow_redirects=False).status_code == 303, path
    assert anon.post("/chat/send", data={"text": "x"},
                     follow_redirects=False).status_code == 303


def test_login_page_has_telegram_autologin(anon):
    html = anon.get("/login").text
    assert "/tg-auth" in html and "telegram-web-app.js" in html


# ---------------------------------------------------------------- AI va qoidalar

def test_chat_page_without_ai(client, monkeypatch):
    for key in ("MISTRAL_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    html = client.get("/chat").text
    assert "AI ulanmagan" in html and 'id="composer"' in html
    # AI yo'q — oddiy qidiruvga tushadi
    r = client.post("/chat/send", data={"text": "Ташкент Москва"},
                    headers={"hx-request": "true"})
    assert "Toshkent" in r.text


def test_chat_send_with_ai_shows_offer_buttons(client, app_env, monkeypatch):
    import brain
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    cid = app_env["cargo_id"]
    steps = [{"role": "assistant", "content": "", "tool_calls": [
                {"id": "call00001", "type": "function", "function": {
                    "name": "find_cargo", "arguments": '{"from_city": "Toshkent"}'}}]},
             {"role": "assistant", "content": f"Eng foydalisi #{cid} <script>x</script>"}]
    monkeypatch.setattr(brain, "chat_completion", lambda p, m: steps.pop(0))
    r = client.post("/chat/send", data={"text": "yuk top"}, headers={"hx-request": "true"})
    assert f"#{cid}" in r.text
    assert "/take" in r.text                       # "Olaman" tugmasi
    assert "<script>x" not in r.text               # model matni ekranlangan
    assert "Eng foydalisi" in client.get("/chat").text   # tarix saqlandi


def test_rules_page_add_and_delete(client):
    import rules
    rules.reset_cache()
    r = client.post("/rules", data={"effect": "block", "from_country": "RU",
                                    "min_rate": "30000000", "currency": "UZS"},
                    follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    rule = rules.list_rules()[0]
    html = client.get("/rules").text
    assert "Россия" in html
    client.post(f"/rules/{rule['id']}/status", data={"status": "delete"})
    assert rules.list_rules() == []


def test_rules_page_bad_rule(client):
    r = client.post("/rules", data={"effect": "block"}, follow_redirects=False)
    assert "err=" in r.headers["location"]


# ---------------------------------------------------------------- yangi fura

def new_truck_form(**over) -> dict:
    base = {"id": "07", "body_type": "ref", "capacity_t": "20", "temp_min": "-20",
            "temp_max": "15", "current_city": "Самарканд", "free_date": "",
            "plate": "30 A 777 AA", "driver": "Bobur", "driver_phone": "",
            "fuel_l_100km": "", "preferred_dir": "RU"}
    base.update(over)
    return base


def test_add_truck(client, app_env, monkeypatch):
    calls = []
    real = pipeline.rematch_all
    monkeypatch.setattr(pipeline, "rematch_all", lambda: calls.append(1) or real())
    assert "Yangi fura" in client.get("/trucks/new").text
    r = client.post("/trucks/new", data=new_truck_form(), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/truck/07?saved=1"
    t = db.get_truck("07")
    assert t["current_city"] == "Samarqand"          # kanonik nom
    assert (t["temp_min"], t["temp_max"]) == (-20, 15)
    assert t["fuel_l_100km"] == 33                   # bo'sh — sukut
    # mavjud yuklar yangi fura uchun ham qayta hisoblandi
    assert calls == [1]
    assert "№07" in client.get("/trucks").text


def test_add_truck_validation(client):
    r = client.post("/trucks/new", data=new_truck_form(id="01"))          # band raqam
    assert r.status_code == 400 and "allaqachon bor" in r.text
    r = client.post("/trucks/new", data=new_truck_form(current_city="Кукуево"))
    assert r.status_code == 400
    r = client.post("/trucks/new", data=new_truck_form(temp_min="10", temp_max="-5"))
    assert r.status_code == 400
    assert db.get_truck("07") is None


def test_tent_truck_has_no_temps(client):
    client.post("/trucks/new", data=new_truck_form(id="08", body_type="tent"))
    t = db.get_truck("08")
    assert t["body_type"] == "tent" and t["temp_min"] is None


def test_edit_body_type(client):
    r = client.post("/trucks/02", data=truck_form(body_type="ref", temp_min="-18",
                                                  temp_max="12", capacity_t="20"),
                    follow_redirects=False)
    assert r.status_code == 303
    t = db.get_truck("02")
    assert t["body_type"] == "ref" and t["temp_min"] == -18
    client.post("/trucks/02", data=truck_form(body_type="tent", capacity_t="20"))
    assert db.get_truck("02")["temp_min"] is None


def test_rematch_all_covers_new_truck(app_env):
    """Yangi fura qo'shilgach — unga yetib boradigan yuk uchun taklif paydo bo'ladi."""
    ids = pipeline.handle_message("Груз Самарканд → Москва, 18т реф +2, 4000$", source="g",
                                  notify=False)
    db.upsert_truck({"id": "09", "body_type": "ref", "capacity_t": 20, "temp_min": -20,
                     "temp_max": 15, "current_city": "Samarqand", "free_date": None,
                     "fuel_l_100km": 33})
    assert db.find_match(ids[0], "09") is None
    assert pipeline.rematch_all() > 0
    assert db.find_match(ids[0], "09") is not None


# ---------------------------------------------------------------- reyslar

def test_truck_on_trip_is_visible(client, app_env):
    m = best_match(app_env["cargo_id"])
    client.post(f"/match/{m['id']}/take")
    home = client.get("/").text
    assert "Yo'lda" in home and "Reys tugadi" in home and "Bekor qilish" in home
    assert "Keyingi yuk" in home                         # qaytish yuki bo'limi
    trips = client.get("/history").text
    assert "Yo'lda" in trips and f"/trip/{m['id']}/finish" in trips


def test_trip_undo_from_panel(client, app_env):
    m = best_match(app_env["cargo_id"])
    before = dict(db.get_truck(m["truck_id"]))
    client.post(f"/match/{m['id']}/take")
    r = client.post(f"/trip/{m['id']}/undo", follow_redirects=False)
    assert r.status_code == 303
    assert db.get_cargo(app_env["cargo_id"])["status"] == "new"
    assert db.get_truck(m["truck_id"])["current_city"] == before["current_city"]
    assert db.active_trips() == []
    # yuk yana taklif sifatida ko'rinadi
    assert "Toshkent → Moskva" in client.get("/").text


def test_trip_finish_from_panel(client, app_env):
    m = best_match(app_env["cargo_id"])
    client.post(f"/match/{m['id']}/take")
    client.post(f"/trip/{m['id']}/finish")
    assert db.active_trips() == []
    html = client.get("/history").text
    assert "Tugagan" in html and "haqiqiy $" in html      # marja kiritish maydoni


def test_offers_ranked_by_margin_per_day(clean_db, truck_tent):
    """Narxi yozilmagan yuk (ball baland bo'lsa ham) narxi bor foydali yukdan keyin."""
    import web
    clean_db.upsert_truck(truck_tent)
    pipeline.handle_message("Груз Ташкент → Москва, 20т тент, 22.09", source="g")
    pipeline.handle_message("Груз Ташкент → Москва, 20т тент, 4000$, 22.09", source="g2")
    ranked = web._best_offers("01", 5)
    per_day = [web._details(r).get("margin_per_day") for r in ranked]
    assert per_day[0] is not None and per_day[-1] is None
    html = web._offer(ranked[-1])
    assert "so'rang" in html and "narxi yozilmagan" in html
    assert "ball" not in web._offer(ranked[0])


# ---------------------------------------------------------------- o'tkazish — joyida yangilash

def _two_offers_for_01(app_env):
    pipeline.handle_message("Груз Ташкент → Казань, 20т тент, 3900$, 22.09", source="g3")
    offers = web._best_offers("01", 2)
    assert len(offers) == 2
    return offers


def test_skip_on_home_shows_next_offer_immediately(client, app_env):
    first, second = _two_offers_for_01(app_env)
    r = client.post(f"/match/{first['id']}/skip",
                    headers={"HX-Request": "true", "HX-Current-URL": "http://testserver/"})
    assert r.headers["HX-Retarget"] == "#truck-01" and r.headers["HX-Reswap"] == "outerHTML"
    assert 'id="truck-01"' in r.text
    assert f'id="m{first["id"]}"' not in r.text          # o'tkazilgani yo'q
    assert f'id="m{second["id"]}"' in r.text             # keyingisi darhol turibdi


def test_skip_on_truck_page_refreshes_offers(client, app_env):
    first, _ = _two_offers_for_01(app_env)
    r = client.post(f"/match/{first['id']}/skip",
                    headers={"HX-Request": "true",
                             "HX-Current-URL": "http://testserver/truck/01"})
    assert r.headers["HX-Retarget"] == "#offers-01"
    assert 'id="offers-01"' in r.text and f'id="m{first["id"]}"' not in r.text
    assert 'id="offers-01"' in client.get("/truck/01").text


def test_skip_elsewhere_keeps_simple_note(client, app_env):
    first, _ = _two_offers_for_01(app_env)
    r = client.post(f"/match/{first['id']}/skip",
                    headers={"HX-Request": "true",
                             "HX-Current-URL": "http://testserver/search?q=x"})
    assert "HX-Retarget" not in r.headers and "O'tkazildi" in r.text


# ---------------------------------------------------------------- animatsiyali emoji

def test_anim_emoji_is_image_with_static_fallback():
    html = web.anim("truck", 30)
    assert '<img src="/static/emoji/truck.webp" width="30"' in html
    assert 'media="(prefers-reduced-motion: reduce)"' in html and "truck.png" in html


def test_emoji_files_served_publicly_with_cache(anon):
    r = anon.get("/static/emoji/truck.webp")               # kirmasdan ham (login sahifasi)
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert "max-age" in r.headers["cache-control"]
    assert anon.get("/static/emoji/truck.png").headers["content-type"] == "image/png"
    for bad in ("nope.webp", "truck.svg", "Truck.webp", "..%2Fweb.py"):
        assert anon.get(f"/static/emoji/{bad}").status_code == 404, bad
    # "../" yo'li — fayl mazmuni hech qachon qaytmaydi (login'ga yo'naltiriladi)
    r = anon.get("/static/emoji/../web.py", follow_redirects=False)
    assert r.status_code in (303, 404) and "def " not in r.text


def test_every_used_emoji_exists(client):
    """Sahifalarda ishlatilgan har bir emoji fayli bor (buzuq rasm bo'lmasin)."""
    import re as _re
    html = "".join(client.get(u).text for u in ("/", "/more", "/chat", "/history", "/login"))
    names = set(_re.findall(r"/static/emoji/([a-z]+)\.webp", html))
    assert {"truck", "sparkles", "box"} <= names
    for n in names:
        assert (web.EMOJI_DIR / f"{n}.webp").is_file() and (web.EMOJI_DIR / f"{n}.png").is_file()
