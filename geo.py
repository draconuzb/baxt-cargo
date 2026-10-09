"""
geo.py — shahar lug'ati, koordinatalar, masofa hisobi.

Matndan shahar nomini topish (rus/uz/lotin/kirill + xatolar bilan yozilgan
variantlar) va ikki nuqta orasidagi taxminiy yo'l masofasini berish.
"""
from __future__ import annotations

import functools
import logging
import math
import re
import time
from dataclasses import dataclass

try:
    from rapidfuzz import fuzz as _fuzz
    from rapidfuzz import process as _process

    def _ratio(a: str, b: str) -> float:
        return _fuzz.ratio(a, b) / 100.0

    def _best_match(t: str, pool: list[str]) -> tuple[str, float] | None:
        hit = _process.extractOne(t, pool, scorer=_fuzz.ratio)
        return (hit[0], hit[1] / 100.0) if hit else None
except ImportError:  # rapidfuzz bo'lmasa stdlib bilan ishlaydi
    from difflib import SequenceMatcher

    def _ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()

    def _best_match(t: str, pool: list[str]) -> tuple[str, float] | None:
        best = max(pool, key=lambda a: _ratio(t, a), default=None)
        return (best, _ratio(t, best)) if best is not None else None


# (kanonik nom, lat, lon, davlat, "alias1|alias2|...")
_RAW = [
    # ---- O'zbekiston ----
    ("Toshkent", 41.3111, 69.2797, "UZ", "ташкент|тошкент|tashkent|toshkent|тшк|tashkent city"),
    ("Samarqand", 39.6542, 66.9597, "UZ", "самарканд|самарқанд|samarkand|samarqand|смк"),
    ("Buxoro", 39.7747, 64.4286, "UZ", "бухара|бухоро|bukhara|buxoro|buhara"),
    ("Andijon", 40.7821, 72.3442, "UZ", "андижан|андижон|andijan|andijon"),
    ("Farg'ona", 40.3842, 71.7843, "UZ", "фергана|фарғона|fergana|fargona|farghona"),
    ("Namangan", 40.9983, 71.6726, "UZ", "наманган|namangan"),
    ("Nukus", 42.4600, 59.6166, "UZ", "нукус|nukus"),
    ("Urganch", 41.5500, 60.6333, "UZ", "ургенч|урганч|urgench|urganch"),
    ("Xiva", 41.3775, 60.3619, "UZ", "хива|khiva|xiva"),
    ("Navoiy", 40.0844, 65.3792, "UZ", "навои|навоий|navoi|navoiy"),
    ("Jizzax", 40.1158, 67.8422, "UZ", "джизак|жиззах|jizzakh|jizzax"),
    ("Guliston", 40.4897, 68.7842, "UZ", "гулистан|гулистон|guliston|gulistan"),
    ("Termiz", 37.2242, 67.2783, "UZ", "термез|термиз|termez|termiz"),
    ("Qarshi", 38.8606, 65.7847, "UZ", "карши|қарши|karshi|qarshi"),
    ("Qo'qon", 40.5286, 70.9425, "UZ", "коканд|қўқон|kokand|qoqon|qo'qon"),
    ("Marg'ilon", 40.4711, 71.7247, "UZ", "маргилан|марғилон|margilan|margilon"),
    ("Chirchiq", 41.4689, 69.5822, "UZ", "чирчик|чирчиқ|chirchik|chirchiq"),
    ("Angren", 41.0167, 70.1436, "UZ", "ангрен|angren"),
    ("Olmaliq", 40.8446, 69.5983, "UZ", "алмалык|олмалиқ|almalyk|olmaliq"),
    ("Bekobod", 40.2206, 69.2686, "UZ", "бекабад|бекобод|bekabad|bekobod"),
    ("Denov", 38.2667, 67.9000, "UZ", "денау|денов|denau|denov"),
    ("Zarafshon", 41.5725, 64.2033, "UZ", "зарафшан|зарафшон|zarafshan|zarafshon"),
    # ---- Qozog'iston ----
    ("Almaty", 43.2220, 76.8512, "KZ", "алматы|алма-ата|алмата|алмаата|almaty|almati|almata"),
    ("Astana", 51.1694, 71.4491, "KZ", "астана|нур-султан|astana|nur-sultan"),
    ("Shymkent", 42.3417, 69.5901, "KZ", "шымкент|чимкент|shymkent|chimkent"),
    ("Aqtobe", 50.2839, 57.1670, "KZ", "актобе|актюбинск|aktobe|aqtobe"),
    ("Aqtau", 43.6510, 51.1975, "KZ", "актау|aktau|aqtau"),
    ("Atyrau", 47.0945, 51.9238, "KZ", "атырау|atyrau"),
    ("Qaraganda", 49.8047, 73.1094, "KZ", "караганда|қарағанды|karaganda"),
    ("Turkiston", 43.2973, 68.2517, "KZ", "туркестан|түркістан|turkestan"),
    ("Taraz", 42.9000, 71.3667, "KZ", "тараз|джамбул|taraz"),
    ("Qyzylorda", 44.8479, 65.5093, "KZ", "кызылорда|қызылорда|kyzylorda"),
    ("Pavlodar", 52.2873, 76.9674, "KZ", "павлодар|pavlodar"),
    ("Oskemen", 49.9787, 82.6014, "KZ", "усть-каменогорск|өскемен|oskemen|ust-kamenogorsk"),
    ("Oral", 51.2333, 51.3667, "KZ", "уральск|орал|uralsk"),
    ("Qostanay", 53.2144, 63.6246, "KZ", "костанай|кустанай|kostanay"),
    ("Semey", 50.4111, 80.2275, "KZ", "семей|семипалатинск|semey"),
    # ---- Rossiya ----
    ("Moskva", 55.7558, 37.6173, "RU", "москва|moscow|moskva|мск|масква"),
    ("Sankt-Peterburg", 59.9311, 30.3609, "RU", "санкт-петербург|питер|спб|saint petersburg|sankt-peterburg|petersburg"),
    ("Qozon", 55.7887, 49.1221, "RU", "казань|қозон|kazan|qozon|татарстан|tatariston|tatarstan"),
    ("Yekaterinburg", 56.8389, 60.6057, "RU", "екатеринбург|екб|yekaterinburg|ekaterinburg"),
    ("Novosibirsk", 55.0084, 82.9357, "RU", "новосибирск|новосиб|novosibirsk"),
    ("Chelyabinsk", 55.1644, 61.4368, "RU", "челябинск|chelyabinsk"),
    ("Samara", 53.2001, 50.1500, "RU", "самара|samara"),
    ("Ufa", 54.7388, 55.9721, "RU", "уфа|ufa"),
    ("Rostov-na-Donu", 47.2357, 39.7015, "RU", "ростов-на-дону|ростов|rostov"),
    ("Krasnodar", 45.0355, 38.9753, "RU", "краснодар|krasnodar"),
    ("Volgograd", 48.7080, 44.5133, "RU", "волгоград|volgograd"),
    ("Voronej", 51.6720, 39.1843, "RU", "воронеж|voronezh|voronej"),
    ("Nijniy Novgorod", 56.3269, 44.0059, "RU", "нижний новгород|нижний|нн|nizhny novgorod"),
    ("Perm", 58.0105, 56.2502, "RU", "пермь|perm"),
    ("Omsk", 54.9885, 73.3242, "RU", "омск|omsk"),
    ("Saratov", 51.5406, 46.0086, "RU", "саратов|saratov"),
    ("Tyumen", 57.1522, 65.5272, "RU", "тюмень|tyumen"),
    ("Orenburg", 51.7727, 55.0988, "RU", "оренбург|orenburg"),
    ("Astraxan", 46.3497, 48.0408, "RU", "астрахань|astrakhan|astraxan"),
    ("Novorossiysk", 44.7239, 37.7686, "RU", "новороссийск|novorossiysk"),
    ("Sochi", 43.6028, 39.7342, "RU", "сочи|sochi"),
    ("Belgorod", 50.5950, 36.5870, "RU", "белгород|belgorod"),
    ("Tolyatti", 53.5303, 49.3461, "RU", "тольятти|tolyatti"),
    ("Ijevsk", 56.8526, 53.2045, "RU", "ижевск|izhevsk"),
    ("Krasnoyarsk", 56.0153, 92.8932, "RU", "красноярск|krasnoyarsk"),
    ("Barnaul", 53.3479, 83.7798, "RU", "барнаул|barnaul"),
    ("Ryazan", 54.6269, 39.6916, "RU", "рязань|ryazan"),
    ("Tula", 54.1961, 37.6182, "RU", "тула|tula"),
    ("Lipetsk", 52.6031, 39.5708, "RU", "липецк|lipetsk"),
    ("Penza", 53.2007, 45.0046, "RU", "пенза|penza"),
    ("Ulyanovsk", 54.3142, 48.4031, "RU", "ульяновск|ulyanovsk"),
    ("Yaroslavl", 57.6261, 39.8845, "RU", "ярославль|yaroslavl"),
    ("Kaluga", 54.5293, 36.2754, "RU", "калуга|kaluga"),
    ("Smolensk", 54.7818, 32.0401, "RU", "смоленск|smolensk"),
    ("Bryansk", 53.2521, 34.3717, "RU", "брянск|bryansk"),
    ("Kursk", 51.7373, 36.1874, "RU", "курск|kursk"),
    ("Stavropol", 45.0428, 41.9734, "RU", "ставрополь|stavropol"),
    ("Maxachqala", 42.9831, 47.5046, "RU", "махачкала|makhachkala"),
    ("Surgut", 61.2540, 73.3962, "RU", "сургут|surgut"),
    ("Nijnevartovsk", 60.9344, 76.5531, "RU", "нижневартовск|nizhnevartovsk"),
    ("Magnitogorsk", 53.4186, 59.0472, "RU", "магнитогорск|magnitogorsk"),
    ("Naberejnie Chelni", 55.7436, 52.3958, "RU", "набережные челны|челны|naberezhnye chelny"),
    ("Kirov", 58.6035, 49.6679, "RU", "киров|kirov"),
    ("Cheboksari", 56.1439, 47.2489, "RU", "чебоксары|cheboksary"),
    ("Tver", 56.8587, 35.9176, "RU", "тверь|tver"),
    ("Vladimir", 56.1290, 40.4070, "RU", "владимир|vladimir"),
    ("Ivanovo", 57.0004, 40.9739, "RU", "иваново|ivanovo"),
    ("Oryol", 52.9651, 36.0785, "RU", "орел|орёл|oryol"),
    ("Tambov", 52.7212, 41.4523, "RU", "тамбов|tambov"),
    ("Sterlitamak", 53.6304, 55.9310, "RU", "стерлитамак|sterlitamak"),
    ("Podolsk", 55.4312, 37.5447, "RU", "подольск|podolsk"),
    ("Ximki", 55.8970, 37.4297, "RU", "химки|khimki"),
    ("Domodedovo", 55.4406, 37.7597, "RU", "домодедово|domodedovo"),
    # ---- Qirg'iziston / Tojikiston / Turkmaniston ----
    ("Bishkek", 42.8746, 74.5698, "KG", "бишкек|bishkek|фрунзе"),
    ("Osh", 40.5283, 72.7985, "KG", "ош|osh"),
    ("Jalal-Abad", 40.9333, 73.0000, "KG", "джалал-абад|жалал-абад|jalal-abad"),
    ("Dushanbe", 38.5598, 68.7870, "TJ", "душанбе|dushanbe"),
    ("Xo'jand", 40.2833, 69.6333, "TJ", "худжанд|хўжанд|khujand|xojand"),
    ("Ashxabad", 37.9601, 58.3261, "TM", "ашхабад|ашгабат|ashgabat|ashxabad"),
    ("Turkmanobod", 39.0733, 63.5786, "TM", "туркменабад|turkmenabat|turkmanobod"),
    # ---- Boshqa yo'nalishlar ----
    ("Minsk", 53.9006, 27.5590, "BY", "минск|minsk"),
    ("Brest", 52.0975, 23.7340, "BY", "брест|brest"),
    ("Gomel", 52.4345, 30.9754, "BY", "гомель|gomel"),
    ("Boku", 40.4093, 49.8671, "AZ", "баку|boku|baku"),
    ("Tbilisi", 41.7151, 44.8271, "GE", "тбилиси|tbilisi"),
    ("Yerevan", 40.1792, 44.4991, "AM", "ереван|yerevan"),
    ("Istanbul", 41.0082, 28.9784, "TR", "стамбул|istanbul"),
    ("Ankara", 39.9334, 32.8597, "TR", "анкара|ankara"),
    ("Mersin", 36.8121, 34.6415, "TR", "мерсин|mersin"),
    ("Qashqar", 39.4704, 75.9898, "CN", "кашгар|kashgar|qashqar"),
    ("Urumchi", 43.8256, 87.6168, "CN", "урумчи|urumqi|urumchi"),
    ("Kobul", 34.5553, 69.2075, "AF", "кабул|kabul|kobul"),
    ("Mozori Sharif", 36.7090, 67.1109, "AF", "мазари-шариф|mazar-i-sharif"),
    ("Tehron", 35.6892, 51.3890, "IR", "тегеран|tehran|tehron"),
    ("Varshava", 52.2297, 21.0122, "PL", "варшава|warsaw|warszawa"),
    ("Riga", 56.9496, 24.1052, "LV", "рига|riga"),
    ("Vilnyus", 54.6872, 25.2797, "LT", "вильнюс|vilnius"),
    # ---- Chegara punktlari (e'lonlarda ko'p, GeoNames ro'yxatida yo'q) ----
    ("Xayraton", 37.2333, 67.4167, "AF", "хайратан|hairatan|xayraton|hayraton"),
    ("Lotfabad", 37.5167, 59.3500, "IR", "лотфабад|лотфабод|lotfabad|lotfobod"),
    ("Saraxs", 36.5449, 61.1577, "IR", "серахс|сарахс|sarakhs|saraxs|seraxs"),
    ("Alashankou", 45.1700, 82.5700, "CN", "алашанькоу|алашанкоу|алашонко|алашанько|alashankou|alashankov|alashonko|alashanko"),
    ("Xorgos", 44.2150, 80.4100, "KZ", "хоргос|horgos|khorgos|xorgos|qorgos"),
    ("Dostyk", 45.2500, 82.4800, "KZ", "достык|dostyk|druzhba kpp"),
    ("Irkeshtam", 39.6800, 73.9000, "KG", "иркештам|irkeshtam|irkishtom"),
    ("Qorako'l", 39.4994, 63.8536, "UZ", "каракуль|каракул|qorako'l|qorakol|qorakul|karakul"),
]

# Kuratorlik qilingan qo'shimcha nomlar (lug'atda yo'q, e'lonlarda bor).
# "Водий" — Farg'ona vodiysi: Andijon/Namangan/Farg'ona, markazi Farg'ona.
_EXTRA_ALIASES = {
    "Farg'ona": "водий|vodiy|водийга|vodiyga|водийдан|vodiydan|фаргона|fargʻona",
    "Qo'qon": "кокон|quqon|қуқон|куқон|kokon",
    "Moskva": "подмосковье|podmoskovye|московская обл|моск обл",
    "Sankt-Peterburg": "ленинградская обл|санкт петербург|с-петербург|с петербург",
    "Toshkent": "ташкент обл|тошкент вил|toshkent vil",
    # O'zbekcha yozilishi (shablonli e'lonlar: "Olmaota shahri", "Ostona shahri")
    "Almaty": "olmaota|олмаота|olma-ota|алма ата",
    "Astana": "ostona|остона",
    "Qaraganda": "qorag'andi|qoragandi|қарағанды|qarag'andi",
    "Ashxabad": "ashxobod|ашхобод|ashgabad",
    "Krasnodar": "krosnadar|кроснадар|korsnador|krasnador|краснадар|кроснодар",
    "Bishkek": "bishkent|бишкент",
    # Viloyatlar: GeoNames yozuvida yo'q shakllari
    "Qarshi": "кашкадарьинская|кашкадарья|qashqadaryo vil",
    "Jizzax": "джизакская|jizzax viloyati|jizzax vil",
    "Nukus": "каракалпакия|каракалпакстан|qoraqalpog'iston|qaraqalpoq|qoraqalpoq",
    "Ufa": "башкирия|bashkiriya",
}

# Kanonik nom o'zgargan shaharlar (GeoNames nomidagi "Shahri" va h.k.) — bazadagi
# eski yozuvlar `db._migrate` da yangisiga o'tkaziladi
RENAMED = {
    "Bulung'ur Shahri": "Bulung'ur",
    "Do'stlik Shahri": "Do'stlik",
    "G'allaorol Shahri": "G'allaorol",
    "G'ijduvon Shahri": "G'ijduvon",
    "G'oliblar Qishlog'i": "G'oliblar",
    'Galaosiyo Shahri': 'Galaosiyo',
    'Ishtixon Shahri': 'Ishtixon',
    'Jomboy Shahri': 'Jomboy',
    'Juma Shahri': 'Juma',
    "Kattaqo'rg'on Shahri": "Kattaqo'rg'on",
    'Kegeyli Shahar': 'Kegeyli',
    'Kogon Shahri': 'Kogon',
    'Nishon Tumani': 'Nishon',
    'Olot Shahri': 'Olot',
    'Paxtakor Shahri': 'Paxtakor',
    'Payariq Shahri': 'Payariq',
    'Romitan Shahri': 'Romitan',
    'Shofirkon Shahri': 'Shofirkon',
    'Uchqurghon Shahri': 'Uchqurghon',
    'Urgut Shahri': 'Urgut',
    'Vobkent Shahri': 'Vobkent',
    "Xo'jayli Shahri": "Xo'jayli",
    'Zomin Shaharchasi': 'Zomin',
}

# Faqat davlat yozilgan e'lon ("Италия — Ташкент", "Rossiya ➡️ Toshkent"):
# yo'nalishning shu tomoni uchun asosiy logistika shahri (taxminiy).
# Shahar topilgan tomonga tegilmaydi (`parser._parse_route`).
COUNTRY_HUB = {
    "UZ": "Toshkent", "RU": "Moskva", "KZ": "Almaty", "KG": "Bishkek", "TJ": "Dushanbe",
    "TM": "Ashxabad", "BY": "Minsk", "TR": "Istanbul", "CN": "Urumchi", "IR": "Tehron",
    "AF": "Mozori Sharif", "AZ": "Boku", "GE": "Tbilisi", "AM": "Yerevan", "PL": "Varshava",
    "LT": "Vilnyus", "LV": "Riga", "IT": "Milan", "DE": "Berlin", "NL": "Rotterdam",
    "FR": "Paris", "ES": "Madrid", "BE": "Brussels", "CZ": "Prague", "AT": "Vienna",
    "HU": "Budapest", "FI": "Helsinki", "EE": "Tallinn", "DK": "Copenhagen", "SE": "Stockholm",
    "RO": "Bucharest", "BG": "Sofia", "SK": "Bratislava", "UA": "Kyiv", "MD": "Chisinau",
}
_COUNTRY_RAW = {
    "UZ": "узбекистан|ўзбекистон|узбекистон|o'zbekiston|ozbekiston|uzbekistan",
    "RU": "россия|рф|rossiya|russia|русия",
    "KZ": "казахстан|қозоғистон|qozog'iston|qozogiston|kazakhstan",
    "KG": "кыргызстан|киргизия|қирғизистон|qirg'iziston|kyrgyzstan",
    "TJ": "таджикистан|тожикистон|tojikiston|tajikistan",
    "TM": "туркменистан|туркманистон|turkmaniston|turkmenistan",
    "BY": "беларусь|белоруссия|belarus|беларус",
    "TR": "турция|turkiya|turkey|туркия",
    "CN": "китай|xitoy|хитой|china",
    "IR": "иран|eron|эрон|iran",
    "AF": "афганистан|афганстан|afg'oniston|afgoniston|afghanistan",
    "AZ": "азербайджан|ozarbayjon|azerbaijan",
    "GE": "грузия|gruziya|georgia",
    "AM": "армения|armaniston|armenia",
    "PL": "польша|polsha|poland",
    "LT": "литва|litva|lithuania",
    "LV": "латвия|latviya|latvia",
    "IT": "италия|italiya|italy",
    "DE": "германия|germaniya|germany",
    "NL": "нидерланды|голландия|gollandiya|netherlands",
    "FR": "франция|fransiya|france",
    "ES": "испания|ispaniya|spain",
    "BE": "бельгия|belgiya|belgium",
    "CZ": "чехия|chexiya|czech",
    "AT": "австрия|avstriya|austria",
    "HU": "венгрия|vengriya|hungary",
    "FI": "финляндия|finlandiya|finland",
    "EE": "эстония|estoniya|estonia",
    "DK": "дания|daniya|denmark",
    "SE": "швеция|shvetsiya|sweden",
    "RO": "румыния|ruminiya|romania",
    "BG": "болгария|bolgariya|bulgaria",
    "SK": "словакия|slovakiya|slovakia",
    "UA": "украина|ukraina|ukraine",
    "MD": "молдова|молдавия|moldova",
}
COUNTRY_INDEX: dict[str, str] = {}


@dataclass(frozen=True)
class City:
    name: str
    lat: float
    lon: float
    country: str


CITIES: dict[str, City] = {}
_ALIAS_INDEX: dict[str, str] = {}  # alias -> kanonik nom
CURATED = {row[0] for row in _RAW}  # qo'lda tekshirilgan asosiy shaharlar
MAJOR_MIN_POP = 150_000
MAJOR: set[str] = set(CURATED)      # panel taklif ro'yxati (datalist) uchun

# Shahar deb xato topilmasligi kerak bo'lgan keng tarqalgan so'zlar
_STOPWORDS = {
    "груз", "гружу", "машина", "машину", "тонн", "тонна", "реф", "тент", "сум",
    "млн", "срочно", "есть", "нужна", "нужен", "цена", "оплата", "контакт",
    "тел", "телефон", "загрузка", "выгрузка", "адрес", "весь", "борт", "ищу",
    "yuk", "mashina", "tonna", "kerak", "narx", "tel", "bor", "yuklash",
    "город", "рейс", "дата", "вес", "тип", "ставка", "груза", "под", "или",
}


def _norm(s: str) -> str:
    s = s.lower().replace("ё", "е").replace("ў", "у").replace("қ", "к")
    s = s.replace("ғ", "г").replace("ҳ", "х").replace("ʻ", "'").replace("`", "'")
    s = s.replace("’", "'").replace("_", " ")         # "Фаргона_Ставрополь"
    s = re.sub(r"[^\w\s'-]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


# Ko'rsatish uchun ruscha nom (panel, bot va AI ruscha gapiradi). Bazada
# kanonik nom qoladi (2-qoida) — bu faqat ko'rinish. Manba: `_RAW` dagi
# birinchi alias (hammasi ruscha): "ташкент" -> "Ташкент".
RU: dict[str, str] = {}
_RU_LOWER = {"на"}
_RU_FIX = {"Oryol": "Орёл"}            # lug'atda "ё" siz yozilgan


def _ru_title(alias: str) -> str:
    parts = re.split(r"([ -])", alias)
    return "".join(p if p in (" ", "-") or p in _RU_LOWER else p[:1].upper() + p[1:]
                   for p in parts)


# Taxminiy qidiruv (xato yozilgan nom) faqat shu nomlar ichida: kuratorlik
# qilinganlar va katta shaharlar. Kichik shaharcha bilan taxminiy moslik —
# yolg'on topilma ("Korsnador" katta shaharga yaqin, qishloqqa emas).
_FUZZY: dict[int, list[str]] = {}
FUZZY_MIN_POP = 100_000
# Faqat shu davlatlar: Yevropa/Xitoy/Eron nomlari bilan taxminiy moslik oddiy
# so'zlarni shaharga aylantirardi ("tent" -> Trento, "turi" -> Turin, "сахар" -> Ahar)
FUZZY_COUNTRIES = {"UZ", "KZ", "KG", "TJ", "TM", "RU", "BY"}
CITIES_FILE = "cities.tsv"


def _add_alias(alias: str, name: str) -> None:
    key = _norm(alias)
    if key and key not in _ALIAS_INDEX:          # birinchi kelgani ustun
        _ALIAS_INDEX[key] = name


def _load_file() -> list[tuple[str, str]]:
    """GeoNames dan yig'ilgan kengaytirilgan lug'at (`cities.tsv`).

    Kuratorlik qilingan `_RAW` ustun: u yerdagi nom, koordinata va aliaslar
    o'zgarmaydi, fayldan faqat qo'shimcha nomlar qo'shiladi. Fayl bo'lmasa
    ham tizim ishlaydi (faqat `_RAW`).
    """
    from pathlib import Path
    path = Path(__file__).parent / CITIES_FILE
    fuzzy: list[tuple[str, str]] = []
    if not path.exists():
        return fuzzy
    regions = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        kind, name, lat, lon, country, pop, ru_name, aliases = line.split("\t")
        aliases_list = [a for a in aliases.split("|") if a]
        if kind == "alias":
            regions.append((name, aliases_list))
            continue
        if name not in CITIES:
            # Kanonik nom alias qilinmaydi: u aliaslar ro'yxatida bo'lsa — keladi,
            # bo'lmasa u oddiy so'z ("Juma", "Chelak") va builder uni chiqarib tashlagan
            CITIES[name] = City(name, float(lat), float(lon), country)
            RU[name] = ru_name or name
        if int(pop or 0) >= MAJOR_MIN_POP:
            MAJOR.add(name)
        big = int(pop or 0) >= FUZZY_MIN_POP and CITIES[name].country in FUZZY_COUNTRIES
        for a in aliases_list:
            _add_alias(a, name)
            if big and len(a) >= 5:
                fuzzy.append((_norm(a), name))
    # Viloyat nomlari ("Qashqadaryo viloyati" -> Qarshi) — shaharlardan keyin:
    # shahar nomi viloyat nomidan ustun turadi
    for name, aliases_list in regions:
        if name in CITIES:
            for a in aliases_list:
                _add_alias(a, name)
    return fuzzy


def _build():
    fuzzy = []
    for name, lat, lon, country, aliases in _RAW:
        city = City(name, lat, lon, country)
        CITIES[name] = city
        RU[name] = _RU_FIX.get(name) or _ru_title(aliases.split("|")[0])
        for a in [name, *aliases.split("|")]:
            _ALIAS_INDEX[_norm(a)] = name
            fuzzy.append((_norm(a), name))
    for name, aliases in _EXTRA_ALIASES.items():
        for a in aliases.split("|"):
            _add_alias(a, name)
    fuzzy += _load_file()
    for alias, name in fuzzy:
        # faqat lug'atda shu nomga ishora qilgan aliaslar (to'qnashuvda yutqazgan emas)
        if len(alias) >= 3 and _ALIAS_INDEX.get(alias) == name:
            _FUZZY.setdefault(len(alias), []).append(alias)
    for length in _FUZZY:
        _FUZZY[length] = sorted(set(_FUZZY[length]))
    for code, aliases in _COUNTRY_RAW.items():
        for a in aliases.split("|"):
            COUNTRY_INDEX[_norm(a)] = code


_build()


def country_of_word(word: str) -> str | None:
    """So'z davlat nomimi: "Россия", "Rossiyadan", "Италии" -> "RU"/"IT"."""
    t = _norm(word)
    if t in COUNTRY_INDEX:
        return COUNTRY_INDEX[t]
    for v in _case_variants(t):
        if v in COUNTRY_INDEX:
            return COUNTRY_INDEX[v]
    return None


def ru(name) -> str:
    """Shahar nomi ruscha: "Toshkent" -> "Ташкент". Lug'atda yo'q bo'lsa — o'zi."""
    if not name:
        return "" if name is None else str(name)
    return RU.get(str(name), str(name))


_RU_TEXT = re.compile(
    r"(?<![\w'])(" + "|".join(re.escape(n) for n in sorted(RU, key=len, reverse=True))
    + r")(?![\w'])")


def ru_text(text: str) -> str:
    """Matndagi kanonik shahar nomlarini ruschaga (AI'ga beriladigan ma'lumot uchun)."""
    return _RU_TEXT.sub(lambda m: RU[m.group(1)], text) if text else text


# O'zbek kelishik qo'shimchalari (lotin va kirill; `_norm` dan keyin — ў→у, қ→к).
# Uzunidan qisqasiga: "gacha" "ga" dan oldin tekshirilsin.
_UZ_SUFFIXES = ("gacha", "гача", "dagi", "даги", "ning", "нинг", "dan", "дан",
                "ga", "га", "da", "да", "ka", "ка", "qa", "ni", "ни")


def _case_variants(t: str) -> list[str]:
    """So'zning qo'shimchasiz shakllari (shahar nomini topish uchun)."""
    out = []
    for suf in _UZ_SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            out.append(t[:-len(suf)])
    # Ruscha kelishiklar: москвы/москву/москве -> москва, казани -> казань,
    # ташкента -> ташкент
    if len(t) >= 5 and t[-1] in "ыуеи":
        out += [t[:-1] + "а", t[:-1] + "ь", t[:-1]]
    if len(t) >= 5 and t[-1] == "а":
        out.append(t[:-1])
    if len(t) >= 5 and t[-1] in "июе":          # России, Италию, Анталии -> -ия
        out.append(t[:-1] + "я")
    return out


@functools.lru_cache(maxsize=200_000)
def lookup(token: str, threshold: float = 0.87) -> str | None:
    """Bitta so'z/ibora bo'yicha shaharni topadi (aniq, keyin taxminiy)."""
    return _lookup(_norm(token), threshold)


@functools.lru_cache(maxsize=200_000)
def _lookup(t: str, threshold: float) -> str | None:
    # Kesh: guruhlardan kuniga 100 mingdan ortiq xabar keladi, so'zlar takrorlanadi —
    # 3 000 shaharli lug'atda har so'z uchun taxminiy qidiruv qimmat.
    if not t or t in _STOPWORDS or len(t) < 2:
        return None
    if t in _ALIAS_INDEX:
        return _ALIAS_INDEX[t]
    # "Toshkentdan", "Almatiga", "Москвы", "Казани" — qo'shimcha/kelishik bilan.
    # Faqat lug'atdagi ANIQ nomga mos kelsa: "tonnaga" shaharga aylanib qolmasin.
    for v in _case_variants(t):
        if v in _ALIAS_INDEX and v not in _STOPWORDS:
            return _ALIAS_INDEX[v]
    if len(t) < 4 or any(ch.isdigit() for ch in t):  # qisqa so'z — faqat aniq moslik
        return None
    pool = [a for n in range(len(t) - 3, len(t) + 4) for a in _FUZZY.get(n, ())]
    best = _best_match(t, pool)
    if best is None or best[1] < threshold:
        return None
    return _ALIAS_INDEX[best[0]]


def find_cities(text: str, max_ngram: int = 3) -> list[tuple[int, str]]:
    """Matndagi barcha shaharlarni (pozitsiya, nom) ko'rinishida qaytaradi.

    "Toshkent-Moskva" kabi chiziqcha bilan yozilgan yo'nalishlarni ham
    ajratadi, lekin "Rostov-na-Donu" kabi qo'shma nomlarni buzmaydi.
    """
    words = _norm(text).split()
    found: list[tuple[int, str]] = []
    i = 0
    while i < len(words):
        hit = None
        for n in range(min(max_ngram, len(words) - i), 0, -1):
            cand = " ".join(words[i:i + n])
            city = lookup(cand)
            if city:
                hit = (i, city, n)
                break
        if hit:
            if not found or found[-1][1] != hit[1]:
                found.append((hit[0], hit[1]))
            i += hit[2]
            continue
        # butun token mos kelmadi — chiziqcha bo'yicha bo'lib ko'ramiz
        if "-" in words[i]:
            for part in words[i].split("-"):
                city = lookup(part)
                if city and (not found or found[-1][1] != city):
                    found.append((i, city))
        i += 1
    return found


def cities_near(name: str | None, km: float) -> set[str]:
    """Shahar va uning atrofi (filtr: "Москва" = Москва + Подольск + Химки…)."""
    center = CITIES.get(name or "")
    if center is None:
        return set()
    if km <= 0:
        return {center.name}
    return {n for n, c in CITIES.items()
            if abs(c.lat - center.lat) < km / 80 and haversine_km(center, c) <= km}


def cities_in_country(code: str | None) -> set[str]:
    return {n for n, c in CITIES.items() if c.country == code}


def resolve_place(text: str | None) -> tuple[str, str] | None:
    """Filtr maydoni: shahar yoki davlat. ("city", "Moskva") | ("country", "RU") | None."""
    if not text or not text.strip():
        return None
    city = lookup(text)
    if city:
        return "city", city
    code = country_of_word(text)
    return ("country", code) if code else None


def haversine_km(a: City, b: City) -> float:
    r = 6371.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp = math.radians(b.lat - a.lat)
    dl = math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


log = logging.getLogger("geo")

def nearest_city(lat: float, lon: float, max_km: float = 150.0) -> str | None:
    """Koordinataga eng yaqin kanonik shaharni topadi (GPS uchun).

    `max_km` dan uzoq bo'lsa None qaytadi: mashina dasht o'rtasida bo'lsa
    uni 400 km narigi shaharda deb ko'rsatish xatoga olib keladi —
    bo'sh probeg noto'g'ri hisoblanadi.
    """
    try:
        point = City("", float(lat), float(lon), "")
    except (TypeError, ValueError):
        return None

    best, best_km = None, float("inf")
    for name, city in CITIES.items():
        km = haversine_km(point, city)
        if km < best_km:
            best, best_km = name, km
    return best if best_km <= max_km else None


_OSRM_CACHE: dict[tuple[str, str], float] = {}
# OSRM himoya kaliti: server yiqilsa uni vaqtincha chetlab o'tamiz
OSRM_MAX_FAILURES = 3
OSRM_COOLDOWN_SEC = 300
_osrm_failures = 0
_osrm_blocked_until = 0.0


def osrm_km(from_city: str, to_city: str, base_url: str) -> float | None:
    """Haqiqiy yo'l masofasi (OSRM server orqali). Internet kerak.

    O'z serveringizga OSRM o'rnatsangiz — bu eng aniq variant.
    Javob keshlanadi, chunki shaharlar juftligi takrorlanadi.
    """
    key = (from_city, to_city)
    if key in _OSRM_CACHE:
        return _OSRM_CACHE[key]
    a, b = CITIES.get(from_city), CITIES.get(to_city)
    if not a or not b:
        return None

    # Server o'chgan bo'lsa har bir hisob 8 soniya kutib turmasin:
    # ketma-ket xatolardan keyin OSRM vaqtincha chetlab o'tiladi.
    global _osrm_failures, _osrm_blocked_until
    if time.time() < _osrm_blocked_until:
        return None

    try:
        import json as _json
        import urllib.request
        url = (f"{base_url.rstrip('/')}/route/v1/driving/"
               f"{a.lon},{a.lat};{b.lon},{b.lat}?overview=false")
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = _json.loads(resp.read())
        km = round(data["routes"][0]["distance"] / 1000)
        _OSRM_CACHE[key] = km
        _osrm_failures = 0
        return km
    except Exception as e:
        _osrm_failures += 1
        if _osrm_failures >= OSRM_MAX_FAILURES:
            _osrm_blocked_until = time.time() + OSRM_COOLDOWN_SEC
            _osrm_failures = 0
            log.warning("OSRM javob bermadi (%s) — %d daqiqaga taxminiy "
                        "hisobga o'tildi", e, OSRM_COOLDOWN_SEC // 60)
        return None


# ---------------------------------------------------------------- koeffitsient

# To'g'ri chiziqni real yo'lga aylantiruvchi koeffitsient. Yo'nalishga qarab
# farq qiladi: O'zbekiston–Rossiya yo'li Qozog'iston orqali aylanib ketadi,
# shu sababli koeffitsient yuqori; bitta davlat ichida yo'l to'g'riroq.
#
# ⚠️ Bu qiymatlar TAXMINIY. Buyurtmachidan 10–15 ta real reysning km'ini
# olgandan keyin `python main.py calibrate` bilan qayta hisoblang —
# natija `road_factors.json` ga yoziladi va shu jadvalni bosib o'tadi.
DEFAULT_ROAD_FACTORS: dict[str, float] = {
    "UZ-RU": 1.42,    # Qozog'iston orqali aylanma
    "UZ-KZ": 1.30,
    "UZ-UZ": 1.25,
    "UZ-KG": 1.35,
    "UZ-TJ": 1.35,
    "UZ-TM": 1.30,
    "KZ-RU": 1.35,
    "KZ-KZ": 1.25,
    "RU-RU": 1.28,
    "RU-BY": 1.25,
    "UZ-TR": 1.45,
    "UZ-CN": 1.50,    # tog' dovonlari
    "default": 1.30,
}

_FACTORS_FILE_NAME = "road_factors.json"
_road_factors: dict[str, float] | None = None


def _normalize_factors(raw: dict) -> dict[str, float]:
    """Kalitlarni bir ko'rinishga keltiradi: "UZ-RU" ham, "RU-UZ" ham ishlaydi."""
    out: dict[str, float] = {}
    for key, value in raw.items():
        if key == "default":
            out[key] = float(value)
            continue
        parts = str(key).upper().split("-")
        if len(parts) == 2:
            out[route_key(parts[0], parts[1])] = float(value)
    return out


def _load_road_factors() -> dict[str, float]:
    """Kalibrovka faylini o'qiydi (bo'lmasa — taxminiy jadval)."""
    global _road_factors
    if _road_factors is None:
        factors = _normalize_factors(DEFAULT_ROAD_FACTORS)
        try:
            import json as _json
            from pathlib import Path
            path = Path(__file__).parent / _FACTORS_FILE_NAME
            if path.exists():
                factors.update(_normalize_factors(
                    _json.loads(path.read_text(encoding="utf-8"))))
        except Exception:
            pass      # buzuq fayl tizimni to'xtatmaydi
        _road_factors = factors
    return _road_factors


def reload_road_factors() -> None:
    """Kalibrovkadan keyin jadvalni qayta o'qish uchun."""
    global _road_factors
    _road_factors = None


def route_key(country_a: str, country_b: str) -> str:
    """Yo'nalish kaliti — tomonlar tartibiga bog'liq emas: "KZ-UZ" = "UZ-KZ"."""
    return "-".join(sorted((country_a, country_b)))


def road_factor_for(country_a: str, country_b: str,
                    default: float | None = None) -> float:
    factors = _load_road_factors()
    key = route_key(country_a, country_b)
    if key in factors:
        return factors[key]
    if default is not None:
        return default
    return factors.get("default", 1.30)


def calibrate(trips: list[dict]) -> dict[str, float]:
    """Real reyslardan koeffitsientlarni hisoblaydi.

    trips: [{"from": "Qozon", "to": "Toshkent", "km": 3100}, ...]
    Har bir davlat juftligi uchun mediana olinadi — bitta g'alati reys
    (yo'l ta'miri, aylanma) natijani buzmasin.
    """
    buckets: dict[str, list[float]] = {}
    for trip in trips:
        a, b = CITIES.get(trip.get("from")), CITIES.get(trip.get("to"))
        km = trip.get("km")
        if not a or not b or not km:
            continue
        straight = haversine_km(a, b)
        if straight < 50:                       # juda yaqin — koeffitsient ishonchsiz
            continue
        buckets.setdefault(route_key(a.country, b.country), []).append(km / straight)

    out: dict[str, float] = {}
    for key, values in buckets.items():
        values.sort()
        mid = len(values) // 2
        median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
        out[key] = round(median, 3)
    return out


# ---------------------------------------------------------------- masofa

def road_km(from_city: str, to_city: str, road_factor: float | None = None) -> float | None:
    """Ikki shahar orasidagi yo'l masofasi (km).

    Tartib: OSRM (aniq) → yo'nalish koeffitsienti (kalibrovka qilingan) →
    umumiy koeffitsient. OSRM ishlamay qolsa tizim to'xtamaydi, shunchaki
    taxminiy hisobga qaytadi.

    `road_factor` berilsa — u faqat yo'nalish uchun alohida koeffitsient
    bo'lmagan holatda ishlatiladi (zaxira qiymat).
    """
    a, b = CITIES.get(from_city), CITIES.get(to_city)
    if not a or not b:
        return None
    if a.name == b.name:
        return 0.0

    try:
        import config
        if getattr(config, "OSRM_URL", ""):
            km = osrm_km(from_city, to_city, config.OSRM_URL)
            if km:
                return km
    except Exception:
        pass

    return round(haversine_km(a, b) * road_factor_for(a.country, b.country, road_factor))


# Davlatlar qo'shnichiligi — chegara o'tishlar sonini taxminlash uchun
_NEIGHBORS = {
    "UZ": {"KZ", "KG", "TJ", "TM", "AF"},
    "KZ": {"UZ", "KG", "TM", "RU", "CN"},
    "KG": {"UZ", "KZ", "TJ", "CN"},
    "TJ": {"UZ", "KG", "AF", "CN"},
    "TM": {"UZ", "KZ", "IR", "AF"},
    "RU": {"KZ", "BY", "GE", "AZ", "CN", "PL", "LV", "LT"},
    "BY": {"RU", "PL", "LV", "LT"},
    "AZ": {"RU", "GE", "IR", "AM"},
    "GE": {"RU", "AZ", "TR", "AM"},
    "TR": {"GE", "IR"},
    "IR": {"TM", "TR", "AF", "AZ"},
    "AF": {"UZ", "TJ", "TM", "IR", "CN"},
    "CN": {"KZ", "KG", "TJ", "AF"},
    "PL": {"BY", "LT"},
    "LV": {"RU", "BY", "LT"},
    "LT": {"BY", "PL", "LV"},
    "AM": {"GE", "AZ", "IR"},
}


def border_crossings(from_city: str, to_city: str) -> int:
    a, b = CITIES.get(from_city), CITIES.get(to_city)
    if not a or not b:
        return 0
    if a.country == b.country:
        return 0
    if b.country in _NEIGHBORS.get(a.country, set()):
        return 1
    return 2  # tranzit orqali (masalan UZ -> RU, KZ orqali)
