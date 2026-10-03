# CLAUDE.md — BAXT TRANSPORT Cargo Finder

> Bu faylni agent har seansda avtomatik o'qiydi. To'liq texnik topshiriq —
> **`TZ.md`**. Ishga tushirish va foydalanish — **`README.md`**.

## Loyiha nima qiladi

Telegram yuk guruhlarini kuzatadi → e'lonni strukturaga aylantiradi →
dublni tashlaydi → **har bir mashina uchun alohida** marja hisoblaydi →
mos yuk topilsa dispetcherga Telegram xabari yuboradi.

**Asosiy tamoyil:** eng qimmat yuk ≠ eng foydali yuk. Taqqoslash mezoni —
**kunlik sof marja** (`margin_usd / trip_days`), chunki mashina resursi pul
emas, vaqt. Bu tamoyil `tests/test_scoring.py::test_daily_margin_wins_over_total`
bilan qotirilgan — bu test buzilsa, o'zgarish noto'g'ri.

## Holat

TZ 5.1–5.8 hammasi bajarilgan: bot tugmalari, `parse_many`, testlar, masofa
kalibrovkasi, GPS, statistika, veb-panel, ishonchlilik. 530+ test.

Buyurtmachining keyingi talablari ham bajarilgan:
  • so'rov bo'yicha qidirish ("Менга Тошкент–Москва юк топиб бер") va
    yo'nalishni kuzatuv (`search.py`, `watches` jadvali);
  • park bo'yicha javob ("бизда нечта мошина бор, тент/реф");
  • **Wialon Local** (gpsmonitor.uz) — `gps.WialonAdapter`, token kutilmoqda;
  • LLM provayderi almashtiriladigan qilindi (Ollama/Mistral/Groq/OpenRouter).

**AI yurak** (buyurtmachining yangi talabi): rahbar (u dispetcher ham) botga
yoki paneldagi "AI yordamchi" ga oddiy gap yozadi — AI park, qidiruv,
"fura + qaytish" rejasi, qoidalar va xotira asboblarini chaqiradi.
Qoidalar ("Rossiyadan 30 mln dan arzon olma") bazaga yoziladi va har bir yuk
hisobida **tokensiz** qo'llanadi. Dastur "Olaman/O'tkazish" qarorlaridan
naqsh topib, qoida **taklif** qiladi (`learn.py`) — rahbar tasdiqlaydi.

Kod tomondan ochiq ish yo'q. Qolgani **buyurtmachi ma'lumoti** (TZ 7):
real xarajatlar, real reyslar km'i, 100 ta real e'lon, guruhlar ro'yxati,
GPS provayderi. Ular kelganda: `tests/ads.jsonl` ga e'lonlar, `/settings`
ga xarajatlar, `python main.py calibrate` ga reyslar.

## Fayllar

| Fayl | Mas'uliyat |
|---|---|
| `config.py` | Boshlang'ich sozlamalar. **Tahrirlanmaydi** |
| `settings.py` | Panelda o'zgartirilgan sozlamalar (`settings` jadvali, 30 s kesh) |
| `geo.py` | Shaharlar, masofa, yo'nalish koeffitsientlari, `nearest_city`, OSRM, ruscha nom (`ru`) |
| `parser.py` | E'lon → `Cargo`; `parse_many` — bir postda bir nechta yuk |
| `dedup.py` | 3 bosqichli dubl filtri (sana boshqa bo'lsa — boshqa yuk) |
| `db.py` | SQLite + `_migrate` (yangi ustunlar shu yerda) |
| `scoring.py` | ★ Marja va 0–100 ball. **Eng ehtiyotkorlik talab qiladigan fayl** |
| `pipeline.py` | Xabarning to'liq yo'li; `handle_message` → `list[int]` |
| `actions.py` | "Olaman"/"O'tkazish" — bot va panel uchun **yagona** mantiq |
| `search.py` | Dispetcher so'rovi ("Toshkent Moskva") → park bo'yicha javob |
| `listener.py` | Telethon (yagona async modul), FloodWait, qayta ulanish |
| `bot.py` | Tugmalar, buyruqlar, haydovchi `/link` + Live Location |
| `notifier.py` | Telegram matnlari (ruscha), soatlik cheklov |
| `gps.py` | GPS manbalari (Protocol) va sinxronlash |
| `analytics.py` | Statistika, yo'nalish bo'yicha bozor stavkasi |
| `web.py` | Veb-panel: qidiruv, statistika, mashina sahifasi, SVG ikonkalar |
| `rules.py` | Kompaniya qoidalari: `block`/`penalty`/`boost`, `scoring` ichida qo'llanadi |
| `learn.py` | Qarorlardan naqsh → qoida taklifi (`proposed`, rahbar tasdiqlaydi) |
| `ai_tools.py` | AI asboblari (LLM'siz, aniq hisob): qidiruv, reja, qoida, xotira |
| `brain.py` | AI yurak: Mistral/Groq tool-calling, provayder almashinuvi, tarix |
| `insight.py` | Kartochkadagi "Почему этот груз" izohi (dastur hisoblaydi, AI emas) |
| `briefing.py` | Ertalabki reja (08:00 Toshkent, `settings.briefing_hour`), `/brief` |
| `ai_eval.py` | AI sifat sinovi: `python main.py ai-eval` (baza nusxasida) |
| `llm_parser.py` | Ixtiyoriy LLM fallback: Ollama/Mistral/Groq/OpenRouter/Anthropic |
| `main.py` | CLI |
| `deploy/` | systemd (listener, bot, web), `backup.sh` |

## Tekshirish

```bash
python main.py demo             # Telegramsiz to'liq oqim
python main.py report --truck 01
pytest                          # oxirida parser aniqligi chiqadi
```

Har qanday o'zgartirishdan keyin `python main.py demo` va `pytest` ishlashi shart.

## Qat'iy qoidalar

1. **Til:** kod inglizcha, izohlar o'zbekcha. Foydalanuvchi ko'radigan
   hamma narsa **ruscha** (buyurtmachi talabi, 2026-09-30): panel, Telegram
   kartochkasi, bot tugmalari va javoblari, AI javobi (savol o'zbekcha
   bo'lsa ham — `brain._script_hint`), dastur yozadigan sabablar
   (`scoring`), hukmlar (`analytics`), sozlama nomlari (`settings`).
   Ruscha son bilan: `web.plural(n, "фура", "фуры", "фур")`. "из Москва"
   deb yozilmaydi — "из г. Москва" (kelishik yo'q).
2. **Shahar nomlari** bazada kanonik (`geo.CITIES` kaliti, "Toshkent"). Bazaga
   xom matn yozilmaydi — panel va bot ham `geo.lookup` orqali o'tkazadi.
   Ko'rinishda — ruscha: panelda `web.ru(city)`, AI'ga `ai_tools.dumps`
   (`geo.ru_text`), Telegramga har xabar `notifier.localize` dan o'tadi
   (`_send_to` va `bot.api` ichida; `callback_data` kanonik qoladi).
   Ruscha nom `geo.lookup` bilan kanonikka qaytadi (`test_geo`).
3. **Pul:** hisob USD'da (`rate_usd`), ko'rsatish asl valyutada.
4. **`cargos.raw_text` hech qachon o'chirilmaydi.**
5. **Yangi kutubxona** faqat zarurat bo'lsa. Majburiy — faqat `telethon`
   (listener). `rapidfuzz`, `fastapi`/`uvicorn` (faqat panel) ixtiyoriy;
   asosiy oqim ularsiz ishlaydi. LLM provayderlari oddiy HTTP orqali —
   `openai`/`anthropic` kutubxonalari kerak emas.
6. **Xato tizimni to'xtatmaydi:** bitta xabarda xato → log, keyingisiga o'tish.
7. **`config.py` tahrirlanmaydi** — qiymatlar `.env` yoki `/settings` orqali.
8. `.env`, `*.session`, `cargo.db` git'ga tushmasin.
9. **Vaqt:** baza UTC da yozadi (`CURRENT_TIMESTAMP`). Vaqt oynalari faqat
   `db.utc_now()` / `db._ago()` bilan — `datetime.now()` bilan solishtirish
   Toshkentda oynalarni 5 soatga qisqartiradi.
10. **Qaror mantiqi** (`take`/`skip`) faqat `actions.py` da — bot va panel
    uni chaqiradi, o'zi yozmaydi.
11. **Baza sxemasi:** yangi ustun — `db._migrate` ga (ishlab turgan bazani
    qayta yaratib bo'lmaydi).
12. **Python ≥ 3.10:** f-string ifodasi ichida `\` ishlatilmasin (3.12 dan
    oldin sintaksis xatosi). Tekshirish: `py -3.10 -m py_compile web.py`.
13. `matches.decision`: `taken` | `skipped` — dispetcher qarori (statistika
    uchun); `cancelled` — tizim tozalashi, statistikaga kirmaydi.
14. **LLM xarajati:** model faqat regex uddalay olmaganda (~5% xabar) va
    faqat `kind != "truck"` bo'lsa chaqiriladi; javob keshlanadi (dubl
    filtri keyin ishlaydi), kunlik chegara bor. Bu uchta himoyani
    olib tashlamang. Model bergan shahar `geo.lookup` dan o'tkaziladi,
    regex topgan qiymat esa har doim ustun turadi.
15. **Dispetcher botga buyruqsiz yozadi.** Shahar nomi bor matn — qidiruv
    so'rovi (`bot._free_text`). Shahar topilmasa bot javob bermaydi:
    dispetcherlar chatida oddiy yozishmalar ham bo'ladi.
16. **Wialon** xatoni HTTP 200 bilan, javob ichida `{"error": N}` qilib
    qaytaradi — `_call` uni tekshiradi. Vaqt esa Unix (UTC), mahalliy
    vaqtga aylantirilmaydi (9-qoidaga qarang).
16a. **Panel tuzilmasi — 5 ta tab** (iPhone birinchi): Сегодня · Грузы · [AI] ·
    Парк · Ещё (`web.TABS`). Yangi sahifa yangi tab emas — mos bo'lim
    ichiga qo'shiladi (`web.MORE`, `web._SECTION`), ichki sahifada
    `top(..., back=...)` bilan orqaga havola. Telefonga jadval emas, ro'yxat
    (`.list`/`.li`, taklif — `_deal`).
17. **Panelda emoji ham, stiker ham yo'q** — faqat bir uslubdagi SVG chiziqli
    ikonkalar `web.ic("nom")` (`_ICON_PATHS`), firma belgisi `web.logo()`.
    Buyurtmachi (2026-10-03): animatsiyali stikerlar "bolalar dasturiga
    o'xshab qolgan" — olib tashlandi (`static/emoji`, `anim()` yo'q). Tizim
    ko'rinishi: rang faqat ma'no uchun (holat, narx, bayroq). Ro'yxatdagi e'lon
    parchasi ham emojisiz (`_preview`; asl matn o'zgarmaydi). Tekshiruv:
    `test_icons_are_svg_not_emoji`, `test_no_sticker_images`. Telegram
    xabarlarida oddiy emoji qoladi — u yerda o'rinli.
18. **Harakatlar** `prefers-reduced-motion` ni hurmat qiladi.
19. Panel qidiruvi va bot qidiruvi bitta `search.py` dan foydalanadi —
    javob ikkala kanalda bir xil bo'lishi kerak.
20. **Bot qulayligi:** dispetcher komanda yodlamaydi — pastdagi doimiy
    tugmalar (`bot.main_keyboard`), `/` menyusi (`setMyCommands`), va "🌐 Панель"
    magic-havola (`bot.panel_link` ↔ `web.magic_valid`, bir xil sir).
    Telegram ichida (Web App) — `web.telegram_user` imzoni tekshiradi.
21. **Dispetcher chati** `.env` da bo'lsa — o'sha; bo'lmasa `/start` yozgan
    chatlar `settings.dispatcher_chats` ga tushadi. `notifier.send` hammasiga
    yuboradi. `bot.allowed` shu ro'yxat bo'yicha cheklaydi.
22. **Panel hech qachon parolsiz ochiq qolmaydi.** Telegram ichidan kirish —
    faqat `initData` imzosi (bot tokeni bilan) to'g'ri va foydalanuvchi
    dispetcherlar ro'yxatida bo'lsa (`/tg-auth`). Middleware'dagi
    `token_valid` tekshiruvini olib tashlamang.
23. **AI raqam o'ylab topmaydi** — hamma hisob `ai_tools` da (`scoring`,
    `rules`). AI'da yukni **olish** asbobi yo'q: u taklif qiladi, "✅ Беру"
    ni odam bosadi (`test_no_take_tool`).
24. **Qoida marjaga tegmaydi** — faqat to'sadi yoki ballni suradi
    (`test_rule_does_not_change_margin`). Stavkasi noma'lum yuk narx qoidasi
    bilan yashirilmaydi. O'rganilgan qoida rahbar tasdiqlamaguncha ishlamaydi.
25. **AI xarajati:** model faqat odam yozganda chaqiriladi (guruh e'lonlari
    uchun emas); tarixda faqat savol va yakuniy javob; kunlik chegara
    `AI_MAX_CALLS_PER_DAY`. Guruh chatida AI ishlamaydi (15-qoida).
    Testlar tashqi AI'ga chiqmaydi — `conftest` kalitlarni o'chiradi.
26. **Baza ulanishi `with db.connect()` da ochiladi va yopiladi** (`db._Conn`).
    Ulanishni `with`siz ochib qoldirmang — listener fayl deskriptori
    chegarasiga yetib, yuklarni jimgina yo'qotadi (2026-09-30 da shunday bo'lgan).
27. **Bitta fura — bitta reys:** reys olingach furaning boshqa ochiq takliflari
    `cancelled` (`db.cancel_truck_matches`), eski tugma "stale" qaytaradi.
    AI bekor qilingan juftlikni `db.revive_match` bilan yangi hisobda ochadi.
28. **Deploy faqat testlar yashil bo'lsa:** `pytest ... | tail -1 && scp` —
    NOTO'G'RI (tail doim muvaffaqiyatli). Natijadan "failed" ni tekshiring.
29. **Reys hayoti:** olindi (`taken`, `finished_at` NULL — "Yo'lda") →
    tugadi (`actions.finish_trip`, haqiqiy marja ixtiyoriy) yoki xato bosilgan
    bo'lsa bekor (`actions.undo_take` → `undone`, yuk yana `new`, fura
    `prev_city/prev_free_date` ga qaytadi; statistikaga kirmaydi). Faqat
    furaning oxirgi reysi bekor qilinadi.
30. **Takliflar tartibi** (`web._rank_offers`): narxi bor foydali yuklar kunlik
    marja bo'yicha → narxi yozilmaganlar ("≥ $X so'rang", `_ask_price`) →
    zararlilar. `score` bo'yicha saralamang: narx noma'lum yuk ham 100 ball oladi.
31. **Narx ishonchliligi:** `scoring.rate_suspicious` — $1500 dan qimmat va
    6 $/km dan ortiq narx deyarli har doim e'lonni noto'g'ri o'qish
    ("200000.00 KZT" -> 20 mln, "1.500.000 mln" -> 500 mln). Bunday narx bilan
    marja hisoblanmaydi (`rate_suspect`), panelda "проверьте цену", kartochkada ❓.
    Aks holda absurd "marja" ro'yxat boshiga chiqadi va AI uni tavsiya qiladi.
    `from_city == to_city` — mos emas. Formula o'zgarsa serverda
    `python main.py rescore` (`save_match` mavjud taklifga tegmaydi).
32. **Mavzu doim yorug'** (buyurtmachi: Telegram tunda panel qop-qora ochilardi).
    Qorong'i — faqat "Ещё → тема" tanlansa (`data-theme="dark"`, grafit, qora
    emas). `prefers-color-scheme` ishlatilmaydi (`test_light_theme_is_default`).
    Buyurtmachi: dizayn "oddiy bo'lib qolmasin", lekin bolalarcha ham emas —
    asosiy bo'lim sarlavhasi firma rangida (bitta to'q ko'k gradient `--hdr`,
    shisha plitkada ikonka; `top(..., tone="cargo|park|trips|ai|more")`,
    bosh sahifada `hero`),
    yo'nalish oldida bayroq (`web.route(a, b)`, `web.place(city)`, SVG `_FLAGS`;
    yangi davlat qo'shilsa bayrog'i ham — `test_every_country_has_flag`), fura
    raqami holat rangida (`st-free/trip/later`), taklif tafsiloti chiplarda (`.mc`).
