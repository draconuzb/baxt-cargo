# BAXT TRANSPORT — Telegram Cargo Finder

Telegram guruhlaridan yuk e'lonlarini yig'adi, tahlil qiladi, dublni tozalaydi
va **har bir mashina uchun aynan qaysi yuk foydali ekanini** hisoblab beradi.

Asosiy g'oya: eng qimmat yuk ≠ eng foydali yuk. 5 000 $ li Moskva yuki 850 km
bo'sh yurish bilan, 1 800 $ li yaqin yukdan kam foyda qoldirishi mumkin. Tizim
har bir variant uchun bo'sh probeg, yoqilg'i, chegara, yo'l xarajatini ayirib,
**kunlik sof marja** bo'yicha taqqoslaydi.

---

## Holat

| Bosqich | Holat | Qayerda |
|---|---|---|
| 1. Telegram → yuk bazasi | ✅ | `listener.py`, `parser.py`, `dedup.py` |
| — bir postda bir nechta yuk | ✅ | `parser.parse_many()` |
| 2. Mashinalar, masofa, filtrlar | ✅ | `scoring.py`, `geo.py` |
| 3. Yoqilg'i, xarajat, marja, ball | ✅ | `scoring.py` |
| Qaytish yuki | ✅ | `scoring.best_roundtrip()` |
| Bot tugmalari va buyruqlar | ✅ | `bot.py`, `actions.py` |
| 4. GPS | ✅ | `gps.py` (Telegram Live Location, fayl, Wialon) |
| Statistika va o'z-o'zidan sozlanish | ✅ | `analytics.py` |
| 5. Veb-panel | ✅ | `web.py` |
| Ishonchlilik (limitlar, loglar, zaxira) | ✅ | `notifier.py`, `listener.py`, `deploy/` |

Testlar: **355 ta**, parser aniqligi to'plamdagi 40 ta real e'londa **100%**.

⚠️ Hisob-kitobdagi xarajat raqamlari hali **taxminiy** — pastdagi
"Buyurtmachidan kerak" bo'limiga qarang.

---

## Tez ishga tushirish (Telegramsiz)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python main.py demo                # namunaviy e'lonlar → baza → kartochkalar
python main.py report --truck 01   # 01-mashina uchun eng yaxshi yuklar
python main.py stats               # statistika
pytest                             # 355 ta test + parser aniqligi
```

`demo` hech qanday sozlamasiz ishlaydi: `trucks.json` bo'lmasa namunaviy
parkni oladi, Telegram sozlanmagan bo'lsa kartochkani konsolga chiqaradi.

---

## Haqiqiy ishga tushirish

### 1. Sozlamalar

```bash
cp .env.example .env               # ichini to'ldiring
cp trucks.example.json trucks.json # 6 ta mashinani yozing
cp sources.example.json sources.json
python main.py init                # baza + mashinalar
```

- `TG_API_ID`, `TG_API_HASH` — <https://my.telegram.org> → API development tools.
- `BOT_TOKEN` — @BotFather. `DISPATCHER_CHAT_ID` — dispetcher chati (bot
  faqat shu chatdagi buyruq va tugmalarga javob beradi).
- `sources.json` — guruhlar `@username` yoki `-100...` id. Akkaunt o'sha
  guruhlarga a'zo bo'lishi kerak. **Yangi akkauntni birdaniga 20 ta guruhga
  qo'shmang** — 2–3 kun ichida asta-sekin qo'shing, aks holda Telegram
  bloklashi mumkin.

**Nega oddiy bot emas?** Bot guruh xabarlarini o'qiy olmaydi (admin qilinmasa).
Shuning uchun Telethon "user session" — tizim akkaunt nomidan **faqat o'qiydi**,
hech kimga yozmaydi. Buning uchun alohida SIM-karta/akkaunt oching.

### 2. Uchta jarayon

| Buyruq | Vazifasi |
|---|---|
| `python main.py listen` | guruhlarni tinglaydi, yuk topsa kartochka yuboradi |
| `python main.py bot` | tugmalar, buyruqlar, GPS sinxronlash, eskirgan yuklarni yopish |
| `python main.py web` | veb-panel, <http://127.0.0.1:8080> |

Birinchi marta `listen` ni **qo'lda** ishga tushiring — Telegram telefon raqam
va kod so'raydi. Keyin `deploy/` dagi systemd fayllari bilan servis qiling:

```bash
sudo cp deploy/baxt-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now baxt-listener baxt-bot baxt-web
```

Zaxira nusxa — kuniga bir marta (`crontab -e`):

```
0 3 * * * /opt/baxt_cargo/deploy/backup.sh >> /var/log/baxt-backup.log 2>&1
```

Eski e'lonlarni bazaga yig'ish (sinov uchun qulay):
`python main.py backfill --limit 300`.

---

## Dispetcher uchun

### Kartochka tugmalari

| Tugma | Nima qiladi |
|---|---|
| 📋 Подробнее | to'liq hisob + e'lonning asl matni |
| 📞 Связаться | telefon raqami alohida xabarda (nusxa olish oson) |
| ✅ Беру | mashina yuk manziliga "ko'chadi", bo'shash sanasi yangilanadi, boshqa mashinalarga taklif bekor bo'ladi va **darhol qaytish yuki** taklif qilinadi |
| ⏭ Пропустить | qaror statistika uchun saqlanadi |

Bitta yukni ikki marta olib bo'lmaydi — na botdan, na paneldan.

### So'rov bo'yicha qidirish

Dispetcher botga **buyruqsiz**, odatdagidek yozadi:

```
Тошкент Москва                 → shu yo'nalishdagi yuklar
Ташкент Москва реф             → faqat refrijerator yuki
Москва                         → Moskvaga va Moskvadan
Ташкент Москва машина 02       → faqat 02-mashina uchun
бизда нечта мошина бор         → park holati
```

Javob **bizning park bo'yicha** hisoblanadi: har bir yuk yonida qaysi mashina,
bo'sh probeg qancha, marja va **kuniga qancha** qoldirishi turadi. Sig'maydigan
yoki juda uzoq bo'sh yurish talab qiladigan yuklar ko'rsatilmaydi.

Yuk hozir bo'lmasa, bot "🔔 Сообщить, когда появится" tugmasini beradi. Shundan
keyin o'sha yo'nalishda yuk chiqishi bilan **ball chegarasidan qat'i nazar**
darhol xabar keladi: *"🔎 По вашему запросу"*. Kuzatuv 7 kundan keyin o'zi
o'chadi; ro'yxat — `/watches`, olib tashlash — `/unwatch 3`.

Buyruq shaklida ham bo'ladi: `/find Ташкент Москва`, `/fleet`, `/watch ...`.

### Bot buyruqlari

```
/list                  hozirgi eng yaxshi takliflar
/trucks                mashinalar qayerda, qachon bo'shaydi
/pos 01 Казань 22.09   mashina holatini qo'lda yangilash
/gps                   GPS bo'yicha hozir yangilash
/cargo 15              yuk kartochkasi
/done 42 1850          reys tugadi: haqiqiy marja (prognoz bilan solishtiriladi)
/stats 30              statistika
/expire                eskirgan e'lonlarni yopish
```

Soatiga 10 tadan ortiq kartochka yuborilmaydi (`MAX_NOTIFY_PER_HOUR`) —
qolganlari "yana N ta mos yuk bor — /list" yig'ma xabarida.

### Haydovchi uchun (GPS)

1. Botga yozadi: `/link 01 01A123AA` (mashina raqami + davlat raqami).
2. Skrepka → Геопозиция → **Транслировать геопозицию** (8 soatgacha).

Bot har 30 daqiqada mashina qaysi shaharda ekanini yangilaydi.
Qoidalar:
- dispetcher qo'lda kiritgan holat **24 soat** GPS'dan ustun turadi;
- 2 soatdan ko'p signal bo'lmasa — dispetcherga ogohlantirish;
- yaqin 150 km da shahar bo'lmasa, holat o'zgartirilmaydi.

### Wialon (gpsmonitor.uz)

Buyurtmachida **Wialon Local** o'rnatilgan. Ulash:

```bash
# .env
WIALON_URL=https://gpsmonitor.uz      # "/wialon/ajax.html" o'zi qo'shiladi
WIALON_TOKEN=...                      # Wialon panelidan olinadi

python main.py wialon                 # ulanishni tekshirish va trekerlar ro'yxati
```

`wialon` buyrug'i har bir treker uchun id, nom, koordinata, signal vaqti va
**qaysi mashinaga bog'langanini** ko'rsatadi. Bog'lanish avtomatik: treker
nomi mashinaning davlat raqamiga (yoki raqamiga) mos kelsa yetarli. Mos
kelmaganlari uchun `.env` ga qo'lda yoziladi:

```
WIALON_UNITS={"12345": "01", "12346": "02"}
```

Keyin holat har 30 daqiqada o'zi yangilanadi (`bot` jarayoni ichida).
Wialon javob bermasa yoki xato qaytarsa — tizim to'xtamaydi, oxirgi ma'lum
holat bilan ishlaydi va logga yozadi.

---

## Veb-panel

```bash
# .env: WEB_PASSWORD=...  (majburiy)
python main.py web                 # http://127.0.0.1:8080
```

| Sahifa | Mazmuni |
|---|---|
| `/` | mashinalar: qayerda, qachon bo'shaydi, top-3 taklif, "Olaman" tugmasi |
| `/search` | **"Toshkent Moskva"** — park bo'yicha qidiruv va kuzatuvlar |
| `/cargos` | yuklar jadvali: filtr (yo'nalish, kuzov, holat, ball, matn), asl e'lon |
| `/cargo/N` | yuk: har bir mashina bo'yicha hisob, olingandan keyin qaytish yuki |
| `/trucks` | mashinalarni tahrirlash, holatni qo'lda yangilash |
| `/truck/N` | bitta mashina: GPS holati, unga mos takliflar, reyslar tarixi |
| `/map` | xarita: mashinalar va aktiv yuklar |
| `/stats` | guruhlar sifati, yo'nalish stavkalari, ball va prognoz aniqligi |
| `/history` | olingan reyslar, prognoz vs haqiqiy marja |
| `/settings` | xarajat parametrlari — **qayta ishga tushirishsiz** |

Bosh sahifa har 45 soniyada o'zi yangilanadi — yangi takliflar sahifani
qayta yuklamasdan paydo bo'ladi. Qorong'i va yorug' rejim bor (pastki
chapdagi tugma, tanlov brauzerda saqlanadi). Telefonda ham ishlaydi,
tugmalar JavaScript'siz ham ishlaydi.

**Internetga ochiq qo'ymang.** Panel sukut bo'yicha faqat `127.0.0.1` da
ochiladi. Masofadan kirish uchun VPN yoki SSH tunnel:

```bash
ssh -L 8080:127.0.0.1:8080 user@server   # keyin brauzerda localhost:8080
```

Tashqi IP'da ochish kerak bo'lsa: `WEB_HOST=0.0.0.0` + `WEB_ALLOWED_IPS`
(VPN manzillari) + HTTPS ortidan `WEB_SECURE_COOKIE=1`.

---

## Hisob-kitob sozlamalari

Natija aniqligi shu raqamlarga bog'liq. Boshlang'ich qiymatlar `config.py` da
(u tahrirlanmaydi); o'zgartirish — panelning **/settings** sahifasida.

| Parametr | Boshlang'ich | Nima |
|---|---|---|
| `fuel_price_usd` | 0.95 | 1 litr dizel |
| `driver_usd_per_km` | 0.06 | haydovchi ulushi |
| `road_usd_per_km` | 0.025 | yo'l to'lovi, ruxsatnoma |
| `border_usd` | 120 | bitta chegara o'tish |
| `fixed_usd` | 80 | yuklash/tushirish, boshqa |
| `target_margin_per_day` | 260 | kunlik maqsad (ball shunga qarab) |
| `market_rate_per_km` | 1.15 | bozor stavkasi (zaxira, pastga qarang) |
| `max_empty_km` | 700 | bundan uzoq bo'sh yurmaymiz |

**Bozor stavkasi** avtomatik: oxirgi 30 kunda bir yo'nalishda 3 tadan ko'p
e'lon yig'ilsa, shu yo'nalishning mediana $/km'i ishlatiladi
(`analytics.route_rate`). Ma'lumot kam bo'lsa — yuqoridagi zaxira qiymat.

**Valyuta kurslari** — `config.RATES_TO_USD`, haftada bir yangilang.

### Masofa

To'g'ri chiziq × yo'nalish koeffitsienti (UZ↔RU 1.42, UZ↔KZ 1.30, ...).
Qozon–Toshkent: 3 086 km (real ~3 100). Real reyslar bilan kalibrovka:

```bash
# trips.json: [{"from": "Qozon", "to": "Toshkent", "km": 3100}, ...]
python main.py calibrate --trips trips.json   # → road_factors.json
```

Yoki o'z OSRM serveringiz (`.env`: `OSRM_URL`) — TZ 5.4-B. OSRM javob
bermasa tizim 5 daqiqaga taxminiy hisobga o'tadi, to'xtamaydi.

---

## Chalkash e'lonlar uchun LLM (ixtiyoriy)

Ba'zi e'lonlar erkin yoziladi: *"bratishka Moskvaga sovutgich kerak edi,
20 ga yaqin, kelishamiz"*. Regex bunday matnni uddalay olmaydi — model
tushunadi. Provayderni o'zingiz tanlaysiz:

```bash
# .env
LLM_PROVIDER=ollama        # o'z serveringizda, bepul (RAM ~6 GB)
LLM_PROVIDER=groq          # bepul reja bor, juda tez
LLM_PROVIDER=mistral       # bepul/arzon reja bor
LLM_PROVIDER=openrouter    # ":free" modellari bor
LLM_PROVIDER=anthropic     # eski sozlama
LLM_API_KEY=...            # ollama uchun kerak emas

python main.py llm         # ulanishni tekshirish
```

Ollama uchun (server o'zingizniki, pul umuman ketmaydi):

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:7b-instruct
# .env: LLM_PROVIDER=ollama
```

Xarajat uchta joyda ushlab turiladi:
1. model faqat regex uddalay olmagan xabarlar uchun chaqiriladi (~5%);
2. javob keshlanadi — bir xil e'lon 5 ta guruhda chiqsa ham bir marta
   so'raladi (dubl filtri bu bosqichdan keyin ishlaydi);
3. `LLM_MAX_CALLS_PER_DAY` (sukut 300) — chegara to'lsa faqat regex ishlaydi.

Model javob bermasa yoki bo'lmag'ur javob qaytarsa, tizim regex natijasi
bilan ishlayveradi. Model bergan shahar nomi ham `geo.CITIES` lug'atidan
o'tkaziladi — bazaga hech qachon xom matn tushmaydi. Regex topgan qiymat
model taklifidan ustun turadi.

**Model nomlari o'zgarib turadi.** `LLM_MODEL` bilan istalganini qo'ying,
`python main.py llm` bilan tekshiring.

## Statistika

```bash
python main.py stats --days 30
```

- qaysi guruhdan kelgan yuklar ko'proq olingan (foydasiz guruhlarni o'chiring);
- "olingan" yuklar bali "tashlangan"lardan yuqorimi — ball formulasi to'g'rimi;
- prognoz va haqiqiy marja farqi (`/done` orqali kiritiladi). Farq doimo bir
  tomonga bo'lsa — xarajat parametrlari noto'g'ri, `/settings` da to'g'rilang.

---

## Fayllar

```
config.py      boshlang'ich sozlamalar (tahrirlanmaydi — .env va /settings orqali)
settings.py    panelda o'zgartirilgan sozlamalar
geo.py         140+ shahar, masofa, koeffitsientlar, eng yaqin shahar (GPS)
parser.py      e'lon matni → Cargo; parse_many — bir postda bir nechta yuk
dedup.py       3 bosqichli dubl filtri
db.py          SQLite: cargos, trucks, matches, gps_positions, settings
scoring.py     ★ marja va 0–100 ball
pipeline.py    xabarning to'liq yo'li
actions.py     "Olaman"/"O'tkazish" — bot va panel uchun umumiy
listener.py    Telethon: guruhlarni o'qish
bot.py         dispetcher boti: tugmalar, buyruqlar, GPS qabul qilish
notifier.py    kartochka matni, soatlik cheklov
gps.py         GPS manbalari va sinxronlash
analytics.py   statistika, yo'nalish bo'yicha bozor stavkasi
web.py         veb-panel (FastAPI)
llm_parser.py  ixtiyoriy: chalkash e'lonlarni model bilan tahlil qilish
main.py        CLI
deploy/        systemd servislari, zaxira skripti
tests/         testlar; tests/ads.jsonl — real e'lonlar to'plami
```

---

## Buyurtmachidan kerak (TZ 7-bo'lim)

Kod tayyor, lekin bu ma'lumotlarsiz marja **taxminiy** bo'lib qoladi:

1. **6 ta mashinaning real ma'lumoti** — ayniqsa yoqilg'i sarfi → `trucks.json`.
2. **Oxirgi 10–15 reysning real xarajati va km'i** → `/settings` va
   `python main.py calibrate`.
3. **100 ta real e'lon matni** → `tests/ads.jsonl` ga qo'shiladi; parser
   aniqligi shu to'plamda o'lchanadi (`pytest` oxirida chiqadi).
4. Kuzatiladigan guruhlar ro'yxati → `sources.json`.
5. GPS treker bormi, qaysi provayder.
6. Kunlik maqsadli marja → `target_margin_per_day`.

**Sinov rejimi (TZ 8):** birinchi 2 hafta 1–2 mashinada. Har bir reysdan
keyin `/done` bilan haqiqiy marjani kiriting; `stats` dagi farq 15% dan kam
bo'lsa — qolgan mashinalarni ulang.
