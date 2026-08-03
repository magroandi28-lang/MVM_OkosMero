"""
Flux — a főoldal élő adatokat magyarázó asszisztense.

Két Supabase-tábla (private séma):
  private.flux_fact_snapshot  — a hiteles adatcsomag, amiből az összefoglaló készült
  private.flux_summary        — az ellenőrzött, megjelenített szöveg (gyorsítótár)

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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

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

FLUX_SZEREP = (
    "Te vagy Flux, az OkosMérő energiapiaci irányítópult asszisztense. "
    "Magyarul, tegezve, barátságosan és tömören beszélsz. "
    "KIZÁRÓLAG a megkapott JSON adatokra támaszkodhatsz. "
    "Számot csak akkor írhatsz le, ha az pontosan szerepel az adatokban. "
    "Nem becsülsz, nem kerekítesz át, nem találsz ki semmit. "
    "Ha egy adat hiányzik, arról a témáról nem beszélsz."
)


# Zárómondatok: nincs bennük élő adat, ezért mindig kimennek —
# a Gemini-változat végére is. Ezek hívják körbe a látogatót az oldalon.
# A modellek bemutatása. Ezek NEM élő adatok, hanem a dokumentált,
# offline validált eredmények — ezért fix szövegek, nem a Gemini írja.
MODELL_UZENETEK = [
    {"sor": "Az OkosMérő két külön módszert használ: a CatBoost előrejelzi a várható "
            "fogyasztást, az STL pedig megkeresi a szokatlan eltéréseket.",
     "szam": None, "cimke": None},
    {"sor": "A CatBoost egy gépi tanulási modell: korábbi villamosenergia-adatokból "
            "tanulta meg, hogy különböző körülmények között általában hogyan változik "
            "az ország fogyasztása.",
     "szam": None, "cimke": None},
    {"sor": "Az STL azt vizsgálja, hogy a ténylegesen bekövetkezett fogyasztás mennyire "
            "tér el a megszokott mintától.",
     "szam": None, "cimke": None},
    {"sor": "A két módszer együtt nemcsak előrejelzést ad, hanem segít felismerni, "
            "amikor az energiarendszer viselkedése valamilyen okból szokatlanná válik.",
     "szam": None, "cimke": None},
]


ZARO_UZENETEK = [
    {"sor": "Kérdezz nyugodtan a beviteli mezőben: az árakról, a fogyasztásról, a nap- "
            "és széltermelésről, az időjárásról vagy a modell pontosságáról is "
            "válaszolok az élő adatokból.",
     "szam": None, "cimke": None},
    {"sor": "Nézz körül nyugodtan: a DAM-árak és töltés fülön negyedórás "
            "bontásban látod a másnapi árakat és az ajánlott töltési ablakokat.",
     "szam": None, "cimke": None},
    {"sor": "Az Energiaelemzés fülön a következő órák fogyasztási előrejelzése, "
            "a Megújulókon a nap- és széltermelés terve és tényleges alakulása várja.",
     "szam": None, "cimke": None},
    {"sor": "Az ML Modell Labor pedig élőben mutatja a két modellt: a CatBoost "
            "gépi tanulási modell órákra előre becsli a fogyasztást, az STL-módszer "
            "pedig a szokásostól eltérő órákat találja meg és keresi rájuk a magyarázatot.",
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
                ma_nap.append(n); ma_szel.append(sz)
                (jovo_nap if t > most_ts else mult_nap).append(n)
                if t > most_ts:
                    jovo_szel.append(sz)
                if i < len(tny_nap) and tny_nap[i] is not None:
                    mert_nap_ma.append(float(tny_nap[i]))
                    utolso_mert_nap = (t, float(tny_nap[i]))
                if i < len(tny_szel) and tny_szel[i] is not None:
                    mert_szel_ma.append(float(tny_szel[i]))
                    utolso_mert_szel = (t, float(tny_szel[i]))
            elif t.date() == holnap_datum:
                holnap_nap.append(n); holnap_szel.append(sz)

        mg = {}
        if ma_nap:
            mg["nap_mai_csucs_mw"] = _kerekit(max(ma_nap))
        if ma_szel:
            mg["szel_mai_csucs_mw"] = _kerekit(max(ma_szel))
            mg["szel_mai_atlag_mw"] = _kerekit(sum(ma_szel) / len(ma_szel))

        # A "várható" szó CSAK a MAI hátralévő órákra igaz. Este a napelemes
        # termelés már lecsengett, ilyenkor múlt időben beszélünk róla.
        if jovo_nap and max(jovo_nap) > 50:
            mg["nap_hatralevo_csucs_mw"] = _kerekit(max(jovo_nap))
        else:
            mg["nap_termeles_mara_lezarult"] = True
            if mult_nap:
                mg["nap_mai_tetozes_mw"] = _kerekit(max(mult_nap))
        if jovo_szel:
            mg["szel_hatralevo_csucs_mw"] = _kerekit(max(jovo_szel))

        # A holnapi terv KÜLÖN mező, saját címkével — sosem keveredhet a maival.
        if holnap_nap:
            mg["nap_holnapi_csucs_mw"] = _kerekit(max(holnap_nap))
        if holnap_szel:
            mg["szel_holnapi_csucs_mw"] = _kerekit(max(holnap_szel))

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
    return f"{facts_hash}:{GEMINI_MODEL}:{PROMPT_VERSION}:{LANGUAGE}"


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
            insert into private.flux_fact_snapshot
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
            from private.flux_summary
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
            insert into private.flux_summary
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
        u.append({
            "sor": f"A CatBoost modell előrejelzése szerint a legmagasabb terhelés "
                   f"várhatóan {_ora_sav(fo['csucs_idopont'])} között alakulhat ki, a "
                   f"fogyasztás csúcsértéke pedig megközelítheti a {csucs} MWh-t.",
            "szam": f"{csucs} MWh", "cimke": "előrejelzett csúcsterhelés"})
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

    if ho is not None:
        u.append({
            "sor": f"A budapesti mért hőmérséklet jelenleg {ho:.0f} °C, ami a hűtési "
                   f"igényen keresztül közvetlenül befolyásolja a rendszerterhelést.",
            "szam": f"{ho:.0f} °C", "cimke": "mért hőmérséklet · Budapest"})

    mg = f.get("megujulok")
    if mg and mg.get("nap_hatralevo_csucs_mw"):
        cs = _ezres(mg["nap_hatralevo_csucs_mw"])
        u.append({
            "sor": f"A napenergia-termelés a mai nap hátralévő részében várhatóan "
                   f"{cs} MW körüli csúcsértéket érhet el.",
            "szam": f"{cs} MW", "cimke": "hátralévő napenergia-csúcs"})
    elif mg and mg.get("nap_termeles_mara_lezarult"):
        tetoz = mg.get("nap_mai_tetozes_mw") or mg.get("nap_mai_csucs_mw")
        if tetoz:
            cs = _ezres(tetoz)
            sor = f"A napelemes termelés mára lecsengett; a mai tetőzés {cs} MW volt."
            # A holnapi terv KÜLÖN mondatban, egyértelmű címkével — sosem
            # keveredhet össze a mai értékkel.
            if mg.get("nap_holnapi_csucs_mw"):
                sor += (f" A napelőtti terv szerint holnap "
                        f"{_ezres(mg['nap_holnapi_csucs_mw'])} MW körüli csúcs várható.")
            u.append({"sor": sor, "szam": f"{cs} MW", "cimke": "mai napenergia-tetőzés"})

    if mg and mg.get("szel_mai_atlag_mw") is not None:
        atl_szel = _ezres(mg["szel_mai_atlag_mw"])
        u.append({
            "sor": f"A széltermelés mai átlaga a napelőtti terv szerint {atl_szel} MW, "
                   f"a napi csúcs {_ezres(mg.get('szel_mai_csucs_mw') or 0)} MW.",
            "szam": f"{atl_szel} MW", "cimke": "mai átlagos széltermelés"})

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

def _gemini(prompt, sema, timeout=GEMINI_TIMEOUT):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY nincs beállítva")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent")
    r = requests.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        json={
            "system_instruction": {"parts": [{"text": FLUX_SZEREP}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.4,
                "responseMimeType": "application/json",
                "responseSchema": sema,
            },
        },
        timeout=timeout,
    )
    r.raise_for_status()
    reszek = r.json()["candidates"][0]["content"]["parts"]
    return json.loads("".join(p.get("text", "") for p in reszek))


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

def uzenetek(data, ajanlas=None, koszonto=KOSZONTO):
    """A főoldali Flux-szövegek. Sosem dob kivételt: hiba esetén sablonra vált."""
    f, minoseg = tenyek(data, ajanlas)
    if f is None:
        return [{"sor": "Élő adatokra várok — amint megérkeznek, mondom, mi történik.",
                 "szam": None, "cimke": None}]

    tartalek = sablon_uzenetek(f) + MODELL_UZENETEK + ZARO_UZENETEK
    if koszonto:
        tartalek[0] = {"sor": koszonto, "szam": None, "cimke": None}

    fh = _hash(f)
    ck = _cache_key(fh)

    # 1) Folyamaton belüli gyorsítótár — nulla hálózat.
    kesz = _memo_olvas(ck)
    if kesz is not None:
        return kesz

    # 2) Adatbázis-gyorsítótár — másik worker már elkészíttethette.
    if _db_ok():
        try:
            tarolt = _summary_olvas(ck)
            if tarolt:
                ertek = json.loads(tarolt)
                _memo_ir(ck, ertek)
                return ertek
        except Exception as e:
            print(f"[FLUX] Gyorsítótár olvasás: {e}", flush=True)

    allapot, model_nev, hibak = "fallback", "deterministic-template-v1", []
    vegleges = tartalek

    try:
        prompt = (
            "Az alábbi JSON a magyar villamosenergia-rendszer élő adatait tartalmazza.\n"
            f"{json.dumps(f, ensure_ascii=False, default=str)}\n\n"
            "Készíts 4-5 megállapítást a főoldalra. Minden megállapítás:\n"
            "- 'sor': egy vagy két teljes mondat, tárgyilagos, szakmai hangnemben "
            "('várhatóan', 'megközelítheti', 'amennyiben megoldható'). A számot írd bele "
            "a mondatba. Ne használj gondolatjelet, ne írj címszavakat, ne tegezd és ne "
            "oktasd ki az olvasót,\n"
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
            "Soha ne adj tanácsot a látogatónak és ne oktasd ki: csak azt mondd el, "
            "mit mutatnak az adatok.\n"
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
            vegleges = (([{"sor": koszonto, "szam": None, "cimke": None}] if koszonto else [])
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
    sor = (f"A CatBoost V10 modell a következő {fo['orak_szama']} órára jelez előre. "
           f"A legmagasabb terhelés {_ora_sav(fo['csucs_idopont'])} között várható, "
           f"a csúcsérték {csucs} MWh.")
    if fo.get("aktualis_ora_mwh"):
        sor += f" A most futó órára {_ezres(fo['aktualis_ora_mwh'])} MWh a jóslat."
    sz_el = fo.get("elteres_heti_atlagtol_szazalek")
    if sz_el is not None and abs(sz_el) >= 1:
        irany = "meghaladja" if sz_el > 0 else "elmarad"
        kot = "a heti átlagot" if sz_el > 0 else "a heti átlagtól"
        sor += (f" Ez {abs(sz_el):.0f} százalékkal {irany} {kot}.")
    return _u(sor, f"{csucs} MWh", "előrejelzett csúcsterhelés")


def _t_napenergia(f):
    mg = f.get("megujulok") or {}
    if not mg:
        return None
    # A MAI, hátralévő órákra vonatkozó csúcs — csak ha tényleg van még hátra.
    if mg.get("nap_hatralevo_csucs_mw"):
        cs = _ezres(mg["nap_hatralevo_csucs_mw"])
        sor = (f"A napelemes termelés a mai nap hátralévő részében várhatóan {cs} MW "
               f"körüli csúcsot érhet el.")
        if mg.get("nap_eddigi_merte_csucs_mw"):
            sor += (f" A ma eddig mért legmagasabb érték "
                    f"{_ezres(mg['nap_eddigi_merte_csucs_mw'])} MW volt.")
        return _u(sor, f"{cs} MW", "hátralévő napenergia-csúcs")

    # A mai termelés lezárult: KIZÁRÓLAG múlt idő, és a holnapi terv külön mondatban.
    tetoz = mg.get("nap_mai_tetozes_mw") or mg.get("nap_mai_csucs_mw")
    if tetoz:
        cs = _ezres(tetoz)
        sor = f"A mai napelemes termelés már lecsengett, a nap tetőzése {cs} MW volt."
        if mg.get("nap_eddigi_merte_csucs_mw"):
            sor += (f" A ténylegesen mért napi maximum "
                    f"{_ezres(mg['nap_eddigi_merte_csucs_mw'])} MW.")
        if mg.get("nap_holnapi_csucs_mw"):
            sor += (f" Holnapra a napelőtti terv "
                    f"{_ezres(mg['nap_holnapi_csucs_mw'])} MW körüli csúcsot jelez.")
        return _u(sor, f"{cs} MW", "mai napenergia-tetőzés")
    if mg.get("nap_holnapi_csucs_mw"):
        cs = _ezres(mg["nap_holnapi_csucs_mw"])
        return _u(f"A holnapi napra a napelőtti terv {cs} MW körüli napelemes csúcsot "
                  f"jelez.", f"{cs} MW", "holnapi napenergia-csúcs")
    return None


def _t_szel(f):
    mg = f.get("megujulok") or {}
    if mg.get("szel_mai_csucs_mw") is None:
        return None
    cs = _ezres(mg["szel_mai_csucs_mw"])
    sor = f"A széltermelés mai csúcsa a napelőtti terv szerint {cs} MW."
    if mg.get("szel_mai_atlag_mw") is not None:
        sor += f" A mai napi átlag {_ezres(mg['szel_mai_atlag_mw'])} MW."
    if mg.get("szel_utolso_mert_mw") is not None:
        sor += (f" A legutóbb mért érték {_ezres(mg['szel_utolso_mert_mw'])} MW "
                f"{_ido(mg.get('szel_utolso_mert_idopont'))}-kor.")
    elif mg.get("szel_hatralevo_csucs_mw") is not None:
        sor += (f" A nap hátralévő részében {_ezres(mg['szel_hatralevo_csucs_mw'])} MW "
                f"a várható maximum.")
    return _u(sor, f"{cs} MW", "mai széltermelési csúcs")


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
        sor = (f"A budapesti mért hőmérséklet most {ho:.0f} °C, ami a fűtési és hűtési "
               f"igényen keresztül közvetlenül hat a rendszerterhelésre.")
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
    return _u(f"A legutóbbi mért országos fogyasztás {_ezres(mert['ertek_mwh'])} MWh volt "
              f"{mert['idopont']}-kor.", f"{_ezres(mert['ertek_mwh'])} MWh",
              "legutóbbi mért fogyasztás")


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
            return v
    return None


def valasz(kerdes, data, ajanlas=None):
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

    # 1) A biztos válasz azonnal kész — erre mindig van mit visszaadni.
    biztos = _sajat_valasz(kerdes, f)

    # 2) A Gemini csak akkor kerül a helyére, ha valóban jobb és hiteles.
    try:
        prompt = (
            "Élő adatok:\n"
            f"{json.dumps(f, ensure_ascii=False, default=str)}\n\n"
            f"A látogató kérdése: {str(kerdes).strip()[:300]}\n\n"
            "Válaszolj EGY megállapítással ugyanabban a formában "
            "('sor', 'szam', 'cimke'). Használd a 'megujulok', 'fogyasztas', 'dam', "
            "'toltes', 'kartyak', 'idojaras', 'modell_pontossag', 'heti_merleg', "
            "'stl' és 'adatminoseg' mezőket. "
            "Ha az időpont nem mai, írd ki, hogy holnapi. "
            "NAPELEM: a 'nap_hatralevo_csucs_mw' a MAI hátralévő órákra vonatkozik; ha "
            "'nap_termeles_mara_lezarult' szerepel, a mai termelésről CSAK múlt időben "
            "beszélhetsz; a 'nap_holnapi_csucs_mw' a HOLNAPI terv, ezt mindig nevezd "
            "meg holnapiként. "
            "Csak akkor mondd, hogy nem tudsz válaszolni, ha a kérdés témájához "
            "TÉNYLEG nincs mező az adatokban."
        )
        v = _gemini(prompt, _UZENET_SEMA, timeout=GEMINI_KERDES_TIMEOUT)
        jo, hibak = _ellenoriz(v.get("uzenetek", [])[:1], f)
        if jo:
            return jo[0]
        print(f"[FLUX] Kérdés elbukott az ellenőrzésen: {hibak}", flush=True)
    except Exception as e:
        print(f"[FLUX] Kérdés: {e}", flush=True)

    if biztos:
        return biztos

    # 3) Nem találtunk témát: ne egy üres "nem tudom" menjen ki, hanem az,
    # hogy pontosan miről lehet kérdezni ebben a pillanatban.
    kepessegek = _t_kepessegek(f)
    if kepessegek:
        return _u("Erre a kérdésre az élő adatokból nem tudok pontos választ adni. "
                  + kepessegek["sor"], None, "amiről kérdezhetsz")
    return _u("Erre a kérdésre az élő adatokból jelenleg nem áll rendelkezésre pontos "
              "válasz.")
