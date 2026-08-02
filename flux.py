"""
Flux — a főoldal élő adatokat magyarázó asszisztense.

Két Supabase-tábla (private séma):
  private.flux_fact_snapshot  — a hiteles adatcsomag, amiből az összefoglaló készült
  private.flux_summary        — az ellenőrzött, megjelenített szöveg (gyorsítótár)

A Gemini-hívás KIZÁRÓLAG itt, a szerveren történik; a kulcs nem kerül a böngészőbe.
Ha a Gemini nem elérhető vagy a válasza nem megy át az ellenőrzésen, determinisztikus
sablon szolgál helyette — kitalált szám sosem jelenik meg.
"""

import os
import json
import hashlib
from datetime import datetime, timedelta, timezone

import requests

try:
    import psycopg
except ImportError:
    psycopg = None

DATABASE_URL = os.environ.get("DATABASE_URL", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

SCHEMA_VERSION = 1
PROMPT_VERSION = "flux-v1"
LANGUAGE = "hu-HU"
TTL_PERC = 20          # ennyi ideig használjuk újra ugyanazt az összefoglalót
GEMINI_TIMEOUT = 12

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


# ============================================================
# 1) TÉNYEK — az app `data` store-jából
# ============================================================

def _kerekit(x, tizedes=0):
    try:
        return round(float(x), tizedes)
    except (TypeError, ValueError):
        return None


def tenyek(data, ajanlas=None):
    """A `fetch()` által előállított `data` dict-ből kivágja azt, amit Flux használhat."""
    if not data or data.get("kritikus_hiba"):
        return None, "invalid"

    f = {}

    dam_ma = data.get("dam_ma_oras") or []
    if dam_ma:
        f["dam"] = {
            "mai_atlag_eur_mwh": _kerekit(sum(dam_ma) / len(dam_ma), 1),
            "mai_min_eur_mwh": _kerekit(min(dam_ma), 1),
            "mai_max_eur_mwh": _kerekit(max(dam_ma), 1),
            "holnapi_ar_publikalva": bool(data.get("holnapi_ar")),
        }

    # A hat KPI-kártya értékei — pontosan az, amit a látogató lát a fejléc alatt.
    kartyak = {}
    if data.get("aho") is not None:
        kartyak["budapest_homerseklet_c"] = _kerekit(data["aho"])
    if data.get("eur_huf") is not None:
        kartyak["eur_huf"] = _kerekit(data["eur_huf"], 1)
    if ajanlas:
        kartyak["jelenlegi_dam_ar_eur_mwh"] = _kerekit(ajanlas.get("akt_ar"), 1)
    if kartyak:
        f["kartyak"] = kartyak

    if ajanlas:
        f["toltes"] = {
            "aktualis_ar_eur_mwh": _kerekit(ajanlas.get("akt_ar"), 1),
            "ajanlott_kezdet": ajanlas.get("aj_kezd"),
            "ajanlott_veg": ajanlas.get("aj_veg"),
            "ajanlott_ar_eur_mwh": _kerekit(ajanlas.get("aj_ar"), 1),
        }

    eredm = data.get("eredm") or []
    if eredm:
        ertekek = [r["fogyasztas"] for r in eredm]
        csucs = max(eredm, key=lambda r: r["fogyasztas"])
        f["fogyasztas"] = {
            "elorejelzett_csucs_mwh": _kerekit(csucs["fogyasztas"]),
            "csucs_idopont": csucs["datum"],
            "elorejelzett_min_mwh": _kerekit(min(ertekek)),
            "orak_szama": len(eredm),
            "modell": "CatBoost V10",
        }
        heti = data.get("heti_atlag")
        if heti:
            ora = datetime.fromisoformat(csucs["datum"]).hour
            if 0 <= ora < len(heti):
                f["fogyasztas"]["elteres_heti_atlagtol_mwh"] = _kerekit(
                    csucs["fogyasztas"] - heti[ora])

    mert = data.get("mert_fogyasztas")
    if mert:
        f["mert_fogyasztas"] = {"ertek_mwh": _kerekit(mert["ertek"]),
                                "idopont": mert["idopont"]}

    val = data.get("validacio") or {}
    napi = val.get("napi") or {}
    if napi:
        f["modell_pontossag"] = {
            "catboost_mae_mwh": _kerekit(napi.get("cb_mae"), 1),
            "mavir_mae_mwh": _kerekit(napi.get("mavir_mae"), 1),
            "orak_szama": napi.get("orak"),
        }

    naplo = val.get("naplo") or []
    if naplo:
        nyitott = sum(1 for r in naplo if r.get("kategoria") == "rejtely")
        f["adatminoseg"] = {
            "jelzes_7_nap": len(naplo),
            "nyitott": nyitott,
            "megmagyarazva": len(naplo) - nyitott,
        }

    meg = data.get("megujulo") or {}
    if meg.get("fc_nap") and meg.get("fc_szel"):
        f["megujulok"] = {
            "nap_varhato_csucs_mw": _kerekit(max(meg["fc_nap"])),
            "szel_varhato_csucs_mw": _kerekit(max(meg["fc_szel"])),
        }
        if (meg.get("hiba_nap") or {}).get("mae") is not None:
            f["megujulok"]["nap_mai_mae_mw"] = _kerekit(meg["hiba_nap"]["mae"])
        if (meg.get("hiba_szel") or {}).get("mae") is not None:
            f["megujulok"]["szel_mai_mae_mw"] = _kerekit(meg["hiba_szel"]["mae"])

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
    kanon = json.dumps(facts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(kanon.encode("utf-8")).hexdigest()


def _cache_key(facts_hash):
    return f"{facts_hash}:{GEMINI_MODEL}:{PROMPT_VERSION}:{LANGUAGE}"


# ============================================================
# 2) ADATBÁZIS
# ============================================================

def _db_ok():
    return bool(DATABASE_URL) and psycopg is not None


def _connect():
    return psycopg.connect(DATABASE_URL, connect_timeout=10, prepare_threshold=None)


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
# 3) DETERMINISZTIKUS SABLON — ez a biztonsági háló
# ============================================================

def _ido(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except Exception:
        return ""


def sablon_uzenetek(f):
    """Kitalálás nélküli, számokból összerakott megállapítások."""
    u = [{"sor": KOSZONTO, "szam": None, "cimke": None}]

    t = f.get("toltes")
    if t and t.get("ajanlott_ar_eur_mwh") is not None:
        u.append({
            "sor": "A következő kedvező töltési ablak már megvan — ekkor a legolcsóbb az áram.",
            "szam": f"{t['ajanlott_ar_eur_mwh']:.0f} €/MWh",
            "cimke": f"{_ido(t['ajanlott_kezdet'])} – {_ido(t['ajanlott_veg'])} · ajánlott ablak",
        })

    fo = f.get("fogyasztas")
    if fo and fo.get("elorejelzett_csucs_mwh"):
        elteres = fo.get("elteres_heti_atlagtol_mwh")
        sor = "A mai csúcsterhelést a CatBoost erre az órára várja."
        if elteres is not None:
            sor = ("A mai csúcs magasabb lesz a heti átlagnál." if elteres > 0
                   else "A mai csúcs a heti átlag alatt marad.")
        u.append({
            "sor": sor,
            "szam": f"{fo['elorejelzett_csucs_mwh']:,.0f} MWh".replace(",", " "),
            "cimke": f"{_ido(fo['csucs_idopont'])} · előrejelzett csúcs",
        })

    m = f.get("modell_pontossag")
    if m and m.get("catboost_mae_mwh") is not None and m.get("mavir_mae_mwh") is not None:
        jobb = m["catboost_mae_mwh"] < m["mavir_mae_mwh"]
        u.append({
            "sor": ("A CatBoost ma pontosabb, mint a hivatalos MAVIR-előrejelzés."
                    if jobb else "Ma a MAVIR-előrejelzés pontosabb a modellünknél."),
            "szam": f"{m['catboost_mae_mwh']:.0f} vs {m['mavir_mae_mwh']:.0f} MWh",
            "cimke": "napi MAE · CatBoost / MAVIR",
        })

    k = f.get("kartyak") or {}
    if k.get("budapest_homerseklet_c") is not None and f.get("dam"):
        u.append({
            "sor": "Budapesten ennyi van most — a hőmérséklet húzza a fogyasztást és az árat is.",
            "szam": f"{k['budapest_homerseklet_c']:.0f} °C",
            "cimke": "Budapest · most",
        })

    mg = f.get("megujulok")
    if mg and mg.get("nap_varhato_csucs_mw"):
        u.append({
            "sor": "A napelemek mai várható csúcstermelése ennyi lesz a rendszerben.",
            "szam": f"{mg['nap_varhato_csucs_mw']:,.0f} MW".replace(",", " "),
            "cimke": "várható napenergia-csúcs",
        })

    a = f.get("adatminoseg")
    if a:
        u.append({
            "sor": ("Minden friss jelzésre van magyarázat." if a["nyitott"] == 0
                    else "Van még jelzés, ami magyarázatra vár az elmúlt hét napból."),
            "szam": f"{a['nyitott']} nyitott",
            "cimke": f"{a['jelzes_7_nap']} jelzés 7 nap · adatminőség-őr",
        })

    return u


# ============================================================
# 4) GEMINI + ELLENŐRZÉS
# ============================================================

def _gemini(prompt, sema):
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
        timeout=GEMINI_TIMEOUT,
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


def _szamok(szoveg):
    """A szövegben szereplő számok — az ellenőrzéshez."""
    jel, akt = [], ""
    for ch in str(szoveg):
        if ch.isdigit() or ch in ",.":
            akt += ch.replace(",", ".")
        else:
            if akt:
                jel.append(akt)
            akt = ""
    if akt:
        jel.append(akt)
    ki = set()
    for s in jel:
        try:
            ki.add(round(float(s.strip(".")), 1))
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
        elif isinstance(x, (int, float)):
            ki.add(round(float(x), 1))
            ki.add(round(float(x), 0))
    bejar(f)
    return ki


def _ellenoriz(uzenetek, f):
    """Minden leírt szám szerepeljen a tényadatokban. Ami nem, azt eldobjuk."""
    ok_szamok = _engedelyezett_szamok(f)
    jo, hibak = [], []
    for u in uzenetek:
        szoveg = f"{u.get('sor','')} {u.get('szam','')} {u.get('cimke','')}"
        idegen = [s for s in _szamok(szoveg)
                  if s not in ok_szamok and round(s, 0) not in ok_szamok]
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
# 5) BELÉPÉSI PONT — ezt hívja az app.py
# ============================================================

def uzenetek(data, ajanlas=None, koszonto=KOSZONTO):
    """A főoldali Flux-szövegek. Sosem dob kivételt: hiba esetén sablonra vált."""
    f, minoseg = tenyek(data, ajanlas)
    if f is None:
        return [{"sor": "Élő adatokra várok — amint megérkeznek, mondom, mi történik.",
                 "szam": None, "cimke": None}]

    tartalek = sablon_uzenetek(f)
    if koszonto:
        tartalek[0] = {"sor": koszonto, "szam": None, "cimke": None}

    fh = _hash(f)
    ck = _cache_key(fh)

    if _db_ok():
        try:
            kesz = _summary_olvas(ck)
            if kesz:
                return json.loads(kesz)
        except Exception as e:
            print(f"[FLUX] Gyorsítótár olvasás: {e}", flush=True)

    snapshot_id = None
    if _db_ok():
        try:
            snapshot_id = _snapshot_ment(
                f, minoseg, fh, {"hianyzo": f["_meta"]["hianyzo_forrasok"]})
        except Exception as e:
            print(f"[FLUX] Snapshot mentés: {e}", flush=True)

    allapot, model_nev, hibak = "fallback", "deterministic-template-v1", []
    vegleges = tartalek

    try:
        prompt = (
            "Az alábbi JSON a magyar villamosenergia-rendszer élő adatait tartalmazza.\n"
            f"{json.dumps(f, ensure_ascii=False, default=str)}\n\n"
            "Készíts 4-5 megállapítást a főoldalra. Minden megállapítás:\n"
            "- 'sor': EGY rövid, tegező mondat (max 110 karakter),\n"
            "- 'szam': egyetlen kiemelt érték mértékegységgel, pontosan az adatokból,\n"
            "- 'cimke': 2-5 szavas kontextus (időpont, forrás).\n"
            "Használhatod a 'kartyak' értékeit is — ezeket a látogató a fejléc alatt látja.\n"
            "Témák: töltési ablak és ár; a mai fogyasztási csúcs és eltérése; "
            "a CatBoost és a MAVIR pontossága; nyitott adatminőségi jelzések.\n"
            "Ne írj olyan számot, ami nincs a JSON-ban."
        )
        valasz = _gemini(prompt, _UZENET_SEMA)
        jo, hibak = _ellenoriz(valasz.get("uzenetek", []), f)
        if len(jo) >= 2:
            vegleges = ([{"sor": koszonto, "szam": None, "cimke": None}] if koszonto else []) + jo
            allapot, model_nev = "gemini", GEMINI_MODEL
        else:
            allapot = "rejected"
    except Exception as e:
        print(f"[FLUX] Gemini: {e}", flush=True)
        hibak = [{"kivetel": str(e)}]

    if _db_ok() and snapshot_id:
        try:
            _summary_ment(snapshot_id, ck, model_nev, allapot,
                          json.dumps(vegleges, ensure_ascii=False), hibak)
        except Exception as e:
            print(f"[FLUX] Summary mentés: {e}", flush=True)

    return vegleges


def valasz(kerdes, data, ajanlas=None):
    """A látogató kérdésére adott egyetlen válasz. Ellenőrzésen bukás esetén őszinte nemet mond."""
    f, _ = tenyek(data, ajanlas)
    nem_tudom = {"sor": "Erre az élő adatokból most nem tudok pontos választ adni.",
                 "szam": None, "cimke": None}
    if f is None or not str(kerdes).strip():
        return nem_tudom
    try:
        prompt = (
            "Élő adatok:\n"
            f"{json.dumps(f, ensure_ascii=False, default=str)}\n\n"
            f"A látogató kérdése: {str(kerdes).strip()[:300]}\n\n"
            "Válaszolj EGY megállapítással ugyanabban a formában "
            "('sor', 'szam', 'cimke'). Ha az adatokból nem válaszolható meg, "
            "a 'sor' legyen: 'Erre az élő adatokból most nem tudok pontos választ adni.' "
            "és a 'szam' maradjon üres."
        )
        v = _gemini(prompt, _UZENET_SEMA)
        jo, _ = _ellenoriz(v.get("uzenetek", [])[:1], f)
        return jo[0] if jo else nem_tudom
    except Exception as e:
        print(f"[FLUX] Kérdés: {e}", flush=True)
        return nem_tudom
