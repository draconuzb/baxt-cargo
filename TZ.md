# TEXNIK TOPSHIRIQ (TZ)
## BAXT TRANSPORT — Telegram Cargo Finder

**Versiya:** 1.0
**Sana:** 2026-09-20
**Buyurtmachi:** BAXT TRANSPORT (6 ta yuk mashinasi, xalqaro yo'nalishlar)
**Holat:** 1–3-bosqich tayyor va sinovdan o'tgan. 4–5-bosqich bajarilishi kerak.

> Bu hujjat ikki maqsad uchun: (1) buyurtmachi bilan kelishuv, (2) VS Code'dagi
> AI-agent uchun to'liq kontekst. Agent avval shu faylni to'liq o'qisin, keyin
> kod yozsin.

---

# 1. BIZNES MAQSAD

## 1.1 Muammo

Dispetcher kuniga 10–15 ta Telegram guruhini qo'lda kuzatadi. Har bir yuk
e'loni o'rtacha **10–20 daqiqa** yashaydi — keyin kimdir olib qo'yadi. Natijada:

- Yaxshi yuklar qo'ldan ketadi (dispetcher ko'rmay qoladi).
- Yuk "qimmat" ko'ringani uchun olinadi, lekin 800 km bo'sh yurishdan keyin
  foyda qolmaydi.
- Bir xil yuk 5 ta guruhda chiqadi — vaqt behuda ketadi.
- Qaytish yuki oldindan o'ylanmaydi, mashina bo'sh qaytadi.

## 1.2 Yechim

Tizim guruhlarni 24/7 kuzatadi, har bir e'lonni strukturaga aylantiradi va
**har bir mashina uchun alohida** foydani hisoblaydi. Mos yuk topilsa —
dispetcherga 30 soniya ichida Telegram xabari boradi.

## 1.3 Asosiy tamoyil

> **Eng qimmat yuk ≠ eng foydali yuk.**

Taqqoslash mezoni — **kunlik sof marja** (`margin_usd / trip_days`), chunki
mashina resursi bu pul emas, **vaqt**. 7 kunda 2 000 $ qoldiradigan reys,
3 kunda 1 200 $ qoldiradigan reysdan yomonroq.

## 1.4 Muvaffaqiyat mezonlari (KPI)

| Ko'rsatkich | Hozir (qo'lda) | Maqsad (3 oydan keyin) |
|---|---|---|
| Bo'sh probeg ulushi | ~25–30% | < 15% |
| Yuk topishgacha vaqt | 2–4 soat | < 30 daqiqa |
| Kunlik marja (1 mashina) | noma'lum | o'lchanadigan va o'sadigan |
| Dispetcher qo'lda ko'radigan e'lon | ~300/kun | ~20/kun (faqat mos kelganlari) |
| E'lon tahlili aniqligi | — | > 90% |

---

# 2. HOZIRGI HOLAT

## 2.1 Tayyor qism (1–3-bosqich)

| Bosqich | Holat | Izoh |
|---|---|---|
| 1. Telegram → yuk bazasi | ✅ | Telethon, matn tahlili, 3 bosqichli dubl filtri |
| 2. Mashinalar, masofa, filtrlar | ✅ | Kuzov, harorat, sig'im, sana, bo'sh probeg |
| 3. Yoqilg'i, xarajat, marja, ball | ✅ | 0–100 ball, kunlik marja bo'yicha |
| Qaytish yuki | ✅ (baho) | Zanjir foydasi hisoblanadi |
| 4. GPS | ❌ | Shahar hozir qo'lda yangilanadi |
| 5. Veb-panel + bot tugmalari | ❌ | Tugmalar hozir **ishlamaydi** |

## 2.2 Sinov natijasi (haqiqiy e'lon matnlarida)

6 ta e'londan: 4 tasi bazaga tushdi, 1 tasi "свободная машина" deb tashlandi,
1 tasi dubl deb aniqlandi (boshqacha so'zlar bilan yozilgan bo'lsa ham).

To'g'ri tanilgan variantlar:
- `Казань → Ташкент, 20 тонн, реф +5, 42 млн сум, загрузка 12.05, +998901234567`
- `Ташкент-Москва 20тн тент 4000$ 18.05` (chiziqcha, probelsiz)
- `Toshkent - Moskva tent 20t 3500$` (lotincha)
- `Андижан → Алматы, черешня 20т реф +2, за тонну 250$` → **5 000 $** (avtomatik ko'paytirdi)
- `Москва - Казань - Ташкент` → zanjir: Moskva → Toshkent, via Qozon

## 2.3 Fayl tuzilmasi

```
baxt_cargo/
├── CLAUDE.md            # agent uchun qisqa qoidalar (avtomatik o'qiladi)
├── TZ.md                # shu hujjat
├── README.md            # ishga tushirish yo'riqnomasi
├── config.py            # BARCHA sozlamalar va xarajat parametrlari
├── geo.py               # 140+ shahar, masofa, chegara soni
├── parser.py            # e'lon matni → struktura
├── dedup.py             # dubl filtri
├── db.py                # SQLite qatlami
├── scoring.py           # ★ marja va ball hisobi (loyihaning yuragi)
├── pipeline.py          # xabarning to'liq yo'li
├── listener.py          # Telethon (guruhlarni o'qish)
├── notifier.py          # dispetcherga kartochka yuborish
├── llm_parser.py        # ixtiyoriy LLM fallback
├── main.py              # CLI
├── requirements.txt
├── .env.example
├── trucks.example.json
└── sources.example.json
```

## 2.4 Ma'lumot oqimi

```
Telegram guruh
     │ (Telethon, user session)
     ▼
listener.py  ──►  pipeline.handle_message()
                        │
                        ├─► parser.parse()        matn → Cargo obyekti
                        │      └─ ishonch < 0.45 bo'lsa → llm_parser.enrich()
                        │
                        ├─► parser.is_usable()    yuk emasmi? tashlab yuboramiz
                        ├─► dedup.is_duplicate()  3 bosqichli tekshiruv
                        ├─► db.insert_cargo()
                        │
                        └─► match_and_notify()
                                 ├─ db.get_trucks()
                                 ├─ scoring.best_trucks()   har mashina uchun ball
                                 ├─ db.save_match()
                                 └─ ball ≥ 65 → notifier.notify_match()
                                                       │
                                                       ▼
                                              Dispetcher Telegram
```

---

# 3. MA'LUMOTLAR MODELI

SQLite (`cargo.db`). 6 ta mashina va ~20 guruh uchun yetarli. Keyinroq
PostgreSQL'ga ko'chiriladi — SQL deyarli bir xil.

## 3.1 `cargos`

| Maydon | Tur | Izoh |
|---|---|---|
| `id` | INTEGER PK | |
| `fingerprint` | TEXT UNIQUE | dubl kaliti (md5, 20 belgi) |
| `from_city`, `to_city` | TEXT | **kanonik nom** (`geo.CITIES` kaliti, masalan `Toshkent`) |
| `via` | TEXT | JSON ro'yxat, oraliq shaharlar |
| `load_date` | TEXT | ISO `YYYY-MM-DD` |
| `weight_t` | REAL | tonna |
| `body_type` | TEXT | `ref` \| `tent` \| `izoterm` \| `bort` \| `konteyner` \| `tral` \| `samosval` |
| `temp_c` | REAL | harorat rejimi |
| `rate`, `currency` | REAL, TEXT | asl valyutada (`UZS`/`USD`/`RUB`/`KZT`) |
| `rate_usd` | REAL | USD'ga o'girilgan (hisob shu bo'yicha) |
| `phone`, `username` | TEXT | kontakt |
| `source`, `source_msg_id` | TEXT, INT | qaysi guruh, qaysi xabar |
| `posted_at` | TEXT | e'lon vaqti |
| `confidence` | REAL | 0–1, tahlil ishonchi |
| `raw_text` | TEXT | **asl matn — hech qachon o'chirilmaydi** (tahlilni yaxshilash uchun kerak) |
| `status` | TEXT | `new` \| `taken` \| `skipped` \| `expired` |

## 3.2 `trucks`

| Maydon | Izoh |
|---|---|
| `id` | `"01"`…`"06"` — dispetcher ishlatadigan raqam |
| `plate`, `driver`, `driver_phone` | |
| `body_type` | `ref` \| `tent` \| `izoterm` |
| `capacity_t` | sig'im, tonna |
| `temp_min`, `temp_max` | refrijerator imkoniyati (tent uchun `null`) |
| `current_city` | **kanonik nom** — 4-bosqichda GPS'dan avtomatik |
| `free_date` | qachon bo'shaydi (ISO) |
| `preferred_dir` | `"RU"`, `"KZ"`, `"UZ"` yoki bo'sh |
| `fuel_l_100km` | yoqilg'i sarfi |
| `active` | 0/1 |

## 3.3 `matches`

| Maydon | Izoh |
|---|---|
| `cargo_id`, `truck_id` | UNIQUE juftlik |
| `score` | 0–100 |
| `empty_km`, `loaded_km`, `margin_usd` | tezkor filtrlash uchun |
| `details` | to'liq hisob JSON'da |
| `notified` | 0/1 |
| `decision` | `taken` \| `skipped` \| NULL — **statistika uchun eng qimmatli maydon** |

---

# 4. BIZNES QOIDALAR (o'zgartirishdan oldin kelishilsin)

## 4.1 Qat'iy filtrlar (`scoring.hard_checks`)

Bularning birortasi bajarilmasa — yuk umuman ko'rsatilmaydi:

1. `weight_t > capacity_t + 0.5` → sig'maydi
2. Yuk refrijerator talab qiladi (`temp_c` bor yoki `body_type == "ref"`),
   mashina esa ref emas
3. Yuk `tral`/`konteyner`/`samosval` talab qiladi, mashina boshqa
4. `temp_c` mashinaning `temp_min…temp_max` diapazonidan tashqarida
5. `empty_km > max_empty_km` (hozir 700 km)
6. Mashina yuklashga ulgurmaydi:
   `free_date + (empty_km / (55 × 9)) > load_date + 2 kun`

> Tent yuk ref mashinaga yuklanishi **mumkin** (qat'iy taqiq emas) — shunchaki
> ball past bo'ladi, chunki ref mashina qimmatroq resurs.

## 4.2 Ball formulasi (`scoring._score`)

| Mezon | Og'irlik | Hisob |
|---|---|---|
| Kunlik marja | **40** | `40 × min(1, margin_per_day / target_margin_per_day)` |
| Bo'sh probeg | **20** | `20 × (1 − min(1, empty_ratio / 0.35))` |
| Stavka bozorga nisbatan | **15** | `15 × min(1, rate_per_km / market_rate_per_km)` |
| Yo'nalish mosligi | **15** | mos 15, neytral 10, mos emas 5 |
| Sana mosligi | **10** | −2…+2 kun → 10; ≤5 kun → 7; uzoq → 4 |

Yig'indi mavjud mezonlar maksimumiga bo'linib 100 ga keltiriladi. Ya'ni
stavka ko'rsatilmagan bo'lsa, "marja" va "stavka" mezonlari hisobdan chiqadi
va qolganlari bo'yicha ball beriladi (lekin kartochkada ogohlantirish chiqadi).

**Bildirishnoma chegarasi:** `pipeline.NOTIFY_THRESHOLD = 65`. Undan past
mosliklar bazada qoladi, lekin xabar yuborilmaydi.

## 4.3 Xarajat modeli (`config.Costs`)

```
total_km   = empty_km + loaded_km
fuel_l     = total_km / 100 × fuel_l_100km
xarajat    = fuel_l × fuel_price_usd
           + borders × border_usd
           + total_km × driver_usd_per_km
           + total_km × road_usd_per_km
           + fixed_usd
marja      = rate_usd − xarajat
trip_days  = total_km / (avg_speed_kmh × driving_hours_per_day) + 1
```

⚠️ Hozirgi qiymatlar **taxminiy**. Buyurtmachi real raqamlarni bergandan keyin
almashtirilishi shart (7-bo'limga qarang).

## 4.4 Dubl aniqlash (`dedup.py`)

Uch bosqich, 36 soatlik oynada:

1. **Fingerprint** — `from|to|load_date|weight|body|rate_bucket(50$)` ning md5
2. **Telefon** — bir xil raqam + bir xil yo'nalish
3. **Matn o'xshashligi** — 90%+ (forward qilingan e'lonlar uchun)

---

# 5. BAJARILISHI KERAK BO'LGAN ISHLAR

Ustuvorlik tartibida. Har bir vazifa uchun **qabul qilish mezoni** berilgan —
ish shu mezon bajarilganda tugagan hisoblanadi.

---

## 5.1 [P0] Bot tugmalarini ishlatish — `bot.py`

**Muammo:** Hozir kartochkadagi `✅ Беру`, `⏭ Пропустить`, `📋 Подробнее`
tugmalari bosilganda hech narsa bo'lmaydi. Callback handler yozilmagan.

**Nima qilish kerak:**

Yangi `bot.py` fayli — Telegram Bot API'dan `getUpdates` long-polling
(webhook shart emas, VPS'da oddiyroq).

Callback formatlari `notifier._keyboard()` da allaqachon belgilangan:

| Callback | Amal |
|---|---|
| `info:<cargo_id>` | To'liq ma'lumot + asl e'lon matnini javob qilib yuborish |
| `call:<cargo_id>` | Telefon raqamini alohida xabar qilib yuborish (nusxa olish oson bo'lsin) |
| `take:<match_id>` | 5.1.1 ga qarang |
| `skip:<match_id>` | `matches.decision='skipped'`, xabarni tahrirlab "⏭ o'tkazildi" deb belgilash |

### 5.1.1 "Беру" bosilganda

```python
db.set_decision(match_id, 'taken')
db.set_cargo_status(cargo_id, 'taken')
# mashinani yangi holatga o'tkazish:
db.set_truck_position(truck_id,
                      city=cargo.to_city,
                      free_date=load_date + ceil(trip_days))
# shu yukning boshqa mashinalar bilan mosliklarini bekor qilish
# dispetcherga tasdiq: "✅ Машина №01 → Ташкент, свободна 28.09"
```

Keyin **darhol qaytish yukini taklif qilish**:
`scoring.best_roundtrip()` natijasini alohida xabar qilib yuborish.

**Qabul qilish mezoni:**
- [ ] Tugma bosilganda 2 soniya ichida javob keladi (`answerCallbackQuery`)
- [ ] `take` dan keyin `trucks` jadvalidagi holat yangilanadi
- [ ] Bitta yuk ikki marta "olib" bo'lmaydi (ikkinchi bosishda "allaqachon olingan")
- [ ] `bot.py` va `listener.py` bir vaqtda, alohida process'da ishlaydi

---

## 5.2 [P0] Bir xabarda bir nechta yuk — `parser.parse_many()`

**Muammo:** Ekspeditorlar ko'pincha bitta postda 5–10 ta yukni ro'yxat qilib
tashlaydi:

```
📦 БУГУНГИ ЮКЛАР
1. Ташкент - Москва, 20т тент, 4000$
2. Самарканд - Казань, 18т реф +2, 3800$
3. Наманган - Екатеринбург, 20т тент, 4200$
Тел: +998901234567
```

Hozirgi `parse()` buni **bitta** yuk deb oladi: `Toshkent → Yekaterinburg`
(birinchi va oxirgi shahar) — bu noto'g'ri va xavfli.

**Nima qilish kerak:**

```python
def parse_many(text, source="", msg_id=None, posted_at=None) -> list[Cargo]:
    """Bitta xabardan bir nechta yukni ajratadi."""
```

Algoritm:
1. Matndagi jami shaharlar sonini hisoblash (`geo.find_cities`)
2. Agar ≥ 4 ta bo'lsa — qatorlarga bo'lish (`\n`, `•`, `1.`, `2.` markerlari)
3. Har bir blokda ≥ 2 shahar bo'lsa — alohida `Cargo` sifatida tahlil qilish
4. Bloklarda topilmagan umumiy maydonlarni (telefon, sana, kontakt) butun
   matndan **meros qilib olish**
5. Agar bironta blok ajralmasa — eski `parse()` natijasini qaytarish

`pipeline.handle_message()` ham `parse_many()` ga o'tkazilsin.

**Qabul qilish mezoni:**
- [ ] Yuqoridagi misol 3 ta alohida yuk sifatida bazaga tushadi
- [ ] Har uchalasida ham telefon `+998901234567` bo'ladi
- [ ] Oddiy bitta yukli e'lonlar avvalgidek ishlaydi (regressiya yo'q)

---

## 5.3 [P0] Test to'plami — `tests/`

**Muammo:** Hozir test yo'q. Parser'ga har bir o'zgartirish eski holatni
buzishi mumkin, lekin buni bilib bo'lmaydi.

**Nima qilish kerak:**

```
tests/
├── ads.jsonl          # real e'lonlar + kutilgan natija
├── test_parser.py
├── test_scoring.py
└── test_dedup.py
```

`ads.jsonl` formati (har qatorda bitta):

```json
{"text": "Казань → Ташкент 20т реф +5 42 млн сум 12.05",
 "expect": {"kind":"cargo","from_city":"Qozon","to_city":"Toshkent",
            "weight_t":20,"body_type":"ref","temp_c":5,
            "rate":42000000,"currency":"UZS"}}
```

> **Buyurtmachidan so'raladi:** guruhlardan **100 ta real e'lon** nusxa qilib
> berish. Bu eng qimmatli material — parser aniqligi shunga bog'liq.
> Vaqtincha `main.py backfill` bilan yig'sa ham bo'ladi.

`test_scoring.py` da tekshiriladigan asosiy holat:

> Ikki yuk: A = 5 000 $, 900 km bo'sh probeg; B = 1 800 $, 0 km bo'sh probeg.
> Tizim **B ni yuqori baholashi kerak**, agar kunlik marja B'da yuqori bo'lsa.
> Bu loyihaning asosiy tamoyili — test bilan qotirilsin.

**Qabul qilish mezoni:**
- [ ] `pytest` ishlaydi, ≥ 30 ta test case
- [ ] Parser aniqligi hisoblanadi va konsolga chiqadi (`X/100 to'g'ri`)

---

## 5.4 [P1] Masofa aniqligi — OSRM

**Muammo:** Hozir masofa = to'g'ri chiziq × 1.30. Qozon–Toshkent uchun
2 760 km chiqadi, real yo'l ~3 100 km. Ya'ni **marja ~10–12% optimistik**.

**Nima qilish kerak (ikki variant):**

**A) Tez yechim (1 soat):** `road_factor` ni kalibrovka qilish. Buyurtmachidan
10–15 ta real reysning km'ini olib, har bir yo'nalish uchun koeffitsientni
hisoblash. Kerak bo'lsa mintaqaga qarab alohida koeffitsient
(`UZ→RU: 1.35`, `UZ ichida: 1.25`).

**B) To'g'ri yechim (1 kun):** VPS'ga OSRM o'rnatish:

```bash
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-extract -p /opt/car.lua /data/central-asia-latest.osm.pbf
# ... partition, customize
docker run -d -p 5000:5000 -v "${PWD}:/data" \
  ghcr.io/project-osrm/osrm-backend osrm-routed --algorithm mld \
  /data/central-asia-latest.osrm
```

Keyin `.env` ga `OSRM_URL=http://127.0.0.1:5000`. Kod tayyor — `geo.osrm_km()`
allaqachon yozilgan va keshlaydi.

Geofabrik'dan kerakli mintaqalar: `central-asia`, `russia`, `kazakhstan`.
RAM: ~8 GB extract uchun, ~2 GB ishlash uchun.

**Qabul qilish mezoni:**
- [ ] `geo.road_km("Qozon","Toshkent")` real yo'lga ±5% aniqlikda mos keladi
- [ ] OSRM ishlamay qolsa tizim taxminiy hisobga qaytadi (crash bo'lmaydi)

---

## 5.5 [P1] GPS integratsiyasi (4-bosqich) — `gps.py`

**Nima qilish kerak:**

Adapter interfeysi — manba almashsa kod o'zgarmasin:

```python
class GpsSource(Protocol):
    def positions(self) -> dict[str, tuple[float, float, datetime]]:
        """{truck_id: (lat, lon, vaqt)}"""
```

Uchta amalga oshirish, ustuvorlik tartibida:

1. **`TelegramLiveLocation`** — haydovchi botga "Live Location" yuboradi
   (Telegram'ning o'zida bor, 8 soatgacha). **Bepul, bugunoq ishlaydi.**
   Eng tez boshlanadigan variant.
2. **`ManualBot`** — dispetcher `/pos 01 Казань 22.09` buyrug'i bilan qo'lda.
   Zaxira variant, doim kerak bo'ladi.
3. **`WialonAdapter`** / boshqa GPS-provayder API'si — mashinalarda allaqachon
   treker bo'lsa. **Buyurtmachidan so'raladi:** qaysi treker ishlatiladi?

Kerakli yangi funksiya `geo.py` da:

```python
def nearest_city(lat: float, lon: float, max_km: float = 150) -> str | None:
    """Koordinataga eng yaqin kanonik shaharni topadi."""
```

Fon vazifasi: har 30 daqiqada `positions()` → `nearest_city()` →
`db.set_truck_position()`.

**Qabul qilish mezoni:**
- [ ] `trucks.current_city` avtomatik yangilanadi
- [ ] GPS 2 soatdan ko'p signal bermasa — dispetcherga ogohlantirish
- [ ] Qo'lda kiritilgan holat GPS'dan ustun turadi (24 soat davomida)

---

## 5.6 [P1] Statistika va o'z-o'zidan sozlanish — `analytics.py`

Bu qismning qiymati vaqt o'tishi bilan oshadi. `matches.decision` maydoni —
oltin ma'lumot: dispetcher nimani olgani va nimani tashlagani.

**Hisoblanadigan ko'rsatkichlar:**

1. **Yo'nalish bo'yicha bozor stavkasi** — oxirgi 30 kundagi e'lonlardan
   har bir `from→to` juftligi uchun mediana `$/km`. Buni `config`dagi
   yagona `market_rate_per_km` o'rniga ishlatish (ancha aniqroq ball).
2. **Guruh sifati** — qaysi guruhdan kelgan yuklar ko'proq "olingan".
   Foydasiz guruhlarni o'chirish.
3. **Ball to'g'rimi?** — "olingan" yuklarning o'rtacha bali vs "tashlangan"
   larniki. Agar farq kichik bo'lsa — formula noto'g'ri, og'irliklar
   qayta ko'rib chiqilsin.
4. **Haqiqiy vs prognoz marja** — reys tugagandan keyin dispetcher haqiqiy
   raqamni kiritadi, tizim farqni o'rganadi.

**Qabul qilish mezoni:**
- [ ] `python main.py stats --days 30` hisobot chiqaradi
- [ ] Yo'nalish bo'yicha stavka `scoring` da ishlatiladi (yetarli ma'lumot bo'lsa)

---

## 5.7 [P2] Veb-panel (5-bosqich)

**Stack tavsiyasi:** FastAPI + HTMX + Tailwind. React shart emas — panel
oddiy, SPA murakkabligi ortiqcha.

**Sahifalar:**

| Sahifa | Mazmuni |
|---|---|
| `/` Dashboard | 6 mashina kartochkasi: qayerda, qachon bo'shaydi, top-3 taklif |
| `/cargos` | Yuklar jadvali: filtr (yo'nalish, kuzov, sana, ball), asl matn |
| `/trucks` | Mashinalarni tahrirlash, holat yangilash |
| `/map` | Xarita: mashinalar + aktiv yuklar (Leaflet + OSM, bepul) |
| `/history` | Olingan reyslar, prognoz vs haqiqiy marja |
| `/settings` | `Costs` parametrlarini brauzerdan o'zgartirish |

**Autentifikatsiya:** oddiy parol yoki Telegram Login Widget. Internetga
ochiq qo'yilmasin — VPN yoki IP cheklovi.

**Qabul qilish mezoni:**
- [ ] Telefonda ham ishlaydi (dispetcher yo'lda bo'ladi)
- [ ] Sozlamalar o'zgarsa qayta ishga tushirish shart emas

---

## 5.8 [P2] Ishonchlilik va ekspluatatsiya

1. **Telegram limitlari.** Telethon `FloodWaitError` beradi — `listener.py`
   da ushlab, kutib, davom ettirish kerak. Akkaunt bloklanmasligi uchun:
   yangi akkauntni darhol 20 ta guruhga qo'shmang, 2–3 kunda asta qo'shing.
2. **Bot API limiti** — sekundiga 30 xabar, bitta chatga daqiqasiga 20.
   Bildirishnomalarni navbatga qo'yish (queue) kerak.
3. **Yuklar eskirishi** — 24 soatdan keyin `status='expired'`. Cron yoki
   fon vazifasi.
4. **Bildirishnoma spam'i** — soatiga maksimal N ta (masalan 10), qolganlari
   yig'ma xabarda ("yana 7 ta mos yuk bor, /list").
5. **Zaxira nusxa** — `cargo.db` ni kuniga bir marta nusxalash.
6. **systemd** — `baxt-listener.service` va `baxt-bot.service`,
   `Restart=always`.
7. **Loglar** — `logging` fayl'ga yozilsin, rotatsiya bilan.
8. **Sirlar** — `.session` fayli va `.env` hech qachon git'ga tushmasin
   (`.gitignore` yozilsin). `.session` fayli = Telegram akkauntga to'liq
   kirish huquqi.

---

# 6. TEXNIK QOIDALAR (agent uchun)

1. **Til:** kod va o'zgaruvchilar — inglizcha. Izohlar va foydalanuvchiga
   ko'rinadigan matnlar — o'zbekcha. Telegram kartochkasi — ruscha
   (dispetcherlar ruschada ishlaydi).
2. **Bog'liqliklar:** yangi kutubxona qo'shishdan oldin o'ylab ko'ring.
   Hozir faqat `telethon` majburiy; `rapidfuzz` va `anthropic` ixtiyoriy va
   ularsiz ham kod ishlaydi (`try/except ImportError` bilan). Shu tamoyil
   saqlansin.
3. **Shahar nomlari** hamma joyda **kanonik** (`geo.CITIES` kaliti). Bazaga
   hech qachon xom matn yozilmaydi.
4. **Pul** — hisob-kitob doim USD'da (`rate_usd`), ko'rsatish asl valyutada.
5. **`raw_text` o'chirilmaydi** — parser'ni yaxshilash uchun kerak.
6. **Sinxron kod** — `pipeline`, `scoring`, `db` sinxron. Faqat `listener`
   async. Aralashtirmaslik uchun `asyncio.to_thread` ishlatilgan.
7. **Xato bo'lsa tizim to'xtamaydi.** Bitta xabarni tahlil qilishda xato
   chiqsa — log'ga yoziladi, keyingisiga o'tiladi.
8. **`config.py` ni to'g'ridan-to'g'ri tahrirlamang** — qiymatlar `.env`
   orqali o'zgartiriladi.

---

# 7. BUYURTMACHIDAN KERAK BO'LGAN MA'LUMOT

Bularsiz hisob-kitob taxminiy bo'lib qoladi. **Eng muhimi — 1, 2 va 7.**

| № | Savol | Nima uchun kerak |
|---|---|---|
| 1 | 6 ta mashina ma'lumoti: kuzov, sig'im, harorat diapazoni, **real yoqilg'i sarfi (л/100км)** — yuk bilan va bo'sh alohida | Marja hisobining asosi |
| 2 | Oxirgi 10–15 reysning **real xarajati**: dizel (qayerda qancha), chegara, yo'l to'lovi, haydovchi ulushi, kutish | `Costs` kalibrovkasi |
| 3 | Qaysi guruhlar kuzatiladi (`@username` yoki taklif havolasi) | `sources.json` |
| 4 | Telegram akkaunt uchun alohida SIM bormi? | Asosiy raqam band bo'lmasin |
| 5 | Mashinalarda GPS treker bormi, qaysi provayder? | 5.5-bo'lim |
| 6 | Dispetcherlar soni, ish vaqti, kim qaror qabul qiladi | Bildirishnoma kimga, qanchaga |
| 7 | **100 ta real e'lon matni** (nusxa) | Parser aniqligi va testlar |
| 8 | Bojxona/TIR o'zimiz qilamizmi yoki broker? Narxi? | `border_usd` |
| 9 | Kunlik maqsadli marja qancha? | `target_margin_per_day` — ball shunga bog'liq |
| 10 | Haftada o'rtacha nechta reys, qaysi yo'nalishlar asosiy? | Ustuvorlikni to'g'ri qo'yish |

---

# 8. BOSQICHMA-BOSQICH REJA

| Hafta | Ish | Natija |
|---|---|---|
| 1 | 5.1 bot tugmalari + 5.2 ko'p yukli xabar + 5.3 testlar | Dispetcher tizimdan foydalana boshlaydi |
| 1 | Guruhlarga ulanish, `backfill`, 100 e'lon yig'ish | Real ma'lumot keladi |
| 2 | Parser'ni real ma'lumotda sozlash, `Costs` kalibrovkasi | Aniqlik 90%+ |
| 2 | 5.4 OSRM | Masofa aniq |
| 3 | 5.5 GPS (avval Telegram Live Location) | Mashina holati avtomatik |
| 4 | 5.6 statistika | Qaysi yo'nalish foydali — raqam bilan |
| 5–6 | 5.7 veb-panel | To'liq tizim |
| doimiy | 5.8 ishonchlilik | 24/7 ishlaydi |

**Sinov rejimi:** birinchi 2 hafta 1–2 mashinada. Dispetcher tizim taklifini
ko'radi, lekin qarorni o'zi qabul qiladi. Prognoz marja va haqiqiy marja
solishtiriladi. Farq 15% dan kam bo'lsa — qolgan 4 mashina ulanadi.

---

# 9. VS CODE'DA ISHNI BOSHLASH

```bash
cd baxt_cargo
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp trucks.example.json trucks.json
python main.py demo        # hammasi ishlayotganini tekshirish
```

Agent uchun vazifalarni **bittalab** bering, bir vaqtda hammasini emas.
Tavsiya etilgan tartib va so'rov namunalari:

1. `TZ.md` ning 5.3 bo'limini bajar: `tests/` papkasini yarat, mavjud
   parser xulq-atvorini qotiradigan testlar yoz. Avval testlar o'tsin.
2. `TZ.md` 5.2: `parser.parse_many()` yoz, `pipeline` ni unga o'tkaz,
   testlar bilan qopla.
3. `TZ.md` 5.1: `bot.py` yoz — callback handler.
4. `TZ.md` 5.5: `geo.nearest_city()` va `gps.py` ni Telegram Live Location
   uchun yoz.

Har bir vazifadan keyin `python main.py demo` va `pytest` ishlashini
tekshiring — regressiya bo'lmasin.
