"""
test_translit.py — o'zbek lotin → kirill (AI javobi kirillda so'ralganda).
"""
from __future__ import annotations

import pytest

import translit


@pytest.mark.parametrize("latin,cyrillic", [
    ("yuk", "юк"), ("bo'sh", "бўш"), ("yo'q", "йўқ"), ("g'alla", "ғалла"),
    ("shahar", "шаҳар"), ("choy", "чой"), ("eng", "энг"), ("kelyapti", "келяпти"),
    ("qaytish", "қайтиш"), ("Marja", "Маржа"), ("uchun", "учун"), ("ma'lumot", "маълумот"),
    ("Fura", "Фура"), ("ertaga", "эртага"), ("yetib", "етиб"), ("oʻrta", "ўрта"),
])
def test_words(latin, cyrillic):
    assert translit.word(latin) == cyrillic


def test_sentence_keeps_tags_numbers_and_acronyms():
    text = "<b>#4 Toshkent → Almaty</b>, 20 t ref\nMarja: $1 251, <b>$459/kun</b>, USD"
    out = translit.to_cyrillic(text)
    assert out.startswith("<b>#4 Тошкент → Алматы</b>")
    assert "20 т реф" in out and "Маржа" in out and "$459/кун" in out
    assert "USD" in out and "<b>" in out


def test_code_block_untouched():
    text = "Xabar: <code>Здравствуйте! Truck 20 t</code> yuboring"
    out = translit.to_cyrillic(text)
    assert "<code>Здравствуйте! Truck 20 t</code>" in out
    assert out.startswith("Хабар") and out.endswith("юборинг")


def test_is_latin():
    assert translit.is_latin("01 uchun yuk: Toshkent")
    assert not translit.is_latin("01 учун энг яхши юк бор: Toshkent")


def test_brain_transliterates_for_cyrillic_question(clean_db, monkeypatch):
    import brain
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("AI_PROVIDERS", "groq")
    monkeypatch.setattr(brain, "chat_completion",
                        lambda p, m, tools=True: {"role": "assistant",
                                                  "content": "Eng foydali yuk yo'q"})
    assert brain.reply(1, "01 учун юк топ").text == "Энг фойдали юк йўқ"
    # lotincha savolga — tegilmaydi
    assert brain.reply(2, "01 uchun yuk top").text == "Eng foydali yuk yo'q"
