"""
test_llm_parser.py — ixtiyoriy LLM fallback (Ollama / Mistral / Groq / ...).

Tarmoqqa chiqilmaydi: HTTP qatlami almashtiriladi. Tekshiriladi —
xarajat nazorati (kesh, kunlik chegara), buzuq javobdan himoya va
model bergan ma'lumot bazaga to'g'ri ko'rinishda tushishi.
"""
from __future__ import annotations

from datetime import date

import pytest

import llm_parser
import parser as ad_parser
import pipeline

MESSY = ("bratishka Moskvaga sovutgich kerak edi, 20 ga yaqin, "
         "12-sida yuklanadi, kelishamiz")
# Regex hech narsa topa olmaydigan matn — model qaytargan qiymatlarni
# tekshirish uchun (MESSY da "sovutgich" so'zi bor, regex undan harorat
# va kuzovni o'zi topadi)
BARE = "bratishka Moskvaga bitta mashina kerak edi, kelishamiz"

GOOD_ANSWER = {
    "kind": "cargo", "from_city": "Ташкент", "to_city": "Москва",
    "load_date": "2026-10-12", "weight_t": 20, "body_type": "ref",
    "temp_c": -18, "rate": 4200, "currency": "USD", "phone": "998901234567",
}


@pytest.fixture(autouse=True)
def clean_llm(monkeypatch):
    llm_parser.reset_state()
    for name in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL",
                 "LLM_MAX_CALLS_PER_DAY", "LLM_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("config.ANTHROPIC_API_KEY", "")
    yield
    llm_parser.reset_state()


class Calls(list):
    """Yuborilgan so'rovlar + javobni boshqarish uchun `state`."""
    state: dict


@pytest.fixture
def fake_http(monkeypatch):
    """`_http_json` o'rniga — yuborilgan so'rovlarni yig'adi."""
    calls = Calls()
    state = {"reply": GOOD_ANSWER, "fail": False}

    def fake(url, payload, headers, timeout):
        calls.append({"url": url, "payload": payload, "headers": headers})
        if state["fail"]:
            return None
        content = state["reply"]
        if isinstance(content, dict):
            import json as _json
            content = _json.dumps(content)
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(llm_parser, "_http_json", fake)
    calls.state = state
    return calls


@pytest.fixture
def groq(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_API_KEY", "test-key")


# ---------------------------------------------------------------- sozlama

def test_disabled_by_default():
    assert llm_parser.enabled() is False
    assert llm_parser.ask("matn") is None


def test_provider_presets(monkeypatch):
    for name, host in (("ollama", "11434"), ("mistral", "mistral.ai"),
                       ("groq", "groq.com"), ("openrouter", "openrouter.ai")):
        monkeypatch.setenv("LLM_PROVIDER", name)
        monkeypatch.setenv("LLM_API_KEY", "k")
        assert host in llm_parser._provider()["base_url"]


def test_ollama_needs_no_key(monkeypatch):
    """O'z serveringizdagi model — kalit ham, pul ham kerak emas."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert llm_parser.enabled() is True


def test_paid_provider_without_key_is_disabled(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    assert llm_parser.enabled() is False


def test_model_and_url_can_be_overridden(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "llama3.2")
    monkeypatch.setenv("LLM_BASE_URL", "http://10.0.0.5:11434/v1/")
    p = llm_parser._provider()
    assert p["model"] == "llama3.2"
    assert p["base_url"] == "http://10.0.0.5:11434/v1"      # oxirgi "/" olib tashlanadi


def test_unknown_provider_needs_url(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mening-serverim")
    assert llm_parser.enabled() is False
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("LLM_MODEL", "my-model")
    assert llm_parser.enabled() is True


def test_old_anthropic_setting_still_works(monkeypatch):
    """Eski sozlama buzilmasin."""
    monkeypatch.setattr("config.ANTHROPIC_API_KEY", "sk-ant-xxx")
    assert llm_parser._provider()["name"] == "anthropic"


def test_none_disables(monkeypatch):
    monkeypatch.setattr("config.ANTHROPIC_API_KEY", "sk-ant-xxx")
    monkeypatch.setenv("LLM_PROVIDER", "none")
    assert llm_parser.enabled() is False


# ---------------------------------------------------------------- so'rov

def test_request_shape(groq, fake_http):
    llm_parser.ask(MESSY, today=date(2026, 10, 1))
    call = fake_http[0]
    assert call["url"].endswith("/chat/completions")
    assert call["headers"]["Authorization"] == "Bearer test-key"
    assert call["payload"]["model"] == "openai/gpt-oss-120b"
    assert "2026-10-01" in call["payload"]["messages"][0]["content"]
    assert call["payload"]["messages"][1]["content"] == MESSY


def test_anthropic_uses_its_own_api(monkeypatch, fake_http):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_KEY", "sk-ant-xxx")
    llm_parser.ask(MESSY)
    call = fake_http[0]
    assert call["url"].endswith("/messages")
    assert call["headers"]["x-api-key"] == "sk-ant-xxx"


# ---------------------------------------------------------------- xarajat

def test_answer_is_cached(groq, fake_http):
    """Bir xil e'lon 5 ta guruhda chiqsa ham bir marta so'raladi.

    Dubl filtri bu bosqichdan KEYIN ishlaydi, shuning uchun kesh shu
    yerda turishi kerak — aks holda har takror uchun pul ketadi.
    """
    for _ in range(5):
        llm_parser.ask(MESSY)
    assert len(fake_http) == 1
    assert llm_parser.usage()["calls"] == 1


def test_different_text_is_asked_again(groq, fake_http):
    llm_parser.ask(MESSY)
    llm_parser.ask("boshqa matn, Qozonga tent kerak")
    assert len(fake_http) == 2


def test_daily_limit(groq, fake_http, monkeypatch):
    monkeypatch.setenv("LLM_MAX_CALLS_PER_DAY", "3")
    for i in range(6):
        llm_parser.ask(f"har xil matn {i}")
    assert len(fake_http) == 3
    assert llm_parser.usage()["calls"] == 3


def test_truck_ads_do_not_reach_llm(clean_db, groq, fake_http, monkeypatch):
    """Bo'sh mashina e'loni bizga kerak emas — unga pul sarflamaymiz."""
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)
    pipeline.handle_message("Свободная машина реф 20т, ищу груз")
    assert fake_http == []


def test_good_ads_do_not_reach_llm(clean_db, groq, fake_http, monkeypatch):
    """Regex uddalagan e'lon modelga yuborilmaydi (xabarlarning ~95%)."""
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)
    pipeline.handle_message("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09")
    assert fake_http == []


# ---------------------------------------------------------------- to'ldirish

def test_enrich_fills_empty_fields(groq, fake_http):
    cargo = llm_parser.enrich(ad_parser.parse(MESSY))
    assert (cargo.from_city, cargo.to_city) == ("Toshkent", "Moskva")
    assert cargo.weight_t == 20 and cargo.body_type == "ref"
    # Harorat regexdan: "sovutgich" = +4°. Regex topgani ustun turadi,
    # model taklif qilgan -18 qabul qilinmaydi.
    assert cargo.temp_c == 4.0
    assert (cargo.rate, cargo.currency) == (4200, "USD")
    assert cargo.load_date == date(2026, 10, 12)
    assert cargo.phone == "+998901234567"
    assert cargo.confidence > 0.9


def test_city_names_are_canonical(groq, fake_http):
    """Model ruscha nom qaytaradi — bazaga kanonik nom tushishi shart."""
    fake_http.state["reply"] = {**GOOD_ANSWER, "from_city": "Казань",
                                "to_city": "Алматы"}
    cargo = llm_parser.enrich(ad_parser.parse(MESSY))
    assert (cargo.from_city, cargo.to_city) == ("Qozon", "Almaty")


def test_unknown_city_is_ignored(groq, fake_http):
    fake_http.state["reply"] = {**GOOD_ANSWER, "from_city": "Кукуево"}
    assert llm_parser.enrich(ad_parser.parse(MESSY)).from_city is None


def test_regex_result_wins(groq, fake_http):
    """Regex aniq qoida bilan topgan — model taxmin qiladi."""
    fake_http.state["reply"] = {**GOOD_ANSWER, "rate": 9999, "currency": "USD"}
    cargo = llm_parser.enrich(
        ad_parser.parse("Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09"))
    assert cargo.rate == 4000


def test_absurd_values_are_rejected(groq, fake_http):
    """Model ba'zan 500 tonna yoki 1000 gradus deb yuboradi."""
    fake_http.state["reply"] = {**GOOD_ANSWER, "weight_t": 500, "temp_c": 900}
    cargo = llm_parser.enrich(ad_parser.parse(BARE))
    assert cargo.weight_t is None and cargo.temp_c is None


def test_unknown_currency_is_rejected(groq, fake_http):
    fake_http.state["reply"] = {**GOOD_ANSWER, "rate": 4200, "currency": "EUR"}
    assert llm_parser.enrich(ad_parser.parse(MESSY)).rate is None


def test_bad_body_type_is_rejected(groq, fake_http):
    fake_http.state["reply"] = {**GOOD_ANSWER, "body_type": "kemа"}
    assert llm_parser.enrich(ad_parser.parse(BARE)).body_type is None


# ---------------------------------------------------------------- himoya

def test_markdown_wrapped_json(groq, fake_http):
    """Kichik modellar javobni ```json ichida qaytaradi."""
    fake_http.state["reply"] = '```json\n{"kind":"cargo","to_city":"Москва"}\n```'
    assert llm_parser.ask(MESSY)["to_city"] == "Москва"


def test_json_with_chatter_around(groq, fake_http):
    fake_http.state["reply"] = 'Mana natija:\n{"kind":"cargo"}\nUmid qilamanki yordam berdi'
    assert llm_parser.ask(MESSY) == {"kind": "cargo"}


def test_garbage_answer_does_not_crash(groq, fake_http):
    fake_http.state["reply"] = "kechirasiz, tushunmadim"
    assert llm_parser.ask(MESSY) is None
    assert llm_parser.enrich(ad_parser.parse(MESSY)).from_city is None


def test_server_down_does_not_crash(groq, fake_http):
    fake_http.state["fail"] = True
    assert llm_parser.ask(MESSY) is None


def test_pipeline_survives_llm_failure(clean_db, groq, monkeypatch):
    """Model yiqilsa ham oqim to'xtamaydi."""
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)
    monkeypatch.setattr(llm_parser, "ask",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("yiqildi")))
    assert pipeline.handle_message(MESSY) == []
    assert pipeline.handle_message(
        "Есть груз Ташкент → Москва, 20т тент, 4000$, 22.09")


def test_llm_saves_an_ad_regex_missed(clean_db, groq, fake_http, monkeypatch):
    """Asosiy foyda: regex tashlab yuborgan e'lon bazaga tushadi."""
    monkeypatch.setattr("notifier.send", lambda *a, **kw: None)
    assert ad_parser.is_usable(ad_parser.parse(MESSY)) is False

    ids = pipeline.handle_message(MESSY, source="grp1")
    assert len(ids) == 1
    row = clean_db.get_cargo(ids[0])
    assert (row["from_city"], row["to_city"]) == ("Toshkent", "Moskva")
    assert row["raw_text"] == MESSY
