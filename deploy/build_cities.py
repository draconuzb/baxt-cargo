"""GeoNames -> cities.tsv (geo.py uchun kengaytirilgan shaharlar lug'ati).

Lug'atni yangilash kerak bo'lganda (odatda shart emas):
    mkdir gn && cd gn
    curl -O https://download.geonames.org/export/dump/cities5000.zip
    curl -O https://download.geonames.org/export/dump/admin1CodesASCII.txt
    for c in UZ KZ KG TJ TM RU BY AZ GE AM TR CN AF IR UA MD MN PL LT LV EE DE NL BE FR \
             IT ES CZ SK HU RO BG AT DK SE FI; do
        curl -o alt_$c.zip https://download.geonames.org/export/dump/alternatenames/$c.zip
    done
    unzip -o '*.zip' && cd .. && python deploy/build_cities.py gn
Keyin: pytest (test_geo, test_audit_2026_10) va namunaviy e'lonlarda tekshirish.

Natija: <loyiha>/cities.tsv, ustunlar:
  kind  name  lat  lon  country  pop  ru  aliases(|)
kind = city  — yangi shahar (yoki kuratorlik qilingan shaharga qo'shimcha aliaslar)
kind = alias — viloyat/respublika nomlari: markaziy shaharga ishora (lat/lon bo'sh)
"""
import collections
import os
import re
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GN = sys.argv[1] if len(sys.argv) > 1 else os.path.join(PROJ, "gn")
sys.path.insert(0, PROJ)
# Eski natija geo'ga yuklanmasin: kuratorlik ro'yxati (_RAW) bilan boshlaymiz
if os.path.exists(os.path.join(PROJ, "cities.tsv")):
    os.remove(os.path.join(PROJ, "cities.tsv"))
import geo  # noqa: E402

CA = {"UZ", "KZ", "KG", "TJ", "TM"}
RUBY = {"RU", "BY"}
NEAR = {"AZ", "GE", "AM", "UA", "MD", "MN", "AF", "IR", "TR"}
EU = {"PL", "LT", "LV", "EE", "DE", "NL", "BE", "FR", "IT", "ES", "CZ", "SK", "HU",
      "RO", "BG", "AT", "DK", "SE", "FI"}
REGION_COUNTRIES = CA | RUBY | {"AZ", "GE", "AM", "UA", "MD"}
LANGS = {"", "ru", "uz", "kk", "ky", "tg", "tk", "en", "be", "az", "tr", "uk"}
BAD_FCODES = {"PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH"}


def keep_city(cc, pop, fcode, admin1):
    if fcode in BAD_FCODES:
        return False
    capital = fcode in ("PPLC", "PPLA")
    if cc in CA:
        return True
    if cc in RUBY:
        return pop >= 15000 or capital
    if cc in NEAR:
        return pop >= 50000 or capital
    if cc == "CN":
        return pop >= 500000 or (admin1 == "13" and pop >= 30000) or fcode == "PPLC"
    if cc in EU:
        return pop >= 100000 or fcode == "PPLC"
    return False


# Umumiy so'zlar — shahar nomi bo'lsa ham alias bo'lmaydi (e'lon matnida
# boshqa ma'noda keladi: "bor" — "есть", "baxt" — kompaniya nomi, ...)
BLACKLIST = {
    "bor", "бор", "baxt", "бахт", "marhamat", "мархамат", "kitob", "китоб", "тара", "tara",
    "свободный", "svobodny", "svobodnyy", "мир", "mir", "дружба", "druzhba", "do'stlik",
    "dostlik", "дустлик", "дўстлик", "o'zbekiston", "ozbekiston", "узбекистан", "uzbekistan",
    "ўзбекистон", "узбекистон", "россия", "rossiya", "russia", "казахстан", "qozog'iston",
    "kazakhstan", "pop", "поп", "пап", "pap", "ok", "ок", "лесной", "lesnoy", "заречный",
    "zarechnyy", "советский", "sovetskiy", "южный", "северный", "радужный", "raduzhnyy",
    "ясный", "yasnyy", "луга", "luga", "нива", "сокол", "sokol", "мирный",
    "mirnyy", "озёры", "озеры", "лиски", "kant", "кант", "тинчлик", "tinchlik",
    "navbahor", "навбахор", "bahor", "баҳор", "бахор", "obod", "обод",
    "яр", "yar", "boston", "bo'ston", "бустон", "bostan",
    "bog'dod", "bogdod", "bagdad", "багдад", "gulbahor", "furqat", "фуркат",
    "yangibozor", "yangi bozor", "tolstoy",
    "lenin", "ленин", "kirov", "киров", "orbita", "орбита", "progress", "прогресс",
    "pobeda", "победа", "druzhba", "zarya", "заря", "vostok", "восток", "rassvet", "рассвет",
    "sputnik", "спутник", "raduga", "радуга", "оса", "osa", "бура", "bura", "уст", "ust",
    "кировский", "октябрьский", "oktyabrskiy", "пролетарский", "первомайский",
    "pervomayskiy", "комсомольский", "kommunar", "коммунар", "энергетик", "energetik",
    "строитель", "stroitel", "горняк", "gornyak", "шахтёр", "шахтер", "металлург",
    "лесозаводск", "центральный", "tsentralnyy", "береговой", "морской", "речной",
    "новый", "новая", "старый", "nov", "нов", "stara", "биг", "бига", "месте", "mestre", "каштан", "сахар", "turi", "тури", "hajmi", "хажми", "tent", "ал", "al", "ак", "ak", "нур", "nur",
    "yuk", "юк", "gruz", "груз", "тент", "tent", "реф", "ref", "тонна", "tonna",
    "kerak", "керак", "narx", "нарх", "naqd", "нахт", "naxt", "avans", "аванс",
    "транзит", "tranzit", "таможня", "bojxona", "божхона", "chegara", "чегара",
    "ташкент", "tashkent",  # kuratorlik qilingan, boshqa shaharga ketmasin
    "imeni", "имени", "posyolok", "посёлок", "посёлок городского типа", "selo", "село",
    "derevnya", "деревня", "stanitsa", "станица", "aul", "аул", "qishloq", "кишлак",
    "batafsil", "transport", "транспорт", "logistika", "логистика", "ilova", "ilovani",
    "oplata", "оплата", "nal", "нал", "договорная", "kelishiladi",
    "chelyabinsk",  # kuratorlik qilingan
    "mo", "мо", "нн", "kz", "uz", "ru", "rf", "рф",
    "ош",  # kuratorlik qilingan (KG), boshqa Oshga ketmasin
    # audit 2026-10-09: kunlik matnda shaharga aylangan oddiy so'zlar
    "хам", "хамма", "ham", "hamma", "хамм", "резина", "rezina", "куба", "kuba", "orzu", "орзу",
    "улан", "ulan", "guli", "гули", "bulgan", "булган", "bo'lgan", "bolgan",
    "juma", "жума", "payshamba", "пайшанба", "chelak", "челак", "nishon", "нишон",
    "g'oliblar", "голиблар", "oliblar",
    "кара", "kara", "qora", "кора", "ала", "ala", "сары", "sary", "sariq", "сарик",
    "дон", "don", "волга", "volga", "урал", "ural", "сибирь", "sibir",
}

GENERIC = re.compile(
    r"\b(?:область|обл|край|республика|респ|автономный округ|автономная область|"
    r"ао|oblast|oblasti|oblysy|oblisi|region|province|viloyati|viloyat|respublikasi|"
    r"o'lkasi|olkasi|ulkasi|muxtor okrugi|muxtor|okrugi|okrug|kray|krai|republic|"
    r"respublika|shahri|city|ili|of|the|vilayati|welaýaty|welayaty|oblys|облысы|облусу|"
    r"вилояти|вилоят|республикаси|шаҳри|шахри|автоном|округ)\b")

TOKEN_OK = re.compile(r"^[a-zа-яёўқғҳәөүұңһі' -]+$")


def norm(s):
    return geo._norm(s)


def variants(alias):
    """Uzbek/translit variantlari: o' -> o'/o/u, g' -> g'/g (har biri alohida), defissiz."""
    out = {alias}
    if "'" in alias:
        forms = {alias}
        for src, dst in (("o'", ("o", "u")), ("g'", ("g",))):
            forms |= {f.replace(src, d) for f in forms for d in dst}
        out |= forms
        out |= {f.replace("'", "") for f in forms}
    if "-" in alias:
        out.add(alias.replace("-", " "))
        out.add(alias.replace("-", ""))
    return {norm(v) for v in out}


def ok_alias(a):
    if not a or len(a) < 3 or a in BLACKLIST or a in geo._STOPWORDS:
        return False
    if a in geo.COUNTRY_INDEX:            # davlat nomi shahar emas ("Туркменистан" shaharchasi)
        return False
    if not TOKEN_OK.match(a) or len(a.split()) > 3:
        return False
    return True


# ---------------------------------------------------------------- shaharlar
rows = {}
for line in open(os.path.join(GN, "cities5000.txt"), encoding="utf-8"):
    f = line.rstrip("\n").split("\t")
    gid, name, ascii_, alts, lat, lon, fcl, fcode, cc = f[0], f[1], f[2], f[3], f[4], f[5], f[6], f[7], f[8]
    admin1, pop = f[10], int(f[14] or 0)
    if fcl != "P" or not keep_city(cc, pop, fcode, admin1):
        continue
    rows[gid] = dict(gid=gid, name=name, ascii=ascii_, lat=float(lat), lon=float(lon), cc=cc,
                     fcode=fcode, admin1=admin1, pop=pop, alts=collections.defaultdict(list))

admin1 = {}
for line in open(os.path.join(GN, "admin1CodesASCII.txt"), encoding="utf-8"):
    code, name, ascii_, gid = line.rstrip("\n").split("\t")
    cc, a1 = code.split(".", 1)
    if cc in REGION_COUNTRIES:
        admin1[gid] = dict(cc=cc, a1=a1, name=name, ascii=ascii_, alts=collections.defaultdict(list))

wanted = set(rows) | set(admin1)
for cc in sorted(CA | RUBY | NEAR | EU | {"CN"}):
    path = os.path.join(GN, f"{cc}.txt")
    if not os.path.exists(path):
        print("yo'q:", cc)
        continue
    for line in open(path, encoding="utf-8"):
        f = line.rstrip("\n").split("\t")
        if len(f) < 4 or f[1] not in wanted:
            continue
        lang, alt = f[2], f[3]
        preferred, historic = f[4] == "1", (len(f) > 7 and f[7] == "1")
        if historic or lang not in LANGS:
            continue
        target = rows.get(f[1]) or admin1.get(f[1])
        target["alts"][lang].append((alt, preferred))

# Kuratorlik qilingan shaharlar (geo._RAW) bilan bog'lash: 30 km ichida, bir davlat
curated = {name: c for name, c in geo.CITIES.items()}
by_gid_canon = {}
for gid, r in rows.items():
    best, best_km = None, 30.0
    for name, c in curated.items():
        if c.country != r["cc"]:
            continue
        km = geo.haversine_km(c, geo.City("", r["lat"], r["lon"], ""))
        if km < best_km:
            best, best_km = name, km
    if best:
        # bir nechta GeoNames nuqtasi bitta kuratorlik shahriga — eng kattasi
        prev = by_gid_canon.get(best)
        if prev is None or rows[prev]["pop"] < r["pop"]:
            by_gid_canon[best] = gid
canon_of = {gid: name for name, gid in by_gid_canon.items()}


def ru_name(r):
    pref = [a for a, p in r["alts"]["ru"] if p]
    anyru = [a for a, _ in r["alts"]["ru"]]
    return (pref or anyru or [r["name"]])[0]


def uz_latin(r):
    for a, p in sorted(r["alts"]["uz"], key=lambda x: not x[1]):
        if re.match(r"^[A-Za-zʻ'’ -]+$", a):
            return a.replace("ʻ", "'").replace("’", "'")
    return None


# Kanonik nomlar (yangi shaharlar uchun)
used = set(geo.CITIES)
out_city = []
for gid, r in sorted(rows.items(), key=lambda kv: -kv[1]["pop"]):
    canon = canon_of.get(gid)
    if canon is None:
        base = (uz_latin(r) if r["cc"] == "UZ" else None) or r["ascii"] or r["name"]
        # "Urgut Shahri", "Zomin Shaharchasi" -> "Urgut", "Zomin"
        base = re.sub(r"\s+(?:Shahri|Shaharchasi|Shahar|Tumani|Qishlog'i)$", "", base)
        canon = base
        if canon in used:
            canon = f"{base} ({r['cc']})"
        if canon in used:
            continue
        used.add(canon)
    names = {r["name"], r["ascii"]}
    for lang, lst in r["alts"].items():
        for a, _ in lst:
            names.add(a)
    aliases = set()
    for n in names:
        n = norm(n.replace("ʻ", "'").replace("’", "'"))
        # "urgut shahri" -> "urgut" ham (e'londa qo'shimchasiz yoziladi)
        bare = re.sub(r"\s+(?:shahri|shaharchasi|shahar|tumani|qishlog'i|город)$", "", n)
        for v in variants(n) | variants(bare):
            if ok_alias(v):
                aliases.add(v)
    r["canon"] = canon
    out_city.append(dict(kind="city", name=canon, lat=r["lat"], lon=r["lon"], cc=r["cc"],
                         pop=r["pop"], ru=ru_name(r) if canon not in geo.RU else geo.RU[canon],
                         aliases=sorted(aliases)))

# ---------------------------------------------------------------- viloyatlar
OVERRIDE = {("UZ", "14"): "Toshkent", ("RU", "47"): "Moskva", ("RU", "42"): "Sankt-Peterburg",
            ("KZ", "01"): "Almaty"}
by_admin = collections.defaultdict(list)
for gid, r in rows.items():
    if "canon" in r:
        by_admin[(r["cc"], r["admin1"])].append(r)
out_alias = []
alias_city = dict(geo._ALIAS_INDEX)
country_of = {n: c.country for n, c in geo.CITIES.items()}
for c in out_city:
    country_of[c["name"]] = c["cc"]
    for al in c["aliases"]:
        alias_city.setdefault(al, c["name"])
for gid, a in admin1.items():
    key = (a["cc"], a["a1"])
    center = OVERRIDE.get(key)
    if center is None:
        # "Minsk viloyati" -> Minsk (viloyat nomi bilan atalgan shahar), PPLA emas
        base = re.sub(r"\s+", " ", GENERIC.sub(" ", norm(a["ascii"]))).strip()
        hit = alias_city.get(base)
        if hit and country_of.get(hit) == a["cc"]:
            center = hit
    if center is None:
        cands = by_admin.get(key) or []
        if not cands:
            continue
        cands.sort(key=lambda r: (r["fcode"] != "PPLA", -r["pop"]))
        center = cands[0]["canon"]
    names = {a["name"], a["ascii"]}
    for lang, lst in a["alts"].items():
        for alt, _ in lst:
            names.add(alt)
    aliases = set()
    for n in names:
        n = norm(n.replace("ʻ", "'").replace("’", "'"))
        stripped = re.sub(r"\s+", " ", GENERIC.sub(" ", n)).strip()
        for v in variants(n) | (variants(stripped) if stripped else set()):
            if ok_alias(v) and len(v) >= 4:
                aliases.add(v)
    out_alias.append(dict(kind="alias", name=center, aliases=sorted(aliases), region=a["ascii"]))

def write():
    with open(os.path.join(PROJ, "cities.tsv"), "w", encoding="utf-8", newline="\n") as f:
        f.write("# Manba: GeoNames (CC BY 4.0), yig'uvchi: deploy/build_cities.py. Qo'lda "
                "tahrirlanmaydi —\n")
        f.write("# kuratorlik qilingan shaharlar va istisnolar geo.py da "
                "(_RAW, _EXTRA_ALIASES).\n")
        f.write("# kind\tname\tlat\tlon\tcountry\tpop\tru\taliases\n")
        for c in out_city:
            f.write(f"city\t{c['name']}\t{c['lat']:.4f}\t{c['lon']:.4f}\t{c['cc']}\t{c['pop']}\t"
                    f"{c['ru']}\t{'|'.join(c['aliases'])}\n")
        for a in out_alias:
            if a["aliases"] and a["name"] in {c["name"] for c in out_city} | {r[0] for r in geo._RAW}:
                f.write(f"alias\t{a['name']}\t\t\t\t\t\t{'|'.join(a['aliases'])}\n")


# Ko'rsatiladigan nom lotin/kirill bo'lmasa ("Fasā") yoki raqamli ("Paris 15") — chiqaramiz
for c in out_city:
    if not TOKEN_OK.match(norm(c["ru"])):
        c["ru"] = c["name"]
out_city = [c for c in out_city if not re.search(r"\d", c["name"] + c["ru"])]

# Ruscha nomi o'ziga qaytmaydigan shahar (boshqa, kattaroq shaharning nomi band
# qilgan yoki nomi umumiy so'z) — chiqariladi (test_every_russian_name_resolves_back)
import importlib  # noqa: E402
curated_names = {r[0] for r in geo._RAW}
for _ in range(6):
    write()
    importlib.reload(geo)
    bad = {n for n in geo.RU if n not in curated_names and geo.lookup(geo.ru(n)) != n}
    if not bad:
        break
    out_city = [c for c in out_city if c["name"] not in bad or c["name"] in curated_names]
    print("chiqarildi:", len(bad))
write()
print("shahar:", len(out_city), "viloyat:", len(out_alias),
      "per country:", collections.Counter(c["cc"] for c in out_city).most_common())
