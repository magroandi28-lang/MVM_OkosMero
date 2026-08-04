"""
Flux — a főoldal élő adatokat magyarázó asszisztense.

Két Supabase-tábla (public séma):
  public.flux_fact_snapshot  — a hiteles adatcsomag, amiből az összefoglaló készült
  public.flux_summary        — az ellenőrzött, megjelenített szöveg (gyorsítótár)

A Gemini-hívás KIZÁRÓLAG itt, a szerveren történik; a kulcs nem kerül a böngészőbe.
Ha a Gemini nem elérhető vagy a válasza nem megy át az ellenőrzésen, determinisztikus
sablon szolgál helyette — kitalált szám sosem jelenik meg.

FONTOS IDŐKEZELÉS: az `app.py` minden időbélyeget budapesti falióra szerint,
időzóna nélküli ISO-szövegként ad át. A szerver viszont UTC-ben jár (Render).
Ezért itt SOHA nem hívunk `datetime.now()`-t: minden "most" a `_most()`-ból jön,
ami budapesti faliórát ad vissza. Enélkül nyáron két órával csúszik a
"múlt / jövő" szétválasztás, és a napelemes termelés este is "várhatóként"
jelenik meg, holott már lecsengett.
"""

import os
import re
import json
import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

try:
    import psycopg
except ImportError:
    psycopg = None

DATABASE_URL = os.environ.get("DATABASE_URL", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# A `gemini-2.5-flash` ingyenes kerete NAPI 20 hívás — ezt egy megosztott
# portfólióoldal néhány látogató alatt kimeríti, és onnantól Flux csak a saját
# sablonjaiból beszél. A `gemini-2.0-flash` ingyenes kerete nagyságrendekkel
# nagyobb, ezért ez az alapértelmezés.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

# Tartalék modellek. Minden modellnek KÜLÖN kerete van, ezért ha az elsőé
# betelt, a másodikkal még mindig van élő, nyelvi modell által fogalmazott
# válasz. A lista a `GEMINI_MODEL`-lel kezdődik, utána a tartalékok jönnek —
# a duplikátumok kiesnek, hogy ugyanazt ne próbáljuk kétszer.
_TARTALEK_MODELLEK = ["gemini-2.0-flash", "gemini-2.0-flash-lite",
                      "gemini-2.5-flash-lite", "gemini-2.5-flash"]
GEMINI_MODELLEK = list(dict.fromkeys([GEMINI_MODEL] + _TARTALEK_MODELLEK))

SCHEMA_VERSION = 2
PROMPT_VERSION = "flux-v2"
LANGUAGE = "hu-HU"
TTL_PERC = 35          # a 30 perces adatfrissítéshez igazítva: egy adatállapot
                       # = egy Gemini-hívás, utána tárolt szöveg megy ki
GEMINI_TIMEOUT = 12         # a főoldali szöveghez (háttérben készül)
GEMINI_KERDES_TIMEOUT = 8   # a látogató kérdéséhez: ő vár rá, ne kelljen sokat

BUDAPEST_TZ = ZoneInfo("Europe/Budapest")


def _most():
    """Budapesti falióra, időzóna nélkül — pontosan olyan alakban, ahogy az
    app.py az ISO időbélyegeit előállítja. A szerver saját zónája nem számít."""
    return datetime.now(BUDAPEST_TZ).replace(tzinfo=None)


KOSZONTO = (
    "Szia, Flux vagyok, az OkosMérő energiaasszisztense. Élő energiapiaci és "
    "időjárási adatokból mutatom meg, mikor kedvezőbb az energia ára, hogyan "
    "alakulhat a következő órák fogyasztása, mit várunk a nap- és széltermeléstől, "
    "és hol talált szokatlan eltérést a rendszer."
)


def _napszak(m=None):
    """Melyik napszakban vagyunk — budapesti falióra szerint."""
    h = (m or _most()).hour
    if h < 5:    return "hajnal"
    if h < 9:    return "reggel"
    if h < 12:   return "délelőtt"
    if h < 14:   return "dél"
    if h < 18:   return "délután"
    if h < 22:   return "este"
    return "éjszaka"


def _udvozles(m=None):
    """Napszaknak megfelelő köszönés. Ettől látszik, hogy Flux tudja, hány óra van."""
    return {"hajnal": "Jó éjszakát!", "reggel": "Jó reggelt!",
            "délelőtt": "Szia!", "dél": "Szia!", "délután": "Szia!",
            "este": "Jó estét!", "éjszaka": "Jó estét!"}[_napszak(m)]


def elo_koszonto(m=None):
    """Élő köszöntő: köszönés + napszak. A látogató első mondatból látja,
    hogy nem egy előre megírt szöveget olvas, hanem valamit, ami tudja,
    mikor nézi az oldalt."""
    m = m or _most()
    return (f"{_udvozles(m)} Flux vagyok, az OkosMérő energiaasszisztense. "
            f"Most {m:%H:%M} van; élő energiapiaci és időjárási adatokból mondom el, "
            f"mi történik éppen a magyar villamosenergia-rendszerben.")

FLUX_SZEREP = (
    "Te vagy Flux, az OkosMérő energiapiaci irányítópult asszisztense. "
    "Magyarul, tegezve, barátságosan és élő, természetes mondatokban beszélsz — "
    "úgy, ahogy egy jó szakértő mesél arról, amit épp lát az adatokban. "
    "Lehetsz érdeklődő és lelkes, ha valami tényleg érdekes; a szakmai "
    "pontosság ettől még nem sérülhet.\n"
    "EGYETLEN kemény szabály van, ettől soha nem térhetsz el: kizárólag a "
    "megkapott JSON adatokra támaszkodhatsz, és számot csak akkor írhatsz le, "
    "ha az pontosan szerepel az adatokban. Nem becsülsz, nem kerekítesz át, "
    "nem találsz ki semmit. Ha egy adat hiányzik, arról a témáról nem beszélsz. "
    "Minden más — a hangnem, a mondatszerkesztés, a hangsúlyok — a te dolgod."
)

# A látogató KÉRDÉSÉRE más hangnem kell, mint a főoldali megállapításokhoz.
# A fenti szerep szándékosan szikár — jelentést ír, nem beszélget. Emiatt a
# válaszok gépiesnek hatottak: se megszólítás, se reakció a kérdésre, csak egy
# tőmondat adatokkal. A szám-szabály ugyanaz marad, a hang viszont emberi.
FLUX_SZEREP_KERDES = (
    "Te vagy Flux, az OkosMérő energiaasszisztense. Egy látogató most kérdezett "
    "tőled az oldalon. Úgy válaszolj, mint egy segítőkész, jókedvű szakértő kolléga, "
    "aki melletted ül: magyarul, tegezve, természetes, élő mondatokban. "
    "NEM jelentést írsz, hanem beszélgetsz.\n"
    "A válasz két-három mondat legyen ebben a menetben:\n"
    "1) rövid emberi reakció a kérdésre (pl. 'Jó kérdés', 'Nézzük', 'Épp jókor kérded'),\n"
    "2) a lényeg az élő adatokból, a számmal a mondatban,\n"
    "3) egy rövid, hasznos hozzáfűzés vagy felajánlás, hogy mit nézhet még meg.\n"
    "Vedd figyelembe a napszakot: hajnalban ne úgy írj, mintha dél lenne.\n"
    "KEMÉNY SZABÁLY, ettől soha nem térhetsz el: kizárólag a megkapott JSON "
    "adatokra támaszkodhatsz, és számot csak akkor írhatsz le, ha az pontosan "
    "szerepel az adatokban. Nem becsülsz, nem kerekítesz át, nem találsz ki semmit. "
    "Ha egy adat hiányzik, mondd meg őszintén, hogy arról most nincs adatod."
)


# Zárómondatok: nincs bennük élő adat, ezért mindig kimennek —
# a Gemini-változat végére is. Ezek hívják körbe a látogatót az oldalon.
# A modellek bemutatása. Ezek NEM élő adatok, hanem a dokumentált,
# offline validált eredmények — ezért fix szövegek, nem a Gemini írja.
# Ebből a nyolc statikus mondatból eredetileg mind a nyolc benne forgott a
# körben, az öt-hat élő megállapítás mellett. Így a látogató idejének majdnem
# a felében bemagolt, adat nélküli szöveget olvasott — ettől hatott
# élettelennek az egész. Kettő-kettő maradt, a többi kikerült.
MODELL_UZENETEK = [
    {"sor": "Az OkosMérő két külön módszert használ: a CatBoost gépi tanulási modell "
            "előrejelzi a várható fogyasztást, az STL pedig megkeresi a szokatlan "
            "eltéréseket és magyarázatot keres rájuk.",
     "szam": None, "cimke": None},
]


ZARO_UZENETEK = [
    {"sor": "Kérdezz nyugodtan a beviteli mezőben: az árakról, a töltési ablakról, a "
            "fogyasztásról, a nap- és széltermelésről, az időjárásról vagy a modell "
            "pontosságáról is válaszolok az élő adatokból.",
     "szam": None, "cimke": None},
    {"sor": "A DAM-árak és töltés fülön negyedórás bontásban látod a másnapi árakat, "
            "az Energiaelemzésen a következő órák fogyasztását, a Megújulókon a nap- és "
            "széltermelést, az ML Modell Laborban pedig a két modellt élőben.",
     "szam": None, "cimke": None},
]


# ============================================================
# 1) TÉNYEK — az app `data` store-jából
# ============================================================

def _kerekit(x, tizedes=0):
    try:
        ertek = float(x)
    except (TypeError, ValueError):
        return None
    if ertek != ertek or ertek in (float("inf"), float("-inf")):
        return None
    return round(ertek, tizedes)


def _ts(iso):
    """ISO szöveg → naiv datetime, vagy None. Sosem dob kivételt."""
    try:
        return datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None


def tenyek(data, ajanlas=None):
    """A `fetch()` által előállított `data` dict-ből kivágja azt, amit Flux használhat.

    Minden itt előállított szám valódi, mért vagy modellezett érték. Amit ez a
    függvény nem tesz bele, arról Flux nem beszélhet — sem a sablon, sem a Gemini.
    """
    if not data or data.get("kritikus_hiba"):
        return None, "invalid"

    most_ts = _most()
    ma_datum = most_ts.date()
    holnap_datum = ma_datum + timedelta(days=1)

    f = {}

    # ---------- Day-ahead árak ----------
    dam_ma = data.get("dam_ma_oras") or []
    if dam_ma:
        legolcsobb_ora = int(min(range(len(dam_ma)), key=lambda i: dam_ma[i]))
        legdragabb_ora = int(max(range(len(dam_ma)), key=lambda i: dam_ma[i]))
        f["dam"] = {
            "mai_atlag_eur_mwh": _kerekit(sum(dam_ma) / len(dam_ma), 1),
            "mai_min_eur_mwh": _kerekit(min(dam_ma), 1),
            "mai_max_eur_mwh": _kerekit(max(dam_ma), 1),
            "mai_legolcsobb_ora": f"{legolcsobb_ora:02d}:00",
            "mai_legdragabb_ora": f"{legdragabb_ora:02d}:00",
            "mai_orak_szama": len(dam_ma),
            "holnapi_ar_publikalva": bool(data.get("holnapi_ar")),
        }

    # A holnapi szelvény a negyedórás görbéből — csak ha már publikálták.
    negyed = data.get("negyed") or {}
    if data.get("holnapi_ar") and negyed.get("ido"):
        holnapi = [float(a) for t, a in zip(negyed["ido"], negyed.get("ar") or [])
                   if (_ts(t) or most_ts).date() == holnap_datum]
        if holnapi:
            f.setdefault("dam", {})
            f["dam"]["holnapi_atlag_eur_mwh"] = _kerekit(sum(holnapi) / len(holnapi), 1)
            f["dam"]["holnapi_min_eur_mwh"] = _kerekit(min(holnapi), 1)
            f["dam"]["holnapi_max_eur_mwh"] = _kerekit(max(holnapi), 1)

    # ---------- A hat KPI-kártya értékei ----------
    # Pontosan az, amit a látogató a fejléc alatt lát.
    kartyak = {}
    if data.get("aho") is not None:
        kartyak["budapest_homerseklet_c"] = _kerekit(data["aho"])
    if data.get("eur_huf") is not None:
        kartyak["eur_huf"] = _kerekit(data["eur_huf"], 1)
    if ajanlas:
        kartyak["jelenlegi_dam_ar_eur_mwh"] = _kerekit(ajanlas.get("akt_ar"), 1)
    if kartyak:
        f["kartyak"] = kartyak

    # ---------- Töltési ajánlás ----------
    if ajanlas:
        f["toltes"] = {
            "aktualis_ar_eur_mwh": _kerekit(ajanlas.get("akt_ar"), 1),
            "ajanlott_kezdet": ajanlas.get("aj_kezd"),
            "ajanlott_veg": ajanlas.get("aj_veg"),
            "ajanlott_ar_eur_mwh": _kerekit(ajanlas.get("aj_ar"), 1),
            "most_is_kedvezo": bool(ajanlas.get("most_jo")),
            "negativ_ar_az_ablakban": bool(ajanlas.get("negativ")),
        }
        if ajanlas.get("akt_veg"):
            f["toltes"]["kedvezo_allapot_vege"] = ajanlas["akt_veg"]
        altok = ajanlas.get("altok") or []
        if altok:
            f["toltes"]["tovabbi_ablakok"] = [
                {"kezdet": t, "ar_eur_mwh": _kerekit(a, 1)} for t, a in altok[:2]]

    # ---------- Fogyasztási előrejelzés ----------
    eredm = data.get("eredm") or []
    if eredm:
        ertekek = [r["fogyasztas"] for r in eredm]
        csucs = max(eredm, key=lambda r: r["fogyasztas"])
        volgy = min(eredm, key=lambda r: r["fogyasztas"])
        f["fogyasztas"] = {
            "elorejelzett_csucs_mwh": _kerekit(csucs["fogyasztas"]),
            "csucs_idopont": csucs["datum"],
            "elorejelzett_min_mwh": _kerekit(volgy["fogyasztas"]),
            "min_idopont": volgy["datum"],
            "orak_szama": len(eredm),
            "elorejelzes_kezdete": eredm[0]["datum"],
            "elorejelzes_vege": eredm[-1]["datum"],
            "modell": "CatBoost V10",
        }
        # A most futó óra jóslata — erre kérdez rá a legtöbb látogató.
        akt_ora = most_ts.replace(minute=0, second=0, microsecond=0)
        akt = next((r for r in eredm if _ts(r["datum"]) == akt_ora), None)
        if akt:
            f["fogyasztas"]["aktualis_ora_mwh"] = _kerekit(akt["fogyasztas"])
            f["fogyasztas"]["aktualis_ora"] = akt["datum"]
        # Elmúlt-e már a mai csúcs? Éjjel igen — ilyenkor nem a csúccsal kell
        # kezdeni, hanem azzal, amit a modell a MOST futó órára jósol.
        csucs_ts = _ts(csucs["datum"])
        f["fogyasztas"]["csucs_meg_hatravan"] = bool(csucs_ts and csucs_ts > most_ts)
        # Este a 24 órás előrejelzési ablak átnyúlik holnapra, ezért a csúcs
        # órája is holnapi lehet. Ilyenkor tilos "mai csúcsot" mondani rá.
        f["fogyasztas"]["csucs_ma_van"] = bool(csucs_ts and csucs_ts.date() == ma_datum)
        heti = data.get("heti_atlag")
        if heti:
            ora = (_ts(csucs["datum"]) or most_ts).hour
            if 0 <= ora < len(heti):
                f["fogyasztas"]["elteres_heti_atlagtol_mwh"] = _kerekit(
                    csucs["fogyasztas"] - heti[ora])

    # Eltérések — ezekből lesz a "mennyivel több / kevesebb" mondat.
    heti = data.get("heti_atlag")
    if heti and eredm:
        atl = sum(heti) / len(heti)
        vart = sum(r["fogyasztas"] for r in eredm) / len(eredm)
        f.setdefault("fogyasztas", {})["elteres_heti_atlagtol_szazalek"] = _kerekit(
            (vart - atl) / atl * 100, 1) if atl else None
        f["fogyasztas"]["heti_atlag_mwh"] = _kerekit(atl)

    mert = data.get("mert_fogyasztas")
    if mert:
        f["mert_fogyasztas"] = {"ertek_mwh": _kerekit(mert["ertek"]),
                                "idopont": mert["idopont"]}

    teg = data.get("tegnaphoz")
    if teg and teg.get("tegnapi_mwh"):
        f["tegnapi_osszevetes"] = {k: v for k, v in teg.items() if v is not None}

    # ---------- Modell pontossága ----------
    val = data.get("validacio") or {}
    # A CatBoost és a MAVIR összevetése CSAK lezárt, teljes napon korrekt.
    # A futó nap részleges órái nem hasonlíthatók össze, ezért kimaradnak.
    ma_str = str(ma_datum)
    lezart = next((n for n in (val.get("napok") or [])
                   if n.get("nap") != ma_str and (n.get("orak") or 0) >= 20), None)
    if lezart:
        f["modell_pontossag"] = {
            "nap": lezart["nap"],
            # A frissített jóslat már ismeri a legutóbbi mért órákat, ezért a
            # MAVIR napra előre készült előrejelzésével NEM ez az összemérhető.
            "catboost_frissitett_mae_mwh": _kerekit(lezart["cb"], 1),
            # Az ELSŐ jóslat készül a leghosszabb horizonton — ez a korrekt
            # összevetés a MAVIR day-ahead előrejelzésével.
            "catboost_elso_jóslat_mae_mwh": _kerekit(lezart.get("first"), 1),
            "mavir_mae_mwh": _kerekit(lezart["mv"], 1),
            "orak_szama": lezart["orak"],
            "megjegyzes": ("A frissített jóslat ismeri a legutóbbi mért órákat, "
                           "ezért a MAVIR-ral az ELSŐ, hosszabb horizontú jóslat "
                           "hasonlítható össze korrektül."),
        }

    het = val.get("het") or {}
    if het.get("orak"):
        f["heti_merleg"] = {
            "kiertekelt_orak": int(het["orak"]),
            "catboost_frissitett_mae_mwh": _kerekit(het.get("cb"), 1),
            "catboost_elso_jóslat_mae_mwh": _kerekit(het.get("first"), 1),
            "mavir_mae_mwh": _kerekit(het.get("mv"), 1),
        }

    # ---------- Adatminőség-őr (STL) ----------
    naplo = val.get("naplo") or []
    if naplo:
        nyitott = sum(1 for r in naplo if r.get("kategoria") == "rejtely")
        kat = {}
        for r in naplo:
            k = r.get("kategoria") or "besorolatlan"
            kat[k] = kat.get(k, 0) + 1
        legfrissebb = naplo[0]
        f["adatminoseg"] = {
            # Az ablak hossza tényadatként is szerepel: enélkül az "elmúlt 7
            # napban" fordulat 7-ese ismeretlen számnak minősült, és emiatt a
            # szám-ellenőrzés a teljes, egyébként helyes mondatot eldobta.
            "ablak_napok": 7,
            "jelzes_7_nap": len(naplo),
            "nyitott": nyitott,
            "megmagyarazva": len(naplo) - nyitott,
            # A kategóriák MAGYARÁZATOK, nem feladatok:
            "extrem_idojaras_db": kat.get("extrem", 0),
            "alacsony_napsugarzas_db": kat.get("napelem", 0),
            "homersekleti_fordulat_db": kat.get("fordulat", 0),
            "meg_vizsgalando_db": kat.get("rejtely", 0),
            "legfrissebb": {
                "idopont": legfrissebb.get("ido"),
                "kategoria": legfrissebb.get("kategoria"),
                "elteres_mwh": _kerekit(legfrissebb.get("residual")),
                "homerseklet_c": _kerekit(legfrissebb.get("homerseklet")),
            },
        }

    stl = data.get("stl") or {}
    if stl.get("stat"):
        f["stl"] = {
            "vizsgalt_napok": int(data.get("stl_napok") or 0),
            "szokatlan_orak_db": int(stl.get("anomalia_db") or 0),
            "trend_iranya": stl["stat"].get("irany"),
            "kuszob_mwh": _kerekit(stl["stat"].get("kuszob")),
        }

    # ---------- Megújulók: MA és HOLNAP külön ----------
    # Ez volt a hibás rész. A `fc_nap` sorozat a célablak végéig tart, ami
    # 14:00 után átnyúlik a HOLNAPI napra. Ha ezen egyben veszünk maximumot,
    # este a HOLNAPI déli csúcs jelenik meg "ma még várható" értékként.
    # Ezért minden órát a saját dátuma szerint sorolunk be.
    meg = data.get("megujulo") or {}
    if meg.get("fc_nap") and meg.get("fc_szel"):
        idok = meg.get("ido") or []
        fc_nap, fc_szel = meg["fc_nap"], meg["fc_szel"]
        tny_nap = meg.get("tny_nap") or []
        tny_szel = meg.get("tny_szel") or []

        # (időbélyeg, érték) párokat tartunk, mert a csúcs ÓRÁJA is kell:
        # "13:00 körül tetőzik" hajnali egykor is értelmes mondat, a
        # "nap hátralévő részében várható" viszont ilyenkor képtelenség —
        # hajnali egykor az egész nap hátravan.
        ma_nap, ma_szel = [], []            # mai óra, jóslat
        jovo_nap, jovo_szel = [], []        # mai óra, még hátra van
        mult_nap = []                       # mai óra, már elmúlt
        holnap_nap, holnap_szel = [], []    # holnapi óra
        mert_nap_ma, mert_szel_ma = [], []  # mai óra, MÉRT termelés
        utolso_mert_nap = utolso_mert_szel = None

        for i, iso in enumerate(idok):
            if i >= len(fc_nap) or i >= len(fc_szel):
                break
            t = _ts(iso)
            if t is None:
                continue
            n, sz = float(fc_nap[i]), float(fc_szel[i])
            if t.date() == ma_datum:
                ma_nap.append((t, n)); ma_szel.append((t, sz))
                (jovo_nap if t > most_ts else mult_nap).append((t, n))
                if t > most_ts:
                    jovo_szel.append((t, sz))
                if i < len(tny_nap) and tny_nap[i] is not None:
                    mert_nap_ma.append(float(tny_nap[i]))
                    utolso_mert_nap = (t, float(tny_nap[i]))
                if i < len(tny_szel) and tny_szel[i] is not None:
                    mert_szel_ma.append(float(tny_szel[i]))
                    utolso_mert_szel = (t, float(tny_szel[i]))
            elif t.date() == holnap_datum:
                holnap_nap.append((t, n)); holnap_szel.append((t, sz))

        def _csucs(parok):
            """A legnagyobb érték és az órája."""
            return max(parok, key=lambda p: p[1]) if parok else (None, None)

        mg = {}
        if ma_nap:
            t_cs, v_cs = _csucs(ma_nap)
            mg["nap_mai_csucs_mw"] = _kerekit(v_cs)
            mg["nap_mai_csucs_ido"] = t_cs.isoformat()
            # A csúcs a jövőben van-e még? Ettől függ, hogy jelen/jövő vagy
            # múlt időben szabad-e beszélni róla.
            mg["nap_csucs_meg_hatravan"] = bool(t_cs > most_ts)
        if ma_szel:
            t_sz, v_sz = _csucs(ma_szel)
            mg["szel_mai_csucs_mw"] = _kerekit(v_sz)
            mg["szel_mai_csucs_ido"] = t_sz.isoformat()
            mg["szel_csucs_meg_hatravan"] = bool(t_sz > most_ts)
            mg["szel_mai_atlag_mw"] = _kerekit(
                sum(v for _, v in ma_szel) / len(ma_szel))

        # A "várható" szó CSAK a MAI hátralévő órákra igaz. Este a napelemes
        # termelés már lecsengett, ilyenkor múlt időben beszélünk róla.
        t_jovo, v_jovo = _csucs(jovo_nap)
        if v_jovo is not None and v_jovo > 50:
            mg["nap_hatralevo_csucs_mw"] = _kerekit(v_jovo)
            mg["nap_hatralevo_csucs_ido"] = t_jovo.isoformat()
        else:
            mg["nap_termeles_mara_lezarult"] = True
            t_mult, v_mult = _csucs(mult_nap)
            if v_mult is not None:
                mg["nap_mai_tetozes_mw"] = _kerekit(v_mult)
                mg["nap_mai_tetozes_ido"] = t_mult.isoformat()
        if jovo_szel:
            mg["szel_hatralevo_csucs_mw"] = _kerekit(_csucs(jovo_szel)[1])

        # A holnapi terv KÜLÖN mező, saját címkével — sosem keveredhet a maival.
        if holnap_nap:
            t_h, v_h = _csucs(holnap_nap)
            mg["nap_holnapi_csucs_mw"] = _kerekit(v_h)
            mg["nap_holnapi_csucs_ido"] = t_h.isoformat()
        if holnap_szel:
            mg["szel_holnapi_csucs_mw"] = _kerekit(_csucs(holnap_szel)[1])

        if (meg.get("hiba_nap") or {}).get("mae") is not None:
            mg["nap_mai_mae_mw"] = _kerekit(meg["hiba_nap"]["mae"])
        if (meg.get("hiba_szel") or {}).get("mae") is not None:
            mg["szel_mai_mae_mw"] = _kerekit(meg["hiba_szel"]["mae"])
        if mert_nap_ma:
            mg["nap_eddigi_merte_csucs_mw"] = _kerekit(max(mert_nap_ma))
        if mert_szel_ma:
            mg["szel_eddigi_merte_csucs_mw"] = _kerekit(max(mert_szel_ma))
        if utolso_mert_nap:
            mg["nap_utolso_mert_mw"] = _kerekit(utolso_mert_nap[1])
            mg["nap_utolso_mert_idopont"] = utolso_mert_nap[0].isoformat()
        if utolso_mert_szel:
            mg["szel_utolso_mert_mw"] = _kerekit(utolso_mert_szel[1])
            mg["szel_utolso_mert_idopont"] = utolso_mert_szel[0].isoformat()
        if mg:
            f["megujulok"] = mg

    # ---------- Napi időjárási kilátás ----------
    daily = data.get("daily") or {}
    if daily.get("max") and daily.get("min"):
        f["idojaras"] = {
            "mai_max_c": _kerekit(daily["max"][0]),
            "mai_min_c": _kerekit(daily["min"][0]),
            "forras": data.get("ido_forras"),
        }
        if len(daily["max"]) > 1 and len(daily["min"]) > 1:
            f["idojaras"]["holnapi_max_c"] = _kerekit(daily["max"][1])
            f["idojaras"]["holnapi_min_c"] = _kerekit(daily["min"][1])

    hianyzo = data.get("hianyzo") or []
    minoseg = "complete" if not hianyzo else "partial"
    if not f:
        return None, "invalid"

    f["_meta"] = {
        "frissites": data.get("frissites"),
        "hianyzo_forrasok": hianyzo,
        # A napszak a szövegek igeidejét és a köszönést vezérli. Enélkül a
        # modell nem tudja, hogy hajnal van-e vagy dél.
        "napszak": _napszak(most_ts),
        "helyi_ido": most_ts.strftime("%H:%M"),
    }
    return f, minoseg


def _hash(facts):
    """A `_meta` KIMARAD: abban a másodpercre pontos frissítési idő van, ami
    minden lekéréskor változik. Ha benne hagynánk, minden oldalletöltés új
    gyorsítótár-kulcsot kapna, és minden látogató új Gemini-hívást indítana."""
    lenyeg = {k: v for k, v in facts.items() if k != "_meta"}
    kanon = json.dumps(lenyeg, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(kanon.encode("utf-8")).hexdigest()


def _cache_key(facts_hash):
    # A napszak resze a kulcsnak: enelkul a koszones atcsuszna a
    # napszakhataron ("Jo estet!" hajnali egykor).
    return (f"{facts_hash}:{GEMINI_MODEL}:{PROMPT_VERSION}:{LANGUAGE}"
            f":{_napszak()}")


# ============================================================
# 2) GYORSÍTÓTÁR — folyamaton belül, adatbázis elé
#
# Az adatbázis-olvasás is hálózati kör (Supabase, TLS-kézfogás). Ha ugyanaz
# az adatállapot már egyszer megkapta a szövegét, azt a folyamat memóriájából
# adjuk vissza: nulla hálózat, azonnali válasz.
# ============================================================

_MEMO = {}
_MEMO_LOCK = threading.Lock()
_MEMO_MAX = 32

# ---- Kvóta-védelem ----
# Ha egyszerre többen nyitják meg az oldalt (pl. egy elküldött pályázat után),
# a Gemini ingyenes kerete percek alatt kimerülhet. Onnantól minden hívás
# 429-cel jön vissza — de mindegyik VÁRAKOZÁSSAL, tehát a látogató úgy éli
# meg, hogy Flux lassú és néma. Ezért az első kvóta-hiba után egy ideig
# meg sem próbáljuk: a determinisztikus válasz azonnal megy ki.
_KVOTA_LOCK = threading.Lock()
_KVOTA_TILTAS_PERC = 10
# Modellenként külön tiltás: ha az egyik kerete betelt, a másiké még élhet.
_kvota_tiltva_eddig = {}

# A főoldali szöveg legyártása egyszerre csak EGY szálon fusson. Enélkül
# öt egyidejű látogató öt külön Gemini-hívást indítana ugyanarra az
# adatállapotra — ötszörös kvótafogyasztás ugyanazért az eredményért.
_GYARTAS_LOCK = threading.Lock()

# A kérdésekre adott válaszok is gyorsítótárba kerülnek. Több látogató
# jellemzően ugyanazt kérdezi ("mit tudsz?", "mennyi az ár?"), ezért ez
# érdemben csökkenti a hívások számát.
_VALASZ_MEMO = {}
_VALASZ_MEMO_MAX = 64
_VALASZ_TTL = 600


def _valasz_memo_ir(kulcs, ertek):
    with _MEMO_LOCK:
        if len(_VALASZ_MEMO) >= _VALASZ_MEMO_MAX:
            legregebbi = min(_VALASZ_MEMO, key=lambda k: _VALASZ_MEMO[k]["lejar"])
            _VALASZ_MEMO.pop(legregebbi, None)
        _VALASZ_MEMO[kulcs] = {"ertek": ertek, "lejar": time.time() + _VALASZ_TTL}


def _kvota_blokkolt(modell):
    with _KVOTA_LOCK:
        return time.time() < _kvota_tiltva_eddig.get(modell, 0.0)


def _kvota_jelez(modell):
    """Kvóta-hiba után egy ideig meg sem hívjuk EZT a modellt."""
    with _KVOTA_LOCK:
        _kvota_tiltva_eddig[modell] = time.time() + _KVOTA_TILTAS_PERC * 60
    print(f"[FLUX] {modell}: kerete betelt — {_KVOTA_TILTAS_PERC} percig "
          f"kihagyom, jöhet a következő modell.", flush=True)


def _elerheto_modellek():
    """Azok a modellek, amelyeknek most nem tiltott a keretük."""
    return [m for m in GEMINI_MODELLEK if not _kvota_blokkolt(m)]


def _frissit_koszonto(uzenetek):
    """A köszöntőben PERCRE PONTOS óra van, a gyorsítótár viszont 35 percig él.
    Emiatt hajnali fél háromkor még az egy órakor legyártott mondat ment ki
    ("Most 01:55 van"), és jogosan tűnt úgy, hogy Flux nem tudja, hány óra van.
    A tárolt szöveg első eleme ezért mindig frissen készül."""
    if not uzenetek:
        return uzenetek
    elso = uzenetek[0]
    if isinstance(elso, dict) and elso.get("kezdo"):
        uj_lista = list(uzenetek)
        uj_lista[0] = {"sor": elo_koszonto(), "szam": None,
                       "cimke": None, "kezdo": True}
        return uj_lista
    return uzenetek


def _memo_olvas(kulcs):
    with _MEMO_LOCK:
        rec = _MEMO.get(kulcs)
        if rec and rec["lejar"] > time.time():
            return rec["ertek"]
        if rec:
            _MEMO.pop(kulcs, None)
    return None


def _memo_ir(kulcs, ertek, ttl_sec=TTL_PERC * 60):
    with _MEMO_LOCK:
        if len(_MEMO) >= _MEMO_MAX:
            legregebbi = min(_MEMO, key=lambda k: _MEMO[k]["lejar"])
            _MEMO.pop(legregebbi, None)
        _MEMO[kulcs] = {"ertek": ertek, "lejar": time.time() + ttl_sec}


# ============================================================
# 3) ADATBÁZIS
# ============================================================

def _db_ok():
    return bool(DATABASE_URL) and psycopg is not None


def _connect():
    return psycopg.connect(DATABASE_URL, connect_timeout=8, prepare_threshold=None)


def _snapshot_ment(facts, minoseg, facts_hash, forras_allapot):
    """Beszúrja (vagy megkeresi) az adatcsomagot, és visszaadja az id-t."""
    lejar = datetime.now(timezone.utc) + timedelta(minutes=TTL_PERC)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into public.flux_fact_snapshot
                (facts_hash, schema_version, data_as_of, quality_status,
                 facts, source_status, expires_at)
            values (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
            on conflict (facts_hash) do update set facts_hash = excluded.facts_hash
            returning id
            """,
            (facts_hash, SCHEMA_VERSION, datetime.now(timezone.utc), minoseg,
             json.dumps(facts, ensure_ascii=False, default=str),
             json.dumps(forras_allapot, ensure_ascii=False), lejar),
        )
        return cur.fetchone()[0]


def _summary_olvas(cache_key):
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select summary_text
            from public.flux_summary
            where cache_key = %s
              and (expires_at is null or expires_at > now())
            order by created_at desc
            limit 1
            """,
            (cache_key,),
        )
        sor = cur.fetchone()
        return sor[0] if sor else None


def _summary_ment(snapshot_id, cache_key, model_name, allapot, szoveg, hibak):
    lejar = datetime.now(timezone.utc) + timedelta(minutes=TTL_PERC)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into public.flux_summary
                (snapshot_id, cache_key, model_name, prompt_version, language_code,
                 result_status, summary_text, validation_errors, expires_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            on conflict (cache_key) do nothing
            """,
            (snapshot_id, cache_key, model_name, PROMPT_VERSION, LANGUAGE,
             allapot, szoveg, json.dumps(hibak, ensure_ascii=False), lejar),
        )


# ============================================================
# 4) DETERMINISZTIKUS SABLON — ez a biztonsági háló
# ============================================================

def _ido(iso):
    """Óra:perc, elé 'holnap' vagy dátum, ha nem a mai napra esik."""
    t = _ts(iso)
    if t is None:
        return ""
    ma = _most().date()
    if t.date() == ma:
        return t.strftime("%H:%M")
    if (t.date() - ma).days == 1:
        return f"holnap {t:%H:%M}"
    return t.strftime("%m.%d. %H:%M")


def _ora_sav(iso):
    """A csúcsóra kezdő és záró időpontja: '18:00 és 19:00'."""
    t = _ts(iso)
    if t is None:
        return ""
    return f"{_ido(iso)} és {(t + timedelta(hours=1)):%H:%M}"


def _ezres(x, tizedes=0):
    return f"{x:,.{tizedes}f}".replace(",", " ")


def _nap_mondat(mg):
    """A napelemes termelés mondata — a NAPSZAKHOZ igazítva.

    A korábbi szöveg minden órában azt írta, hogy "a nap hátralévő részében
    várható". Hajnali egykor ez képtelenség: olyankor az egész nap hátravan,
    a mondat mégis úgy hangzik, mintha a nap már félig eltelt volna. Ezért a
    csúcs ÓRÁJA kerül a mondatba, és három eset van:
      - a csúcs még hátravan   -> "13:00 körül tetőzik"
      - a csúcs elmúlt, de van még termelés -> tetőzés múlt időben + hátralévő
      - a termelés lecsengett  -> kizárólag múlt idő, a holnapi terv külön
    """
    if not mg:
        return None

    if mg.get("nap_termeles_mara_lezarult"):
        tetoz = mg.get("nap_mai_tetozes_mw") or mg.get("nap_mai_csucs_mw")
        if not tetoz:
            return None
        cs = _ezres(tetoz)
        ido = _ido(mg.get("nap_mai_tetozes_ido") or mg.get("nap_mai_csucs_ido"))
        sor = f"A mai napelemes termelés lecsengett; a tetőzés {ido} körül {cs} MW volt."
        # A holnapi terv KÜLÖN mondatban, egyértelmű címkével — sosem
        # keveredhet össze a mai értékkel.
        if mg.get("nap_holnapi_csucs_mw"):
            sor += (f" Holnapra a napelőtti terv "
                    f"{_ezres(mg['nap_holnapi_csucs_mw'])} MW körüli csúcsot jelez.")
        return {"sor": sor, "szam": f"{cs} MW", "cimke": "mai napenergia-tetőzés"}

    if mg.get("nap_csucs_meg_hatravan") and mg.get("nap_mai_csucs_mw"):
        cs = _ezres(mg["nap_mai_csucs_mw"])
        ido = _ido(mg.get("nap_mai_csucs_ido"))
        return {"sor": f"A napelemes termelés a mai napelőtti terv szerint {ido} "
                       f"körül tetőzik, {cs} MW-tal.",
                "szam": f"{cs} MW", "cimke": "mai napenergia-csúcs"}

    if mg.get("nap_hatralevo_csucs_mw"):
        # A napi csúcs már elmúlt, de még van termelés hátra.
        cs = _ezres(mg["nap_hatralevo_csucs_mw"])
        sor = ""
        if mg.get("nap_mai_csucs_mw"):
            sor = (f"A mai napelemes tetőzés {_ido(mg.get('nap_mai_csucs_ido'))} körül "
                   f"{_ezres(mg['nap_mai_csucs_mw'])} MW volt. ")
        sor += (f"A nap hátralévő részében {cs} MW a legmagasabb várható érték.")
        return {"sor": sor, "szam": f"{cs} MW", "cimke": "hátralévő napenergia-csúcs"}
    return None


def _tegnap_mondat(f):
    """Összevetés a TEGNAPI azonos órával — mért adatból, nem jóslatból."""
    t = f.get("tegnapi_osszevetes") or {}
    sz = t.get("elteres_szazalek")
    if sz is None:
        return None
    ma_v, teg_v = _ezres(t["mai_mwh"]), _ezres(t["tegnapi_mwh"])
    if abs(sz) < 1:
        sor = (f"A legutóbbi lezárt mért óra ({t['ora']}) fogyasztása {ma_v} MWh, "
               f"gyakorlatilag ugyanannyi, mint tegnap ugyanekkor ({teg_v} MWh).")
    else:
        irany = "több" if sz > 0 else "kevesebb"
        sor = (f"A legutóbbi lezárt mért óra ({t['ora']}) fogyasztása {ma_v} MWh, "
               f"ami {abs(sz):.0f} százalékkal {irany}, mint tegnap ugyanekkor "
               f"({teg_v} MWh).")
    atl = t.get("atlag_elteres_szazalek")
    if atl is not None and abs(atl) >= 1:
        sor += (f" A mai nap eddigi átlaga {abs(atl):.0f} százalékkal "
                f"{'magasabb' if atl > 0 else 'alacsonyabb'}, mint tegnap ugyaneddig.")
    return {"sor": sor, "szam": f"{sz:+.0f}%", "cimke": "eltérés a tegnapi azonos órától"}


def _ho_mondat(ho, f):
    """A hőmérséklet mondata a HŐMÉRSÉKLETHEZ igazítva.

    A korábbi szöveg minden esetben a "hűtési igényen keresztül" fordulatot
    használta — éjjel, 12 fokban is. A hűtés csak melegben magyarázat, a fűtés
    csak hidegben; a kettő között egyik sem, ilyenkor a napi szélsőértékek
    mondanak többet."""
    ido = f.get("idojaras") or {}
    if ho >= 24:
        sor = (f"A budapesti mért hőmérséklet most {ho:.0f} °C; ezen a szinten a hűtés "
               f"érezhetően megemeli a rendszerterhelést.")
    elif ho <= 10:
        sor = (f"A budapesti mért hőmérséklet most {ho:.0f} °C; ezen a szinten a fűtési "
               f"igény hajtja fel a rendszerterhelést.")
    else:
        sor = (f"A budapesti mért hőmérséklet most {ho:.0f} °C, ami mérsékelt fűtési és "
               f"hűtési igényt jelent.")
    if ido.get("mai_max_c") is not None and ido.get("mai_min_c") is not None:
        sor += (f" A mai napi szélsőértékek {ido['mai_min_c']:.0f} és "
                f"{ido['mai_max_c']:.0f} °C.")
    return sor


def _szel_mondat(mg):
    """A széltermelés mondata — a napelemessel azonos igeidő-logikával:
    ha a napi csúcs órája még hátravan, jelen időben; ha elmúlt, múlt időben."""
    if not mg or mg.get("szel_mai_csucs_mw") is None:
        return None
    cs = _ezres(mg["szel_mai_csucs_mw"])
    ido = _ido(mg.get("szel_mai_csucs_ido"))
    if mg.get("szel_csucs_meg_hatravan"):
        sor = (f"A szélerőművek mai termelése a napelőtti terv szerint {ido} körül "
               f"tetőzik, {cs} MW-tal.")
    else:
        sor = (f"A szélerőművek mai termelése a napelőtti terv szerint {ido} körül "
               f"tetőzött, {cs} MW-tal.")
    if mg.get("szel_mai_atlag_mw") is not None:
        sor += f" A napi átlag {_ezres(mg['szel_mai_atlag_mw'])} MW."
    return {"sor": sor, "szam": f"{cs} MW", "cimke": "mai széltermelési csúcs"}


def sablon_uzenetek(f):
    """Élő megállapítások, tárgyilagos megfogalmazásban."""
    u = [{"sor": KOSZONTO, "szam": None, "cimke": None}]

    k = f.get("kartyak") or {}
    d = f.get("dam")
    t = f.get("toltes")
    fo = f.get("fogyasztas")
    ho = k.get("budapest_homerseklet_c")

    if k.get("jelenlegi_dam_ar_eur_mwh") is not None and d and d.get("mai_atlag_eur_mwh"):
        most = k["jelenlegi_dam_ar_eur_mwh"]
        atl = d["mai_atlag_eur_mwh"]
        u.append({
            "sor": f"A másnapi piac aktuális ára {most:.0f} €/MWh, ami a mai "
                   f"{atl:.0f} €/MWh-s napi átlaghoz képest "
                   f"{'kedvezőbb' if most < atl else 'magasabb'} szintet jelent.",
            "szam": f"{most:.0f} €/MWh", "cimke": "aktuális day-ahead ár"})

    if t and t.get("ajanlott_ar_eur_mwh") is not None:
        u.append({
            "sor": f"A legkedvezőbb árszint {_ido(t['ajanlott_kezdet'])} és "
                   f"{_ido(t['ajanlott_veg'])} között várható, ekkor a day-ahead ár "
                   f"{t['ajanlott_ar_eur_mwh']:.0f} €/MWh. Amennyiben megoldható, a "
                   f"halasztható fogyasztásokat érdemes lehet erre az időszakra ütemezni.",
            "szam": f"{t['ajanlott_ar_eur_mwh']:.0f} €/MWh",
            "cimke": "a legkedvezőbb időszak ára"})

    if fo and fo.get("elorejelzett_csucs_mwh"):
        csucs = _ezres(fo["elorejelzett_csucs_mwh"])
        if fo.get("aktualis_ora_mwh"):
            # A látogatót elsősorban az érdekli, MOST mit mond a modell. A napi
            # csúcs csak akkor kerül előre, ha még hátravan; éjjel a már elmúlt
            # csúcsot előretenni félrevezető volt.
            akt = _ezres(fo["aktualis_ora_mwh"])
            sor = (f"A CatBoost modell a most futó órára {akt} MWh országos "
                   f"fogyasztást jelez.")
            # Az `_ora_sav` maga kiírja a "holnap" szót, ha az időpont nem mai —
            # ezért a "holnapi" jelzőt csak akkor tesszük ki, ha ott nem hangzik el.
            nap_szo = "A mai csúcs" if fo.get("csucs_ma_van") else "A csúcs"
            if fo.get("csucs_meg_hatravan"):
                sor += (f" {nap_szo} {_ora_sav(fo['csucs_idopont'])} között "
                        f"várható, {csucs} MWh körül.")
            else:
                sor += (f" {nap_szo} {_ido(fo['csucs_idopont'])} körül volt, "
                        f"{csucs} MWh.")
            u.append({"sor": sor, "szam": f"{akt} MWh",
                      "cimke": "előrejelzés a jelen órára"})
        elif fo.get("csucs_meg_hatravan"):
            u.append({
                "sor": f"A CatBoost modell előrejelzése szerint a legmagasabb terhelés "
                       f"várhatóan {_ora_sav(fo['csucs_idopont'])} között alakulhat ki, a "
                       f"fogyasztás csúcsértéke pedig megközelítheti a {csucs} MWh-t.",
                "szam": f"{csucs} MWh", "cimke": "előrejelzett csúcsterhelés"})
        else:
            u.append({
                "sor": f"A csúcsterhelés {_ido(fo['csucs_idopont'])} körül alakult "
                       f"ki, {csucs} MWh értéken.",
                "szam": f"{csucs} MWh", "cimke": "csúcsterhelés"})
        if ho is not None and ho >= 35:
            u.append({
                "sor": "Amennyiben megoldható, a halasztható nagyobb fogyasztásokat "
                       "érdemes lehet a várható csúcsidőszakon kívülre időzíteni.",
                "szam": None, "cimke": None})
        sz_el = fo.get("elteres_heti_atlagtol_szazalek")
        if sz_el is not None and abs(sz_el) >= 1:
            u.append({
                "sor": (f"A következő órák várható fogyasztása {abs(sz_el):.0f} százalékkal "
                        f"meghaladja az ugyanezekre az órákra jellemző heti átlagot."
                        if sz_el > 0 else
                        f"A következő órák várható fogyasztása {abs(sz_el):.0f} százalékkal "
                        f"elmarad az ugyanezekre az órákra jellemző heti átlagtól."),
                "szam": f"{sz_el:+.0f}%", "cimke": "eltérés a heti átlagtól"})

    teg = _tegnap_mondat(f)
    if teg:
        u.append(teg)

    if ho is not None:
        u.append({"sor": _ho_mondat(ho, f), "szam": f"{ho:.0f} °C",
                  "cimke": "mért hőmérséklet · Budapest"})

    mg = f.get("megujulok")
    nap_sor = _nap_mondat(mg)
    if nap_sor:
        u.append(nap_sor)

    szel_sor = _szel_mondat(mg)
    if szel_sor:
        u.append(szel_sor)

    m = f.get("modell_pontossag")
    elso = (m or {}).get("catboost_elso_jóslat_mae_mwh")
    if m and elso is not None:
        u.append({
            "sor": f"A legutóbbi lezárt napon a modell egy nappal korábban készített "
                   f"előrejelzése átlagosan {elso:.0f} MWh-val tért el a tényleges "
                   f"fogyasztástól.",
            "szam": f"{elso:.0f} MWh", "cimke": "átlagos előrejelzési eltérés"})

    a = f.get("adatminoseg")
    if a and a["jelzes_7_nap"]:
        reszek = []
        if a["extrem_idojaras_db"]:
            reszek.append(f"{a['extrem_idojaras_db']} esetben szélsőséges időjárás")
        if a["alacsony_napsugarzas_db"]:
            reszek.append(f"{a['alacsony_napsugarzas_db']} esetben alacsony napsugárzás")
        if a["homersekleti_fordulat_db"]:
            reszek.append(f"{a['homersekleti_fordulat_db']} esetben jelentős "
                          f"hőmérsékleti fordulat")
        sor = (f"Az elmúlt hét napban {a['jelzes_7_nap']} órában tért el a fogyasztás a "
               f"szokásos mintázattól.")
        if reszek:
            sor += f" A háttérben {', '.join(reszek)} állt."
        if a["meg_vizsgalando_db"]:
            sor += f" További {a['meg_vizsgalando_db']} eset vizsgálata folyamatban van."
        u.append({"sor": sor, "szam": f"{a['jelzes_7_nap']} óra",
                  "cimke": "szokatlan órák · elmúlt 7 nap"})

    return u


# ============================================================
# 5) GEMINI + ELLENŐRZÉS
# ============================================================

class GeminiKvotaHiba(RuntimeError):
    """Elfogyott a nyelvi modell kerete (HTTP 429 / RESOURCE_EXHAUSTED).

    Ez nem programhiba, hanem átmeneti állapot: a keret idővel újratöltődik.
    Ezért külön típus — a látogatónak őszintén megmondjuk, ahelyett hogy
    Flux csak szűkszavúbbá válna minden magyarázat nélkül."""


class GeminiLassuHiba(RuntimeError):
    """A nyelvi modell nem válaszolt időben."""


def _gemini(prompt, sema, timeout=GEMINI_TIMEOUT, szerep=None, homerseklet=0.4):
    """Végigpróbálja az elérhető modelleket, amíg valamelyik válaszol.

    Minden modellnek KÜLÖN ingyenes kerete van. Ha csak egyet használnánk,
    annak kimerülésével Flux azonnal elnémulna nyelvileg — pedig a következő
    modell keretéből még bőven van. Aminek betelt a kerete, azt egy ideig
    átugorjuk, hálózati hívás nélkül."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY nincs beállítva")
    elerheto = _elerheto_modellek()
    if not elerheto:
        raise GeminiKvotaHiba("minden modell kerete betelt, a hívás kihagyva")
    utolso_hiba = None
    for modell in elerheto:
        try:
            return _gemini_egy(modell, prompt, sema, timeout, szerep, homerseklet)
        except GeminiKvotaHiba as e:
            utolso_hiba = e
            continue          # jöhet a következő modell, saját kerettel
    raise utolso_hiba or GeminiKvotaHiba("nincs elérhető modell")


def _gemini_egy(modell, prompt, sema, timeout, szerep, homerseklet):
    """Egyetlen modell meghívása."""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{modell}:generateContent")
    try:
        r = requests.post(
            url,
            headers={"x-goog-api-key": GEMINI_API_KEY,
                     "Content-Type": "application/json"},
            json={
                "system_instruction": {"parts": [{"text": szerep or FLUX_SZEREP}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": homerseklet,
                    "responseMimeType": "application/json",
                    "responseSchema": sema,
                },
            },
            timeout=timeout,
        )
    except requests.exceptions.Timeout as e:
        raise GeminiLassuHiba(f"{modell} időtúllépés ({timeout} mp)") from e
    if r.status_code != 200:
        # A Google a hiba OKÁT a válasz törzsében küldi (rossz kulcs, nem
        # létező modellnév, kimerült kvóta). A puszta `raise_for_status()`
        # ezt eldobta, ezért a naplóban csak egy szám látszott, és nem
        # lehetett megmondani, miért hallgat Flux.
        torzs = r.text[:400]
        if r.status_code == 429 or "RESOURCE_EXHAUSTED" in torzs:
            _kvota_jelez(modell)
            raise GeminiKvotaHiba(f"{modell} kvóta: {torzs}")
        if r.status_code == 404:
            # Nem létező modellnév: ne próbáljuk újra, de a többi még jöhet.
            _kvota_jelez(modell)
            raise GeminiKvotaHiba(f"{modell} nem érhető el: {torzs}")
        raise RuntimeError(f"{modell} HTTP {r.status_code}: {torzs}")
    valasz_json = r.json()
    jeloltek = valasz_json.get("candidates") or []
    if not jeloltek:
        raise RuntimeError(f"{modell} üres válasz: {str(valasz_json)[:400]}")
    reszek = jeloltek[0].get("content", {}).get("parts") or []
    szoveg = "".join(p.get("text", "") for p in reszek)
    if not szoveg.strip():
        raise RuntimeError(f"{modell} nem adott szöveget: {str(jeloltek[0])[:400]}")
    return json.loads(szoveg)


_UZENET_SEMA = {
    "type": "object",
    "properties": {
        "uzenetek": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sor": {"type": "string"},
                    "szam": {"type": "string"},
                    "cimke": {"type": "string"},
                },
                "required": ["sor"],
            },
        }
    },
    "required": ["uzenetek"],
}


_IDO_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\b")
_DATUM_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}\.\s?\d{1,2}\.")
_EZRES_RE = re.compile(r"(?<=\d)[\s\u00a0\u202f](?=\d)")
_SZAM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def _szamok(szoveg):
    """A szövegben szereplő számok. Az időpontok és dátumok nem számítanak
    adatnak — azok az ISO mezőkből származnak, nem a modell találta ki őket."""
    s = _DATUM_RE.sub(" ", str(szoveg))
    s = _IDO_RE.sub(" ", s)
    s = _EZRES_RE.sub("", s)          # "4 695" -> "4695"
    ki = set()
    for m in _SZAM_RE.finditer(s):
        try:
            ki.add(float(m.group().replace(",", ".")))
        except ValueError:
            pass
    return ki


def _engedelyezett_szamok(f):
    ki = set()

    def bejar(x):
        if isinstance(x, dict):
            for v in x.values():
                bejar(v)
        elif isinstance(x, list):
            for v in x:
                bejar(v)
        elif isinstance(x, bool):
            # Pythonban a True `int`-nek is számít. Enélkül minden logikai
            # mező beengedte volna az 1-et az elfogadott számok közé, vagyis
            # egy kitalált "1 €/MWh" is átment volna az ellenőrzésen.
            return
        elif isinstance(x, (int, float)):
            ki.add(round(float(x), 1))
            ki.add(round(float(x), 0))
    bejar(f)
    return ki


def _ismert(x, ok_szamok):
    """Kerekítési tűréssel: 1% vagy 1 egység, amelyik nagyobb."""
    for a in ok_szamok:
        if abs(x - a) <= max(1.0, abs(a) * 0.01):
            return True
    return False


def _ellenoriz(uzenetek, f):
    """Minden leírt szám szerepeljen a tényadatokban. Ami nem, azt eldobjuk."""
    ok_szamok = _engedelyezett_szamok(f)
    jo, hibak = [], []
    for u in uzenetek:
        szoveg = f"{u.get('sor','')} {u.get('szam','')} {u.get('cimke','')}"
        idegen = [s for s in _szamok(szoveg) if not _ismert(s, ok_szamok)]
        if idegen:
            hibak.append({"sor": u.get("sor"), "ismeretlen_szamok": sorted(idegen)})
            continue
        if not str(u.get("sor", "")).strip():
            continue
        jo.append({"sor": u["sor"].strip(),
                   "szam": (u.get("szam") or None),
                   "cimke": (u.get("cimke") or None)})
    return jo, hibak


# ============================================================
# 6) BELÉPÉSI PONT — ezt hívja az app.py
# ============================================================

def uzenetek(data, ajanlas=None, koszonto=None):
    """A főoldali Flux-szövegek. Sosem dob kivételt: hiba esetén sablonra vált."""
    f, minoseg = tenyek(data, ajanlas)
    if f is None:
        return [{"sor": "Élő adatokra várok — amint megérkeznek, mondom, mi történik.",
                 "szam": None, "cimke": None}]

    # Alapertelmezesben elo koszonto keszul: napszak szerinti koszones es a
    # pontos ido. A hivo felulirhatja sajat szoveggel.
    koszonto = koszonto or elo_koszonto()

    tartalek = sablon_uzenetek(f) + MODELL_UZENETEK + ZARO_UZENETEK
    if koszonto:
        # A "kezdo" jelzés miatt a böngésző a köszöntőt EGYSZER játssza le,
        # utána kihagyja a körből. Eddig minden fordulóban újrakezdte a
        # "Szia, Flux vagyok..." mondattal, ami ismétlődőnek és gépiesnek hatott.
        tartalek[0] = {"sor": koszonto, "szam": None, "cimke": None, "kezdo": True}

    fh = _hash(f)
    ck = _cache_key(fh)

    # 1) Folyamaton belüli gyorsítótár — nulla hálózat.
    kesz = _memo_olvas(ck)
    if kesz is not None:
        return _frissit_koszonto(kesz)

    # 2) Adatbázis-gyorsítótár — másik worker már elkészíttethette.
    if _db_ok():
        try:
            tarolt = _summary_olvas(ck)
            if tarolt:
                ertek = json.loads(tarolt)
                _memo_ir(ck, ertek)
                return _frissit_koszonto(ertek)
        except Exception as e:
            print(f"[FLUX] Gyorsítótár olvasás: {e}", flush=True)

    # Egyszerre csak egy szál gyárt. A zár NEM várakozó: ha épp más látogató
    # hívása fut, ez a látogató azonnal megkapja a determinisztikus szöveget,
    # ahelyett hogy akár 12 másodpercet várna valaki más Gemini-hívása mögött.
    # A következő oldalletöltés már a kész, gazdagabb változatot kapja.
    if not _GYARTAS_LOCK.acquire(blocking=False):
        return tartalek
    try:
        kesz = _memo_olvas(ck)
        if kesz is not None:
            return _frissit_koszonto(kesz)
        return _frissit_koszonto(_gyart(f, ck, fh, minoseg, koszonto, tartalek))
    finally:
        _GYARTAS_LOCK.release()


def _gyart(f, ck, fh, minoseg, koszonto, tartalek):
    """A főoldali szöveg tényleges legyártása. Csak a `_GYARTAS_LOCK` alatt fut."""
    allapot, model_nev, hibak = "fallback", "deterministic-template-v1", []
    vegleges = tartalek

    try:
        prompt = (
            "Az alábbi JSON a magyar villamosenergia-rendszer élő adatait tartalmazza.\n"
            f"{json.dumps(f, ensure_ascii=False, default=str)}\n\n"
            "Készíts 4-5 megállapítást a főoldalra. Minden megállapítás:\n"
            "- 'sor': egy vagy két élő, jól olvasható mondat. A számot írd bele a "
            "mondatba. Ne címszavakat írj, hanem mondatokat, és ne legyél kioktató — "
            "azt mondd el, mit LÁTSZ az adatokban, és ha valami szokatlan vagy "
            "érdekes, azt nyugodtan emeld ki,\n"
            "- 'szam': egyetlen kiemelt érték mértékegységgel, pontosan az adatokból,\n"
            "- 'cimke': 2-5 szavas kontextus, ami megmondja, MI az a szám "
            "(pl. 'előrejelzett csúcsterhelés'), soha ne önmagában álló mértékegység.\n"
            "Használhatod a 'kartyak' értékeit is — ezeket a látogató a fejléc alatt látja.\n"
            "Témák: töltési ablak és ár; a mai fogyasztási csúcs és eltérése; "
            "a CatBoost és a MAVIR pontossága; nyitott adatminőségi jelzések.\n"
            "Az időpontok ISO dátumot tartalmaznak: ha az időpont nem a mai napra "
            "esik, ÍRD KI, hogy holnapi ablakról van szó.\n"
            "NAPELEM — a mezőnevek pontosan megmondják, melyik NAPRÓL van szó, "
            "ezt soha ne keverd össze:\n"
            "  * 'nap_hatralevo_csucs_mw' = a MAI nap HÁTRALÉVŐ óráira várható csúcs. "
            "Csak akkor beszélj mai várható napenergiáról, ha EZ a mező szerepel.\n"
            "  * 'nap_termeles_mara_lezarult' = a mai napelemes termelés MÁR VÉGET ÉRT. "
            "Ilyenkor KIZÁRÓLAG múlt időben írhatsz róla ('a mai tetőzés ennyi VOLT'), "
            "és tilos bármilyen mai várható értéket említeni.\n"
            "  * 'nap_mai_tetozes_mw' = a MAI, MÁR ELMÚLT tetőzés — múlt idő.\n"
            "  * 'nap_holnapi_csucs_mw' = a HOLNAPI napra szóló terv. Ha ezt említed, "
            "ÍRD KI a mondatba, hogy HOLNAPRÓL van szó.\n"
            "Az érdekesség a fontos: mi az, ami eltér a megszokottól. Kioktatás és "
            "üres udvariaskodás nélkül.\n"
            "A legérdekesebb az ELTÉRÉS: mennyivel több vagy kevesebb a szokásosnál "
            "(fogyasztás a heti átlaghoz, ár a mai átlaghoz, napenergia az eddigi csúcshoz).\n"
            "SZÓHASZNÁLAT: a 'kartyak.budapest_homerseklet_c' a MOST mért érték — "
            "'most ennyi van', soha ne 'várható' vagy 'ma ennyi lesz'.\n"
            "Az 'adatminoseg.jelzes_7_nap' az elmúlt 7 nap ÖSSZES szokatlan órája; a "
            "kategóriák ennek a bontása, ezért együtt említsd őket. "
            "Az 'adatminoseg' kategóriái MAGYARÁZATOK, nem feladatok: az "
            "extrem_idojaras_db a szélsőséges időjárás miatti órák száma, az "
            "alacsony_napsugarzas_db a borult órákat jelenti, a homersekleti_fordulat_db "
            "a napon belüli nagy hőmérséklet-fordulatot. Mondd el, MI OKOZTA az eltérést, "
            "ne azt, hogy mit kell megvizsgálni. Csak a meg_vizsgalando_db esetén beszélj "
            "nyitott kérdésről.\n"
            "A 'modell_pontossag' MINDIG egy LEZÁRT napra vonatkozik: úgy fogalmazz, "
            "hogy 'a legutóbbi lezárt napon', és soha ne írd, hogy 'ma'. "
            "A MAVIR-előrejelzést NE hasonlítsd össze a mi modellünkkel és ne állítsd, "
            "hogy bármelyik pontosabb — az összevetés módszertani validálása még "
            "folyamatban van. Csak a saját modell eltéréséről beszélj.\n"
            "Ne írj olyan számot, ami nincs a JSON-ban."
        )
        valasz_json = _gemini(prompt, _UZENET_SEMA)
        jo, hibak = _ellenoriz(valasz_json.get("uzenetek", []), f)
        if len(jo) >= 2:
            vegleges = (([{"sor": koszonto, "szam": None, "cimke": None,
                           "kezdo": True}] if koszonto else [])
                        + jo + MODELL_UZENETEK + ZARO_UZENETEK)
            allapot, model_nev = "gemini", GEMINI_MODEL
        else:
            allapot = "rejected"
    except Exception as e:
        print(f"[FLUX] Gemini: {e}", flush=True)
        hibak = [{"kivetel": str(e)}]

    # A kész szöveg azonnal a memóriába kerül, hogy a következő látogató
    # már ne várjon rá. A mentés a DB-be csak ezután, "legjobb szándék" alapon.
    _memo_ir(ck, vegleges)

    if _db_ok():
        try:
            snapshot_id = _snapshot_ment(
                f, minoseg, fh, {"hianyzo": f["_meta"]["hianyzo_forrasok"]})
            _summary_ment(snapshot_id, ck, model_nev, allapot,
                          json.dumps(vegleges, ensure_ascii=False), hibak)
        except Exception as e:
            print(f"[FLUX] Snapshot/summary mentés: {e}", flush=True)

    return vegleges


# ============================================================
# 7) A LÁTOGATÓ KÉRDÉSE
#
# Alapelv: Flux SOHA ne hallgasson el. Először determinisztikus, az élő
# adatokból számolt választ állítunk elő a kérdés témájára — ez akkor is
# kész van, ha nincs Gemini-kulcs, ha a Gemini időtúllépéssel elszáll, vagy
# ha a válasza elbukik a szám-ellenőrzésen. A Gemini csak akkor kerül a
# helyére, ha valóban átment minden ellenőrzésen.
# ============================================================

def _u(sor, szam=None, cimke=None):
    return {"sor": sor, "szam": szam, "cimke": cimke}


# Témák: (kulcsszavak, kezelő függvény neve). A sorrend számít — az első
# egyező téma nyer, ezért a specifikusabb kulcsszavak állnak elöl.
def _t_udvozles(f):
    """Ha a látogató köszön vagy megszólít, Flux visszaköszön — napszak
    szerint —, és rögtön felajánl valamit, amiről kérdezhet."""
    kep = _t_kepessegek(f)
    sor = f"{_udvozles()} Örülök, hogy benéztél. "
    if kep:
        sor += kep["sor"]
    else:
        sor += "Amint megérkeznek az élő adatok, mondom, mi történik."
    return _u(sor, None, "amiről kérdezhetsz")


def _t_koszonom(f):
    return _u("Szívesen! Ha bármi mást is meg akarsz nézni, csak kérdezz.",
              None, None)


def _t_kepessegek(f):
    temak = []
    if f.get("dam") or f.get("toltes"):
        temak.append("a másnapi (day-ahead) villamosenergia-árak és a legkedvezőbb "
                     "töltési időszak")
    if f.get("fogyasztas"):
        temak.append("a következő órák fogyasztási előrejelzése és a várható csúcs")
    if f.get("megujulok"):
        temak.append("a nap- és széltermelés terve, valamint a mért alakulása")
    if f.get("kartyak", {}).get("budapest_homerseklet_c") is not None or f.get("idojaras"):
        temak.append("a budapesti hőmérséklet és a napi időjárási kilátás")
    if f.get("kartyak", {}).get("eur_huf") is not None:
        temak.append("az EUR/HUF árfolyam")
    if f.get("modell_pontossag") or f.get("heti_merleg"):
        temak.append("a CatBoost előrejelzés pontossága a lezárt napokon")
    if f.get("adatminoseg") or f.get("stl"):
        temak.append("az STL által talált szokatlan órák és a magyarázatuk")
    if not temak:
        return None
    return _u("Az élő adatokból ezekről tudok beszélni: " + "; ".join(temak) + ".",
              None, "amiről kérdezhetsz")


def _t_ar(f):
    d, t = f.get("dam") or {}, f.get("toltes") or {}
    k = f.get("kartyak") or {}
    most = k.get("jelenlegi_dam_ar_eur_mwh")
    if most is not None and d.get("mai_atlag_eur_mwh") is not None:
        atl = d["mai_atlag_eur_mwh"]
        viszony = "kedvezőbb" if most < atl else ("magasabb" if most > atl else "azonos")
        sor = (f"A day-ahead ár jelenleg {most:.0f} €/MWh, ami a mai {atl:.0f} €/MWh-s "
               f"napi átlaghoz képest {viszony} szint. A mai árak a "
               f"{d['mai_min_eur_mwh']:.0f} és {d['mai_max_eur_mwh']:.0f} €/MWh sávban "
               f"mozogtak, a legolcsóbb óra {d['mai_legolcsobb_ora']}, a legdrágább "
               f"{d['mai_legdragabb_ora']} volt.")
        return _u(sor, f"{most:.0f} €/MWh", "aktuális day-ahead ár")
    if d.get("mai_atlag_eur_mwh") is not None:
        return _u(f"A mai day-ahead árak átlaga {d['mai_atlag_eur_mwh']:.0f} €/MWh, a "
                  f"sáv alja {d['mai_min_eur_mwh']:.0f}, teteje "
                  f"{d['mai_max_eur_mwh']:.0f} €/MWh.",
                  f"{d['mai_atlag_eur_mwh']:.0f} €/MWh", "mai átlagos day-ahead ár")
    if t.get("aktualis_ar_eur_mwh") is not None:
        return _u(f"A day-ahead ár jelenleg {t['aktualis_ar_eur_mwh']:.0f} €/MWh.",
                  f"{t['aktualis_ar_eur_mwh']:.0f} €/MWh", "aktuális day-ahead ár")
    return None


def _t_holnapi_ar(f):
    d = f.get("dam") or {}
    if d.get("holnapi_atlag_eur_mwh") is not None:
        return _u(f"A holnapi árak már publikálva vannak: az átlag "
                  f"{d['holnapi_atlag_eur_mwh']:.0f} €/MWh, a legalacsonyabb szelvény "
                  f"{d['holnapi_min_eur_mwh']:.0f}, a legmagasabb "
                  f"{d['holnapi_max_eur_mwh']:.0f} €/MWh.",
                  f"{d['holnapi_atlag_eur_mwh']:.0f} €/MWh", "holnapi átlagár")
    if d and not d.get("holnapi_ar_publikalva"):
        return _u("A holnapi day-ahead árakat még nem publikálták; az új árszelvények "
                  "14:00 körül érkeznek meg a piacról.", None, "holnapi árak")
    return None


def _t_toltes(f):
    t = f.get("toltes") or {}
    if t.get("ajanlott_ar_eur_mwh") is None:
        return None
    sor = (f"A legkedvezőbb töltési időszak {_ido(t['ajanlott_kezdet'])} és "
           f"{_ido(t['ajanlott_veg'])} között van, ekkor a day-ahead ár "
           f"{t['ajanlott_ar_eur_mwh']:.0f} €/MWh.")
    if t.get("negativ_ar_az_ablakban"):
        sor += " Ebben az ablakban negatív árszelvény is van."
    if t.get("most_is_kedvezo"):
        sor += " Az aktuális ár is a kedvező sávban van, tehát most sem érdemes várni."
    if t.get("tovabbi_ablakok"):
        masodik = t["tovabbi_ablakok"][0]
        sor += (f" További kedvező kezdés {_ido(masodik['kezdet'])} "
                f"({masodik['ar_eur_mwh']:.0f} €/MWh).")
    return _u(sor, f"{t['ajanlott_ar_eur_mwh']:.0f} €/MWh", "a legkedvezőbb időszak ára")


def _t_fogyasztas(f):
    fo = f.get("fogyasztas") or {}
    if not fo.get("elorejelzett_csucs_mwh"):
        mert = f.get("mert_fogyasztas") or {}
        if mert.get("ertek_mwh"):
            return _u(f"A legutóbbi mért országos fogyasztás {_ezres(mert['ertek_mwh'])} "
                      f"MWh volt {mert['idopont']}-kor.",
                      f"{_ezres(mert['ertek_mwh'])} MWh", "legutóbbi mért fogyasztás")
        return None
    csucs = _ezres(fo["elorejelzett_csucs_mwh"])
    # A kérdésre a JELEN órával kezdünk — a látogatót az érdekli, most mi van.
    if fo.get("aktualis_ora_mwh"):
        sor = (f"A CatBoost V10 modell a most futó órára "
               f"{_ezres(fo['aktualis_ora_mwh'])} MWh országos fogyasztást jelez.")
        if fo.get("csucs_meg_hatravan"):
            sor += (f" A csúcs {_ora_sav(fo['csucs_idopont'])} között várható, "
                    f"{csucs} MWh.")
        else:
            sor += f" A csúcs {_ido(fo['csucs_idopont'])} körül volt, {csucs} MWh."
    else:
        sor = (f"A CatBoost V10 modell a következő {fo['orak_szama']} órára jelez előre. "
               f"A legmagasabb terhelés {_ora_sav(fo['csucs_idopont'])} között várható, "
               f"a csúcsérték {csucs} MWh.")
    sz_el = fo.get("elteres_heti_atlagtol_szazalek")
    if sz_el is not None and abs(sz_el) >= 1:
        irany = "meghaladja" if sz_el > 0 else "elmarad"
        kot = "a heti átlagot" if sz_el > 0 else "a heti átlagtól"
        sor += (f" Ez {abs(sz_el):.0f} százalékkal {irany} {kot}.")
    return _u(sor, f"{csucs} MWh", "előrejelzett csúcsterhelés")


def _t_napenergia(f):
    mg = f.get("megujulok") or {}
    alap = _nap_mondat(mg)
    if not alap:
        if mg.get("nap_holnapi_csucs_mw"):
            cs = _ezres(mg["nap_holnapi_csucs_mw"])
            return _u(f"A holnapi napra a napelőtti terv {cs} MW körüli napelemes "
                      f"csúcsot jelez.", f"{cs} MW", "holnapi napenergia-csúcs")
        return None
    # Kérdésre a mért értéket is hozzátesszük, ha van — az a "mi történik MOST".
    if mg.get("nap_eddigi_merte_csucs_mw"):
        alap = dict(alap)
        alap["sor"] += (f" A ma ténylegesen mért legmagasabb érték "
                        f"{_ezres(mg['nap_eddigi_merte_csucs_mw'])} MW.")
    return alap


def _t_szel(f):
    mg = f.get("megujulok") or {}
    alap = _szel_mondat(mg)
    if not alap:
        return None
    if mg.get("szel_utolso_mert_mw") is not None:
        alap = dict(alap)
        alap["sor"] += (f" A legutóbb mért érték "
                        f"{_ezres(mg['szel_utolso_mert_mw'])} MW "
                        f"{_ido(mg.get('szel_utolso_mert_idopont'))}-kor.")
    return alap


def _t_megujulo_pontossag(f):
    mg = f.get("megujulok") or {}
    if mg.get("nap_mai_mae_mw") is None and mg.get("szel_mai_mae_mw") is None:
        return None
    reszek = []
    if mg.get("nap_mai_mae_mw") is not None:
        reszek.append(f"a napelemes terv átlagosan {_ezres(mg['nap_mai_mae_mw'])} MW-tal")
    if mg.get("szel_mai_mae_mw") is not None:
        reszek.append(f"a szélerőművi terv átlagosan {_ezres(mg['szel_mai_mae_mw'])} MW-tal")
    kiemelt = mg.get("nap_mai_mae_mw", mg.get("szel_mai_mae_mw"))
    return _u(f"A mai órákban, ahol már van mért adat, {' és '.join(reszek)} tért el a "
              f"tényleges termeléstől.", f"{_ezres(kiemelt)} MW",
              "napelőtti terv mai eltérése")


def _t_homerseklet(f):
    k = f.get("kartyak") or {}
    ido = f.get("idojaras") or {}
    ho = k.get("budapest_homerseklet_c")
    if ho is None and not ido:
        return None
    if ho is not None:
        sor = _ho_mondat(ho, f)
    else:
        sor = "A budapesti hőmérsékletről ma a következőket mutatják az adatok."
        if ido.get("mai_max_c") is not None:
            sor += (f" A mai napi maximum {ido['mai_max_c']:.0f} °C, a minimum "
                    f"{ido['mai_min_c']:.0f} °C.")
    if ido.get("holnapi_max_c") is not None:
        sor += f" Holnap {ido['holnapi_max_c']:.0f} °C-os csúcs várható."
    return _u(sor, f"{ho:.0f} °C" if ho is not None else None,
              "mért hőmérséklet · Budapest" if ho is not None else "időjárás")


def _t_arfolyam(f):
    e = (f.get("kartyak") or {}).get("eur_huf")
    if e is None:
        return None
    return _u(f"Az EUR/HUF árfolyam {e:.1f} forint. Ez váltja át a €/MWh-ban jegyzett "
              f"day-ahead árat forintra, ezért a hazai költség két dolgon múlik: az "
              f"árszinten és az árfolyamon.", f"{e:.1f} Ft", "EUR/HUF árfolyam")


def _t_modell(f):
    m = f.get("modell_pontossag") or {}
    h = f.get("heti_merleg") or {}
    if m.get("catboost_elso_jóslat_mae_mwh") is not None:
        elso = m["catboost_elso_jóslat_mae_mwh"]
        sor = (f"A legutóbbi lezárt napon ({m['nap']}, {m['orak_szama']} kiértékelt óra) "
               f"a CatBoost egy nappal korábban készített előrejelzése átlagosan "
               f"{elso:.0f} MWh-val tért el a tényleges fogyasztástól.")
        if m.get("catboost_frissitett_mae_mwh") is not None:
            sor += (f" A legutóbbi mért órákat is ismerő, frissített jóslat eltérése "
                    f"{m['catboost_frissitett_mae_mwh']:.0f} MWh.")
        return _u(sor, f"{elso:.0f} MWh", "átlagos előrejelzési eltérés")
    if h.get("catboost_elso_jóslat_mae_mwh") is not None:
        return _u(f"Ezen a héten eddig {h['kiertekelt_orak']} lezárt órát értékeltünk ki; "
                  f"a modell első jóslatának átlagos eltérése "
                  f"{h['catboost_elso_jóslat_mae_mwh']:.0f} MWh.",
                  f"{h['catboost_elso_jóslat_mae_mwh']:.0f} MWh", "heti átlagos eltérés")
    return _u("A CatBoost V10 gépi tanulási modell órákra előre becsli az ország "
              "fogyasztását, az STL-módszer pedig a szokásostól eltérő órákat keresi meg. "
              "A pontossági kiértékelés mindig lezárt napokra készül.",
              None, "a két modell")


def _t_anomalia(f):
    a = f.get("adatminoseg") or {}
    s = f.get("stl") or {}
    if a.get("jelzes_7_nap"):
        reszek = []
        if a["extrem_idojaras_db"]:
            reszek.append(f"{a['extrem_idojaras_db']} esetben szélsőséges időjárás")
        if a["alacsony_napsugarzas_db"]:
            reszek.append(f"{a['alacsony_napsugarzas_db']} esetben alacsony napsugárzás")
        if a["homersekleti_fordulat_db"]:
            reszek.append(f"{a['homersekleti_fordulat_db']} esetben jelentős "
                          f"hőmérsékleti fordulat")
        sor = (f"Az elmúlt hét napban {a['jelzes_7_nap']} órában tért el a fogyasztás a "
               f"szokásos mintázattól.")
        if reszek:
            sor += f" A háttérben {', '.join(reszek)} állt."
        if a["meg_vizsgalando_db"]:
            sor += f" További {a['meg_vizsgalando_db']} eset vizsgálata folyamatban van."
        return _u(sor, f"{a['jelzes_7_nap']} óra", "szokatlan órák · elmúlt 7 nap")
    if s.get("vizsgalt_napok"):
        return _u(f"Az STL a legutóbbi {s['vizsgalt_napok']} nap fogyasztását bontja "
                  f"trendre, napi mintázatra és maradékra; ebben az ablakban "
                  f"{s['szokatlan_orak_db']} szokatlan órát talált. A trend jelenleg "
                  f"{s.get('trend_iranya') or 'stabil'}.",
                  f"{s['szokatlan_orak_db']} óra", "szokatlan órák · STL")
    return None


def _t_mert_fogyasztas(f):
    mert = f.get("mert_fogyasztas") or {}
    if not mert.get("ertek_mwh"):
        return None
    return _u(f"A legutóbbi lezárt mért óra ({mert['idopont']}) országos fogyasztása "
              f"{_ezres(mert['ertek_mwh'])} MWh. A mért adat természeténél fogva "
              f"egy-két órát késik a valós időhöz képest.",
              f"{_ezres(mert['ertek_mwh'])} MWh", "legutóbbi mért fogyasztás")


def _t_tegnap(f):
    return _tegnap_mondat(f)


def _t_ido_most(f):
    """"Hány óra van?" / "milyen napszak van?" — ettől látszik, hogy Flux
    tudja, mikor nézi valaki az oldalt."""
    meta = f.get("_meta") or {}
    if not meta.get("helyi_ido"):
        return None
    return _u(f"Most {meta['helyi_ido']} van Budapesten, {meta.get('napszak')}. "
              f"Minden időpont, amit mondok, budapesti idő szerint értendő.",
              meta["helyi_ido"], "pontos idő · Budapest")


def _t_frissites(f):
    meta = f.get("_meta") or {}
    if not meta.get("frissites"):
        return None
    sor = (f"Az élő adatok legutóbb {meta['frissites']}-kor frissültek (budapesti idő). "
           f"A források: ENTSO-E (árak, fogyasztás, termelés), Open-Meteo vagy Visual "
           f"Crossing (időjárás) és az ECB (árfolyam).")
    if meta.get("hianyzo_forrasok"):
        sor += f" Jelenleg nem elérhető: {', '.join(meta['hianyzo_forrasok'])}."
    return _u(sor, None, "adatfrissítés")


# A sorrend számít: a specifikusabb minta áll elöl.
_TEMAK = [
    (("köszönöm", "köszi", "kösz ", "hálás"), _t_koszonom),
    (("szia", "helló", "hello", "hali", "jó reggelt", "jó estét", "jó napot",
      "üdv", "csá"), _t_udvozles),
    (("mit tudsz", "miben tudsz", "mire vagy", "ki vagy", "mit csinálsz", "segít",
      "miről tudsz", "mit kérdez", "mihez értesz", "mi ez az oldal"), _t_kepessegek),
    (("holnapi ár", "holnap ár", "holnapi day", "holnapi dam", "holnap mennyi lesz az ár",
      "holnapi árak"), _t_holnapi_ar),
    (("tölt", "mikor kapcsol", "mikor indít", "mosogép", "mosógép", "olcsó ablak",
      "legolcsóbb"), _t_toltes),
    (("napelem", "napenergia", "szolár", "solar", "napsugár", "pv"), _t_napenergia),
    (("szél", "szeler", "wind"), _t_szel),
    (("megújuló pontos", "termelés pontos", "napelőtti terv"), _t_megujulo_pontossag),
    (("árfolyam", "eur/huf", "eurhuf", "forint", "euró árfolyam", "huf"), _t_arfolyam),
    (("mavir", "catboost", "modell", "pontos", "mae", "hiba", "előrejelzés jó",
      "mennyire pontos", "gépi tanul", "mesterséges intelligencia"), _t_modell),
    (("anomál", "szokatlan", "stl", "adatminőség", "eltérés a szokásos", "rendellenes",
      "nyitott eset"), _t_anomalia),
    (("tegnap", "tegnapi", "előző nap", "tegnaphoz", "elmúlt naphoz",
      "mennyivel nőtt", "mennyivel csökkent", "változott a fogyaszt"), _t_tegnap),
    (("hány óra", "mennyi az idő", "milyen napszak", "éjszaka van", "nappal van",
      "pontos idő"), _t_ido_most),
    (("mért fogyasztás", "aktuális fogyasztás", "most mennyi a fogyasztás"),
     _t_mert_fogyasztas),
    (("fogyaszt", "terhel", "csúcs", "mwh", "mennyit fogyaszt", "rendszerterhel"),
     _t_fogyasztas),
    (("hőmérsék", "meleg", "hideg", "fok", "időjárás", "eső", "°c", "hány fok"),
     _t_homerseklet),
    (("ár", "árak", "olcsó", "drága", "dam", "day-ahead", "tőzsd", "€", "eur/mwh",
      "piac"), _t_ar),
    (("frissít", "adatforrás", "honnan", "mikor frissül", "milyen adat"), _t_frissites),
]


# Rövid, emberi felütések. Nem díszítés: enélkül minden válasz ugyanazzal a
# szikár adatmondattal indul, és Flux úgy hat, mint egy kijelzőtábla. A
# sorrend körbejár, hogy ne ugyanaz jöjjön minden kérdésre.
_FELUTESEK = [
    "Nézzük. ",
    "Épp jókor kérded. ",
    "Megnéztem az élő adatokat. ",
    "Erre tudok válaszolni. ",
    "Máris. ",
]
_felutes_szamlalo = {"i": 0}


def _felutessel(u):
    """Emberi felütés a válasz elé. A saját válaszainkra vonatkozik — a
    Gemini a saját szerepéből amúgy is így fogalmaz."""
    if not u or not u.get("sor"):
        return u
    # A köszönésre és a köszönetre adott válasz már eleve személyes.
    if u["sor"].startswith(("Jó ", "Szia", "Szívesen", "Most ")):
        return u
    i = _felutes_szamlalo["i"] % len(_FELUTESEK)
    _felutes_szamlalo["i"] += 1
    ki = dict(u)
    ki["sor"] = _FELUTESEK[i] + u["sor"]
    return ki


def _sajat_valasz(kerdes, f):
    """Determinisztikus válasz a kérdés témájára, kizárólag az élő adatokból.

    Ez a válasz akkor is elkészül, ha a Gemini nem elérhető — ezért Flux
    sosem marad néma. A számok közvetlenül a `tenyek()` mezőiből jönnek,
    ezért ellenőrzésre nincs szükség: nincs honnan kitalálni semmit."""
    k = str(kerdes).lower().strip()
    if not k:
        return None
    for minta, kezelo in _TEMAK:
        if not any(m in k for m in minta):
            continue
        try:
            v = kezelo(f)
        except Exception as e:
            print(f"[FLUX] Téma ({kezelo.__name__}): {e}", flush=True)
            v = None
        if v:
            return _felutessel(v)
    return None


def valasz(kerdes, data, ajanlas=None, kontextus=None):
    """A látogató kérdésére adott egyetlen válasz.

    Sorrend: (1) determinisztikus válasz a témára, (2) Gemini, ha átmegy az
    ellenőrzésen, (3) ha a téma sem talált, elmondjuk, miről tudunk beszélni.
    Kivételt sosem dob."""
    f, _ = tenyek(data, ajanlas)
    if f is None:
        return _u("Élő adatokra várok — amint megérkeznek, válaszolok a kérdésedre.")
    if not str(kerdes).strip():
        return _u("Kérdezz nyugodtan: az árakról, a töltési ablakról, a fogyasztásról, "
                  "a nap- és széltermelésről, az időjárásról vagy a modell pontosságáról "
                  "is tudok beszélni.", None, "amiről kérdezhetsz")

    # 0) Ugyanarra a kérdésre, ugyanabban az adatállapotban ne hívjuk újra a
    # modellt. Több látogató jellemzően ugyanazt kérdezi, és a kvóta közös.
    valasz_kulcs = f"{_hash(f)}:{' '.join(str(kerdes).lower().split())[:120]}"
    tarolt = None
    with _MEMO_LOCK:
        rec = _VALASZ_MEMO.get(valasz_kulcs)
        if rec and rec["lejar"] > time.time():
            tarolt = rec["ertek"]
        elif rec:
            _VALASZ_MEMO.pop(valasz_kulcs, None)
    if tarolt is not None:
        return tarolt

    # 1) A biztos válasz azonnal kész — erre mindig van mit visszaadni.
    biztos = _sajat_valasz(kerdes, f)
    if biztos is None and kontextus:
        # "És ez?" — a kérdés a képernyőn olvasott mondatra utal vissza.
        # Ilyenkor a téma AZ olvasott mondatból derül ki, nem a kérdésből.
        biztos = _sajat_valasz(kontextus, f)
    if biztos is None:
        # Naplózzuk, mire nem találtunk témát — ebből bővíthető a kulcsszólista.
        print(f"[FLUX] Nem talált témát a kérdésre: {str(kerdes)[:120]!r}", flush=True)

    # 2) A Gemini csak akkor kerül a helyére, ha valóban jobb és hiteles.
    # Ha átmeneti okból (kvóta, lassúság) nem sikerül, ezt a látogató is
    # megtudja — a válasz elé kerül egy rövid, őszinte mondat.
    elonezet = ""
    try:
        meta = f.get("_meta") or {}
        prompt = (
            f"Most {meta.get('helyi_ido')} van Budapesten, tehát "
            f"{meta.get('napszak')} van. Ehhez igazítsd a hangnemet és az igeidőket.\n\n"
            "Élő adatok:\n"
            f"{json.dumps(f, ensure_ascii=False, default=str)}\n\n"
            + (f"A látogató ÉPPEN EZT a megállapítást olvasta a képernyőn, "
               f"amikor kérdezett: \"{str(kontextus).strip()[:300]}\"\n"
               f"Ha a kérdés visszautal rá ('ez', 'erről', 'miért'), erre "
               f"vonatkozik.\n\n" if kontextus else "")
            + f"A látogató kérdése: {str(kerdes).strip()[:300]}\n\n"
            "Válaszolj EGY elemmel a megadott formában. A 'sor' a beszélgető "
            "válasz (2-3 mondat), a 'szam' egyetlen kiemelt érték mértékegységgel "
            "az adatokból, a 'cimke' 2-5 szóban megmondja, mi az a szám. "
            "Használhatod a 'megujulok', 'fogyasztas', 'tegnapi_osszevetes', 'dam', "
            "'toltes', 'kartyak', 'idojaras', 'modell_pontossag', 'heti_merleg', "
            "'stl' és 'adatminoseg' mezőket.\n"
            "IDŐ: ha egy időpont nem a mai napra esik, írd ki, hogy holnapi.\n"
            "NAPELEM: a 'nap_csucs_meg_hatravan' megmondja, hogy a mai csúcs még "
            "hátravan-e. Ha 'nap_termeles_mara_lezarult' szerepel, a mai termelésről "
            "CSAK múlt időben beszélhetsz; a 'nap_holnapi_csucs_mw' a HOLNAPI terv, "
            "ezt mindig nevezd meg holnapiként.\n"
            "Csak akkor mondd, hogy nem tudsz válaszolni, ha a kérdés témájához "
            "TÉNYLEG nincs mező az adatokban."
        )
        v = _gemini(prompt, _UZENET_SEMA, timeout=GEMINI_KERDES_TIMEOUT,
                    szerep=FLUX_SZEREP_KERDES, homerseklet=0.7)
        jo, hibak = _ellenoriz(v.get("uzenetek", [])[:1], f)
        if jo:
            _valasz_memo_ir(valasz_kulcs, jo[0])
            return jo[0]
        print(f"[FLUX] Kérdés elbukott az ellenőrzésen: {hibak}", flush=True)
    except GeminiKvotaHiba as e:
        # Ez nem hiba, hanem átmeneti állapot — a látogató megérdemli, hogy
        # megtudja, miért lett Flux hirtelen szűkszavú.
        print(f"[FLUX] Kérdés — kvóta: {e}", flush=True)
        elonezet = ("Most épp betelt a nyelvi modell kerete, úgyhogy tömörebben "
                    "fogalmazok — amint újratöltődik, megint bővebben válaszolok. "
                    "Az adatok viszont ugyanúgy élők: ")
    except GeminiLassuHiba as e:
        print(f"[FLUX] Kérdés — lassú: {e}", flush=True)
        elonezet = ("A nyelvi modell most lassan felel, ezért röviden mondom, "
                    "de az adatok élők: ")
    except Exception as e:
        print(f"[FLUX] Kérdés: {e}", flush=True)

    if biztos:
        if elonezet:
            biztos = dict(biztos)
            # A magyarázó mondat után a saját mondat kisbetűvel folytatódik.
            sor = biztos["sor"]
            biztos["sor"] = elonezet + (sor[0].lower() + sor[1:] if sor else sor)
        else:
            # Csak a "tiszta" választ tesszük el. A kvóta- vagy lassúság-jelzés
            # átmeneti állapotot ír le, azt nem szabad 10 percre bebetonozni.
            _valasz_memo_ir(valasz_kulcs, biztos)
        return biztos

    # 3) Nem találtunk témát: ne egy üres "nem tudom" menjen ki, hanem az,
    # hogy pontosan miről lehet kérdezni ebben a pillanatban.
    kepessegek = _t_kepessegek(f)
    if kepessegek:
        return _u("Erre a kérdésre az élő adatokból nem tudok pontos választ adni. "
                  + kepessegek["sor"], None, "amiről kérdezhetsz")
    return _u("Erre a kérdésre az élő adatokból jelenleg nem áll rendelkezésre pontos "
              "válasz.")


# ============================================================
# 8) INDULÁSI DIAGNOSZTIKA
#
# Ebből a Render naplójában egy pillantással látszik, hogy a Gemini
# egyáltalán be van-e kötve. Ha a kulcs hiányzik vagy a modellnév rossz,
# Flux némán a determinisztikus válaszokra esik vissza — ez a sor mondja
# meg, hogy melyik eset áll fenn.
# ============================================================
print(
    f"[FLUX] Indulás — Gemini kulcs: "
    f"{'BEÁLLÍTVA (' + str(len(GEMINI_API_KEY)) + ' karakter)' if GEMINI_API_KEY else 'HIÁNYZIK'}"
    f" | modellek: {', '.join(GEMINI_MODELLEK)}"
    f" | adatbázis: {'igen' if _db_ok() else 'nem'}",
    flush=True,
)
