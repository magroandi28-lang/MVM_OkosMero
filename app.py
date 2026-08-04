import dash
from concurrent.futures import ThreadPoolExecutor, as_completed
from dash import html, dcc, callback, Input, Output, State
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
import joblib
import requests
import os
import time
import threading
import math
from datetime import datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo
from statsmodels.tsa.seasonal import STL
import holidays
from dotenv import load_dotenv
try:
    import psycopg
except ImportError:
    psycopg = None
load_dotenv()

import flux

app = dash.Dash(__name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    suppress_callback_exceptions=True,
    meta_tags=[{"name":"viewport","content":"width=device-width,initial-scale=1"}])
app.title = "OkosMérő"
server = app.server

# Gzip-tömörítés: a Plotly és a Dash JS-csomagok így töredék méretben
# érkeznek meg — mobilneten ez a betöltés legnagyobb tétele.
try:
    from flask_compress import Compress
    Compress(server)
except Exception as _e:
    print(f"[INFO] Tömörítés nem aktív: {_e}", flush=True)

BASE = os.path.dirname(os.path.abspath(__file__))
ENTSOE_API_KEY = os.environ.get("ENTSOE_API_KEY","")
VISUAL_CROSSING_KEY = os.environ.get("VISUAL_CROSSING_KEY","")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
BUDAPEST_TZ = ZoneInfo("Europe/Budapest")
hu_holidays = holidays.Hungary(years=list(range(2015,2028)))

MODEL_VERSION = "catboost-v10"


def _db_available():
    if not DATABASE_URL:
        print("[INFO] DATABASE_URL nincs beallitva - forecast mentes kihagyva", flush=True)
        return False
    if psycopg is None:
        print("[HIBA] A psycopg csomag hianyzik - forecast mentes kihagyva", flush=True)
        return False
    return True


def _db_connect():
    """Supabase/Supavisor transaction poolerhez prepared statement nélkül."""
    return psycopg.connect(
        DATABASE_URL,
        connect_timeout=10,
        prepare_threshold=None,
    )


def _aware_budapest(value):
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize(BUDAPEST_TZ, ambiguous=False, nonexistent="shift_forward")
    return ts.tz_convert(BUDAPEST_TZ)


def _completed_load_utc(load, now):
    """Csak teljesen lezárt, órás tényadatok UTC időbélyeggel."""
    if load is None or len(load) == 0:
        return pd.Series(dtype="float64")

    load_utc = load.copy()
    idx = pd.DatetimeIndex(load_utc.index)
    if idx.tz is None:
        idx = idx.tz_localize(
            BUDAPEST_TZ, ambiguous="infer", nonexistent="shift_forward"
        )
    else:
        idx = idx.tz_convert(BUDAPEST_TZ)

    load_utc.index = idx.tz_convert("UTC").floor("h")
    load_utc = load_utc.groupby(level=0).mean().sort_index()

    now_utc = _aware_budapest(now).tz_convert("UTC")
    completed_cutoff = now_utc.floor("h") - pd.Timedelta(hours=1)
    return load_utc[load_utc.index <= completed_cutoff]


def save_pending_forecasts(forecast, generated_at, input_quality_label,
                           stl_anomaly_lag_count, source_type,
                           mavir_forecast=None):
    """Az élő célórák legutolsó jóslata.

    Célóránként pontosan egy ideiglenes sor marad. Újraszámoláskor csak
    ezt a sort frissíti. A már lezárt forecast_log sorhoz soha nem nyúl.
    """
    if not forecast or not _db_available():
        return

    generated_at = _aware_budapest(generated_at)
    run_id = f"{MODEL_VERSION}-{generated_at:%Y%m%dT%H%M%S%z}"
    mavir_forecast = mavir_forecast or {}
    rows = []

    for horizon_h, item in enumerate(forecast, start=1):
        target_local = pd.Timestamp(item["datum"])
        mavir_value = mavir_forecast.get(target_local)
        target_time = _aware_budapest(target_local).tz_convert("UTC")

        rows.append((
            run_id,
            generated_at.to_pydatetime(),
            target_time.to_pydatetime(),
            int(horizon_h),
            float(item["fogyasztas"]),
            float(mavir_value) if mavir_value is not None else None,
            MODEL_VERSION,
            input_quality_label,
            int(stl_anomaly_lag_count or 0),
            source_type,
            target_time.to_pydatetime(),
            float(item["fogyasztas"]),        # first_pred: csak az ELSŐ írásnál marad meg
            generated_at.to_pydatetime(),     # first_generated_at
            int(horizon_h),                   # first_horizon_h
        ))

    sql = """
        insert into public.forecast_pending (
            run_id, generated_at, target_time, horizon_h,
            catboost_pred_mwh, mavir_forecast_mwh, model_version,
            input_quality_label, stl_anomaly_lag_count, source_type, updated_at,
            first_pred_mwh, first_generated_at, first_horizon_h
        ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        on conflict (target_time) do update set
            first_pred_mwh = coalesce(
                public.forecast_pending.first_pred_mwh, excluded.first_pred_mwh),
            first_generated_at = coalesce(
                public.forecast_pending.first_generated_at, excluded.first_generated_at),
            first_horizon_h = coalesce(
                public.forecast_pending.first_horizon_h, excluded.first_horizon_h),
            run_id = excluded.run_id,
            generated_at = excluded.generated_at,
            horizon_h = excluded.horizon_h,
            catboost_pred_mwh = excluded.catboost_pred_mwh,
            mavir_forecast_mwh = coalesce(
                excluded.mavir_forecast_mwh,
                public.forecast_pending.mavir_forecast_mwh
            ),
            model_version = excluded.model_version,
            input_quality_label = excluded.input_quality_label,
            stl_anomaly_lag_count = excluded.stl_anomaly_lag_count,
            source_type = excluded.source_type,
            updated_at = excluded.updated_at
    """

    try:
        with _db_connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
        print(f"[INFO] forecast_pending frissitve: {len(rows)} celora", flush=True)
    except Exception as e:
        print(f"[HIBA] forecast_pending mentes: {type(e).__name__}: {e}", flush=True)


def fill_missing_pending_mavir(mavir_forecast):
    """Csak a hiányzó MAVIR-értékeket tölti ki, meglévőt nem ír felül."""
    if not mavir_forecast or not _db_available():
        return

    rows = []
    for target_local, value in mavir_forecast.items():
        if value is None:
            continue
        target_time = _aware_budapest(target_local).tz_convert("UTC")
        rows.append((float(value), target_time.to_pydatetime()))

    if not rows:
        return

    sql = """
        update public.forecast_pending
        set mavir_forecast_mwh = %s,
            updated_at = now()
        where target_time = %s
          and mavir_forecast_mwh is null
    """
    try:
        with _db_connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
    except Exception as e:
        print(f"[HIBA] hianyzo MAVIR visszatoltes: {type(e).__name__}: {e}", flush=True)


def finalize_completed_forecasts(load, now):
    """Lezárja azt az egy célórát/sorokat, amelyeket a modell már nem jósol újra.

    A forecast_log-ba kizárólag akkor kerül sor, ha:
      - az adott órához már van teljes, lezárt tényadat;
      - létezik hozzá korábban eltett CatBoost-jóslat;
      - létezik hozzá MAVIR-jóslat.

    A célóra bekerülése után a sor változtathatatlan. A pending sor törlődik.
    """
    if not _db_available():
        return

    completed = _completed_load_utc(load, now)
    if completed.empty:
        print("[INFO] forecast_log: nincs uj lezart ora", flush=True)
        return

    times = [t.to_pydatetime() for t in completed.index]
    values = [float(v) for v in completed.values]

    sql = """
        with actuals as (
            select *
            from unnest(%s::timestamptz[], %s::float8[])
                 as a(target_time, actual_mwh)
        ),
        inserted as (
            insert into public.forecast_log (
                run_id, generated_at, target_time, horizon_h,
                catboost_pred_mwh, mavir_forecast_mwh, actual_mwh,
                catboost_abs_error, mavir_abs_error, model_version,
                input_quality_label, stl_anomaly_lag_count, source_type,
                first_pred_mwh, first_generated_at, first_horizon_h,
                first_abs_error
            )
            select
                p.run_id,
                p.generated_at,
                p.target_time,
                p.horizon_h,
                p.catboost_pred_mwh,
                p.mavir_forecast_mwh,
                a.actual_mwh,
                abs(p.catboost_pred_mwh - a.actual_mwh),
                abs(p.mavir_forecast_mwh - a.actual_mwh),
                p.model_version,
                p.input_quality_label,
                p.stl_anomaly_lag_count,
                p.source_type,
                p.first_pred_mwh,
                p.first_generated_at,
                p.first_horizon_h,
                case when p.first_pred_mwh is null then null
                     else abs(p.first_pred_mwh - a.actual_mwh) end
            from public.forecast_pending p
            join actuals a on a.target_time = p.target_time
            where p.mavir_forecast_mwh is not null
            on conflict (target_time) do nothing
            returning target_time
        )
        delete from public.forecast_pending p
        using inserted i
        where p.target_time = i.target_time
    """

    try:
        with _db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (times, values))
                finalized = cur.rowcount
        print(f"[INFO] forecast_log: {finalized} celora veglegesen lezarva", flush=True)
    except Exception as e:
        print(f"[HIBA] forecast_log lezaras: {type(e).__name__}: {e}", flush=True)

def _anomalia_kategoria(t, residual, w, ido_map):
    """A nyomozás szabályai kódban. Sorrend = bizonyíték-erősség.
    None, ha nincs időjárás-kontextus (régi órák)."""
    if not w:
        return None
    temp = float(w["Homerseklet_C"])

    # 1) Időjárási extrém — a modell saját küszöbeivel összhangban
    if temp >= 30.0 or temp <= -5.0:
        return "extrem"

    # 2) Napelem-árnyék — nappali túlfogyasztás gyenge sugárzásnál:
    # a sugárzás nem éri el a környező napok AZONOS ÓRÁI maximumának 45%-át
    if 8 <= t.hour <= 16 and residual > 0:
        sug = float(w["Napsugarzas_W_m2"] or 0)
        tarsak = [float(r["Napsugarzas_W_m2"] or 0)
                  for dt, r in ido_map.items() if dt.hour == t.hour and dt != t]
        ref = max(tarsak) if tarsak else 0.0
        if ref > 100 and sug < ref * 0.45:
            return "napelem"

    # 3) Fordulat — a hőmérséklet 24 óra alatt legalább 6 fokot ugrott
    w_prev = ido_map.get(t - pd.Timedelta(hours=24))
    if w_prev is not None:
        if abs(temp - float(w_prev["Homerseklet_C"])) >= 6.0:
            return "fordulat"

    # 4) Ami marad: valódi kivizsgálandó
    return "rejtely"


def get_validacio_adatok():
    """A 2. oldal élő validációs sávja: napi MAE-k, win-rate, STL-kategóriák."""
    if not DATABASE_URL or psycopg is None:
        return None
    try:
        with _db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    select (target_time at time zone 'Europe/Budapest')::date as nap,
                           count(*), avg(catboost_abs_error), avg(mavir_abs_error),
                           avg(first_abs_error)
                    from public.forecast_log
                    group by 1 order by 1 desc limit 3
                """)
                napok = [{"nap": str(r[0]), "orak": int(r[1]),
                          "cb": float(r[2]), "mv": float(r[3]),
                          "first": float(r[4]) if r[4] is not None else None}
                         for r in cur.fetchall()]
                cur.execute("""
                    select count(*), avg(case when catboost_abs_error < mavir_abs_error
                                              then 1.0 else 0.0 end)
                    from public.forecast_log
                """)
                o = cur.fetchone()
                osszes = {"orak": int(o[0] or 0),
                          "win": float(o[1])*100 if o[1] is not None else None}
                cur.execute("""
                    select coalesce(kategoria,'besorolatlan'), count(*)
                    from public.stl_anomalia group by 1
                """)
                kat = {r[0]: int(r[1]) for r in cur.fetchall()}
                # ---- Heti napló: e hét (hétfőtől) kumulált eredményei ----
                cur.execute("""
                    select count(*),
                           avg(catboost_abs_error),
                           avg(first_abs_error),
                           avg(mavir_abs_error)
                    from public.forecast_log
                    where (target_time at time zone 'Europe/Budapest')
                          >= date_trunc('week', now() at time zone 'Europe/Budapest')
                """)
                h = cur.fetchone()
                het = {"orak": int(h[0] or 0),
                       "cb": float(h[1]) if h[1] is not None else None,
                       "first": float(h[2]) if h[2] is not None else None,
                       "mv": float(h[3]) if h[3] is not None else None}
                cur.execute("""
                    select extract(isodow from (target_time at time zone 'Europe/Budapest'))::int,
                           avg(first_abs_error), avg(mavir_abs_error)
                    from public.forecast_log
                    where (target_time at time zone 'Europe/Budapest')
                          >= date_trunc('week', now() at time zone 'Europe/Budapest')
                    group by 1
                """)
                het["napok"] = {int(r[0]): bool(float(r[1]) <= float(r[2]))
                                for r in cur.fetchall()
                                if r[1] is not None and r[2] is not None}
                cur.execute("""
                    select target_time at time zone 'Europe/Budapest', catboost_abs_error
                    from public.forecast_log
                    where (target_time at time zone 'Europe/Budapest')
                          >= date_trunc('week', now() at time zone 'Europe/Budapest')
                    order by catboost_abs_error asc limit 1
                """)
                jo = cur.fetchone()
                cur.execute("""
                    select target_time at time zone 'Europe/Budapest', catboost_abs_error
                    from public.forecast_log
                    where (target_time at time zone 'Europe/Budapest')
                          >= date_trunc('week', now() at time zone 'Europe/Budapest')
                    order by catboost_abs_error desc limit 1
                """)
                rossz = cur.fetchone()
                het["legjobb"] = ({"ido": jo[0].isoformat(), "err": float(jo[1])}
                                  if jo else None)
                het["legrosszabb"] = ({"ido": rossz[0].isoformat(), "err": float(rossz[1])}
                                      if rossz else None)
                # ---- Anomália-napló: az utolsó 7 nap esetei, legfrissebb elöl ----
                cur.execute("""
                    select target_time at time zone 'Europe/Budapest',
                           residual_mwh, homerseklet_c, kategoria
                    from public.stl_anomalia
                    where target_time >= now() - interval '7 days'
                    order by target_time desc
                """)
                naplo = [{"ido": r[0].isoformat(),
                          "residual": float(r[1]),
                          "homerseklet": float(r[2]) if r[2] is not None else None,
                          "kategoria": r[3]}
                         for r in cur.fetchall()]
        return {"napok": napok, "osszes": osszes, "kategoriak": kat, "het": het,
                "naplo": naplo}
    except Exception as e:
        print(f"[HIBA] validacios adatok: {type(e).__name__}: {e}", flush=True)
        return None

def save_stl_anomalies(load_series, stl_res, kuszob, atlag, ido_map, dam_oras):
    """STL-anomáliák mentése teljes kontextussal (fogyasztás + időjárás + ár).

    Csak a küszöböt átlépő órák kerülnek be. Az időjárás/ár oszlopok
    None-ok maradnak, ha az adott óra kívül esik a lekért ablakon —
    a friss anomáliáknál mindig lesz kontextus."""
    if not _db_available():
        return
    resid = stl_res.resid
    mask = abs(resid - atlag) > kuszob
    if not mask.any():
        return

    sql = """
        insert into public.stl_anomalia (
            target_time, actual_mwh, expected_mwh, residual_mwh, threshold_mwh,
            homerseklet_c, szelsebesseg_kmh, napsugarzas_w_m2, csapadek_mm,
            dam_eur_mwh, ora, hetvege, unnepnap, kategoria
        ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        on conflict (target_time) do update set
            kategoria = coalesce(excluded.kategoria, public.stl_anomalia.kategoria),
            residual_mwh = excluded.residual_mwh,
            expected_mwh = excluded.expected_mwh,
            threshold_mwh = excluded.threshold_mwh
    """
    rows = []
    for t in resid.index[mask]:
        w = ido_map.get(t) or {}
        tny = float(load_series.loc[t])
        r = float(resid.loc[t])
        rows.append((
            _aware_budapest(t).tz_convert("UTC").to_pydatetime(),
            tny, tny - r, r, float(kuszob),
            float(w["Homerseklet_C"]) if w else None,
            float(w["Szelsebesseg_kmh"]) if w else None,
            float(w["Napsugarzas_W_m2"]) if w else None,
            float(w["Csapadek_mm"]) if w else None,
            float(dam_oras[t]) if t in dam_oras else None,
            int(t.hour), t.weekday() >= 5, t.date() in hu_holidays,
            _anomalia_kategoria(t, r, w if w else None, ido_map),
        ))
    try:
        with _db_connect() as conn:
            with conn.cursor() as cur:
                for r in rows:
                    cur.execute(sql, r)
        print(f"[INFO] stl_anomalia: {len(rows)} ora mentve/frissitve", flush=True)
    except Exception as e:
        print(f"[HIBA] stl_anomalia mentes: {type(e).__name__}: {e}", flush=True)


C = {'bg':'#050d1a','sb':'#070f1e','card':'#0a1628','card2':'#0f1923','brd':'#1a2d42',
     'txt':'#cbd5e1','mut':'#64748b','or':'#FF6600','gr':'#10b981','bl':'#0066CC',
     'rd':'#ef4444','yw':'#f59e0b','cy':'#4b9cd3','wh':'#f1f5f9'}

CHART = dict(paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',
    font=dict(color=C['txt'],family='Inter,sans-serif',size=10),
    margin=dict(l=40,r=15,t=35,b=35),
    xaxis=dict(gridcolor=C['brd'],showline=False,color=C['mut'],zeroline=False),
    yaxis=dict(gridcolor=C['brd'],showline=False,color=C['mut'],zeroline=False),
    showlegend=False)

# ============================================================
# MODELL: CatBoost V10 — validált: MAE 108.48 MWh | MAPE 2.50%
# (MAVIR hivatalos napelőtti előrejelzés u.azon teszten: 244.45)
#
# A modell ÓRÁRA jósol, nem napra. Egy sor = egy óra, 44 oszloppal.
# A tanítás minden célórához a T-24h MÉRT fogyasztást adta oda
# (`shift(24)` a történelmi táblán). Ezért a jóslat célablaka
# NEM naptári nap, hanem: [utolsó mért óra + 1h, +24h].
# Így minden lag valódi mért érték, pontosan mint tanításkor.
# ============================================================
bundle = None
MODELL_HIBA = None
try:
    bundle = joblib.load(f"{BASE}/catboost_model_final.pkl")
    MODEL = bundle["model"]
    FEATURES = bundle["features"]
    CAT_FEATS = bundle.get("cat_features") or []
except Exception as e:
    MODELL_HIBA = str(e)

def model_predict(X_df):
    """Kategorikus konverzió a bundle előírása szerint (int64→string),
    majd CatBoost jóslás + 2000 MWh alsó levágás."""
    X = X_df[FEATURES].copy()
    for c in CAT_FEATS:
        X[c] = X[c].astype('int64').astype(str)
    return np.maximum(MODEL.predict(X), 2000.0)

# ============================================================
# GYORSÍTÓTÁR
#
# `gen` = generációs bélyeg. Ha megváltozik (pl. új nap kezdődik,
# vagy lezajlott a 14:00-s aukció), a polcon lévő adat elavultnak
# számít akkor is, ha a TTL még nem járt le. Ez öli meg az éjféli
# beragadást. A hibatűrés viszont megmarad: ha a friss hívás
# elbukik, a régi — valódi — adatot adjuk vissza, nem találunk ki
# semmit.
# ============================================================
CACHE = {}

# A LESZÁRMAZTATOTT csomag gyorsítótára. A `CACHE` a nyers forrásokat őrzi,
# ez pedig a belőlük kiszámolt végeredményt: jóslat, STL, validáció. Amíg a
# bemenet ujjlenyomata változatlan, egyetlen látogató sem fizeti ki újra a
# CatBoost-futást, a robusztus STL-bontást és a négy adatbázis-kört.
SZAMITAS_CACHE = {"ujjlenyomat": None, "ido": 0.0, "csomag": None}
SZAMITAS_TTL = 900          # 15 perc: ennyinél frissebb marad a validációs sáv
SZAMITAS_LOCK = threading.Lock()

def cachelt(kulcs, ttl_sec, fn, ok_index, gen=None, force=False, ujra=2):
    most = time.time()
    rec = CACHE.get(kulcs)
    friss = (rec is not None and not force
             and (most - rec["ido"]) < ttl_sec
             and (gen is None or rec.get("gen") == gen))
    if friss:
        return rec["ertek"]

    # Újrapróbálkozás CSAK akkor, ha nincs korábbi jó adat: különben a
    # látogató fölöslegesen várna, holott van mit megjeleníteni.
    max_kiserlet = ujra if rec is None else 1
    ertek = None
    utolso_hiba = None
    for kiserlet in range(1, max_kiserlet + 1):
        try:
            ertek = fn()
        except Exception as e:
            print(f"[HIBA] {kulcs} lekérés ({kiserlet}.): {e}", flush=True)
            ertek, utolso_hiba = None, e
        if ertek is not None and ertek[ok_index]:
            CACHE[kulcs] = {"ido": most, "gen": gen, "ertek": ertek}
            return ertek
        if kiserlet < max_kiserlet:
            time.sleep(1.0)

    if rec:
        print(f"[CACHE] {kulcs}: friss hívás sikertelen, korábbi jó adat "
              f"({int((most-rec['ido'])/60)} perce)", flush=True)
        return rec["ertek"]
    if ertek is None and utolso_hiba is not None:
        raise utolso_hiba
    return ertek

def _helyi_most():
    """Budapesti falióra, a szerver saját időzónájától függetlenül."""
    return datetime.now(BUDAPEST_TZ).replace(tzinfo=None)

def _ma():
    return _helyi_most().replace(hour=0,minute=0,second=0,microsecond=0)

def _nap_elotag(dt):
    """'', 'Holnap ' vagy '07.11. ' — a hero-kártya és a listák elé."""
    d = (dt.date() - _helyi_most().date()).days
    if d == 0: return ""
    if d == 1: return "Holnap "
    return f"{dt:%m.%d}. "

def _entsoe():
    from entsoe import EntsoePandasClient
    return EntsoePandasClient(api_key=ENTSOE_API_KEY, timeout=30)

def _helyi(sorozat):
    s = sorozat.copy()
    s.index = s.index.tz_convert('Europe/Budapest').tz_localize(None)
    return s

def _orasra(sorozat):
    """Helyi idejű sorozat → {pd.Timestamp: float} órás szótár.
    A régi `_naponkent_oras` napokra bontott — az kötötte naptári
    naphoz a modellt. Időbélyeg-kulcs mellett a célablak szabadon
    átléphet éjfélt."""
    s = sorozat.resample('h').mean().dropna()
    return {t: float(v) for t, v in s.items()}

# ============================================================
# ÉLŐ ADATFORRÁSOK
# ============================================================
def get_eur_huf():
    try:
        r = requests.get("https://data-api.ecb.europa.eu/service/data/EXR/D.HUF.EUR.SP00.A",
            params={"startPeriod":(_helyi_most()-timedelta(days=7)).strftime("%Y-%m-%d"),
                    "endPeriod":_helyi_most().strftime("%Y-%m-%d"),"format":"csvdata"},timeout=10)
        df = pd.read_csv(StringIO(r.text))[["TIME_PERIOD","OBS_VALUE"]].dropna()
        df["OBS_VALUE"] = pd.to_numeric(df["OBS_VALUE"],errors="coerce")
        return float(df["OBS_VALUE"].dropna().iloc[-1]),True
    except Exception as e:
        print(f"[HIBA] ECB árfolyam: {e}", flush=True)
        return None,False

def get_ho():
    for kiserlet in range(2):
        try:
            r = requests.get("https://api.open-meteo.com/v1/forecast",
                params={"latitude":47.5,"longitude":19.0,"current_weather":"true","timezone":"Europe/Budapest"},timeout=10)
            d = r.json()
            if "current_weather" not in d:
                print(f"[HIBA] Open-Meteo (hőmérséklet) {kiserlet+1}. — HTTP {r.status_code}: {str(d)[:150]}", flush=True)
                time.sleep(1.0); continue
            return float(d["current_weather"]["temperature"]),True
        except Exception as e:
            print(f"[HIBA] Open-Meteo (hőmérséklet) {kiserlet+1}.: {e}", flush=True)
            time.sleep(1.0)
    return None,False

def get_ho_vc():
    if not VISUAL_CROSSING_KEY: return None,False
    try:
        r = requests.get("https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/47.5,19.0/today",
            params={"key":VISUAL_CROSSING_KEY,"unitGroup":"metric","include":"current","elements":"temp"},timeout=15)
        d = r.json() if r.status_code==200 else {}
        if "currentConditions" in d and d["currentConditions"].get("temp") is not None:
            return float(d["currentConditions"]["temp"]),True
        print(f"[HIBA] Visual Crossing (hőmérséklet): HTTP {r.status_code}", flush=True)
        return None,False
    except Exception as e:
        print(f"[HIBA] Visual Crossing (hőmérséklet): {e}", flush=True)
        return None,False

def get_ho_barmelyik():
    t,ok = get_ho()
    if ok: return t,ok
    return get_ho_vc()

def get_idojaras():
    """Órás időjárás TEGNAPELŐTTŐL +3 napig.

    Tegnapelőtt azért kell, mert a gördülő ablak legkorábbi órája
    közvetlenül éjfél után van, és ahhoz a delta24-hez a 24 órával
    korábbi óra is szükséges."""
    ma = _ma()
    for kiserlet in range(2):
        try:
            r = requests.get("https://api.open-meteo.com/v1/forecast",
                params={"latitude":47.5,"longitude":19.0,
                    "hourly":"temperature_2m,relative_humidity_2m,direct_radiation,wind_speed_10m,precipitation",
                    "daily":"temperature_2m_max,temperature_2m_min,weathercode",
                    "timezone":"Europe/Budapest",
                    "start_date":(ma-timedelta(days=2)).strftime("%Y-%m-%d"),
                    "end_date":(ma+timedelta(days=3)).strftime("%Y-%m-%d")},timeout=15)
            d = r.json()
            if "hourly" not in d:
                print(f"[HIBA] Open-Meteo (előrejelzés) {kiserlet+1}. — HTTP {r.status_code}: {str(d)[:150]}", flush=True)
                time.sleep(1.0); continue
            hourly = pd.DataFrame({"Datum":pd.to_datetime(d["hourly"]["time"]),
                "Homerseklet_C":d["hourly"]["temperature_2m"],
                "Paratartalom_szazalek":d["hourly"]["relative_humidity_2m"],
                "Napsugarzas_W_m2":d["hourly"]["direct_radiation"],
                "Szelsebesseg_kmh":d["hourly"]["wind_speed_10m"],
                "Csapadek_mm":d["hourly"]["precipitation"]}).dropna(subset=["Homerseklet_C"])
            daily = {"max":d["daily"]["temperature_2m_max"][2:6],
                     "min":d["daily"]["temperature_2m_min"][2:6],
                     "code":d["daily"]["weathercode"][2:6]}
            if len(hourly) < 72:
                time.sleep(1.0); continue
            return hourly,daily,True
        except Exception as e:
            print(f"[HIBA] Open-Meteo (előrejelzés) {kiserlet+1}.: {e}", flush=True)
            time.sleep(1.0)
    return None,None,False

def get_idojaras_vc():
    if not VISUAL_CROSSING_KEY: return None,None,False
    try:
        ma = _ma()
        kezd = (ma-timedelta(days=2)).strftime("%Y-%m-%d")
        veg = (ma+timedelta(days=3)).strftime("%Y-%m-%d")
        r = requests.get(
            f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/47.5,19.0/{kezd}/{veg}",
            params={"key":VISUAL_CROSSING_KEY,"unitGroup":"metric","include":"hours,days",
                    "elements":"datetime,temp,humidity,solarradiation,windspeed,precip,tempmax,tempmin"},timeout=20)
        d = r.json() if r.status_code==200 else {}
        if "days" not in d or len(d["days"])<4:
            print(f"[HIBA] Visual Crossing (előrejelzés): HTTP {r.status_code}", flush=True)
            return None,None,False
        sorok = []
        for nap in d["days"]:
            for h in nap.get("hours",[]):
                sorok.append({"Datum":pd.to_datetime(f"{nap['datetime']} {h['datetime']}"),
                    "Homerseklet_C":h.get("temp"),
                    "Paratartalom_szazalek":h.get("humidity") or 0,
                    "Napsugarzas_W_m2":h.get("solarradiation") or 0,
                    "Szelsebesseg_kmh":h.get("windspeed") or 0,
                    "Csapadek_mm":h.get("precip") or 0})
        hourly = pd.DataFrame(sorok).dropna(subset=["Homerseklet_C"])
        daily = {"max":[n.get("tempmax") for n in d["days"][2:6]],
                 "min":[n.get("tempmin") for n in d["days"][2:6]],
                 "code":[None]*len(d["days"][2:6])}
        return hourly,daily,True
    except Exception as e:
        print(f"[HIBA] Visual Crossing (előrejelzés): {e}", flush=True)
        return None,None,False

def get_idojaras_barmelyik():
    h,dl,ok = get_idojaras()
    if ok: return h,dl,ok,"Open-Meteo"
    h,dl,ok = get_idojaras_vc()
    if ok:
        print("[INFO] Időjárás: Visual Crossing tartalék aktív", flush=True)
        return h,dl,ok,"Visual Crossing"
    return None,None,False,None

def get_dam():
    """DAM árak tegnapelőttől a legutolsó publikált óráig.

    Két alakban jönnek vissza:
      `oras`   — {iso időbélyeg: €/MWh} órás. Ebből épül a modell
                 DAM_EUR_MWh és DAM_delta24 oszlopa.
      `negyed` — a mai naptól előre a natív negyedórás görbe.
                 Ez táplálja a töltési kártyát. 14:05 után holnap
                 23:00-ig ér, tehát a kártya éjfélkor nem hal meg.
    """
    if not ENTSOE_API_KEY:
        print("[HIBA] ENTSO-E: nincs API kulcs", flush=True)
        return None,False
    try:
        c = _entsoe(); ma = _ma()
        s = pd.Timestamp((ma-timedelta(days=2)).strftime("%Y-%m-%d"),tz="Europe/Budapest")
        e = pd.Timestamp((ma+timedelta(days=2)).strftime("%Y-%m-%d"),tz="Europe/Budapest")
        arak = c.query_day_ahead_prices("HU",start=s,end=e)
        if isinstance(arak,pd.DataFrame): arak = arak.iloc[:,0]
        arak = _helyi(arak)
        if len(arak) == 0:
            print("[HIBA] ENTSO-E (DAM): üres válasz", flush=True)
            return None,False

        oras = _orasra(arak)
        ma_ts = pd.Timestamp(ma)
        if not any(t.date() == ma.date() for t in oras):
            print("[HIBA] ENTSO-E (DAM): a mai árak nem érhetők el", flush=True)
            return None,False

        elore = arak[arak.index >= ma_ts]
        utolso = max(oras)

        # A mai nap 24 órás ára a KPI-kártya trendvonalához
        ma_oras = [oras.get(ma_ts + pd.Timedelta(hours=h)) for h in range(24)]
        ma_oras = [v for v in ma_oras if v is not None]

        return {"oras":{t.isoformat(): v for t,v in oras.items()},
                "negyed":{"ido":[t.isoformat() for t in elore.index],
                          "ar":[float(x) for x in elore.values]},
                "ma_oras": ma_oras,
                "utolso_ar_ora": utolso.isoformat(),
                "holnapi_ar": utolso.date() > ma.date()},True
    except Exception as e:
        print(f"[HIBA] ENTSO-E (DAM árak): {e}", flush=True)
        return None,False

def get_load():
    """Elmúlt 17 nap mért fogyasztása (lag336h-hoz) + legutolsó mért érték."""
    if not ENTSOE_API_KEY: return None,False
    try:
        c = _entsoe(); ma = _ma()
        s = pd.Timestamp((ma-timedelta(days=17)).strftime("%Y-%m-%d"),tz="Europe/Budapest")
        e = pd.Timestamp(_helyi_most(),tz="Europe/Budapest") + pd.Timedelta(hours=1)
        load = c.query_load("HU",start=s,end=e)
        if isinstance(load,pd.DataFrame): load = load.iloc[:,0]
        load = load.resample('h').mean().dropna()
        load = _helyi(load)
        if len(load) < 15*24:
            print(f"[HIBA] ENTSO-E (fogyasztás): kevés adat ({len(load)} óra)", flush=True)
            return None,False
        return load,True
    except Exception as e:
        print(f"[HIBA] ENTSO-E (fogyasztás): {e}", flush=True)
        return None,False

def get_load_forecast():
    """ENTSO-E/MAVIR official load forecast, normalized to local hourly timestamps."""
    if not ENTSOE_API_KEY: return None,False
    try:
        c = _entsoe(); ma = _ma()
        s = pd.Timestamp((ma-timedelta(days=1)).strftime("%Y-%m-%d"),tz="Europe/Budapest")
        e = pd.Timestamp((ma+timedelta(days=3)).strftime("%Y-%m-%d"),tz="Europe/Budapest")
        forecast = c.query_load_forecast("HU",start=s,end=e)
        if isinstance(forecast,pd.DataFrame): forecast = forecast.iloc[:,0]
        forecast = forecast.resample('h').mean().dropna()
        forecast = _helyi(forecast)
        values = _orasra(forecast)
        if not values:
            print("[HIBA] ENTSO-E (fogyasztasi elorejelzes): ures valasz", flush=True)
            return None,False
        return {t.isoformat():v for t,v in values.items()},True
    except Exception as e:
        print(f"[HIBA] ENTSO-E (fogyasztasi elorejelzes): {e}", flush=True)
        return None,False

def get_naposzel_fc():
    """Hivatalos napelőtti nap/szél termelés-előrejelzés, időbélyeg szerint."""
    if not ENTSOE_API_KEY: return None,False
    try:
        c = _entsoe(); ma = _ma()
        s = pd.Timestamp((ma-timedelta(days=2)).strftime("%Y-%m-%d"),tz="Europe/Budapest")
        e = pd.Timestamp((ma+timedelta(days=2)).strftime("%Y-%m-%d"),tz="Europe/Budapest")
        g = c.query_wind_and_solar_forecast("HU",start=s,end=e)
        g.index = g.index.tz_convert('Europe/Budapest').tz_localize(None)
        napo = [x for x in g.columns if 'Solar' in str(x)]
        szelo = [x for x in g.columns if 'Wind' in str(x)]
        nap_s = g[napo].sum(axis=1) if napo else pd.Series(0.0, index=g.index)
        szel_s = g[szelo].sum(axis=1) if szelo else pd.Series(0.0, index=g.index)
        nap_d = _orasra(nap_s); szel_d = _orasra(szel_s)
        if not nap_d:
            print("[HIBA] ENTSO-E (nap/szél): üres válasz", flush=True)
            return None,False
        return {"nap":{t.isoformat():v for t,v in nap_d.items()},
                "szel":{t.isoformat():v for t,v in szel_d.items()}},True
    except Exception as e:
        print(f"[HIBA] ENTSO-E (nap/szél előrejelzés): {e}", flush=True)
        return None,False

def get_termeles():
    """A MÉRT nap- és széltermelés a mai naptól mostanáig.

    Ez az egyetlen hely, ahol tényadatot kérünk a termelésről. A modell
    ezt NEM használja — a 2. oldal panelje viszont mellé teszi a jóslatnak,
    hogy látszódjon, mennyit téved a napelőtti előrejelzés.

    Nem kritikus forrás: ha elbukik, a panel a jóslat-görbét mutatja.
    """
    if not ENTSOE_API_KEY: return None,False
    try:
        c = _entsoe(); ma = _ma()
        s = pd.Timestamp(ma.strftime("%Y-%m-%d"),tz="Europe/Budapest")
        e = pd.Timestamp(_helyi_most(),tz="Europe/Budapest") + pd.Timedelta(hours=1)
        g = c.query_generation("HU",start=s,end=e)
        # a 'Consumption' oszlopok a szivattyús tározó fogyasztását jelentik
        napo = [x for x in g.columns if 'Solar' in str(x) and 'Consumption' not in str(x)]
        szelo = [x for x in g.columns if 'Wind' in str(x) and 'Consumption' not in str(x)]
        g.index = g.index.tz_convert('Europe/Budapest').tz_localize(None)
        nap_s = g[napo].sum(axis=1) if napo else pd.Series(0.0, index=g.index)
        szel_s = g[szelo].sum(axis=1) if szelo else pd.Series(0.0, index=g.index)
        nap_d = _orasra(nap_s); szel_d = _orasra(szel_s)
        if not nap_d:
            print("[HIBA] ENTSO-E (mért termelés): üres válasz", flush=True)
            return None,False
        return {"nap":{t.isoformat():v for t,v in nap_d.items()},
                "szel":{t.isoformat():v for t,v in szel_d.items()}},True
    except Exception as e:
        print(f"[HIBA] ENTSO-E (mért termelés): {e}", flush=True)
        return None,False

# ============================================================
# CÉLABLAK — a modell feladatának pontos kijelölése
#
# A tanítás minden célórához a T-24h MÉRT értéket adta. Ezért:
#   kezdet = utolsó mért óra + 1 óra
#   vég    = kezdet + 23 óra  (feljebb nem lehet: a lag24h elfogyna)
# Az ablakot ezen felül vágja a publikált ár is. Ezért:
#   09:00-kor  09:00 → ma 23:00      (15 óra)
#   14:05-kor  14:00 → holnap 13:00  (24 óra)
# A horizont hosszát a piaci információs határ szabja meg.
# ============================================================
LAGOK = [24,48,72,96,120,144,168,336]

def celablak(load, dam_oras, ido_map, fc_nap, fc_szel):
    utolso_mert = load.index.max()
    load_d = load.to_dict()
    kezd = utolso_mert + pd.Timedelta(hours=1)
    orak = []
    for i in range(24):
        dt = kezd + pd.Timedelta(hours=i)
        elozo = dt - pd.Timedelta(hours=24)
        if dt not in dam_oras or elozo not in dam_oras: break
        if dt not in ido_map or elozo not in ido_map: break
        if dt not in fc_nap or elozo not in fc_nap: break
        if dt not in fc_szel or elozo not in fc_szel: break
        if any((dt - pd.Timedelta(hours=k)) not in load_d for k in LAGOK): break
        if any((dt - pd.Timedelta(hours=24*k)) not in load_d for k in range(1,8)): break
        orak.append(dt)
    return orak

# ============================================================
# ELŐREJELZÉS — direkt, óránként, láncolás nélkül (V10)
# Minden lag valódi MÉRT érték. Nincs visszalépő pótlás.
# ============================================================
def elorejelez(orak, dam_oras, ido_map, load, fc_nap, fc_szel, eur_huf):
    load_d = load.to_dict()
    H = lambda n: pd.Timedelta(hours=n)

    sorok = []
    for dt in orak:
        w = ido_map[dt]
        w_prev = ido_map[dt - H(24)]
        temp = float(w["Homerseklet_C"])
        h = int(dt.hour)

        l24 = float(load_d[dt - H(24)])
        l48 = float(load_d[dt - H(48)])
        l168 = float(load_d[dt - H(168)])
        # a tanítás `shift(1).rolling(7)` mintája: az előző 7 azonos óra
        sh = [float(load_d[dt - H(24*k)]) for k in range(1,8)]

        sorok.append({
            "DAM_EUR_MWh": dam_oras[dt],
            "Homerseklet_C": temp,
            "Paratartalom_szazalek": float(w["Paratartalom_szazalek"]),
            "Napsugarzas_W_m2": float(w["Napsugarzas_W_m2"]),
            "Szelsebesseg_kmh": float(w["Szelsebesseg_kmh"]),
            "Csapadek_mm": float(w["Csapadek_mm"]),
            "EUR_HUF": eur_huf,
            "Ora": h, "Het_napja": dt.weekday()+1, "Honap": dt.month,
            "Unnepnap": 1 if dt.date() in hu_holidays else 0,
            "Hetvege": 1 if dt.weekday()>=5 else 0,
            "Extrem_hideg": 1 if temp < -5 else 0,
            "Extrem_meleg": 1 if temp > 30 else 0,
            "Fogyasztas_lag24h": l24,
            "Fogyasztas_lag48h": l48,
            "Fogyasztas_lag72h": float(load_d[dt - H(72)]),
            "Fogyasztas_lag96h": float(load_d[dt - H(96)]),
            "Fogyasztas_lag120h": float(load_d[dt - H(120)]),
            "Fogyasztas_lag144h": float(load_d[dt - H(144)]),
            "Fogyasztas_lag168h": l168,
            "Fogyasztas_lag336h": float(load_d[dt - H(336)]),
            "Fogyasztas_same_hour_mean7": float(np.mean(sh)),
            "Fogyasztas_same_hour_median7": float(np.median(sh)),
            "Fogyasztas_same_hour_min7": float(np.min(sh)),
            "Fogyasztas_same_hour_max7": float(np.max(sh)),
            "Fogyasztas_trend_24_168": l24 - l168,
            "Fogyasztas_trend_24_48": l24 - l48,
            "Homerseklet_delta24": temp - float(w_prev["Homerseklet_C"]),
            "Napsugarzas_delta24": float(w["Napsugarzas_W_m2"]) - float(w_prev["Napsugarzas_W_m2"]),
            "DAM_delta24": dam_oras[dt] - dam_oras[dt - H(24)],
            "Nap_fc_MW": fc_nap[dt], "Szel_fc_MW": fc_szel[dt],
            "Nap_fc_delta24": fc_nap[dt] - fc_nap[dt - H(24)],
            "Szel_fc_delta24": fc_szel[dt] - fc_szel[dt - H(24)],
            "Ora_sin": float(np.sin(2*np.pi*h/24)), "Ora_cos": float(np.cos(2*np.pi*h/24)),
            "Het_sin": float(np.sin(2*np.pi*(dt.weekday()+1)/7)),
            "Het_cos": float(np.cos(2*np.pi*(dt.weekday()+1)/7)),
            "Ev_sin": float(np.sin(2*np.pi*dt.dayofyear/365.25)),
            "Ev_cos": float(np.cos(2*np.pi*dt.dayofyear/365.25)),
            "Cooling_degree": max(temp-21, 0.0), "Heating_degree": max(16-temp, 0.0),
            "Cooling_x_hour": max(temp-21, 0.0)*h,
        })

    X = pd.DataFrame(sorok)
    josolt = model_predict(X)

    ki = []
    for i, dt in enumerate(orak):
        w = ido_map[dt]
        h = int(dt.hour)
        csap = float(w["Csapadek_mm"] or 0); sug = float(w["Napsugarzas_W_m2"] or 0)
        if csap > 0.3: ikon = "☂"
        elif h < 6 or h >= 21: ikon = "☾"
        elif sug > 120: ikon = "☀"
        else: ikon = "☁"
        ki.append({"datum": dt.isoformat(), "ora": h,
                   "homerseklet": float(X["Homerseklet_C"].iloc[i]),
                   "fogyasztas": float(josolt[i]),
                   "dam_ar": float(dam_oras[dt]),
                   "nap_fc": float(fc_nap[dt]), "szel_fc": float(fc_szel[dt]),
                   "ikon": ikon,
                   "koltseg_mft": float(josolt[i])*dam_oras[dt]*eur_huf/1_000_000})
    return ki

# ============================================================
# "MIKOR TÖLTS?" — ajánlási logika a negyedórás árakból
#
# A modell ehhez nem kell: ez tisztán ár-alapú. Az ablak mostantól
# a legutolsó publikált negyedóráig tart — 14:05 után holnap
# 23:45-ig. Ezért éjfélkor nem ürül ki.
# ============================================================
TOLTES_PERC = 60   # a keresett összefüggő töltési blokk hossza

def toltes_ajanlas(negyed):
    most = _helyi_most()
    idok = [datetime.fromisoformat(t) for t in negyed["ido"]]
    arak = [float(a) for a in negyed["ar"]]
    if len(idok) < 2:
        return None
    lepes = max(1, int(round((idok[1]-idok[0]).total_seconds()/60)))
    ablak = max(1, TOLTES_PERC // lepes)

    jovo = [(t,a) for t,a in zip(idok,arak) if t + timedelta(minutes=lepes) > most]
    if not jovo:
        return None
    ablak = min(ablak, len(jovo))
    t_l = [x[0] for x in jovo]; a_l = [x[1] for x in jovo]

    atlagok = [(i, float(np.mean(a_l[i:i+ablak]))) for i in range(len(a_l)-ablak+1)]
    fo_i, fo_ar = min(atlagok, key=lambda x: x[1])
    fo_kezd = t_l[fo_i]
    fo_veg = fo_kezd + timedelta(minutes=lepes*ablak)   # a tényleges ablakhossz
    fo_min = float(np.min(a_l[fo_i:fo_i+ablak]))

    # összefüggő negatív blokk a fő ablak körül
    negativ = any(a < 0 for a in a_l[fo_i:fo_i+ablak])
    if negativ:
        i0 = fo_i
        while i0 > 0 and a_l[i0-1] < 0: i0 -= 1
        i1 = fo_i + ablak - 1
        while i1 < len(a_l)-1 and a_l[i1+1] < 0: i1 += 1
        aj_kezd = t_l[i0]
        aj_veg = t_l[i1] + timedelta(minutes=lepes)
        # A kiírt ár mindig a KIÍRT IDŐSZAK átlaga. Korábban itt a fo_min
        # állt: az egyórás ablak minimuma, miközben az időszak a teljes
        # negatív blokk volt — két különböző halmaz statisztikája egy sorban.
        aj_ar = float(np.mean(a_l[i0:i1+1]))
    else:
        aj_kezd, aj_veg, aj_ar = fo_kezd, fo_veg, fo_ar

    # alternatívák: egész órás kezdet, az ajánlott blokkon kívül,
    # egymástól és a blokktól legalább 2 órára
    altok = []
    blokk = timedelta(minutes=lepes*ablak)
    for i, atl in sorted(atlagok, key=lambda x: x[1]):
        t = t_l[i]
        if t.minute != 0: continue
        if not (t >= aj_veg or t + blokk <= aj_kezd): continue
        if abs((t - aj_kezd).total_seconds()) < 2*3600: continue
        if any(abs((t - m).total_seconds()) < 2*3600 for m,_ in altok): continue
        altok.append((t, atl))
        if len(altok) == 2: break

    # az AKTUÁLIS negyedóra ára (nem a legközelebbi rácspont!)
    akt_ar = a_l[0]
    optimalis_most = fo_kezd <= most < fo_veg

    # Tolerancia-sáv: felesleges váratni, ha az ár már most is jó.
    # IGEN, ha: negatív ár, VAGY az optimum +10 €-n belül,
    # VAGY a hátralévő órák átlagárának 30%-a alatt van.
    hatralevo_atl = float(np.mean(a_l))
    kuszob = max(fo_ar + 10.0, hatralevo_atl * 0.30)
    most_jo = optimalis_most or akt_ar < 0 or akt_ar <= kuszob

    # Meddig tart a kedvező állapot? Az összefüggő szelvények vége,
    # amíg a feltétel fennáll.
    akt_veg = None
    if most_jo:
        felt = (lambda a: a < 0) if akt_ar < 0 else (lambda a: a <= kuszob)
        j = 0
        while j < len(a_l)-1 and felt(a_l[j+1]): j += 1
        akt_veg = t_l[j] + timedelta(minutes=lepes)
        if optimalis_most:
            akt_veg = max(akt_veg, fo_veg)

    return {"aj_kezd":aj_kezd.isoformat(),"aj_veg":aj_veg.isoformat(),"aj_ar":aj_ar,
            "fo_kezd":fo_kezd.isoformat(),"fo_veg":fo_veg.isoformat(),
            "fo_ar":fo_ar,"fo_min":fo_min,
            "altok":[(t.isoformat(),a) for t,a in altok],
            "negativ":negativ,
            "most_jo":most_jo,
            "akt_veg":akt_veg.isoformat() if akt_veg else None,
            "akt_ar":float(akt_ar),
            "lepes":lepes,
            "grafikon":{"ido":[t.isoformat() for t in idok],"ar":arak}}

def dam_szin(ar, atlag):
    if ar < 0: return C['gr']
    elif ar < atlag * 0.7: return '#a3e635'
    elif ar > atlag * 1.3: return C['rd']
    return C['yw']

def ar_szin(ar):
    """A hero-grafikon oszlopszínei. A jelmagyarázat ugyanezt használja."""
    if ar < -10: return "#00bfae"
    if ar < 0:   return "#00e0c2"
    if ar < 50:  return "#c9df16"
    if ar < 100: return "#ff9800"
    return "#ff3b30"

AR_SAVOK = [("< –10 €","#00bfae"),("–10 – 0 €","#00e0c2"),("0 – 50 €","#c9df16"),
            ("50 – 100 €","#ff9800"),("> 100 €","#ff3b30")]

CS = {"background":C['card'],"border":f"1px solid {C['brd']}","borderRadius":"14px","padding":"18px","height":"100%"}

# ============================================================
# KPI KÁRTYÁK
# ============================================================
KPI_GRID_STYLE = {
    "display": "grid",
    "gridTemplateColumns": "repeat(6, minmax(190px, 1fr))",
    "gap": "10px",
    "width": "100%",
    "overflowX": "auto",
    "padding": "0 0 2px 0",
    "margin": "0 0 18px 0",
}

def _rgba(hex_color, alpha):
    try:
        h = str(hex_color).replace("#", "").strip()
        if len(h) != 6:
            raise ValueError("not a hex color")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"rgba({r},{g},{b},{alpha})"
    except Exception:
        return f"rgba(255,255,255,{alpha})"

def _mini_sparkline(trend, szin, jelolt_i=None):
    """A fehér karika ARRA a pontra kerül, amelyiket a kártya nagy
    száma mutat. Vonal: lineáris, nem spline — a spline a 36px-es
    sávban ellapította a csúcsot, ezért tűnt minden nap egyformának."""
    if trend is None:
        return None
    try:
        y = [float(v) for v in trend if v is not None and not pd.isna(v)]
    except Exception:
        y = []
    if len(y) < 2:
        return None
    if jelolt_i is not None and not (0 <= jelolt_i < len(y)):
        jelolt_i = None
    ymin, ymax = min(y), max(y)
    pad = max((ymax - ymin) * 0.08, 0.5)
    fig = go.Figure()
    fig.add_trace(go.Scatter(y=y, mode="lines",
        line=dict(color=szin, width=2.1),
        fill="tozeroy", fillcolor=_rgba(szin, 0.055),
        hoverinfo="skip", showlegend=False))
    if jelolt_i is not None:
        fig.add_trace(go.Scatter(x=[jelolt_i], y=[y[jelolt_i]], mode="markers",
            marker=dict(size=7, color=szin, line=dict(width=1.1, color="rgba(255,255,255,.78)")),
            hoverinfo="skip", showlegend=False))
    fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=0,r=0,t=0,b=0), height=36,
        xaxis=dict(visible=False, fixedrange=True),
        yaxis=dict(visible=False, fixedrange=True, range=[ymin-pad, ymax+pad]),
        showlegend=False)
    return dcc.Graph(figure=fig, config={"displayModeBar": False, "staticPlot": True},
        style={"position":"absolute","left":"13px","right":"13px","bottom":"10px",
               "height":"36px","zIndex":"2","pointerEvents":"none"})

# A 3. kártya (Budapest-sziluett) és a 4. (EUR-hullám) képét a layout.css
# ::after rétege rajzolja, `background-size: 98% auto` / `100% auto` mérettel.
# Innen NEM rajzoljuk ki még egyszer: két példány kerülne egymásra, eltérő
# méretben. A CSS-változat mutatja a teljes sziluettet, a `cover` levágná.

def _segment_bar(szin):
    elemek = []
    for i in range(9):
        aktiv = 2 <= i <= 5
        elemek.append(html.Div(style={
            "height":"12px","flex":"1","borderRadius":"4px",
            "background": f"linear-gradient(180deg, {_rgba(szin,.92)}, {_rgba(szin,.52)})" if aktiv else "rgba(30,45,64,.72)",
            "boxShadow": f"0 0 10px {_rgba(szin,.24)}" if aktiv else "none",
            "border":"1px solid rgba(255,255,255,.04)"}))
    return html.Div(elemek, style={"position":"absolute","left":"18px","right":"18px",
        "bottom":"18px","display":"flex","gap":"4px","zIndex":"2"})

def _quality_bars(szin, val):
    try:
        aktiv_db = int("".join(ch for ch in str(val).split("/")[0] if ch.isdigit()) or 0)
    except Exception:
        aktiv_db = 0
    aktiv = min(18, max(1, aktiv_db * 3)) if aktiv_db > 0 else 0
    elemek = []
    for i in range(18):
        on = i < aktiv
        elemek.append(html.Div(style={
            "width":"7px","height":"20px","borderRadius":"8px",
            "background": f"linear-gradient(180deg, {_rgba(szin,.96)}, {_rgba(szin,.48)})" if on else "rgba(30,45,64,.70)",
            "boxShadow": f"0 0 9px {_rgba(szin,.22)}" if on else "none",
            "opacity":"1" if on else ".62"}))
    return html.Div(elemek, style={"position":"absolute","left":"18px","right":"14px",
        "bottom":"17px","display":"flex","gap":"5px","alignItems":"flex-end",
        "zIndex":"2","overflow":"hidden"})

def kep(fajlnev, alt, className=None, style=None):
    """Kép WebP-ben, PNG tartalékkal.

    Az oldal képei együtt 2,2 MB-ot tettek ki, ebből egyedül a nyitókép
    1,4 MB-ot — mobilneten ez volt a betöltés legnagyobb egyetlen tétele.
    Ugyanezek WebP-ben 151 KB-ot foglalnak. A `<picture>` elem miatt a modern
    böngésző CSAK a WebP-t tölti le; ha valamelyik régi böngésző nem ismeri,
    az eredeti PNG-t kapja, tehát senkinél nem marad üres a hely."""
    # FIGYELEM: a `<picture>` NEM esik vissza az `<img>`-re, ha a `<source>`
    # típusa támogatott, de a fájl 404. Ezért a WebP-változat csak akkor
    # kapcsolható be, ha mind a 8 .webp tényleg ott van az assets mappában.
    # Amíg nincs, marad az eredeti PNG — az mindig működik.
    return html.Img(src=dash.get_asset_url(fajlnev), alt=alt,
                    className=className, style=style)


def kpi(cim, val, sub, szin, trend=None, jelolt_i=None):
    cim_lower = str(cim).lower()
    value = str(val)
    unit = ""
    for u in ["€/MWh", "MWh", "°C", "Ft"]:
        if value.endswith(u):
            value = value.replace(u, "").strip()
            unit = u
            break

    # Kártyánként EGY tulajdonos rajzol:
    #   1. DAM ár, 2. Előrejelzett fogyasztás -> innen, valódi Plotly-görbe
    #   3. Budapest, 4. EUR/HUF               -> layout.css ::after (kép)
    #   5. Legolcsóbb ablak, 6. Adatminőség   -> innen
    vizual = None
    if "dam" in cim_lower or "fogyasztás" in cim_lower:
        vizual = _mini_sparkline(trend, szin, jelolt_i)
    elif "legolcsóbb" in cim_lower:
        vizual = _segment_bar(szin)
    elif "adatminőség" in cim_lower:
        vizual = _quality_bars(szin, val)

    card_style = {
        "position":"relative","height":"132px","minHeight":"132px","overflow":"hidden",
        "borderRadius":"12px","padding":"17px 18px 10px 18px",
        "background": (f"radial-gradient(circle at 92% 14%, {_rgba(szin,.14)} 0%, rgba(5,13,26,0) 28%),"
                       f"linear-gradient(180deg, rgba(10,22,40,.98) 0%, rgba(7,15,30,.96) 100%)"),
        "border": f"1px solid {_rgba(szin,.24)}",
        "boxShadow": f"inset 0 0 22px {_rgba(szin,.045)}"}

    return html.Div([
        html.Div(style={"position":"absolute","top":"16px","right":"17px","width":"7px",
            "height":"7px","borderRadius":"999px","background":szin,
            "boxShadow": f"0 0 9px {_rgba(szin,.85)}","zIndex":"4"}),
        html.Div([
            html.Div(str(cim).upper(), className="kpi-label", style={
                "position":"relative","zIndex":"3","marginBottom":"9px","whiteSpace":"nowrap",
                "overflow":"hidden","textOverflow":"ellipsis","letterSpacing":"1.15px",
                "textTransform":"uppercase"}),
            html.Div([
                html.Span(value, className="kpi-value",
                    style={"display":"inline","position":"relative","zIndex":"3","lineHeight":"1"}),
                html.Span(f" {unit}" if unit else "", style={"fontSize":"15px","fontWeight":"700",
                    "color":C['wh'],"position":"relative","zIndex":"3","lineHeight":"1"})
            ], style={"position":"relative","zIndex":"3","whiteSpace":"nowrap","overflow":"hidden",
                      "textOverflow":"ellipsis","marginBottom":"8px"}),
            html.Div(sub, className="kpi-sub", style={"position":"relative","zIndex":"3",
                "color":szin,"whiteSpace":"nowrap","overflow":"hidden","textOverflow":"ellipsis"})
        ], style={"position":"relative","zIndex":"3","paddingRight":"18px"}),
        # `vizual if vizual else ...` NEM működik: a Dash-komponensek
        # igazságértéke a gyerekeik számából jön. A dcc.Graph-nak nincs
        # gyereke, tehát `bool(Graph) == False` — a régi kód emiatt
        # csendben eldobta a valódi sparkline-t, és üres divet tett a
        # helyére. Innen ered, hogy a KPI-kártyákon beégetett SVG-vonal
        # állt valódi adat helyett.
        vizual if vizual is not None else html.Div()
    ], className="kpi-card", style=card_style)

def src_sor(nev,ok):
    return html.Div([
        html.Span(nev,style={"fontSize":"10px","color":C['mut']}),
        html.Span("● Élő" if ok else "○ Nem elérhető",style={"fontSize":"10px","color":C['gr'] if ok else C['rd']})
    ],style={"display":"flex","justifyContent":"space-between","padding":"2px 0"})

def hiba_panel(hianyzo, modell_hiba=None):
    sorok = []
    if modell_hiba:
        sorok.append(html.Div([
            html.Div("⚠ A modell nem tölthető be",style={"fontSize":"14px","fontWeight":"600","color":C['rd']}),
            html.Div(f"Részletek: {modell_hiba}",style={"fontSize":"11px","color":C['mut'],"marginTop":"4px"}),
            html.Div("Ellenőrizd, hogy a catboost_model_final.pkl az app.py mellett van-e.",
                style={"fontSize":"11px","color":C['txt'],"marginTop":"4px"})
        ],style={"marginBottom":"16px"}))
    if hianyzo:
        sorok.append(html.Div([
            html.Div("⚠ Élő adatforrás nem elérhető",style={"fontSize":"14px","fontWeight":"600","color":C['rd']}),
            html.Div(f"Érintett: {', '.join(hianyzo)}",style={"fontSize":"12px","color":C['txt'],"marginTop":"6px"}),
            html.Div("Az alkalmazás kizárólag élő adatokkal működik. Ellenőrizd az API kulcsokat és a "
                     "hálózati kapcsolatot — az oldal 30 percen belül automatikusan újrapróbálkozik.",
                style={"fontSize":"11px","color":C['mut'],"marginTop":"6px"})
        ]))
    return html.Div(sorok,style={"background":C['card'],"border":f"1px solid {C['rd']}",
        "borderRadius":"14px","padding":"24px","maxWidth":"640px"})

def hianyzo_panel(cim, uzenet):
    return html.Div([
        html.Div(cim,style={"fontSize":"11px","fontWeight":"600","color":C['wh'],"marginBottom":"12px"}),
        html.Div([
            html.Div("⚠",style={"fontSize":"22px","color":C['yw'],"textAlign":"center","marginBottom":"8px"}),
            html.Div(uzenet,style={"fontSize":"11px","color":C['mut'],"textAlign":"center"})
        ],style={"display":"flex","flexDirection":"column","justifyContent":"center","flex":"1","padding":"20px 10px"})
    ],style={**CS,"display":"flex","flexDirection":"column"})

HEADER = html.Header([
    html.Div([
        html.Div([
            html.Div("OM",className="brand-mark"),
            html.Div([
                html.Div("OkosMérő",className="brand-name"),
                html.Div("ENERGIAPIACI IRÁNYÍTÓPULT",className="brand-subtitle")
            ])
        ],className="brand-lockup"),
        html.Div([
            html.Span("ÉLŐ ADAT",className="live-badge"),
            html.Div(id="statusz",className="header-status"),
            html.Button("Frissítés",id="manual-refresh",n_clicks=0,className="refresh-button")
        ],className="header-meta")
    ],className="app-header-inner")
],className="app-header")

NAV_TABS = html.Div([
    html.Div("Főoldal",id="nav-fooldal",n_clicks=0,
        style={"color":C['gr'],"borderBottom":f"2px solid {C['gr']}"},className="nav-tab"),
    html.Div("DAM-árak és töltés",id="nav-toltes",n_clicks=0,
        style={"color":C['mut'],"borderBottom":"2px solid transparent"},className="nav-tab"),
    html.Div("Energiaelemzés",id="nav-elemzes",n_clicks=0,
        style={"color":C['mut'],"borderBottom":"2px solid transparent"},className="nav-tab"),
    html.Div("Megújulók",id="nav-megujulok",n_clicks=0,
        style={"color":C['mut'],"borderBottom":"2px solid transparent"},className="nav-tab"),
    html.Div("ML Modell Labor",id="nav-mllabor",n_clicks=0,
        style={"color":C['mut'],"borderBottom":"2px solid transparent"},className="nav-tab")
],className="nav-tabs-row",
   style={"display":"grid","gridTemplateColumns":"repeat(5, minmax(0, 1fr))"})

app.layout = html.Div([
    HEADER,
    html.Main([
        html.Div(id="kpi-sor",className="kpi-strip"),
        NAV_TABS,
        html.Div(id="oldal-content"),
        html.Div([
            html.Div(id="src-panel"),
            html.Div(id="modell-panel")
        ],style={"display":"none"}),
        dcc.Interval(id="refresh",interval=1800*1000,n_intervals=0),

        dcc.Store(id="oldal",data="fooldal"),
        dcc.Store(id="adatok",data=None),
        dcc.Store(id="flux-uzenetek",data=None),
        dcc.Store(id="flux-valasz",data=None),
        dcc.Store(id="flux-noop",data=None),
        dcc.Store(id="flux-varakozas",data=None),
        dcc.Store(id="flux-kerdes-kontextus",data=None),
    ],className="app-main")
],className="app-shell")

@callback(Output("adatok","data"),
    [Input("refresh","n_intervals"),Input("manual-refresh","n_clicks")],
    running=[
        (Output("manual-refresh","disabled"),True,False),
        (Output("manual-refresh","children"),"Frissítés…","Frissítés"),
    ])
def fetch(n,_manual):
    manual = dash.ctx.triggered_id == "manual-refresh"

    if bundle is None:
        return {"kritikus_hiba":True,"hianyzo":[],"modell_hiba":MODELL_HIBA}

    most = _helyi_most(); ma = _ma()
    # Generációs bélyegek: éjfélkor és a 14:00-s aukciónál automatikusan
    # elavul a polc tartalma, TTL-től függetlenül.
    gen_nap = f"{ma:%Y-%m-%d}"
    gen_aukcio = f"{gen_nap}|{'pm' if most.hour >= 14 else 'am'}"
    gen_ora = f"{most:%Y-%m-%d-%H}"

    # ---- A nyolc külső forrás PÁRHUZAMOSAN ----
    # Korábban ez a nyolc hívás egymás után futott. Öt közülük az ENTSO-E,
    # amely önmagában 2-5 másodperc; így egy hideg betöltés 20-30 másodperc
    # volt, és a látogató addig az "Élő adatok betöltése..." feliratot nézte.
    # A hívások függetlenek egymástól, ezért egyszerre indíthatók: a teljes
    # idő a leglassabb forrásé lesz, nem az összegük.
    parhuzamos = {
        "ecb":      (lambda: cachelt("ecb", 6*3600, get_eur_huf, 1,
                                     gen=gen_nap, force=manual), (None, False)),
        "idojaras": (lambda: cachelt("idojaras", 3600, get_idojaras_barmelyik, 2,
                                     gen=gen_ora, force=manual), (None, None, False, None)),
        "dam":      (lambda: cachelt("dam", 1800, get_dam, 1,
                                     gen=gen_aukcio, force=manual), (None, False)),
        "load":     (lambda: cachelt("load", 3600, get_load, 1,
                                     gen=gen_ora, force=manual), (None, False)),
        "mavir":    (lambda: cachelt("load_forecast", 3600, get_load_forecast, 1,
                                     gen=gen_aukcio, force=manual), (None, False)),
        "napszelfc": (lambda: cachelt("napszelfc", 3600, get_naposzel_fc, 1,
                                      gen=gen_aukcio, force=manual), (None, False)),
        "ho":       (lambda: cachelt("homerseklet", 1800, get_ho_barmelyik, 1,
                                     gen=gen_ora, force=manual), (None, False)),
        "termeles": (lambda: cachelt("termeles", 1800, get_termeles, 1,
                                     gen=gen_ora, force=manual), (None, False)),
    }
    eredmenyek = {}
    with ThreadPoolExecutor(max_workers=len(parhuzamos)) as vegrehajto:
        jovok = {nev: vegrehajto.submit(fn) for nev, (fn, _) in parhuzamos.items()}
        for nev, jovo in jovok.items():
            try:
                eredmenyek[nev] = jovo.result()
            except Exception as e:
                # A cachelt() csak akkor dob, ha nincs korábbi jó adat sem.
                # Ilyenkor a forrás egyszerűen hiányzónak számít.
                print(f"[HIBA] {nev} párhuzamos lekérés: {e}", flush=True)
                eredmenyek[nev] = parhuzamos[nev][1]

    eur_huf,eur_ok = eredmenyek["ecb"]
    ido_df,daily,ido_ok,ido_forras = eredmenyek["idojaras"]
    dam,dam_ok = eredmenyek["dam"]
    load,load_ok = eredmenyek["load"]
    mavir_fc,mavir_fc_ok = eredmenyek["mavir"]
    fcs,fc_ok = eredmenyek["napszelfc"]
    aho,ho_ok = eredmenyek["ho"]
    term,term_ok = eredmenyek["termeles"]

    hianyzo = []
    if not dam_ok: hianyzo.append("ENTSO-E (DAM árak)")
    if not load_ok: hianyzo.append("ENTSO-E (fogyasztás)")
    if not fc_ok: hianyzo.append("ENTSO-E (nap/szél előrejelzés)")
    if not ido_ok: hianyzo.append("Időjárás (Open-Meteo és Visual Crossing)")
    if not eur_ok: hianyzo.append("ECB (árfolyam)")

    # A DAM az egyetlen kritikus forrás: nélküle nincs sem kártya, sem jóslat.
    if not dam_ok:
        return {"kritikus_hiba":True,"hianyzo":hianyzo,"modell_hiba":None}

    # ---- A SZÁMÍTÁS GYORSÍTÓTÁRA ----
    # Idáig csak a nyers források gyorsítótárazódtak. Minden, ami ezután jön —
    # a CatBoost jóslat, a 720 órás robusztus STL-bontás, négy külön Supabase-
    # kapcsolat írásra és a validációs lekérdezések —, MINDEN oldalletöltéskor
    # újrafutott, akkor is, ha a bemenet betűre ugyanaz volt. Ez volt az oldal
    # lassúságának a fő oka: nem a lekérés, hanem a fölösleges újraszámolás.
    #
    # Az ujjlenyomat a bemenet azonossága: amíg ugyanaz, a korábban kiszámolt
    # csomag megy vissza. Az időfüggő részek (töltési ablak, aktuális óra) nem
    # itt készülnek, hanem a `render`-ben a negyedórás árgörbéből, ezért a
    # kártyák és a "Mikor tölts?" panel a gyorsítótár mellett is élők maradnak.
    ujjlenyomat = "|".join(str(x) for x in [
        dam.get("utolso_ar_ora"), dam.get("holnapi_ar"), len(dam.get("ma_oras") or []),
        (load.index.max().isoformat() if load_ok else "-"), (len(load) if load_ok else 0),
        (len(fcs.get("nap") or {}) if fc_ok else 0),
        (len(mavir_fc or {}) if mavir_fc_ok else 0),
        (len(term.get("nap") or {}) if term_ok else 0),
        (len(ido_df) if ido_ok else 0), eur_huf, aho, gen_ora,
    ])
    if not manual:
        with SZAMITAS_LOCK:
            kesz = SZAMITAS_CACHE.get("csomag")
            if (kesz is not None
                    and SZAMITAS_CACHE.get("ujjlenyomat") == ujjlenyomat
                    and (time.time() - SZAMITAS_CACHE.get("ido", 0)) < SZAMITAS_TTL):
                return kesz

    dam_oras = {pd.Timestamp(k): v for k,v in dam["oras"].items()}

    eredm = None
    ablak_h = 0
    if ido_ok and load_ok and eur_ok and fc_ok:
        try:
            ido_map = {r["Datum"]: r for r in ido_df.to_dict("records")}
            fc_nap = {pd.Timestamp(k): v for k,v in fcs["nap"].items()}
            fc_szel = {pd.Timestamp(k): v for k,v in fcs["szel"].items()}
            orak = celablak(load, dam_oras, ido_map, fc_nap, fc_szel)
            if len(orak) >= 6:
                eredm = elorejelez(orak, dam_oras, ido_map, load, fc_nap, fc_szel, eur_huf)
                ablak_h = len(orak)
            else:
                print(f"[INFO] Célablak túl rövid ({len(orak)} óra) — jóslat kihagyva", flush=True)
                hianyzo.append("Előrejelzés (rövid célablak)")
        except Exception as e:
            print(f"[HIBA] Előrejelzés: {e}", flush=True)
            hianyzo.append("Előrejelzés")

    # ---- Megújuló: jóslat a teljes sávra, mért érték a múlt órákra ----
    # A sáv a mai éjféltől a célablak végéig tart, hogy a "most" vonaltól
    # balra a jóslat és a tény egymás mellett látszódjon.
    megujulo = None
    if fc_ok:
        try:
            fc_nap_d = {pd.Timestamp(k): v for k,v in fcs["nap"].items()}
            fc_szel_d = {pd.Timestamp(k): v for k,v in fcs["szel"].items()}
            tny_nap_d = {pd.Timestamp(k): v for k,v in term["nap"].items()} if term_ok else {}
            tny_szel_d = {pd.Timestamp(k): v for k,v in term["szel"].items()} if term_ok else {}

            veg = (pd.Timestamp(eredm[-1]["datum"]) if eredm
                   else pd.Timestamp(most).floor('h'))
            orak2 = pd.date_range(pd.Timestamp(ma), veg, freq='h')

            ido=[]; f_nap=[]; f_szel=[]; t_nap=[]; t_szel=[]
            for t in orak2:
                if t not in fc_nap_d or t not in fc_szel_d:
                    continue
                ido.append(t.isoformat())
                f_nap.append(float(fc_nap_d[t])); f_szel.append(float(fc_szel_d[t]))
                t_nap.append(float(tny_nap_d[t]) if t in tny_nap_d else None)
                t_szel.append(float(tny_szel_d[t]) if t in tny_szel_d else None)

            # A mai jóslat eddigi hibája — csak azokon az órákon, ahol van mérés.
            def _hiba(f, t):
                p = [(a,b) for a,b in zip(f,t) if b is not None]
                if len(p) < 3: return None
                mae = float(np.mean([abs(a-b) for a,b in p]))
                atl = float(np.mean([b for _,b in p]))
                bias = float(np.mean([a-b for a,b in p]))
                return {"mae":mae,"rel":(mae/atl*100 if atl > 1 else None),
                        "bias":bias,"orak":len(p)}

            if ido:
                megujulo = {"ido":ido,"fc_nap":f_nap,"fc_szel":f_szel,
                            "tny_nap":t_nap,"tny_szel":t_szel,
                            "hiba_nap":_hiba(f_nap,t_nap),
                            "hiba_szel":_hiba(f_szel,t_szel),
                            "mert_ok":term_ok}
        except Exception as e:
            print(f"[HIBA] Megújuló idősor: {e}", flush=True)

    stl_data = None
    stl_napok = 0
    if load_ok:
        try:
            # A get_load 17 napot hoz, tehát a 720 órás ablak sosem telik meg.
            # A felirat a TÉNYLEGES hosszt mutassa, ne egy remélt 30 napot.
            s = load.tail(720) if len(load)>=720 else load
            stl_napok = max(1, round(len(s)/24))
            res = STL(s,period=24,seasonal=25,robust=True).fit()
            std=float(res.resid.std()); mean=float(res.resid.mean()); kuszob=2.5*std
            mask=abs(res.resid-mean)>kuszob
            # A négy idősor 0,1 MWh-ra kerekítve megy a böngészőbe. Teljes
            # lebegőpontos alakban négyszer ~700 érték a `dcc.Store` JSON
            # méretének a nagy részét adta, miközben a grafikonokon egy MWh
            # tizede sem látszik.
            _k1 = lambda sor: [round(float(x), 1) for x in sor]
            stl_data={"trend":_k1(res.trend),"seasonal":_k1(res.seasonal),
                "idok":[t.isoformat() for t in s.index],
                "residual":_k1(res.resid),"original":_k1(s),
                "anomalia_db":int(mask.sum()),
                "stat":{"std":std,"mean":mean,"kuszob":kuszob,
                    "irany":"emelkedő" if res.trend.iloc[-1]>res.trend.iloc[-24] else "csökkenő"}}
            if ido_ok and dam_ok:
                save_stl_anomalies(s, res, kuszob, mean,
                    {r["Datum"]: r for r in ido_df.to_dict("records")},
                    dam_oras)
        except Exception as e:
            print(f"[HIBA] STL: {e}", flush=True)

    # ---- Összevetés a TEGNAPI azonos órával ----
    # A heti átlag a "megszokotthoz" méri a fogyasztást, ez viszont a konkrét
    # előző naphoz — erre a leggyakoribb a kérdés: mennyivel több vagy kevesebb
    # ma, mint tegnap ugyanekkor.
    tegnaphoz = None
    if load_ok and len(load) > 25:
        try:
            t_most = load.index.max()
            t_tegnap = t_most - pd.Timedelta(hours=24)
            if t_tegnap in load.index:
                v_most = float(load.loc[t_most]); v_tegnap = float(load.loc[t_tegnap])
                tegnaphoz = {
                    "ora": t_most.strftime("%H:%M"),
                    "mai_mwh": round(v_most, 1),
                    "tegnapi_mwh": round(v_tegnap, 1),
                    "elteres_mwh": round(v_most - v_tegnap, 1),
                    "elteres_szazalek": (round((v_most - v_tegnap) / v_tegnap * 100, 1)
                                         if v_tegnap else None),
                }
                # A két nap eddigi átlaga is: az egy óra önmagában zajos lehet.
                ma_eddig = load[load.index >= pd.Timestamp(ma)]
                tegnap_ugyaneddig = load[(load.index >= pd.Timestamp(ma) - pd.Timedelta(days=1))
                                         & (load.index <= t_tegnap)]
                if len(ma_eddig) >= 3 and len(tegnap_ugyaneddig) >= 3:
                    a_ma = float(ma_eddig.mean()); a_teg = float(tegnap_ugyaneddig.mean())
                    tegnaphoz["mai_atlag_mwh"] = round(a_ma, 1)
                    tegnaphoz["tegnapi_atlag_mwh"] = round(a_teg, 1)
                    tegnaphoz["atlag_elteres_szazalek"] = (
                        round((a_ma - a_teg) / a_teg * 100, 1) if a_teg else None)
        except Exception as e:
            print(f"[HIBA] Tegnapi összevetés: {e}", flush=True)

    mert = None
    heti_atlag = None
    if load_ok:
        mert = {"ertek":float(load.iloc[-1]),"idopont":load.index.max().strftime("%H:%M")}
        try:
            u7 = load.tail(7*24)
            heti_atlag = [float(u7[u7.index.hour == o].mean()) for o in range(24)]
        except Exception as e:
            print(f"[HIBA] Heti átlag: {e}", flush=True)

    mavir_forecast = ({pd.Timestamp(k):float(v) for k,v in mavir_fc.items()}
                      if mavir_fc_ok else {})

    # 1) Először a már lezárt órák kerülnek a végleges forecast_log táblába.
    # Ezeket a modell a celablak() logikája miatt többé nem jósolja újra.
    if load_ok:
        if mavir_forecast:
            fill_missing_pending_mavir(mavir_forecast)
        finalize_completed_forecasts(load, most)

    # 2) A még jövőbeli célórák csak az ideiglenes pending táblába kerülnek.
    # Célóránként egy sor van, amelyet az újraszámolás addig frissíthet,
    # amíg meg nem érkezik az adott óra lezárt tényadata.
    if eredm:
        input_quality_label = "complete" if not hianyzo else "degraded"
        save_pending_forecasts(
            forecast=eredm,
            generated_at=most,
            input_quality_label=input_quality_label,
            stl_anomaly_lag_count=(stl_data or {}).get("anomalia_db", 0),
            source_type="manual" if manual else "automatic",
            mavir_forecast=mavir_forecast,
        )
    validacio = get_validacio_adatok()
    csomag = {
        "eredm":eredm,
        "validacio":validacio,
        "ablak_h":ablak_h,
        "eur_huf":eur_huf if eur_ok else None,
        "negyed":dam["negyed"],
        "dam_ma_oras":dam["ma_oras"],
        "holnapi_ar":dam["holnapi_ar"],
        "ido_forras":ido_forras if ido_ok else None,
        "aho":aho if ho_ok else None,
        "mert_fogyasztas":mert,
        "tegnaphoz":tegnaphoz,
        "heti_atlag":heti_atlag,
        "megujulo":megujulo,
        "stl":stl_data,
        "stl_napok":stl_napok,
        "daily":daily if ido_ok else None,
        "frissites":most.strftime("%H:%M:%S"),
        "frissites_tipus":"kézi" if manual else "automatikus",
        "fb":{"ENTSO-E":not (dam_ok and load_ok and fc_ok),"Időjárás":not ido_ok,"ECB":not eur_ok},
        "hianyzo":hianyzo}

    # A kész csomag a polcra kerül: a következő látogató ugyanezt kapja
    # újraszámolás nélkül, amíg a bemenet ugyanaz.
    with SZAMITAS_LOCK:
        SZAMITAS_CACHE.update({"ujjlenyomat":ujjlenyomat,"ido":time.time(),"csomag":csomag})
    return csomag

@callback(Output("oldal","data"),
    [Input(f"nav-{x}","n_clicks")
     for x in ["fooldal","toltes","elemzes","megujulok","mllabor"]],
    prevent_initial_call=True)
def nav(*_):
    ctx=dash.callback_context
    if not ctx.triggered: return "fooldal"
    return ctx.triggered[0]["prop_id"].split(".")[0].replace("nav-","")

def _kpi_naplo(data):
    return ((data.get("validacio") or {}).get("naplo")) or []

def _kpi_nyitott(data):
    return sum(1 for r in _kpi_naplo(data) if r.get("kategoria") == "rejtely")

def _kpi_or_felirat(data):
    naplo = _kpi_naplo(data)
    if not naplo:
        return "Nincs jelzés az elmúlt 7 napban"
    megm = sum(1 for r in naplo
               if r.get("kategoria") in ("extrem", "napelem", "fordulat"))
    return f"{len(naplo)} jelzés 7 nap · {megm} megmagyarázva"

def _kpi_or_szin(data):
    ny = _kpi_nyitott(data)
    if not _kpi_naplo(data) or ny == 0:
        return C['gr']
    return C['yw'] if ny <= 2 else C['or']


NB = {"display":"flex","alignItems":"center","justifyContent":"center",
    "padding":"14px 12px","cursor":"pointer","transition":"all 0.2s","background":"transparent"}

@callback([Output("statusz","children"),Output("kpi-sor","children"),
    Output("oldal-content","children"),Output("src-panel","children"),
    Output("modell-panel","children"),
    Output("nav-fooldal","style"),Output("nav-toltes","style"),
    Output("nav-elemzes","style"),Output("nav-megujulok","style"),
    Output("nav-mllabor","style")],
    [Input("adatok","data"),Input("oldal","data")])
def render(data,oldal):
    ns=[{**NB,"color":C['gr'],"borderBottom":f"2px solid {C['gr']}"} if oldal==x
        else {**NB,"color":C['mut'],"borderBottom":"2px solid transparent"}
        for x in ["fooldal","toltes","elemzes","megujulok","mllabor"]]

    mk = (bundle or {}).get("metrikak",{})
    modell_info = html.Div([
        html.Div("Modell",style={"fontSize":"9px","color":C['cy'],"fontWeight":"bold"}),
        html.Div("CatBoost V10 — direkt 24h",style={"fontSize":"10px","color":C['mut']}),
        html.Div(f"MAE {mk.get('mae',0):.1f} MWh | MAPE {mk.get('mape',0):.2f}%"
                 if bundle else "Modell nem elérhető",
            style={"fontSize":"9px","color":C['gr'] if bundle else C['rd'],"marginTop":"2px"})
    ])

    if data is None:
        return (html.Div("Élő adatok betöltése...",style={"color":C['yw']}),
            html.Div(),html.Div(),html.Div(),modell_info,*ns)

    if data.get("kritikus_hiba"):
        statusz = html.Div([
            html.Span("● ",style={"color":C['rd']}),
            html.Span("Éles adatkapcsolat megszakadt",style={"fontSize":"12px","color":C['rd'],"fontWeight":"500"})
        ])
        src = html.Div([src_sor(k, k not in " ".join(data.get("hianyzo",[])))
                        for k in ["ENTSO-E","Időjárás","ECB"]])
        return (statusz,html.Div(),
            hiba_panel(data.get("hianyzo",[]),data.get("modell_hiba")),
            src,modell_info,*ns)

    edf = pd.DataFrame(data["eredm"]) if data.get("eredm") else None
    eur_huf=data.get("eur_huf"); aho=data.get("aho")
    fb=data["fb"]; hianyzo=data.get("hianyzo",[])

    stl_db=data["stl"]["anomalia_db"] if data["stl"] else 0
    stl_tot=len(data["stl"]["trend"]) if data["stl"] else 0

    aj = toltes_ajanlas(data["negyed"]) if data.get("negyed") else None

    dam_ma = data.get("dam_ma_oras") or []
    ma_atlag = float(np.mean(dam_ma)) if dam_ma else 0.0
    dam_most = float(aj["akt_ar"]) if aj else (dam_ma[_helyi_most().hour] if dam_ma else 0.0)
    dam_sz = dam_szin(dam_most, ma_atlag if ma_atlag else 1.0)

    # A nagy szám legyen SZÁM, mint a többi kártyán — az időszak megy az alsó
    # sorba. Így a mértékegység-leválasztás is működik, és nem vágja le az
    # ellipszis (a "Holnap 13:15 · 3 €" nem fért ki egy sorban).
    legolcs_ar = "–"
    legolcs_ido = "Nincs publikált ár"
    if aj:
        k = datetime.fromisoformat(aj["aj_kezd"])
        v = datetime.fromisoformat(aj["aj_veg"])
        legolcs_ar = f"{aj['aj_ar']:.0f} €/MWh"
        legolcs_ido = f"{_nap_elotag(k)}{k:%H:%M} – {v:%H:%M}"

    dam_cimke = "Holnapi árak elérhetők" if data.get("holnapi_ar") else "Holnapi árak publikálás előtt"
    ido_txt = f" — Időjárás: {data['ido_forras']}" if data.get("ido_forras") else ""

    if hianyzo:
        statusz=html.Div([
            html.Span("● ",style={"color":C['yw']}),
            html.Span(f"Részleges élő adatok — nem elérhető: {', '.join(hianyzo)} — "
                f"frissítve: {data['frissites']} (Budapest)",
                style={"fontSize":"12px","color":C['yw'],"fontWeight":"500"})
        ])
    else:
        statusz=html.Div([
            html.Span("● ",style={"color":C['gr']}),
            html.Span(f"Élő adatok frissítve: {data['frissites']} (Budapest) — {dam_cimke}{ido_txt}",
                style={"fontSize":"12px","color":C['txt'],"fontWeight":"500"})
        ])

    src=html.Div([src_sor(k,not v) for k,v in fb.items()])

    # A besorolás a nap SAJÁT NEGYEDEIHEZ mér, nem az átlaghoz.
    #
    # Az átlaghoz mérés azért nem működött, mert a napi árgörbe erősen jobbra
    # ferde: néhány drága esti óra (ma 405 €/MWh) felhúzza az átlagot, így a
    # 24 órából 15-18 "átlag alatt" van. A felirat igaz volt, de egész nap
    # ugyanazt mutatta, tehát nem mondott semmit.
    #
    # A kvartilisek ezt megoldják: a nap legolcsóbb negyede "Olcsó", a
    # legdrágább negyede "Drága", a köztes fele "Átlagos". Ez a ferdeségtől
    # függetlenül mindig megkülönböztet, és visszahozza az eredeti szavakat.
    if dam_most < 0:
        kat = "Negatív ár"
    elif len(dam_ma) >= 8:
        also = float(np.percentile(dam_ma, 25))
        felso = float(np.percentile(dam_ma, 75))
        kat = ("Olcsó" if dam_most <= also else
               ("Drága" if dam_most >= felso else "Átlagos"))
    elif ma_atlag:
        kat = "Olcsó" if dam_most < ma_atlag else "Drága"
    else:
        kat = "Aktuális ár"

    # A modell jóslata AZ AKTUÁLIS ÓRÁRA. Nem eredm[0]-t veszünk: a célablak
    # első sora rendszerint a most futó óra, de az ENTSO-E mérési késése
    # ingadozik, ezért időbélyeg szerint keresünk.
    most_ora = _helyi_most().replace(minute=0, second=0, microsecond=0)
    akt_sor = None; akt_i = None
    if data.get("eredm"):
        for i, r in enumerate(data["eredm"]):
            if datetime.fromisoformat(r["datum"]) == most_ora:
                akt_sor, akt_i = r, i
                break
        if akt_sor is None:
            akt_sor, akt_i = data["eredm"][0], 0

    # A fehér karika oda kerüljön, ahol a kártya nagy száma van.
    dam_jelolt = most_ora.hour if len(dam_ma) == 24 else None
    stl_napok = data.get("stl_napok") or 0

    ksor=html.Div([
        kpi("Jelenlegi DAM ár",f"{dam_most:.0f} €/MWh",kat,dam_sz,
            dam_ma or None, dam_jelolt),
        kpi("Előrejelzett fogyasztás",
            f"{akt_sor['fogyasztas']:,.0f} MWh".replace(","," ") if akt_sor else "–",
            (f"CatBoost V10 · {datetime.fromisoformat(akt_sor['datum']):%H:%M}"
             if akt_sor else "Előrejelzés nem elérhető"),
            C['bl'], edf["fogyasztas"].tolist() if edf is not None else None,
            int(np.argmax(edf["fogyasztas"].values)) if edf is not None else None),
        kpi("Budapest",f"{aho:.0f} °C" if aho is not None else "–","Most",C['yw']),
        kpi("EUR/HUF",f"{eur_huf:.1f} Ft" if eur_huf is not None else "–","Árfolyam",C['bl']),
        kpi("Legolcsóbb ablak",legolcs_ar,legolcs_ido,C['gr']),
        kpi("Adatminőség-őr",
            f"{_kpi_nyitott(data)} nyitott" if data["stl"] else "–",
            _kpi_or_felirat(data) if data["stl"] else "Nem elérhető",
            _kpi_or_szin(data)),
    ],className="kpi-grid", style=KPI_GRID_STYLE)

    if oldal=="fooldal":
        page = nyitooldal()
    elif oldal=="toltes":
        page=fooldal(data,aj)
    elif oldal=="elemzes":
        page=elemzes(edf,data)
    elif oldal=="megujulok":
        page=megujulok(data)
    else:
        page=mllabor(data)
    return statusz,ksor,page,src,modell_info,*ns

# ============================================================
# ÚJ FŐOLDAL — nyitó vizuál (a KPI-sort és a fejlécet a layout rajzolja)
# ============================================================
def nyitooldal():
    """Nyitó vizuál + Flux. A szöveg keret nélkül, az égbolt előtt jelenik meg."""
    flux_reteg = html.Div([
        html.Div([
            html.Div(style={"width":"5px","height":"5px","borderRadius":"999px",
                            "background":C['gr'],"boxShadow":f"0 0 8px {_rgba(C['gr'],.8)}"}),
            html.Div("FLUX",style={"color":"#5b7a92","fontSize":"10px","fontWeight":"700",
                                   "letterSpacing":"3px"})
        ],style={"display":"flex","alignItems":"center","gap":"7px"}),

        html.Div([
            html.Span(id="flux-szoveg"),
            html.Span(className="flux-kurzor")
        ],style={"minHeight":"84px","color":"#dbe7f2","fontSize":"15px","lineHeight":"1.7",
                 "textWrap":"pretty"}),

        html.Div([
            html.Div(id="flux-szam",style={"color":C['wh'],"fontSize":"34px",
                                           "fontWeight":"700","lineHeight":"1"}),
            html.Div(id="flux-cimke",style={"color":C['mut'],"fontSize":"10px","fontWeight":"600",
                                            "letterSpacing":"1.4px","textTransform":"uppercase"})
        ],id="flux-szam-doboz",style={"display":"none","flexDirection":"column","gap":"4px"}),

        # debounce=True: enélkül az Enter (`n_submit`) és a beírt szöveg
        # (`value`) külön üzenetben ér a szerverhez, és versenyeznek. Aki
        # gépelés után azonnal Entert üt, annál a `value` még az ELŐZŐ állapot
        # — a callback üres szöveget lát, nem csinál semmit, és csak a MÁSODIK
        # Enterre indul el a válasz. A debounce az Enterre egyszerre küldi a
        # kettőt, így az első leütés is működik.
        dcc.Input(id="flux-kerdes",type="text",debounce=True,
                  placeholder="Kérdezz Fluxtól, majd Enter…",className="flux-kerdes"),

    ],className="flux-reteg")

    return html.Div([
        kep("hero-grid.png", "Villamosenergia-hálózat", className="hero-kep",
            style={"display":"block","width":"100%","height":"auto"}),
        flux_reteg
    ], className="hero-panel",
       style={"position":"relative","width":"100%","overflow":"hidden",
              "border":f"1px solid {C['brd']}","borderRadius":"8px",
              "background":"#071529","minHeight":"120px"})


# ---- Flux: szövegek az élő adatokból (Gemini a szerveren, flux.py) ----
@callback(Output("flux-uzenetek","data"),
    Input("adatok","data"),
    prevent_initial_call=False)
def flux_szovegek(data):
    if not data or data.get("kritikus_hiba"):
        return [{"sor":"Élő adatokra várok — amint megérkeznek, mondom, mi történik.",
                 "szam":None,"cimke":None}]
    try:
        aj = toltes_ajanlas(data["negyed"]) if data.get("negyed") else None
        return flux.uzenetek(data, aj)
    except Exception as e:
        print(f"[FLUX] Szövegek: {e}", flush=True)
        return [{"sor":flux.elo_koszonto(),"szam":None,"cimke":None}]


# ---- Flux: a látogató kérdése ----
# A kérdést NEM közvetlenül az Enter indítja, hanem a `flux-kerdes-kontextus`
# store változása. Így garantált a sorrend: előbb fut a böngészőben az a
# kliensoldali callback, amelyik beleteszi a store-ba az ÉPPEN OLVASOTT
# megállapítást, és csak utána indul a szerveroldali válasz — amely így már
# ismeri a kérdés kontextusát. Fordított sorrendben a szerver mindig az előző
# képernyőállapotot látná.
@callback([Output("flux-valasz","data"),Output("flux-kerdes","value")],
    Input("flux-kerdes-kontextus","data"),
    [State("flux-kerdes","value"),State("adatok","data")],
    prevent_initial_call=True)
def flux_kerdes(kontextus, kerdes, data):
    if not kerdes or not str(kerdes).strip():
        return dash.no_update, dash.no_update
    try:
        aj = toltes_ajanlas(data["negyed"]) if (data or {}).get("negyed") else None
        v = flux.valasz(kerdes, data, aj,
                        kontextus=(kontextus or {}).get("sor"),
                        mar_koszont=bool((kontextus or {}).get("mar_koszont")))
    except Exception as e:
        print(f"[FLUX] Kérdés callback: {e}", flush=True)
        v = {"sor":"Erre az élő adatokból most nem tudok pontos választ adni.",
             "szam":None,"cimke":None}
    # A sorszám nélkül a böngésző nem venné észre, ha valaki UGYANAZT kérdezi
    # kétszer: a `dcc.Store` értéke betűre azonos lenne, a gépelés nem indulna
    # újra, és a látogató úgy látná, hogy Flux másodszorra nem válaszol.
    return {**v, "_n": int(time.time() * 1000)}, ""


# ---- Flux: az adatok átadása a böngészőnek ----
# A gépelést az assets/flux.js végzi saját ütemben. Így az oldal újraépítése
# (15 mp-es óra, fülváltás) nem szakítja meg és nem kezdi elölről a szöveget.
app.clientside_callback(
    """
    function(uzenetek, valasz) {
        window.FLUX_DATA = {uzenetek: uzenetek || [], valasz: valasz || null};
        if (window.fluxIndit) window.fluxIndit();
        return window.dash_clientside.no_update;
    }
    """,
    Output("flux-noop","data"),
    [Input("flux-uzenetek","data"),Input("flux-valasz","data")],
)

# ---- Flux: azonnali visszajelzés a kérdésre ----
# A szerver a válaszhoz megnézi az élő adatokat, ez néhány másodperc. Enélkül
# a látogató annyit lát, hogy leütötte az Entert és semmi nem történik — ez
# volt az egyik oka annak, hogy "Flux nem válaszol". A jelzés a böngészőben
# fut, tehát a hálózati körre nem kell megvárni.
app.clientside_callback(
    """
    function(n_submit) {
        if (!n_submit) return window.dash_clientside.no_update;
        if (window.fluxVarakozas) window.fluxVarakozas();
        // Az ÉPPEN OLVASOTT megállapítás elmegy a kérdéssel: enélkül az
        // "és ez mit jelent?" kérdésre Flux nem tudta, mire vonatkozik a "ez".
        var sor = (window.fluxAktualis ? window.fluxAktualis() : "");
        // Köszönt-e már ebben a böngésző-munkamenetben? Enélkül a nyelvi
        // modell MINDEN válaszot "Szia!"-val kezdett, ami pár kérdés után
        // modorossá vált. Az első kérdés után ez a jelző igazra vált, és a
        // szerver ennek megfelelően utasítja a modellt.
        var mar = !!window.FLUX_MAR_KOSZONT;
        window.FLUX_MAR_KOSZONT = true;
        return {sor: sor, mar_koszont: mar, n: n_submit};
    }
    """,
    Output("flux-kerdes-kontextus","data"),
    Input("flux-kerdes","n_submit"),
    prevent_initial_call=True,
)

# ============================================================
# DAM-ÁRAK ÉS TÖLTÉS — hero-kártya: "Mikor tölts?"
# ============================================================
def fooldal(data,aj):
    if not aj:
        return hianyzo_panel("MIKOR TÖLTS?",
            "Nincs hátralévő publikált árszelvény. Az új napelőtti árak "
            "14:00 körül érkeznek.")

    most = _helyi_most()
    aj_kezd = datetime.fromisoformat(aj["aj_kezd"])
    aj_veg = datetime.fromisoformat(aj["aj_veg"])
    aj_ar = float(aj["aj_ar"])
    toltheto = bool(aj["most_jo"])

    def hatralevo(cel):
        perc = max(0, math.ceil((cel - most).total_seconds() / 60))
        ora, perc = divmod(perc, 60)
        return f"{ora} óra {perc} perc" if ora else f"{perc} perc"

    if toltheto:
        aktiv_veg = datetime.fromisoformat(aj["akt_veg"])
        dontes = "IGEN"; dontes_seged = "Még"
        dontes_ido = hatralevo(aktiv_veg)
        idoszak_cimke = "Kedvező időszak vége"
        idoszak = f"{_nap_elotag(aktiv_veg)}{aktiv_veg:%H:%M}"
        dontes_ar = float(aj["akt_ar"])
    else:
        dontes = "NEM"; dontes_seged = "Várj még"
        dontes_ido = hatralevo(aj_kezd)
        idoszak_cimke = "Következő optimális időszak"
        idoszak = f"{_nap_elotag(aj_kezd)}{aj_kezd:%H:%M} – {aj_veg:%H:%M}"
        dontes_ar = aj_ar

    terv = [(aj_kezd, aj_veg, aj_ar)]
    for ido, ar in aj.get("altok", []):
        kezd = datetime.fromisoformat(ido)
        terv.append((kezd, kezd + timedelta(minutes=TOLTES_PERC), float(ar)))

    terv_sorok = [
        html.Div([
            html.Span(f"{_nap_elotag(kezd)}{kezd:%H:%M} – {veg:%H:%M}",className="charge-plan-time"),
            html.Span(f"{ar:.0f} €/MWh",className="charge-plan-price")
        ],className="charge-plan-row")
        for kezd,veg,ar in terv[:3]
    ]

    # --- DAM oszlopdiagram: mostantól a legutolsó publikált negyedóráig ---
    g = aj["grafikon"]
    idok = [datetime.fromisoformat(t) for t in g["ido"]]
    arak = [float(a) for a in g["ar"]]
    lepes = int(aj["lepes"])

    # az AKTUÁLIS szelvény: az utolsó, amelyik már elkezdődött.
    # (A régi kód a legközelebbi rácspontot vette, és rá is írta az árat.)
    mult = [i for i,t in enumerate(idok) if t <= most]
    most_i = mult[-1] if mult else 0

    fig = go.Figure()
    slot_ms = lepes * 60 * 1000
    bar_width = slot_ms * 0.66
    glow_width = slot_ms * 0.94
    pozitiv_y = [max(a,0.0) for a in arak]
    negativ_y = [min(a,0.0) for a in arak]

    if any(a < 0 for a in arak):
        fig.add_trace(go.Bar(x=idok,y=negativ_y,width=glow_width,
            marker=dict(color="rgba(0,224,194,0.16)",line=dict(width=0)),
            hoverinfo="skip",showlegend=False))
    fig.add_trace(go.Bar(x=idok,y=negativ_y,width=bar_width,
        marker=dict(color=[ar_szin(a) for a in arak],line=dict(width=0)),
        hoverinfo="skip",showlegend=False))
    fig.add_trace(go.Bar(x=idok,y=pozitiv_y,width=bar_width,
        marker=dict(color=[ar_szin(a) for a in arak],line=dict(width=0)),
        hoverinfo="skip",showlegend=False))
    # egyetlen hover-réteg, hogy a buborék kiszámítható legyen
    fig.add_trace(go.Scatter(x=idok,y=arak,mode="markers",
        marker=dict(size=10,color="rgba(0,0,0,0)"),
        hovertemplate="%{x|%m.%d. %H:%M}<br>%{y:.0f} €/MWh<extra></extra>",
        showlegend=False))

    fig.add_hline(y=0,line=dict(color=C['mut'],width=1))
    fig.add_shape(type="line",x0=most,x1=most,y0=0,y1=1,yref="paper",
        line=dict(color=C['wh'],width=1,dash="dot"))
    fig.add_trace(go.Scatter(x=[idok[most_i]],y=[arak[most_i]],mode="markers",
        marker=dict(size=9,color=C['wh'],line=dict(width=2,color=ar_szin(arak[most_i]))),
        hoverinfo="skip",showlegend=False))
    fig.add_annotation(x=idok[most_i],y=arak[most_i],
        text=f"<b>{arak[most_i]:.0f} €</b><br>{idok[most_i]:%H:%M}",
        showarrow=True,arrowhead=0,ax=34,ay=-42,
        bgcolor="#0f1d31",bordercolor=C['brd'],borderwidth=1,borderpad=7,
        font=dict(color=C['wh'],size=10))

    # éjfél-elválasztó, ha az ablak átnyúlik holnapra
    napok = sorted({t.date() for t in idok})
    for d in napok[1:]:
        hatar = datetime.combine(d, datetime.min.time())
        fig.add_shape(type="line",x0=hatar,x1=hatar,y0=0,y1=1,yref="paper",
            line=dict(color="#3c5873",width=1,dash="dash"))
        fig.add_annotation(x=hatar,y=1.0,yref="paper",yanchor="bottom",
            text=_nap_elotag(hatar).strip() or f"{d:%m.%d}",
            showarrow=False,font=dict(size=9,color="#7fa3c4"))

    ar_min, ar_max = min(arak), max(arak)
    y_padding = max(5.0, max(ar_max - ar_min, 1.0) * 0.08)
    y_min = min(0.0, ar_min - y_padding) if ar_min < 0 else 0.0
    y_max = max(0.0, ar_max + y_padding)
    if y_max <= y_min: y_max = y_min + 10.0

    fig.update_layout(paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color=C['mut'],family='Inter,sans-serif',size=10),
        margin=dict(l=48,r=18,t=18,b=30),height=200,showlegend=False,
        hovermode="closest",barmode="overlay",bargap=0.1,
        xaxis=dict(type="date",showgrid=False,color=C['mut'],
            tickformat="%H:%M",dtick=3*60*60*1000,fixedrange=True,
            range=[idok[0],idok[-1]+timedelta(minutes=lepes)]),
        yaxis=dict(gridcolor=C['brd'],color=C['mut'],
            zeroline=False,fixedrange=True,range=[y_min,y_max]))

    state_class = "is-negative" if float(aj["akt_ar"]) < 0 else (
        "is-ready" if toltheto else "is-waiting")
    egy_soros_idoszak = f"{idoszak} · {dontes_ar:.0f} €/MWh"

    ora_szam = len(idok) * lepes / 60.0

    return html.Div([
        html.Div([
            html.Div([
                html.Div("MIKOR TÖLTS?",className="charge-eyebrow"),
                html.Div([
                    html.Div([
                        kep("sports-coupe-board.png", "Elektromos sportautó",
                            className="charge-car-source"),
                        html.Div(className="charge-headlight charge-headlight-bal"),
                        html.Div(className="charge-headlight charge-headlight-jobb"),
                        html.Div(className="charge-floor-glow")
                    ],className="charge-car-stage")
                ],className="charge-car-frame")
            ],className=f"charge-car-showcase {state_class}"),
            html.Div([
                html.Div([
                    html.Div([
                        html.Div(dontes,className="charge-decision"),
                        html.Div(dontes_seged,className="charge-decision-helper"),
                        html.Div(dontes_ido,className="charge-countdown")
                    ],className="charge-gauge-face")
                ],className=f"charge-gauge {state_class}"),
                html.Div([
                    html.Div(idoszak_cimke,className="charge-next-label"),
                    html.Div(egy_soros_idoszak,className="charge-next-period")
                ],className="charge-next"),
                html.Details([
                    html.Summary("Töltési terv",className="charge-plan-button"),
                    html.Div(terv_sorok,className="charge-plan-list")
                ],className="charge-plan")
            ],className=f"charge-decision-panel {state_class}")
        ],className="charge-summary"),
        html.Div([
            html.Div([
                html.Div(f"DAM ÁRAK — A KÖVETKEZŐ {ora_szam:.0f} ÓRA",className="charge-chart-title"),
                html.Div("€/MWh",className="charge-unit")
            ],className="charge-chart-header"),
            dcc.Graph(figure=fig,config={"displayModeBar":False},className="charge-chart-graph"),
            html.Div([
                html.Div(nev,className="price-band",
                    style={"background":szin,"color":"#04121f"})
                for nev,szin in AR_SAVOK
            ],className="charge-price-scale")
        ],className="charge-chart")
    ],className="charge-hero-card")

# ============================================================
# 2. OLDAL — a gördülő célablak
# ============================================================
def _zona(h):
    if h < 6: return ("Éjszaka","#0a1a33","#4b6a94")
    if h < 9: return ("Reggel","#10233c","#6f8fb8")
    if h < 16: return ("Napközben","#0d1f30","#7fa3c4")
    if h < 21: return ("Esti csúcs","#2a1a0e","#d99a5b")
    return ("Éjszaka","#0a1a33","#4b6a94")

# A napelőtti nap/szél előrejelzés tapasztalati szórása, 8646 órán mérve
# (2025-07-01 … 2026-06-25, ENTSO-E jóslat vs. ENTSO-E mért termelés).
# Napi összegre a napok 80%-a ebbe a sávba esett.
FC_SAV = {"nap": (-0.19, 0.48), "szel": (-0.20, 1.25)}
FC_MAE = {"nap": 19, "szel": 34}   # órás MAE, az átlagos termelés %-ában

KAT_META = [
    ("extrem", "Időjárási extrém", "#f59e0b", "anomalia-extrem.png"),
    ("napelem", "Napelem-árnyék", "#4dd0e1", "anomalia-napelem.png"),
    ("fordulat", "Trendváltás", "#10b981", "anomalia-fordulat.png"),
    ("rejtely", "Vizsgálat alatt", "#FF6600", "anomalia-rejtely.png"),
]
HETNAP = ["hétfő","kedd","szerda","csütörtök","péntek","szombat","vasárnap"]
MAVIR_KEK = "#4da3ff"


def _heti_naplo_panel(v):
    """Heti hadijelentés a forecast_log-ból: három modell kumulált hibája,
    napi győztes-pöttyök, a hét legjobb és legnehezebb órája.
    A 'napelőtti' sor a first_pred oszlopból jön — az a MAVIR-ral azonos
    horizontú, fair összevetés."""
    cim = html.Div("HETI NAPLÓ", style={"fontSize":"13px","fontWeight":"700","color":C['wh']})
    het = (v or {}).get("het") or {}
    if not het.get("orak"):
        return html.Div([cim,
            html.Div("A hét első lezárt órái után itt jelenik meg a heti összesítés.",
                style={"fontSize":"11px","color":C['mut'],"marginTop":"14px"})], style=CS)

    savok_adat = [
        ("CatBoost", "élő, 1–2h", het.get("cb"), C['gr'], 1.0, True),
        ("CatBoost", "napelőtti", het.get("first"), C['gr'], 0.55, False),
        ("MAVIR", "napelőtti", het.get("mv"), MAVIR_KEK, 0.7, False),
    ]
    ervenyes = [s[2] for s in savok_adat if s[2] is not None]
    max_err = max(ervenyes) if ervenyes else 1.0

    savok = []
    for nev, cimke, ert, szin, op, glow in savok_adat:
        if ert is None:
            jobb = html.Span("gyűjtés alatt", style={"fontSize":"10px","color":C['mut']})
            sav = html.Div()
        else:
            jobb = html.Span(f"{ert:.0f}", style={"fontSize":"12px","fontWeight":"600",
                "color":szin if op >= 1.0 else _rgba(szin, 0.85)})
            sav = html.Div(style={"height":"14px",
                "width":f"{max(5.0, ert/max_err*58):.0f}%",
                "background":szin,"opacity":str(op),"borderRadius":"4px",
                "boxShadow":f"0 0 8px {_rgba(szin,.4)}" if glow else "none"})
        savok.append(html.Div([
            html.Span([nev+" ", html.Span(f"({cimke})", style={"color":C['mut']})],
                style={"width":"150px","flex":"0 0 auto","fontSize":"11px","color":C['txt']}),
            sav, jobb,
        ], style={"display":"flex","alignItems":"center","gap":"8px","marginBottom":"7px"}))

    ROVID = ["H","K","Sze","Cs","P","Szo","V"]
    # A dcc.Store JSON-t tárol: a dict kulcsai szövegként térnek vissza,
    # ezért kellett a visszaalakítás — enélkül egyetlen pötty sem talált gazdát.
    napok = {int(k): v for k, v in (het.get("napok") or {}).items()}
    cb_gyoz = sum(1 for w in napok.values() if w)
    mv_gyoz = sum(1 for w in napok.values() if not w)
    pottyok = []
    for i in range(1, 8):
        if i in napok:
            szin = C['gr'] if napok[i] else MAVIR_KEK
            stilus = {"width":"22px","height":"22px","borderRadius":"50%",
                "background":szin,"boxShadow":f"0 0 7px {_rgba(szin,.5)}"}
        else:
            stilus = {"width":"22px","height":"22px","borderRadius":"50%",
                "background":C['brd']}
        pottyok.append(html.Div([html.Div(style=stilus),
            html.Div(ROVID[i-1], style={"fontSize":"9px","color":C['mut'],
                "textAlign":"center","marginTop":"3px"})]))
    pottyok.append(html.Span(f"{cb_gyoz} : {mv_gyoz}",
        style={"fontSize":"11px","color":C['txt'],"marginLeft":"8px","alignSelf":"center"}))

    def _ora_kartya(cimke, rek, szin, extra=""):
        if not rek:
            return html.Div()
        dt = datetime.fromisoformat(rek["ido"])
        return html.Div([
            html.Div(cimke, style={"fontSize":"9px","color":C['mut'],"textTransform":"uppercase"}),
            html.Div(f"{HETNAP[dt.weekday()]} {dt:%H:%M}", style={"fontSize":"13px",
                "color":szin,"fontWeight":"600","marginTop":"3px"}),
            html.Div(f"{rek['err']:.0f} MWh eltérés{extra}", style={"fontSize":"10px",
                "color":"#94a3b8","marginTop":"1px"}),
        ], style={"background":C['card2'],"borderRadius":"8px","padding":"9px"})

    rossz = het.get("legrosszabb")
    rossz_extra = ""
    if rossz:
        o = datetime.fromisoformat(rossz["ido"]).hour
        if 17 <= o <= 21: rossz_extra = " · esti csúcs"

    return html.Div([cim,
        html.Div("Kumulált átlaghiba a hét lezárt óráin (MWh/óra)",
            style={"fontSize":"11px","color":"#94a3b8","margin":"3px 0 14px"}),
        *savok,
        html.Div([
            html.Div("Napi győztes (napelőtti CatBoost vs MAVIR · azonos horizont)",
                style={"fontSize":"10px","color":C['mut'],"textTransform":"uppercase",
                       "letterSpacing":"0.05em","marginBottom":"6px"}),
            html.Div(pottyok, style={"display":"flex","gap":"7px","alignItems":"flex-start"}),
        ], style={"borderTop":f"1px solid {C['brd']}","paddingTop":"11px",
                  "marginTop":"7px","marginBottom":"12px"}),
        html.Div([
            _ora_kartya("A hét legjobb órája", het.get("legjobb"), C['gr']),
            _ora_kartya("A hét legnehezebb órája", rossz, C['or'], rossz_extra),
        ], style={"display":"grid","gridTemplateColumns":"1fr 1fr","gap":"8px"}),
        html.Div(f"A napelőtti sor a first_pred oszlopból — a MAVIR-ral azonos "
                 f"horizontú, fair összevetés · {het['orak']} lezárt óra a héten",
            style={"fontSize":"10px","color":C['mut'],
                   "borderTop":f"1px solid {C['brd']}","paddingTop":"9px","marginTop":"12px"}),
    ], style=CS)


def elemzes(edf, data):
    if edf is None:
        return html.Div([
            html.Div("Energiaelemzés",style={"fontSize":"16px","fontWeight":"600",
                "color":C['wh'],"marginBottom":"14px"}),
            hianyzo_panel("Fogyasztás-előrejelzés",
                "Az előrejelzéshez szükséges élő adatforrások egyike jelenleg nem elérhető. "
                "A panel automatikusan megjelenik, amint minden forrás él.")
        ])

    dtok = [datetime.fromisoformat(s) for s in edf["datum"]]
    N = len(dtok)
    x = list(range(N))
    tick_i = list(range(0, N, 3))
    tick_t = [f"{dtok[i]:%H:%M}" for i in tick_i]
    cim = f"A KÖVETKEZŐ {N} ÓRA"

    def cimke(dt): return f"{_nap_elotag(dt)}{dt:%H:%M}"

    ejfel_i = next((i for i in range(1,N) if dtok[i].hour == 0), None)

    # ================================================================
    # PANEL 1 — Fogyasztás + hőmérséklet, KÖZÖS óratengelyen
    # Külön kártyákon összenyomódtak. Egymás alatt, egy x-tengelyen
    # nemcsak elférnek, hanem látszik is, amit a modell tud: a terhelés
    # és a hőmérséklet ugyanabban az órában mozog.
    # ================================================================
    y = [float(v) for v in edf["fogyasztas"].tolist()]
    y_lo, y_hi = min(y), max(y)
    y_ter = max(y_hi - y_lo, 1.0)
    y_also = y_lo - y_ter*0.10
    y_felso = y_hi + y_ter*0.12

    def terheles_szin(v):
        t = 0.0 if y_hi == y_lo else (v - y_lo) / (y_hi - y_lo)
        allomasok = [(0.0,(47,127,214)),(0.35,(77,163,255)),(0.6,(230,223,0)),
                     (0.8,(255,152,0)),(1.0,(255,59,48))]
        for (t0,c0),(t1,c1) in zip(allomasok, allomasok[1:]):
            if t <= t1:
                a = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
                return (f"rgb({int(c0[0]+(c1[0]-c0[0])*a)},"
                        f"{int(c0[1]+(c1[1]-c0[1])*a)},"
                        f"{int(c0[2]+(c1[2]-c0[2])*a)})")
        return "rgb(255,59,48)"

    tmp = [float(v) for v in edf["homerseklet"].tolist()]
    t_lo, t_hi = min(tmp), max(tmp)
    t_ter = max(t_hi - t_lo, 1.0)
    iy = t_lo - t_ter*0.42
    t_also = t_lo - t_ter*0.62
    t_felso = t_hi + t_ter*0.30

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.08, row_heights=[0.62, 0.38])

    # --- napszak-zónák a tényleges órákból, összefüggő szakaszokra vonva ---
    i = 0
    while i < N:
        nev, bg, fg = _zona(dtok[i].hour)
        j = i
        while j+1 < N and _zona(dtok[j+1].hour)[0] == nev:
            j += 1
        fig.add_vrect(x0=i-0.5, x1=j+0.5, fillcolor=bg, opacity=0.5,
                      line_width=0, layer="below", row=1, col=1)
        if j - i >= 1:
            fig.add_annotation(x=(i+j)/2, y=1.03, yref="y domain", yanchor="bottom",
                text=nev, showarrow=False, font=dict(size=10, color=fg), row=1, col=1)
        i = j + 1

    # --- fogyasztás: neon-izzás, szakaszonként színezve ---
    for k in range(N-1):
        szin = terheles_szin((y[k]+y[k+1])/2)
        for w,op in [(12,0.05),(8,0.09),(5,0.15)]:
            fig.add_trace(go.Scatter(x=[k,k+1],y=[y[k],y[k+1]],mode="lines",
                line=dict(color=szin,width=w),opacity=op,
                hoverinfo="skip",showlegend=False), row=1, col=1)
        fig.add_trace(go.Scatter(x=[k,k+1],y=[y[k],y[k+1]],mode="lines",
            line=dict(color=szin,width=2.6),hoverinfo="skip",showlegend=False), row=1, col=1)

    fig.add_trace(go.Scatter(x=x,y=y,mode="markers",
        marker=dict(size=10,color="rgba(0,0,0,0)"),
        customdata=[cimke(t) for t in dtok],
        hovertemplate="%{customdata}<br>%{y:,.0f} MWh<extra></extra>",
        showlegend=False), row=1, col=1)

    i_max = int(np.argmax(y)); i_min = int(np.argmin(y))

    def _nyil(i):
        """A buborék a vászon belseje felé nyíljon, különben levágja a szél."""
        if i < N * 0.30:  return 74
        if i > N * 0.70:  return -74
        return -74

    fig.add_trace(go.Scatter(x=[i_max],y=[y[i_max]],mode="markers",
        marker=dict(size=9,color=C['wh'],line=dict(width=2,color="#ff3b30")),
        hoverinfo="skip",showlegend=False), row=1, col=1)
    fig.add_annotation(x=i_max,y=y[i_max],
        text=f"<b>Csúcs: {cimke(dtok[i_max])} — {y[i_max]:,.0f} MWh</b>".replace(","," "),
        showarrow=True,arrowhead=0,ax=_nyil(i_max),ay=-30,bgcolor="#0f1d31",
        bordercolor="#ff3b30",borderwidth=1,borderpad=6,
        font=dict(color=C['wh'],size=11), row=1, col=1)
    fig.add_trace(go.Scatter(x=[i_min],y=[y[i_min]],mode="markers",
        marker=dict(size=8,color=C['wh'],line=dict(width=2,color="#4da3ff")),
        hoverinfo="skip",showlegend=False), row=1, col=1)
    fig.add_annotation(x=i_min,y=y[i_min],
        text=f"Minimum: {cimke(dtok[i_min])} — {y[i_min]:,.0f} MWh".replace(","," "),
        showarrow=True,arrowhead=0,ax=-_nyil(i_min),ay=30,bgcolor="#0f1d31",
        bordercolor="#2a3a4c",borderwidth=1,borderpad=5,
        font=dict(color="#94a3b8",size=10), row=1, col=1)

    # --- hőmérséklet: izzó pontok, ugyanaz a stílus mint eddig ---
    for meret, opac in [(22,0.08),(15,0.14),(10,0.25)]:
        fig.add_trace(go.Scatter(x=x,y=tmp,mode="markers",
            marker=dict(size=meret,color="#4da3ff",opacity=opac),
            hoverinfo="skip",showlegend=False), row=2, col=1)
    fig.add_trace(go.Scatter(x=x,y=tmp,mode="lines",
        line=dict(color="#4da3ff",width=2),hoverinfo="skip",showlegend=False), row=2, col=1)
    fig.add_trace(go.Scatter(x=x,y=tmp,mode="markers+text",
        marker=dict(size=6,color="#bfe0ff",line=dict(width=1.4,color="#4da3ff")),
        text=[f"{tmp[k]:.0f}°" if k % 2 == 0 else "" for k in range(N)],
        textposition="top center",textfont=dict(size=10,color=C['wh']),
        customdata=[cimke(t) for t in dtok],
        hovertemplate="%{customdata}<br>%{y:.1f} °C<extra></extra>",
        showlegend=False), row=2, col=1)

    ikonok = edf["ikon"].tolist()
    fig.add_trace(go.Scatter(x=tick_i,y=[iy]*len(tick_i),mode="text",
        text=[ikonok[k] for k in tick_i],
        textfont=dict(size=18,color=["#f59e0b" if ikonok[k]=="☀" else "#94a3b8" for k in tick_i]),
        hoverinfo="skip",showlegend=False), row=2, col=1)

    if ejfel_i:
        fig.add_vline(x=ejfel_i-0.5, line=dict(color="#3c5873",width=1,dash="dash"),
                      row="all", col=1)

    fig.update_layout(paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color=C['mut'],family='Inter,sans-serif',size=10),
        margin=dict(l=52,r=22,t=30,b=28),height=400,showlegend=False,hovermode="closest")
    fig.update_xaxes(range=[-0.5,N-0.5],tickvals=tick_i,ticktext=tick_t,
        gridcolor="#101f35",color=C['mut'],zeroline=False,fixedrange=True,row=1,col=1)
    fig.update_xaxes(range=[-0.5,N-0.5],tickvals=tick_i,ticktext=tick_t,
        gridcolor="#101f35",color=C['mut'],zeroline=False,fixedrange=True,row=2,col=1)
    fig.update_yaxes(title="MWh",range=[y_also,y_felso],gridcolor="#101f35",
        color=C['mut'],zeroline=False,fixedrange=True,row=1,col=1)
    fig.update_yaxes(title="°C",range=[t_also,t_felso],gridcolor="#101f35",
        color=C['mut'],zeroline=False,fixedrange=True,row=2,col=1)

    heti = data.get("heti_atlag")
    if heti and all(v == v for v in heti):
        ref = float(np.mean([heti[t.hour] for t in dtok]))
        d = (float(np.mean(y)) / ref - 1) * 100
        irany = "magasabb" if d >= 0 else "alacsonyabb"
        kontextus = (f"Az elmúlt 7 nap azonos óráinak átlagánál ~{abs(d):.0f}%-kal {irany} "
                     f"fogyasztás várható · Delta V10 · CatBoost ML")
    else:
        kontextus = "Delta V10 · CatBoost ML"
    if N < 24:
        kontextus += f" · a horizont {N} óra — a 24 órás ablak a ~14:00-s DAM-aukció után nyílik"

    fogy_panel = html.Div([
        html.Div(f"FOGYASZTÁS ÉS HŐMÉRSÉKLET — {cim}",
            style={"fontSize":"13px","fontWeight":"700","color":C['wh']}),
        html.Div(kontextus, style={"fontSize":"11px","color":"#94a3b8",
            "marginTop":"3px","marginBottom":"2px"}),
        html.Div("A hűtési és fűtési fokszám a modell legerősebb időjárási bemenete — "
                 "a két görbe együtt mozog.",
            style={"fontSize":"10px","color":C['mut'],"marginBottom":"6px"}),
        dcc.Graph(figure=fig,config={"displayModeBar":False},style={"height":"400px"})
    ], style=CS)

    # ================================================================
    # PANEL 2 — Megújuló: JÓSLAT és MÉRT egymás mellett
    # A "most" vonaltól balra két görbe fut: amit tegnap megjósoltak,
    # és ami ténylegesen megtörtént. Jobbra csak a jóslat.
    # ================================================================
    meg = data.get("megujulo")
    if not meg:
        megujulo_panel = html.Div([
            html.Div("MEGÚJULÓ ELŐREJELZÉS",
                style={"fontSize":"13px","fontWeight":"700","color":C['wh']}),
            html.Div("A nap/szél előrejelzés jelenleg nem elérhető.",
                style={"fontSize":"11px","color":C['mut'],"marginTop":"16px"})
        ], style=CS)
    else:
        megujulo_panel = _megujulo_panel(meg, dtok, cim)

    return html.Div([
        html.Div("Energiaelemzés", style={"fontSize":"16px","fontWeight":"600",
            "color":C['wh'],"marginBottom":"14px"}),
        dbc.Row([dbc.Col(fogy_panel, md=12)], className="g-3 mb-3"),
        dbc.Row([
            dbc.Col(_heti_naplo_panel(data.get("validacio")), md=12),
        ], className="g-3")
    ])


def _megujulo_panel(meg, dtok, cim):
    """Nap felül, szél alul, KÖZÖS óratengelyen.

    A két y-tengely (nap balra, szél jobbra) átláthatatlan volt: a cián
    szélgörbe átvágott az arany napkupolán, és semmi nem mondta meg,
    melyik görbe melyik skálán fut. Külön sávban ez a kérdés fel sem merül.

    Szaggatott = jóslat. Telt, izzó = mért. A 'most' vonaltól jobbra
    csak jóslat van, ezt a háttér is jelzi.
    """
    NAP = "#f59e0b"; NAP_TENY = "#ffd75e"
    SZEL = "#4dd0e1"; SZEL_TENY = "#a9f4ff"

    mdtok = [datetime.fromisoformat(s) for s in meg["ido"]]
    M = len(mdtok)
    mx = list(range(M))
    mtick = list(range(0, M, 3))
    mtick_t = [f"{mdtok[i]:%H:%M}" for i in mtick]

    def cimke(dt): return f"{_nap_elotag(dt)}{dt:%H:%M}"

    f_nap = meg["fc_nap"]; f_szel = meg["fc_szel"]
    t_nap = meg["tny_nap"]; t_szel = meg["tny_szel"]
    mert_ig = max([i for i,v in enumerate(t_nap) if v is not None], default=-1)

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.10, row_heights=[0.55, 0.45])

    for sor, (fc, tny, szin, szin_t) in enumerate(
            [(f_nap, t_nap, NAP, NAP_TENY), (f_szel, t_szel, SZEL, SZEL_TENY)], start=1):

        # jóslat: halvány szaggatott, finom kitöltéssel
        fig.add_trace(go.Scatter(x=mx, y=fc, mode="lines",
            line=dict(color=szin, width=1.6, dash="dot"), opacity=0.8,
            fill="tozeroy", fillcolor=f"rgba({int(szin[1:3],16)},{int(szin[3:5],16)},{int(szin[5:7],16)},0.06)",
            customdata=[cimke(t) for t in mdtok],
            hovertemplate="%{customdata}<br>%{y:,.0f} MW<extra>jóslat</extra>",
            showlegend=False), row=sor, col=1)

        # mért: telt vonal, izzó réteggel
        if mert_ig >= 0:
            xs = mx[:mert_ig+1]; ys = tny[:mert_ig+1]
            for r_, o_ in [(12, 0.07), (7, 0.14)]:
                fig.add_trace(go.Scatter(x=xs, y=ys, mode="markers",
                    marker=dict(size=r_, color=szin_t, opacity=o_),
                    hoverinfo="skip", showlegend=False), row=sor, col=1)
            fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines",
                line=dict(color=szin_t, width=2.6),
                customdata=[cimke(t) for t in mdtok[:mert_ig+1]],
                hovertemplate="%{customdata}<br>%{y:,.0f} MW<extra>mért</extra>",
                showlegend=False), row=sor, col=1)

    nap_top = max(max(f_nap), max([v for v in t_nap if v is not None], default=0)) * 1.18 or 1.0
    szel_top = max(max(f_szel), max([v for v in t_szel if v is not None], default=0)) * 1.40 or 1.0

    # a jövő félhomályban — nincs mihez hasonlítani
    if 0 <= mert_ig < M-1:
        fig.add_vrect(x0=mert_ig+0.5, x1=M-0.5, fillcolor="#0b1829", opacity=0.30,
                      line_width=0, layer="below", row="all", col=1)
        fig.add_vline(x=mert_ig+0.5, line=dict(color=C['wh'], width=1, dash="dot"),
                      row="all", col=1)
        fig.add_annotation(x=mert_ig*0.5, y=1.06, yref="y domain", yanchor="bottom",
            text="mért · szaggatott a jóslat", showarrow=False,
            font=dict(size=9, color="#94a3b8"), row=1, col=1)
        fig.add_annotation(x=(mert_ig+M)/2, y=1.06, yref="y domain", yanchor="bottom",
            text="csak jóslat", showarrow=False,
            font=dict(size=9, color="#64748b"), row=1, col=1)

    # éjfél
    ej = next((i for i in range(1,M) if mdtok[i].hour == 0), None)
    if ej:
        fig.add_vline(x=ej-0.5, line=dict(color="#3c5873", width=1, dash="dash"),
                      row="all", col=1)

    # turbina-motívum a szél sávban, két ponton
    pole_h = szel_top * 0.10; blade_h = pole_h * 0.75; bdx = M * 0.011
    for k in [i for i in (M//4, 3*M//4) if 0 <= i < M]:
        alap = f_szel[k] + pole_h * 0.35; hub = alap + pole_h
        for x0,x1,y0,y1 in [(k,k,alap,hub), (k,k,hub,hub+blade_h),
                            (k,k+bdx,hub,hub-blade_h*0.5), (k,k-bdx,hub,hub-blade_h*0.5)]:
            fig.add_shape(type="line", x0=x0, x1=x1, y0=y0, y1=y1,
                line=dict(color="#a9ecf5", width=1), row=2, col=1)

    fig.update_layout(paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color=C['mut'], family='Inter,sans-serif', size=10),
        margin=dict(l=52, r=18, t=30, b=28), height=320, showlegend=False,
        hovermode="closest")
    for rr in (1, 2):
        fig.update_xaxes(range=[-0.5, M-0.5], tickvals=mtick, ticktext=mtick_t,
            gridcolor="#101f35", color=C['mut'], zeroline=False, fixedrange=True,
            row=rr, col=1)
    fig.update_yaxes(title_text="Nap (MW)", range=[0, nap_top], gridcolor="#101f35",
        color=NAP, zeroline=False, fixedrange=True, row=1, col=1)
    fig.update_yaxes(title_text="Szél (MW)", range=[0, szel_top], gridcolor="#101f35",
        color=SZEL, zeroline=False, fixedrange=True, row=2, col=1)

    # ---------------- jobb oldali összesítő ----------------
    def _hibakartya(nev, h, szin, ikon):
        if not h:
            return html.Div([
                html.Div([
                    html.Span(ikon, style={"color":szin,"fontSize":"12px","marginRight":"6px"}),
                    html.Span(f"{nev} — mai eltérés", style={"fontSize":"8px","color":C['mut'],
                        "textTransform":"uppercase","letterSpacing":"0.03em"})
                ], style={"display":"flex","alignItems":"center"}),
                html.Div("nincs elég mért óra", style={"fontSize":"11px",
                    "color":C['mut'],"marginTop":"3px"})
            ], style={"background":C['card2'],"borderRadius":"8px","padding":"9px","marginBottom":"5px"})
        rel = f" ({h['rel']:.0f}%)" if h.get("rel") else ""
        irany = "felülbecsült" if h["bias"] > 0 else "alulbecsült"
        return html.Div([
            html.Div([
                html.Span(ikon, style={"color":szin,"fontSize":"12px","marginRight":"6px"}),
                html.Span(f"{nev} — mai eltérés", style={"fontSize":"8px","color":C['mut'],
                    "textTransform":"uppercase","letterSpacing":"0.03em"})
            ], style={"display":"flex","alignItems":"center"}),
            html.Div([
                html.Span(f"{h['mae']:,.0f}".replace(","," "),
                    style={"fontSize":"14px","fontWeight":"600","color":szin}),
                html.Span(f" MW{rel}", style={"fontSize":"9px","color":C['mut']})
            ], style={"marginTop":"2px"}),
            html.Div(f"{h['orak']} mért óra · átlagban {irany}",
                style={"fontSize":"9px","color":C['mut'],"marginTop":"2px"})
        ], style={"background":C['card2'],"borderRadius":"8px","padding":"9px","marginBottom":"5px"})

    def _sor(nev, ertek, szin, ikon):
        return html.Div([
            html.Span([html.Span(ikon, style={"color":szin,"marginRight":"5px"}), nev],
                style={"fontSize":"10px","color":C['mut']}),
            html.Span(ertek, style={"fontSize":"10px","color":szin,"fontWeight":"500"})
        ], style={"display":"flex","justifyContent":"space-between","padding":"4px 2px"})

    # Az összeg CSAK a hátralévő, még be nem következett órákra — ÉS CSAK A MAI
    # NAPRA.
    #
    # Itt volt a hiba: `M` az egész sorozat hossza, ami 14:00 után átnyúlik a
    # HOLNAPI napra (a célablak addig tart). Az összeg így a mai maradékot és a
    # holnapi teljes nappalt is tartalmazta, vagyis nagyjából másfél napot —
    # 35 752 MWh jött ki napelemre, holott egy magyar nyári nap teljes termelése
    # 20-25 ezer MWh. A felirat közben "a hátralévő órákra" volt, ami a mai
    # napot ígéri.
    ma_datum = _helyi_most().date()
    ma_utolso = max((i for i, d in enumerate(mdtok) if d.date() == ma_datum),
                    default=M - 1)
    holnap_idx = [i for i, d in enumerate(mdtok) if d.date() > ma_datum]

    jovo = slice(mert_ig + 1, ma_utolso + 1)
    jovo_orak = max(0, ma_utolso - mert_ig)

    if jovo_orak > 0:
        ossz_cimke = f"Várható termelés a mai hátralévő {jovo_orak} órára (MWh)"
    elif holnap_idx:
        # A mai nap lezárult. Nulla helyett a holnapi tervet mutatjuk — de
        # egyértelműen holnapiként megnevezve, nem "hátralévőként".
        jovo = slice(holnap_idx[0], holnap_idx[-1] + 1)
        ossz_cimke = "Várható termelés holnapra (MWh)"
    else:
        ossz_cimke = "Várható termelés a hátralévő órákra (MWh)"

    jn = float(np.sum(f_nap[jovo])); jsz = float(np.sum(f_szel[jovo]))

    def _sav_txt(ossz, kulcs):
        # Este a napelemes összeg jogosan nulla, de a "0 – 0" úgy néz ki, mintha
        # elromlott volna valami. Kimondjuk az okát.
        if ossz < 1:
            return "≈ 0 · lement a nap" if kulcs == "nap" else "≈ 0"
        lo, hi = FC_SAV[kulcs]
        return f"{ossz*(1+lo):,.0f} – {ossz*(1+hi):,.0f}".replace(","," ")

    # A jósolt csúcs ugyanabból az ablakból, mint az összeg — különben a
    # kártya két sora két különböző időszakról beszélne.
    _kezd = jovo.start or 0
    if (jovo.stop or 0) > _kezd:
        nap_cs = int(np.argmax(f_nap[jovo])) + _kezd
        szel_cs = int(np.argmax(f_szel[jovo])) + _kezd
    else:
        nap_cs = int(np.argmax(f_nap)); szel_cs = int(np.argmax(f_szel))

    osszesito = html.Div([
        html.Div("A JÓSLAT PONTOSSÁGA MA", style={"fontSize":"11px","fontWeight":"500",
            "color":C['wh'],"marginBottom":"10px"}),
        _hibakartya("Nap", meg.get("hiba_nap"), NAP_TENY, "☀"),
        _hibakartya("Szél", meg.get("hiba_szel"), SZEL_TENY, "◇"),
        html.Div(style={"borderTop":f"1px solid {C['brd']}","marginTop":"6px","marginBottom":"6px"}),
        html.Div(f"Jósolt csúcs a hátralévő {jovo_orak} órában",
            style={"fontSize":"9px","color":C['mut'],"marginBottom":"3px"}),
        _sor("Nap", f"{cimke(mdtok[nap_cs])} · {f_nap[nap_cs]:,.0f} MW".replace(","," "), NAP, "☀"),
        _sor("Szél", f"{cimke(mdtok[szel_cs])} · {f_szel[szel_cs]:,.0f} MW".replace(","," "), SZEL, "◇"),
        html.Div(style={"borderTop":f"1px solid {C['brd']}","marginTop":"6px","marginBottom":"6px"}),
        html.Div(ossz_cimke,
            style={"fontSize":"9px","color":C['mut'],"marginBottom":"3px"}),
        _sor("Nap", _sav_txt(jn, "nap"), NAP, "☀"),
        _sor("Szél", _sav_txt(jsz, "szel"), SZEL, "◇"),
        html.Div("Tartomány, nem pontszám: a napok 80%-a ebbe a sávba esett "
                 "(8646 óra, 2025–2026).",
            style={"fontSize":"8px","color":C['mut'],"marginTop":"6px","lineHeight":"1.35"})
    ], style={"paddingLeft":"4px"})

    mert_txt = ("" if meg.get("mert_ok")
                else " · a mért termelés jelenleg nem elérhető, csak a jóslat látszik")

    return html.Div([
        html.Div("MEGÚJULÓ ELŐREJELZÉS", style={"fontSize":"13px","fontWeight":"700","color":C['wh']}),
        html.Div(f"ENTSO-E napelőtti jóslat vs. mért termelés{mert_txt}",
            style={"fontSize":"11px","color":"#94a3b8","marginTop":"3px"}),
        html.Div(f"Történelmi pontosság: nap ±{FC_MAE['nap']}%, szél ±{FC_MAE['szel']}% "
                 f"(órás MAE az átlagos termelés arányában). A csúcsórákban mindkettő alulbecsül.",
            style={"fontSize":"10px","color":C['mut'],"marginTop":"2px"}),
        dbc.Row([
            dbc.Col(dcc.Graph(figure=fig, config={"displayModeBar":False},
                style={"height":"320px"}), lg=8, md=12),
            dbc.Col(osszesito, lg=4, md=12),
        ], className="g-3", style={"marginTop":"12px"})
    ], style=CS)


KAT_SZIN = {k: sz for k, _, sz, _ in KAT_META}
KAT_NEV = {k: n for k, n, _, _ in KAT_META}
KAT_KEP = {k: kp for k, _, _, kp in KAT_META}


def _ts_naiv(iso):
    """A napló ISO időbélyege → időzóna nélküli datetime, vagy None."""
    try:
        t = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    return t.replace(tzinfo=None) if t.tzinfo else t


def _reziduum_panel(stl, naplo):
    """Az STL reziduuma az utolsó 7 napon, ±2,5σ küszöbbel. A küszöböt
    átlépő órák a kategóriájuk színét kapják — így a grafikon és a napló
    ugyanazt a nyelvet beszéli."""
    if not stl or not stl.get("idok"):
        return hianyzo_panel("REZIDUUM ÉS ANOMÁLIÁK",
            "Kevés élő mérési adat az elemzéshez.")
    idok = [datetime.fromisoformat(t) for t in stl["idok"]]
    resid = [float(x) for x in stl["residual"]]
    n7 = 7 * 24
    idok = idok[-n7:]; resid = resid[-n7:]
    stat = stl["stat"]; kuszob = float(stat["kuszob"]); atlag = float(stat["mean"])

    # A naplóban szereplő órák — időbélyeg szerint. A napló a FELISMERÉS
    # pillanatában mért reziduumot is tárolja; ez a hiteles érték, mert az
    # akkori küszöbhöz mérve lépte át a határt.
    naplo_map = {}
    for r in (naplo or []):
        t_ = _ts_naiv(r.get("ido"))
        if t_ is not None:
            naplo_map[t_] = r

    fig = go.Figure()
    fig.add_hline(y=atlag + kuszob, line=dict(color=C['rd'], width=1, dash="dash"))
    fig.add_hline(y=atlag - kuszob, line=dict(color=C['rd'], width=1, dash="dash"))
    fig.add_annotation(x=idok[0], y=atlag + kuszob, text="+2,5σ", showarrow=False,
        yanchor="bottom", xanchor="left", font=dict(size=9, color=C['rd']))
    fig.add_annotation(x=idok[0], y=atlag - kuszob, text="−2,5σ", showarrow=False,
        yanchor="top", xanchor="left", font=dict(size=9, color=C['rd']))
    fig.add_trace(go.Scatter(x=idok, y=resid, mode="lines",
        line=dict(color="#4b9cd3", width=1.5),
        hovertemplate="%{x|%m.%d. %H:%M}<br>%{y:+,.0f} MWh<extra>reziduum</extra>",
        showlegend=False))

    # A pontok a NAPLÓBÓL jönnek, nem újraszámolt küszöbpróbából.
    #
    # Korábban a grafikon maga döntötte el, mi számít anomáliának: minden
    # betöltéskor újrafuttatta az `|reziduum - átlag| > küszöb` próbát a FRISSEN
    # számolt STL-en. Csakhogy az STL gördülő felbontás — ahogy új órák
    # érkeznek, a trend és a napi ritmus újraillesztődik, és ugyanannak az
    # órának megváltozik a maradéka. Emiatt a naplóban szereplő, egyszer már
    # felismert órák a grafikonról eltűntek (velük a jelmagyarázat is), és a
    # két nézet ellentmondott egymásnak — pedig a panel alcíme épp azt ígéri,
    # hogy ugyanazt a nyelvet beszélik.
    #
    # Mostantól a napló a hiteles forrás: azokat az órákat jelöljük, amelyeket
    # a rendszer annak idején — az AKKORI küszöbhöz mérve — anomáliának ismert
    # fel, és a felismeréskor mért reziduummal rajzoljuk ki őket.
    # A pont a FELISMERÉSKOR mért reziduumon ül — ezt az értéket NEM számoljuk
    # újra.
    #
    # Az STL gördülő felbontás: minden oldalbetöltéskor újrailleszti a trendet
    # és a napi ritmust a legutóbbi ~408 órára. Egy óra maradéka ettől
    # megváltozik — ami tegnap este -730 MWh volt, ma ugyanarra az órára +300.
    # Emiatt a korábban felismert anomáliák előbb eltűntek a grafikonról, majd
    # amikor a mai görbére tettem őket, a küszöbön BELÜLRE kerültek, holott az
    # alcím azt ígéri, hogy átlépik. Mindkettő félrevezető volt.
    #
    # Ami egy adott napon anomália volt, az anomália is marad: a napló őrzi az
    # akkor mért értéket, és a grafikon ezt mutatja. Így a hét kiugrásai
    # együtt, változatlanul látszanak.
    csoportok = {}
    ablak_kezd, ablak_veg = idok[0], idok[-1]
    for t_, r in naplo_map.items():
        if not (ablak_kezd <= t_ <= ablak_veg):
            continue                      # kívül esik a mutatott 7 napon
        try:
            y_ = float(r.get("residual"))
        except (TypeError, ValueError):
            continue
        kat = r.get("kategoria")
        cs = csoportok.setdefault(kat, {"x": [], "y": []})
        cs["x"].append(t_); cs["y"].append(y_)
    for kat, pontok in csoportok.items():
        szin = KAT_SZIN.get(kat, "#94a3b8")
        nev = KAT_NEV.get(kat, "besorolás előtti")
        fig.add_trace(go.Scatter(x=pontok["x"], y=pontok["y"], mode="markers",
            name=nev, marker=dict(size=8, color=szin,
                line=dict(width=1, color="rgba(255,255,255,.6)")),
            hovertemplate="%{x|%m.%d. %H:%M}<br>%{y:+,.0f} MWh"
                          "<extra>" + nev + "</extra>"))

    fig.update_layout(paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color=C['mut'], family='Inter,sans-serif', size=10),
        margin=dict(l=48, r=16, t=8, b=26), height=290,
        legend=dict(orientation="h", yanchor="top", y=-0.18,
            bgcolor="rgba(0,0,0,0)", font=dict(size=10, color=C['txt'])),
        xaxis=dict(type="date", gridcolor="#101f35", color=C['mut'],
            tickformat="%m.%d", fixedrange=True),
        yaxis=dict(title="MWh", gridcolor="#101f35", color=C['mut'],
            zeroline=False, fixedrange=True))

    return html.Div([
        html.Div("REZIDUUM ÉS ANOMÁLIÁK — UTOLSÓ 7 NAP",
            style={"fontSize":"13px","fontWeight":"700","color":C['wh']}),
        html.Div("Mért fogyasztás mínusz trend és napi ritmus · a színes pontok a "
                 "±2,5σ küszöböt átlépő, osztályozott órák a felismerés napján "
                 "mért értékükkel",
            style={"fontSize":"11px","color":"#94a3b8","margin":"3px 0 8px"}),
        dcc.Graph(figure=fig, config={"displayModeBar": False}, style={"height":"320px"})
    ], style=CS)


def _anomalia_naplo_panel(naplo):
    cim = [html.Div("ANOMÁLIA-NAPLÓ",
               style={"fontSize":"13px","fontWeight":"700","color":C['wh']}),
           html.Div("Utolsó 7 nap · legfrissebb elöl · minden eset tartósan tárolva",
               style={"fontSize":"11px","color":"#94a3b8","margin":"3px 0 12px"})]
    if not naplo:
        return html.Div([*cim,
            html.Div("Nincs jelzett óra az elmúlt 7 napban — a bemenő adat a "
                     "megszokott sávban mozog.",
                style={"fontSize":"11px","color":C['gr']})], style=CS)
    sorok = []
    for r in naplo[:9]:
        dt = datetime.fromisoformat(r["ido"])
        kat = r.get("kategoria")
        szin = KAT_SZIN.get(kat, "#94a3b8")
        nev = KAT_NEV.get(kat, "besorolás előtti")
        elojel_szin = "#f59e0b" if r["residual"] > 0 else "#4da3ff"
        ho = (f"{r['homerseklet']:.0f} °C · "
              if r.get("homerseklet") is not None else "")
        if kat in KAT_KEP:
            ikon = kep(KAT_KEP[kat], nev,
                style={"width":"34px","height":"28px","objectFit":"contain",
                       "flex":"0 0 auto"})
        else:
            ikon = html.Div("STL", style={"width":"34px","height":"28px",
                "flex":"0 0 auto","display":"flex","alignItems":"center",
                "justifyContent":"center","fontSize":"8px","color":C['mut'],
                "background":"#141e2e","borderRadius":"6px"})
        sorok.append(html.Div([ikon,
            html.Div([
                html.Div([f"{dt:%m.%d}. {HETNAP[dt.weekday()]} {dt:%H:%M} · ",
                    html.Span(f"{r['residual']:+,.0f} MWh".replace(","," "),
                        style={"color":elojel_szin,"fontWeight":"600"})],
                    style={"fontSize":"11px","color":C['wh']}),
                html.Div(f"{ho}{nev}", style={"fontSize":"9px","color":C['mut'],
                    "marginTop":"1px"}),
            ], style={"flex":"1","minWidth":"0"}),
        ], style={"display":"flex","alignItems":"center","gap":"10px",
            "background":C['card2'],"borderLeft":f"3px solid {szin}",
            "borderRadius":"8px","padding":"7px 10px","marginBottom":"5px"}))
    if len(naplo) > 9:
        sorok.append(html.Div(f"… és további {len(naplo)-9} eset a 7 napból",
            style={"fontSize":"10px","color":C['mut'],"marginTop":"6px"}))
    return html.Div([*cim, *sorok], style=CS)


def _stl_ador_nagy(naplo, kategoriak):
    """A 3. oldal alsó, kinagyított mérlege: négy kategória-csempe,
    az elmúlt 7 nap darabszámaival és a teljes történeti összesítéssel."""
    naplo = naplo or []
    friss = {}
    for r in naplo:
        k = r.get("kategoria") or "besorolatlan"
        friss[k] = friss.get(k, 0) + 1
    osszes = kategoriak or {}
    csempek = []
    for kulcs, nev, szin, kepfajl in KAT_META:
        csempek.append(html.Div([
            kep(kepfajl, nev, style={"width":"84px",
                "height":"66px","objectFit":"contain","margin":"0 auto 8px",
                "display":"block"}),
            html.Div(str(friss.get(kulcs, 0)), style={"fontSize":"30px",
                "fontWeight":"600","color":szin,"textAlign":"center",
                "lineHeight":"1"}),
            html.Div(nev, style={"fontSize":"12px","color":C['txt'],
                "textAlign":"center","marginTop":"5px"}),
            html.Div(f"összesen: {osszes.get(kulcs, 0)}", style={"fontSize":"9px",
                "color":C['mut'],"textAlign":"center","marginTop":"2px"}),
        ], style={"background":C['card2'],"border":f"1px solid {_rgba(szin,.3)}",
            "borderRadius":"12px","padding":"16px 12px"}))
    nyitott = friss.get("rejtely", 0)
    besorolatlan = friss.get("besorolatlan", 0)
    lab = f"{len(naplo)} jelzés az elmúlt 7 napban · {nyitott} vizsgálat alatt"
    if besorolatlan:
        lab += f" · {besorolatlan} a kontextusgyűjtés indulása előttről"
    lab += " — az osztályozás a következő tanítás mintasúlyait alapozza meg"
    return html.Div([
        html.Div("STL ADATŐR", style={"fontSize":"14px","fontWeight":"700",
            "color":C['wh']}),
        html.Div("Ok szerinti mérleg az elmúlt 7 nap jelzéseiről",
            style={"fontSize":"11px","color":"#94a3b8","margin":"3px 0 14px"}),
        html.Div(csempek, style={"display":"grid",
            "gridTemplateColumns":"repeat(auto-fit, minmax(170px, 1fr))",
            "gap":"10px"}),
        html.Div(lab, style={"fontSize":"10px","color":C['mut'],
            "borderTop":f"1px solid {C['brd']}","paddingTop":"9px",
            "marginTop":"12px"}),
    ], style=CS)


def _ar_megujulo_panel(meg, negyed):
    """Az összekötő kártya: a mai mért megújuló termelés és a DAM-ár
    egy tengelypárban. A felirat nem tankönyv, hanem az AZNAPI tényadat
    dinamikus összefoglalója. A negatív árú órák zöld pöttyként ülnek
    a termelési görbén."""
    cim = html.Div("MEGÚJULÓK ÉS A MAI ÁRAK",
        style={"fontSize":"13px","fontWeight":"700","color":C['wh']})
    if not meg or not negyed or not negyed.get("ido"):
        return html.Div([cim,
            html.Div("A panelhez a megújuló termelés és a mai árak együtt kellenek — "
                     "az egyik forrás jelenleg nem elérhető.",
                style={"fontSize":"11px","color":C['mut'],"marginTop":"14px"})], style=CS)

    most = _helyi_most()
    ma = most.date()
    mdtok = [datetime.fromisoformat(s) for s in meg["ido"]]
    fc = [float(n) + float(s) for n, s in zip(meg["fc_nap"], meg["fc_szel"])]
    tny = [(float(a) + float(b)) if (a is not None and b is not None) else None
           for a, b in zip(meg["tny_nap"], meg["tny_szel"])]
    mert_ig = max([i for i, v in enumerate(tny) if v is not None], default=-1)

    # órás átlagár a negyedórás sorból
    ar_ora = {}
    for ts, a in zip(negyed["ido"], negyed["ar"]):
        t = datetime.fromisoformat(ts).replace(minute=0, second=0, microsecond=0)
        ar_ora.setdefault(t, []).append(float(a))
    ar_ora = {k: float(np.mean(v)) for k, v in ar_ora.items()}
    arak_sor = [ar_ora.get(t) for t in mdtok]

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=mdtok, y=fc, mode="lines", name="Megújuló — jóslat",
        line=dict(color=C['gr'], width=1.6, dash="dot"), opacity=0.7,
        hovertemplate="%{x|%H:%M}<br>%{y:,.0f} MW<extra>jóslat</extra>"),
        secondary_y=False)
    if mert_ig >= 0:
        fig.add_trace(go.Scatter(x=mdtok[:mert_ig+1], y=tny[:mert_ig+1],
            mode="lines", name="Megújuló — mért",
            line=dict(color=C['gr'], width=2.6),
            hovertemplate="%{x|%H:%M}<br>%{y:,.0f} MW<extra>mért</extra>"),
            secondary_y=False)
    fig.add_trace(go.Scatter(x=mdtok, y=arak_sor, mode="lines", name="DAM ár",
        line=dict(color="#ff9800", width=2.2),
        hovertemplate="%{x|%H:%M}<br>%{y:.0f} €/MWh<extra>ár</extra>"),
        secondary_y=True)

    neg_x, neg_y = [], []
    for i, t in enumerate(mdtok):
        a = arak_sor[i]
        if a is not None and a < 0:
            neg_x.append(t)
            neg_y.append(tny[i] if (i <= mert_ig and tny[i] is not None) else fc[i])
    if neg_x:
        fig.add_trace(go.Scatter(x=neg_x, y=neg_y, mode="markers",
            name="Negatív árú óra",
            marker=dict(size=10, color=C['gr'],
                line=dict(width=2, color="rgba(255,255,255,.75)")),
            hovertemplate="%{x|%H:%M}<br>negatív ár<extra></extra>"),
            secondary_y=False)

    fig.add_shape(type="line", x0=most, x1=most, y0=0, y1=1, yref="paper",
        line=dict(color=C['wh'], width=1, dash="dot"))

    fig.update_layout(paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color=C['mut'], family='Inter,sans-serif', size=10),
        margin=dict(l=52, r=52, t=10, b=28), height=290,
        legend=dict(orientation="h", yanchor="top", y=-0.16,
            bgcolor="rgba(0,0,0,0)", font=dict(size=10, color=C['txt'])),
        hovermode="closest",
        xaxis=dict(type="date", gridcolor="#101f35", color=C['mut'],
            tickformat="%H:%M", fixedrange=True))
    fig.update_yaxes(title_text="MW", secondary_y=False, gridcolor="#101f35",
        color=C['gr'], zeroline=False, fixedrange=True, rangemode="tozero")
    fig.update_yaxes(title_text="€/MWh", secondary_y=True, showgrid=False,
        color="#ff9800", zeroline=False, fixedrange=True)

    # ---- dinamikus alcím + jobb oldali számkártyák, csak tényadatból ----
    zart = [(tny[i], arak_sor[i]) for i in range(mert_ig + 1)
            if tny[i] is not None and arak_sor[i] is not None]
    korr = None
    if len(zart) >= 6:
        m_ = [z[0] for z in zart]; a_ = [z[1] for z in zart]
        if float(np.std(m_)) > 1 and float(np.std(a_)) > 0.1:
            korr = float(np.corrcoef(m_, a_)[0, 1])

    if mert_ig >= 0:
        cs_i = int(np.argmax([v if v is not None else -1 for v in tny[:mert_ig+1]]))
        csucs_txt = f"{mdtok[cs_i]:%H:%M} · {tny[cs_i]:,.0f} MW".replace(",", " ")
        csucs_cimke = "Mért megújuló-csúcs eddig"
    else:
        cs_i = int(np.argmax(fc))
        csucs_txt = f"{mdtok[cs_i]:%H:%M} · {fc[cs_i]:,.0f} MW".replace(",", " ")
        csucs_cimke = "Várható megújuló-csúcs (jóslat)"

    mai_arak = [(t, a) for t, a in ar_ora.items() if t.date() == ma]
    armin = None
    if mai_arak:
        armin_t, armin = min(mai_arak, key=lambda x: x[1])
        armin_txt = f"{armin_t:%H:%M} · {armin:.0f} €/MWh"
    else:
        armin_txt = "–"

    mai_neg = [(datetime.fromisoformat(ts), float(a))
               for ts, a in zip(negyed["ido"], negyed["ar"])
               if datetime.fromisoformat(ts).date() == ma and float(a) < 0]
    if mai_neg:
        neg_txt = f"{len(mai_neg)} negyedóra"
        neg_sub = (f"{min(t for t, _ in mai_neg):%H:%M} – "
                   f"{max(t for t, _ in mai_neg):%H:%M} között")
    else:
        neg_txt = "0"
        neg_sub = "ma nem volt negatív ár"

    if mert_ig >= 0 and mai_arak:
        alcim = (f"Ma {mdtok[cs_i]:%H:%M}-kor volt eddig a legmagasabb mért nap+szél "
                 f"termelés — a nap legolcsóbb órája {armin_t:%H:%M}-kor: "
                 f"{armin:.0f} €/MWh.")
    else:
        alcim = ("A mért termelés beérkezéséig a napelőtti jóslat látszik — "
                 "a tényadatok óránként frissülnek.")

    def _szamkartya(felirat, ertek, sub, szin):
        return html.Div([
            html.Div(felirat, style={"fontSize":"9px","color":C['mut'],
                "textTransform":"uppercase"}),
            html.Div(ertek, style={"fontSize":"16px","fontWeight":"600",
                "color":szin,"marginTop":"3px"}),
            html.Div(sub, style={"fontSize":"9px","color":"#94a3b8","marginTop":"2px"}),
        ], style={"background":C['card2'],"borderRadius":"8px","padding":"10px",
                  "marginBottom":"8px"})

    if korr is not None:
        korr_kartya = _szamkartya("Mai együttmozgás (mért órákon)", f"{korr:+.2f}",
            "több megújuló → olcsóbb óra" if korr < -0.3 else "ma gyenge az együttmozgás",
            C['gr'] if korr < -0.3 else C['yw'])
    else:
        korr_kartya = _szamkartya("Mai együttmozgás", "gyűjtés alatt",
            "legalább 6 mért óra kell hozzá", C['mut'])

    return html.Div([cim,
        html.Div(alcim, style={"fontSize":"11px","color":"#94a3b8","margin":"3px 0 12px"}),
        dbc.Row([
            dbc.Col(dcc.Graph(figure=fig, config={"displayModeBar": False},
                style={"height":"310px"}), lg=8, md=12),
            dbc.Col([korr_kartya,
                _szamkartya(csucs_cimke, csucs_txt, "nap + szél együtt", C['gr']),
                _szamkartya("A nap legolcsóbb órája", armin_txt, "DAM órás átlag", "#ff9800"),
                _szamkartya("Negatív árú idő ma", neg_txt, neg_sub, C['gr']),
            ], lg=4, md=12),
        ], className="g-3"),
        html.Div("Háttér: az alacsony költségű nap- és széltermelés kiszorítja a "
                 "drágább erőműveket a napelőtti aukción — ezért mozog a két görbe "
                 "jellemzően ellentétesen. Ez köti össze az időjárást, a termelést, "
                 "az árat és a főoldali töltési döntést.",
            style={"fontSize":"10px","color":C['mut'],
                   "borderTop":f"1px solid {C['brd']}","paddingTop":"9px",
                   "marginTop":"10px"}),
    ], style=CS)


def megujulok(data):
    meg = data.get("megujulo")
    if meg:
        felso = _megujulo_panel(meg, [], "")
    else:
        felso = hianyzo_panel("MEGÚJULÓ TERMELÉS",
            "A nap/szél előrejelzés jelenleg nem elérhető.")
    return html.Div([
        html.Div("Megújuló energia", style={"fontSize":"16px","fontWeight":"600",
            "color":C['wh'],"marginBottom":"14px"}),
        dbc.Row([dbc.Col(felso, md=12)], className="g-3 mb-3"),
        dbc.Row([dbc.Col(_ar_megujulo_panel(meg, data.get("negyed")), md=12)],
                className="g-3"),
    ])


def mllabor(data):
    v = data.get("validacio") or {}
    naplo = v.get("naplo") or []
    return html.Div([
        html.Div("Gépi Tanulás Modell Labor", style={"fontSize":"16px",
            "fontWeight":"600","color":C['wh'],"marginBottom":"14px"}),
        dbc.Row([
            dbc.Col(_reziduum_panel(data.get("stl"), naplo), lg=8, md=12),
            dbc.Col(_anomalia_naplo_panel(naplo), lg=4, md=12),
        ], className="g-3 mb-3"),
        dbc.Row([
            dbc.Col(_stl_ador_nagy(naplo, v.get("kategoriak")), md=12)
        ], className="g-3"),
    ])


if __name__=="__main__":
    # Lokális futtatás. Élesben (Render) a gunicorn indítja: gunicorn app:server
    port = int(os.environ.get("PORT", 8050))
    debug = os.environ.get("DASH_DEBUG", "false").lower() == "true"
    app.run(debug=debug, host="0.0.0.0", port=port)
