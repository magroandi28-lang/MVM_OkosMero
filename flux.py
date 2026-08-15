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

A KÖSZÖNÉS a szerver dolga, nem a nyelvi modellé. A promptban meg volt adva a
helyes köszönés, de a modell nem tartotta be: éjfél után is "Jó reggelt!"-tel
kezdett, és a determinisztikus ág egyáltalán nem köszönt — így a látogató
kiszámíthatatlannak élte meg. Mostantól a `_koszones_javit()` MINDEN válaszon
végigfut: levágja a modell saját köszönését, és ha kell, a napszakhoz illőt
teszi a helyére. Lásd a 7) szakaszt.
"""

import os
import re
import json
import random
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
PROMPT_VERSION = "flux-v3"
LANGUAGE = "hu-HU"
TTL_PERC = 35          # a 30 perces adatfrissítéshez igazítva: egy adatállapot
                       # = egy Gemini-hívás, utána tárolt szöveg megy ki
GEMINI_TIMEOUT = 12         # a főoldali szöveghez (háttérben készül)

# A FŐOLDALI körbejáró szöveghez kell-e nyelvi modell?
#
# Alapból NEM. A főoldali mondatokat a `sablon_uzenetek()` állítja elő, szintén
# az élő adatokból, napszakhoz igazított igeidővel — csak állandóbb szerkezettel,
# mint a Gemini. Cserébe a napi keret TELJES EGÉSZE a látogatók kérdéseire marad,
# oda, ahol a nyelvi modell tényleg számít.
# Ha valaki mégis a Geminire bízná a főoldalt: FLUX_FOOLDAL_GEMINI=1
FOOLDAL_GEMINI = os.environ.get("FLUX_FOOLDAL_GEMINI", "0") == "1"
GEMINI_KERDES_TIMEOUT = 10  # a látogató kérdéséhez. Ez FELSŐ KORLÁT, nem
                            # várakozási idő: a modell rendszerint 2-4 mp
                            # alatt felel.

# A kérdésekre adott válasz hőfoka. 0,7-ről 0,85-re emelve: a szám-ellenőrzés
# amúgy is kiszűr minden kitalált értéket, tehát a magasabb hőfok NEM a
# pontosság rovására megy — csak a mondatszerkezet lesz változatosabb. A régi
# beállítás mellett a válaszok szerkezete gyakorlatilag mindig ugyanaz volt.
GEMINI_KERDES_HOFOK = 0.85

BUDAPEST_TZ = ZoneInfo("Europe/Budapest")


def _most():
    """Budapesti falióra, időzóna nélkül — pontosan olyan alakban, ahogy az
    app.py az ISO időbélyegeit előállítja. A szerver saját zónája nem számít."""
    return datetime.now(BUDAPEST_TZ).replace(tzinfo=None)


# ============================================================
# 0) NYELV — magyar és angol
#
# Flux kétnyelvű. A nyelvet NEM találgatjuk mondatonként: a látogató
# állítja a HU/EN kapcsolóval, és ez az állapot végigmegy mindenen — a
# körbejáró szövegen, a válaszokon, a kiemelt címkéken.
#
# Emellett van felismerés is, de az nem "angolul válaszol egyszer": ha
# valaki angolul kérdez, a `valasz()` visszaadja a felismert nyelvet, és az
# app.py ÁTBILLENTI a kapcsolót. Így nem fordulhat elő, hogy az angol válasz
# után magyarul folytatódik a körhinta — ez volt a fő kockázat.
#
# Minden megjelenő szöveg ebben a szótárban van, kulcs szerint párban. Az
# indulási ellenőrzés (a fájl végén) kiírja, ha az egyik nyelvből kimarad
# egy kulcs — így nem tud csendben elcsúszni a két változat.
# ============================================================

NYELVEK = ("hu", "en")
ALAP_NYELV = "hu"


def _nyelv_ok(nyelv):
    """Ismeretlen vagy hiányzó nyelvkód esetén a magyar az alapértelmezés."""
    return nyelv if nyelv in NYELVEK else ALAP_NYELV


SZ = {
    # ---------------- MAGYAR ----------------
    "hu": {
        # --- köszönés, napszak ---
        "udv_hajnal": "Szia!",
        "udv_reggel": "Jó reggelt!",
        "udv_delelott": "Szia!",
        "udv_del": "Szia!",
        "udv_delutan": "Szia!",
        "udv_este": "Jó estét!",
        "udv_ejszaka": "Jó estét!",
        "napszak_hajnal": "hajnal",
        "napszak_reggel": "reggel",
        "napszak_delelott": "délelőtt",
        "napszak_del": "dél",
        "napszak_delutan": "délután",
        "napszak_este": "este",
        "napszak_ejszaka": "éjszaka",

        # --- köszöntő ---
        "koszonto_statikus":
            "Szia, Flux vagyok, az OkosMérő energiaasszisztense. Élő energiapiaci és "
            "időjárási adatokból mutatom meg, mikor kedvezőbb az energia ára, hogyan "
            "alakulhat a következő órák fogyasztása, mit várunk a nap- és "
            "széltermeléstől, és hol talált szokatlan eltérést a rendszer.",
        "koszonto_elo":
            "{udv} Flux vagyok, az OkosMérő energiaasszisztense. Élő energiapiaci és "
            "időjárási adatokból mondom el, mi történik éppen a magyar "
            "villamosenergia-rendszerben.",

        # --- állandó záró üzenetek ---
        "modell_bemutatas":
            "Az OkosMérő két külön módszert használ: a CatBoost gépi tanulási modell "
            "előrejelzi a várható fogyasztást, az STL pedig megkeresi a szokatlan "
            "eltéréseket és magyarázatot keres rájuk.",
        "zaro_kerdezz":
            "Kérdezz nyugodtan a beviteli mezőben: az árakról, a töltési ablakról, a "
            "fogyasztásról, a nap- és széltermelésről, az időjárásról vagy a modell "
            "pontosságáról is válaszolok az élő adatokból.",
        "zaro_fulek":
            "A DAM-árak és töltés fülön negyedórás bontásban látod a másnapi árakat, "
            "az Energiaelemzésen a következő órák fogyasztását, a Megújulókon a nap- "
            "és széltermelést, az ML Modell Laborban pedig a két modellt élőben.",
        # EZ a sor csak magyar módban megy ki: ez árulja el, hogy Flux angolul is tud.
        "zaro_angol_tipp":
            "You can also ask me in English — switch with the EN button.",

        # --- általános ---
        "varok_adatra":
            "Élő adatokra várok — amint megérkeznek, mondom, mi történik.",
        "varok_adatra_kerdes":
            "Élő adatokra várok — amint megérkeznek, válaszolok a kérdésedre.",
        "ures_kerdes":
            "Kérdezz nyugodtan: az árakról, a töltési ablakról, a fogyasztásról, a "
            "nap- és széltermelésről, az időjárásról vagy a modell pontosságáról is "
            "tudok beszélni.",
        "nincs_valasz":
            "Erre a kérdésre az élő adatokból jelenleg nem áll rendelkezésre pontos "
            "válasz.",
        "nincs_valasz_de":
            "Erre a kérdésre az élő adatokból nem tudok pontos választ adni. ",
        "kvota_elonezet":
            "Most épp betelt a nyelvi modell kerete, úgyhogy tömörebben fogalmazok — "
            "amint újratöltődik, megint bővebben válaszolok. Az adatok viszont "
            "ugyanúgy élők: ",
        "holnap_elotag": "holnap ",

        # --- címkék ---
        "c_amirol": "amiről kérdezhetsz",
        "c_dam_most": "aktuális day-ahead ár",
        "c_dam_mai_atlag": "mai átlagos day-ahead ár",
        "c_holnapi_atlag": "holnapi átlagár",
        "c_holnapi_arak": "holnapi árak",
        "c_legkedvezobb": "a legkedvezőbb időszak ára",
        "c_jelen_ora": "előrejelzés a jelen órára",
        "c_csucs": "előrejelzett csúcsterhelés",
        "c_csucs_volt": "csúcsterhelés",
        "c_heti_elteres": "eltérés a heti átlagtól",
        "c_tegnap": "eltérés tegnaphoz",
        "c_ho": "mért hőmérséklet · Budapest",
        "c_idojaras": "időjárás",
        "c_nap_tetozes": "mai napenergia-tetőzés",
        "c_nap_csucs": "mai napenergia-csúcs",
        "c_nap_hatra": "hátralévő napenergia-csúcs",
        "c_nap_holnap": "holnapi napenergia-csúcs",
        "c_szel_hatra": "hátralévő széltermelési csúcs",
        "c_szel_csucs": "mai széltermelési csúcs",
        "c_megujulo_mae": "napelőtti terv mai eltérése",
        "c_arfolyam": "EUR/HUF árfolyam",
        "c_modell_mae": "átlagos előrejelzési eltérés",
        "c_heti_mae": "heti átlagos eltérés",
        "c_ket_modell": "a két modell",
        "c_szokatlan": "szokatlan órák · elmúlt 7 nap",
        "c_szokatlan_stl": "szokatlan órák · STL",
        "c_mert_fogy": "legutóbbi mért fogyasztás",
        "c_pontos_ido": "pontos idő · Budapest",
        "c_frissites": "adatfrissítés",
    },

    # ---------------- ANGOL ----------------
    "en": {
        # --- greetings, time of day ---
        # A "Good night" angolul is ELKÖSZÖNÉS, ezért hajnalban a semleges
        # "Hi there!" megy ki — ugyanaz a megfontolás, mint magyarul.
        "udv_hajnal": "Hi there!",
        "udv_reggel": "Good morning!",
        "udv_delelott": "Hi there!",
        "udv_del": "Hi there!",
        "udv_delutan": "Good afternoon!",
        "udv_este": "Good evening!",
        "udv_ejszaka": "Good evening!",
        "napszak_hajnal": "early morning",
        "napszak_reggel": "morning",
        "napszak_delelott": "late morning",
        "napszak_del": "midday",
        "napszak_delutan": "afternoon",
        "napszak_este": "evening",
        "napszak_ejszaka": "night",

        # --- intro ---
        "koszonto_statikus":
            "Hi, I'm Flux, the energy assistant of OkosMérő. Using live power-market "
            "and weather data I show you when electricity is cheaper, how consumption "
            "may develop over the next hours, what to expect from solar and wind "
            "generation, and where the system found unusual deviations.",
        "koszonto_elo":
            "{udv} I'm Flux, the energy assistant of OkosMérő. From live power-market "
            "and weather data I tell you what is happening right now in the Hungarian "
            "electricity system.",

        # --- fixed closing messages ---
        "modell_bemutatas":
            "OkosMérő uses two separate methods: the CatBoost machine-learning model "
            "forecasts expected consumption, while STL finds unusual deviations and "
            "looks for explanations behind them.",
        "zaro_kerdezz":
            "Feel free to ask in the input box: I answer from live data about prices, "
            "the charging window, consumption, solar and wind generation, the weather "
            "or the accuracy of the model.",
        "zaro_fulek":
            "On the DAM prices and charging tab you see tomorrow's prices in "
            "quarter-hourly detail, on Energy analysis the consumption of the coming "
            "hours, on Renewables the solar and wind output, and in the ML Model Lab "
            "the two models live.",
        # Angol módban ez a sor nem kell: a látogató már angolul olvas.
        "zaro_angol_tipp": "",

        # --- general ---
        "varok_adatra":
            "Waiting for live data — as soon as it arrives, I'll tell you what's "
            "happening.",
        "varok_adatra_kerdes":
            "Waiting for live data — as soon as it arrives, I'll answer your question.",
        "ures_kerdes":
            "Go ahead and ask: I can talk about prices, the charging window, "
            "consumption, solar and wind generation, the weather or the accuracy of "
            "the model.",
        "nincs_valasz":
            "There is no precise answer available for this in the live data right now.",
        "nincs_valasz_de":
            "I can't give a precise answer to that from the live data. ",
        "kvota_elonezet":
            "My language model has just hit its quota, so I'll keep this short — once "
            "it resets I'll be more talkative again. The data is just as live though: ",
        "holnap_elotag": "tomorrow ",

        # --- labels ---
        "c_amirol": "what you can ask about",
        "c_dam_most": "current day-ahead price",
        "c_dam_mai_atlag": "today's average day-ahead price",
        "c_holnapi_atlag": "tomorrow's average price",
        "c_holnapi_arak": "tomorrow's prices",
        "c_legkedvezobb": "price of the cheapest window",
        "c_jelen_ora": "forecast for the current hour",
        "c_csucs": "forecast peak load",
        "c_csucs_volt": "peak load",
        "c_heti_elteres": "deviation from the weekly average",
        "c_tegnap": "change vs yesterday",
        "c_ho": "measured temperature · Budapest",
        "c_idojaras": "weather",
        "c_nap_tetozes": "today's solar peak",
        "c_nap_csucs": "today's solar peak",
        "c_nap_hatra": "remaining solar peak",
        "c_nap_holnap": "tomorrow's solar peak",
        "c_szel_hatra": "remaining wind peak",
        "c_szel_csucs": "today's wind peak",
        "c_megujulo_mae": "today's day-ahead plan error",
        "c_arfolyam": "EUR/HUF exchange rate",
        "c_modell_mae": "average forecast error",
        "c_heti_mae": "weekly average error",
        "c_ket_modell": "the two models",
        "c_szokatlan": "unusual hours · last 7 days",
        "c_szokatlan_stl": "unusual hours · STL",
        "c_mert_fogy": "latest measured consumption",
        "c_pontos_ido": "current time · Budapest",
        "c_frissites": "data refresh",
    },
}


# ---- A determinisztikus mondatok sablonjai ----
#
# Külön blokkban, hogy a fenti szótár olvasható maradjon. Ugyanaz a kulcs
# mindkét nyelvben; az indulási ellenőrzés kiírja, ha az egyikből kimarad.
# Ezek NEM fordítások szó szerint: az angol változat ott is természetes
# mondat, ahol a magyar toldalékolás mást kívánna.

SZ["hu"].update({
    # --- napelem ---
    "nap_lecsengett": "A mai napelemes termelés lecsengett; a tetőzés {ido} "
                      "körül {cs} MW volt.",
    "nap_holnapi_terv": " Holnapra a napelőtti terv {cs} MW körüli csúcsot jelez.",
    "nap_meg_hatra": "A napelemes termelés a mai napelőtti terv szerint {ido} "
                     "körül tetőzik, {cs} MW-tal.",
    "nap_tetozes_volt": "A mai napelemes tetőzés {ido} körül {cs} MW volt. ",
    "nap_hatralevo": "A nap hátralévő részében {cs} MW a legmagasabb várható érték.",
    "nap_holnapi_onallo": "A holnapi napra a napelőtti terv {cs} MW körüli "
                          "napelemes csúcsot jelez.",
    "nap_mert_ma": " A ma ténylegesen mért legmagasabb érték {cs} MW.",
    # --- szél ---
    "szel_hatra": "A szélerőművek termelése a mai nap hátralévő részében {cs} "
                  "MW-ig emelkedhet a napelőtti terv szerint.",
    "szel_atlag": " A napi átlag {a} MW.",
    "szel_tetozott": "A szélerőművek mai termelése {ido} körül tetőzött, {cs} MW-tal.",
    "szel_utolso_mert": " A legutóbb mért érték {v} MW {ido}-kor.",
    # --- tegnapi összevetés ---
    "tegnap_azonos": "A legutóbbi lezárt mért óra ({ora}) fogyasztása {ma} MWh, "
                     "gyakorlatilag ugyanannyi, mint tegnap ugyanekkor ({teg} MWh).",
    "tegnap_elter": "A legutóbbi lezárt mért óra ({ora}) fogyasztása {ma} MWh, ami "
                    "{sz} százalékkal {irany}, mint tegnap ugyanekkor ({teg} MWh).",
    "tegnap_tobb": "több",
    "tegnap_kevesebb": "kevesebb",
    "tegnap_atlag": " A mai nap eddigi átlaga {sz} százalékkal {irany}, mint "
                    "tegnap ugyaneddig.",
    "tegnap_magasabb": "magasabb",
    "tegnap_alacsonyabb": "alacsonyabb",
    # --- hőmérséklet ---
    "ho_meleg": "A budapesti mért hőmérséklet most {ho} °C; ezen a szinten a hűtés "
                "érezhetően megemeli a rendszerterhelést.",
    "ho_hideg": "A budapesti mért hőmérséklet most {ho} °C; ezen a szinten a fűtési "
                "igény hajtja fel a rendszerterhelést.",
    "ho_kozepes": "A budapesti mért hőmérséklet most {ho} °C, ami mérsékelt fűtési "
                  "és hűtési igényt jelent.",
    "ho_szelso": " A mai napi szélsőértékek {lo} és {hi} °C.",
    "ho_nincs_mert": "A budapesti hőmérsékletről ma a következőket mutatják az adatok.",
    "ho_napi": " A mai napi maximum {hi} °C, a minimum {lo} °C.",
    "ho_holnap": " Holnap {hi} °C-os csúcs várható.",
    # --- ár, töltés ---
    "s_dam": "A másnapi piac aktuális ára {most} €/MWh, ami a mai {atl} €/MWh-s "
             "napi átlaghoz képest {viszony} szintet jelent.",
    "viszony_kedvezobb": "kedvezőbb",
    "viszony_magasabb": "magasabb",
    "viszony_azonos": "azonos",
    "s_toltes": "A legkedvezőbb árszint {k} és {v} között várható, ekkor a "
                "day-ahead ár {ar} €/MWh. Amennyiben megoldható, a halasztható "
                "fogyasztásokat érdemes lehet erre az időszakra ütemezni.",
    # --- fogyasztás ---
    "s_fogy_jelen": "A CatBoost modell a most futó órára {akt} MWh országos "
                    "fogyasztást jelez.",
    "s_csucs_hatra_mai": " A mai csúcs {sav} között várható, {csucs} MWh körül.",
    "s_csucs_hatra": " A csúcs {sav} között várható, {csucs} MWh körül.",
    "s_csucs_volt_mai": " A mai csúcs {ido} körül volt, {csucs} MWh.",
    "s_csucs_volt": " A csúcs {ido} körül volt, {csucs} MWh.",
    "s_csucs_onallo": "A CatBoost modell előrejelzése szerint a legmagasabb terhelés "
                      "várhatóan {sav} között alakulhat ki, a fogyasztás csúcsértéke "
                      "pedig megközelítheti a {csucs} MWh-t.",
    "s_csucs_kialakult": "A csúcsterhelés {ido} körül alakult ki, {csucs} MWh értéken.",
    "s_hoseg_tipp": "Amennyiben megoldható, a halasztható nagyobb fogyasztásokat "
                    "érdemes lehet a várható csúcsidőszakon kívülre időzíteni.",
    "s_heti_tobb": "A következő órák várható fogyasztása {sz} százalékkal meghaladja "
                   "az ugyanezekre az órákra jellemző heti átlagot.",
    "s_heti_keves": "A következő órák várható fogyasztása {sz} százalékkal elmarad "
                    "az ugyanezekre az órákra jellemző heti átlagtól.",
    # --- modell, anomália ---
    "s_modell_mae": "A legutóbbi lezárt napon a modell egy nappal korábban készített "
                    "előrejelzése átlagosan {mae} MWh-val tért el a tényleges "
                    "fogyasztástól.",
    "s_anom_fo": "Az elmúlt hét napban {db} órában tért el a fogyasztás a szokásos "
                 "mintázattól.",
    "s_anom_hatter": " A háttérben {reszek} állt.",
    "s_anom_extrem": "{db} esetben szélsőséges időjárás",
    "s_anom_napelem": "{db} esetben alacsony napsugárzás",
    "s_anom_fordulat": "{db} esetben jelentős hőmérsékleti fordulat",
    "s_anom_nyitott": " További {db} eset vizsgálata folyamatban van.",
    "u_ora": "{db} óra",
    "es_kotoszo": " és ",
})

SZ["en"].update({
    # --- solar ---
    "nap_lecsengett": "Today's solar output has faded out; it peaked around {ido} "
                      "at {cs} MW.",
    "nap_holnapi_terv": " For tomorrow the day-ahead plan indicates a peak of "
                        "around {cs} MW.",
    "nap_meg_hatra": "According to today's day-ahead plan, solar output peaks "
                     "around {ido} at {cs} MW.",
    "nap_tetozes_volt": "Today's solar output peaked around {ido} at {cs} MW. ",
    "nap_hatralevo": "For the rest of the day the highest expected value is {cs} MW.",
    "nap_holnapi_onallo": "For tomorrow the day-ahead plan indicates a solar peak "
                          "of around {cs} MW.",
    "nap_mert_ma": " The highest value actually measured today is {cs} MW.",
    # --- wind ---
    "szel_hatra": "According to the day-ahead plan, wind output may rise to {cs} MW "
                  "over the rest of today.",
    "szel_atlag": " The daily average is {a} MW.",
    "szel_tetozott": "Wind output peaked around {ido} today, at {cs} MW.",
    "szel_utolso_mert": " The latest measured value is {v} MW at {ido}.",
    # --- comparison with yesterday ---
    "tegnap_azonos": "The last completed measured hour ({ora}) consumed {ma} MWh — "
                     "practically the same as yesterday at the same time ({teg} MWh).",
    "tegnap_elter": "The last completed measured hour ({ora}) consumed {ma} MWh, "
                    "which is {sz} percent {irany} than yesterday at the same time "
                    "({teg} MWh).",
    "tegnap_tobb": "more",
    "tegnap_kevesebb": "less",
    "tegnap_atlag": " Today's average so far is {sz} percent {irany} than yesterday "
                    "up to the same point.",
    "tegnap_magasabb": "higher",
    "tegnap_alacsonyabb": "lower",
    # --- temperature ---
    "ho_meleg": "The measured temperature in Budapest is {ho} °C right now; at this "
                "level cooling noticeably lifts system load.",
    "ho_hideg": "The measured temperature in Budapest is {ho} °C right now; at this "
                "level heating demand drives system load up.",
    "ho_kozepes": "The measured temperature in Budapest is {ho} °C right now, which "
                  "means moderate heating and cooling demand.",
    "ho_szelso": " Today's daily extremes are {lo} and {hi} °C.",
    "ho_nincs_mert": "Here is what the data shows about the temperature in Budapest "
                     "today.",
    "ho_napi": " Today's maximum is {hi} °C and the minimum {lo} °C.",
    "ho_holnap": " Tomorrow a peak of {hi} °C is expected.",
    # --- price, charging ---
    "s_dam": "The current day-ahead price is {most} €/MWh, which is a {viszony} "
             "level compared with today's daily average of {atl} €/MWh.",
    "viszony_kedvezobb": "more favourable",
    "viszony_magasabb": "higher",
    "viszony_azonos": "matching",
    "s_toltes": "The most favourable price level is expected between {k} and {v}, "
                "when the day-ahead price is {ar} €/MWh. If you can, it's worth "
                "scheduling deferrable loads into that window.",
    # --- consumption ---
    "s_fogy_jelen": "The CatBoost model forecasts {akt} MWh of national consumption "
                    "for the current hour.",
    "s_csucs_hatra_mai": " Today's peak is expected between {sav}, at around "
                         "{csucs} MWh.",
    "s_csucs_hatra": " The peak is expected between {sav}, at around {csucs} MWh.",
    "s_csucs_volt_mai": " Today's peak was around {ido}, at {csucs} MWh.",
    "s_csucs_volt": " The peak was around {ido}, at {csucs} MWh.",
    "s_csucs_onallo": "According to the CatBoost model the highest load is expected "
                      "between {sav}, with consumption approaching {csucs} MWh.",
    "s_csucs_kialakult": "Peak load occurred around {ido}, at {csucs} MWh.",
    "s_hoseg_tipp": "If possible, it's worth moving larger deferrable loads outside "
                    "the expected peak period.",
    "s_heti_tobb": "Expected consumption over the coming hours exceeds the weekly "
                   "average for the same hours by {sz} percent.",
    "s_heti_keves": "Expected consumption over the coming hours falls short of the "
                    "weekly average for the same hours by {sz} percent.",
    # --- model, anomalies ---
    "s_modell_mae": "On the last completed day, the forecast the model had made a "
                    "day earlier deviated from actual consumption by {mae} MWh on "
                    "average.",
    "s_anom_fo": "Over the past seven days, consumption deviated from the usual "
                 "pattern in {db} hours.",
    "s_anom_hatter": " Behind them: {reszek}.",
    "s_anom_extrem": "extreme weather in {db} cases",
    "s_anom_napelem": "low solar irradiance in {db} cases",
    "s_anom_fordulat": "a significant temperature swing in {db} cases",
    "s_anom_nyitott": " {db} further cases are still under investigation.",
    "u_ora": "{db} hours",
    "es_kotoszo": " and ",
})


# ---- A kérdésre adott determinisztikus válaszok szövegei ----

SZ["hu"].update({
    "h_orulok": "Örülök, hogy benéztél. ",
    "h_orulok_nincs": "Amint megérkeznek az élő adatok, mondom, mi történik.",
    "h_szivesen1": "Szívesen! Ha bármi mást is meg akarsz nézni, csak kérdezz.",
    "h_szivesen2": "Nagyon szívesen — szólj, ha másra is kíváncsi vagy.",
    "h_szivesen3": "Szívesen. Bármikor kérdezhetsz az árakról, a fogyasztásról "
                   "vagy a megújulókról.",
    "h_kep_bevezeto": "Az élő adatokból ezekről tudok beszélni: ",
    "kep_ar": "a másnapi (day-ahead) villamosenergia-árak és a legkedvezőbb "
              "töltési időszak",
    "kep_fogyasztas": "a következő órák fogyasztási előrejelzése és a várható csúcs",
    "kep_megujulo": "a nap- és széltermelés terve, valamint a mért alakulása",
    "kep_ido": "a budapesti hőmérséklet és a napi időjárási kilátás",
    "kep_arfolyam": "az EUR/HUF árfolyam",
    "kep_modell": "a CatBoost előrejelzés pontossága a lezárt napokon",
    "kep_stl": "az STL által talált szokatlan órák és a magyarázatuk",
    "h_ar_fo": "A day-ahead ár jelenleg {most} €/MWh, ami a mai {atl} €/MWh-s napi "
               "átlaghoz képest {viszony} szint. A mai árak a {lo} és {hi} €/MWh "
               "sávban mozogtak, a legolcsóbb óra {olcso}, a legdrágább {draga} volt.",
    "h_ar_atlag": "A mai day-ahead árak átlaga {atl} €/MWh, a sáv alja {lo}, "
                  "teteje {hi} €/MWh.",
    "h_ar_egyszeru": "A day-ahead ár jelenleg {most} €/MWh.",
    "h_holnap_van": "A holnapi árak már publikálva vannak: az átlag {atl} €/MWh, "
                    "a legalacsonyabb szelvény {lo}, a legmagasabb {hi} €/MWh.",
    "h_holnap_nincs": "A holnapi day-ahead árakat még nem publikálták; az új "
                      "árszelvények 14:00 körül érkeznek meg a piacról.",
    "h_toltes_fo": "A legkedvezőbb töltési időszak {k} és {v} között van, ekkor a "
                   "day-ahead ár {ar} €/MWh.",
    "h_toltes_negativ": " Ebben az ablakban negatív árszelvény is van.",
    "h_toltes_most": " Az aktuális ár is a kedvező sávban van, tehát most sem "
                     "érdemes várni.",
    "h_toltes_tovabbi": " További kedvező kezdés {k} ({ar} €/MWh).",
    "h_fogy_mert": "A legutóbbi mért országos fogyasztás {v} MWh volt {ido}-kor.",
    "h_fogy_jelen": "A CatBoost V10 modell a most futó órára {akt} MWh országos "
                    "fogyasztást jelez.",
    "h_fogy_csucs_hatra": " A csúcs {sav} között várható, {csucs} MWh.",
    "h_fogy_csucs_volt": " A csúcs {ido} körül volt, {csucs} MWh.",
    "h_fogy_ablak": "A CatBoost V10 modell a következő {n} órára jelez előre. A "
                    "legmagasabb terhelés {sav} között várható, a csúcsérték "
                    "{csucs} MWh.",
    "h_fogy_heti_tobb": " Ez {sz} százalékkal meghaladja a heti átlagot.",
    "h_fogy_heti_keves": " Ez {sz} százalékkal elmarad a heti átlagtól.",
    "h_meg_nap": "a napelemes terv átlagosan {v} MW-tal",
    "h_meg_szel": "a szélerőművi terv átlagosan {v} MW-tal",
    "h_meg_fo": "A mai órákban, ahol már van mért adat, {reszek} tért el a "
                "tényleges termeléstől.",
    "h_arfolyam": "Az EUR/HUF árfolyam {e} forint. Ez váltja át a €/MWh-ban jegyzett "
                  "day-ahead árat forintra, ezért a hazai költség két dolgon múlik: "
                  "az árszinten és az árfolyamon.",
    "h_modell_nap": "A legutóbbi lezárt napon ({nap}, {orak} kiértékelt óra) a "
                    "CatBoost egy nappal korábban készített előrejelzése átlagosan "
                    "{mae} MWh-val tért el a tényleges fogyasztástól.",
    "h_modell_frissitett": " A legutóbbi mért órákat is ismerő, frissített jóslat "
                           "eltérése {mae} MWh.",
    "h_modell_heti": "Ezen a héten eddig {orak} lezárt órát értékeltünk ki; a modell "
                     "első jóslatának átlagos eltérése {mae} MWh.",
    "h_modell_altalanos": "A CatBoost V10 gépi tanulási modell órákra előre becsli "
                          "az ország fogyasztását, az STL-módszer pedig a szokásostól "
                          "eltérő órákat keresi meg. A pontossági kiértékelés mindig "
                          "lezárt napokra készül.",
    "h_stl_altalanos": "Az STL a legutóbbi {nap} nap fogyasztását bontja trendre, "
                       "napi mintázatra és maradékra; ebben az ablakban {db} "
                       "szokatlan órát talált. A trend jelenleg {irany}.",
    "h_stl_stabil": "stabil",
    "h_mert": "A legutóbbi lezárt mért óra ({ido}) országos fogyasztása {v} MWh. A "
              "mért adat természeténél fogva egy-két órát késik a valós időhöz képest.",
    "h_ido": "Most {ido} van Budapesten, {napszak}. Minden időpont, amit mondok, "
             "budapesti idő szerint értendő.",
    "h_frissites": "Az élő adatok legutóbb {ido}-kor frissültek (budapesti idő). A "
                   "források: ENTSO-E (árak, fogyasztás, termelés), Open-Meteo vagy "
                   "Visual Crossing (időjárás) és az ECB (árfolyam).",
    "h_frissites_hianyzo": " Jelenleg nem elérhető: {lista}.",
    "h_elharitas": "Elnézést, ebben nem tudok segíteni — én kizárólag az OkosMérő "
                   "élő adataival és a magyar energiahelyzettel foglalkozom. Erre a "
                   "kérdésre egy általános nyelvi asszisztens jobb választás. ",
    "h_elharitas_nincs": "Amint megérkeznek az élő adatok, szívesen mesélek az "
                         "energiapiacról.",
})

SZ["en"].update({
    "h_orulok": "Good to see you here. ",
    "h_orulok_nincs": "As soon as the live data arrives, I'll tell you what's "
                      "happening.",
    "h_szivesen1": "You're welcome! If there's anything else you'd like to see, "
                   "just ask.",
    "h_szivesen2": "Anytime — let me know if you're curious about something else.",
    "h_szivesen3": "Glad to help. You can ask me about prices, consumption or "
                   "renewables whenever you like.",
    "h_kep_bevezeto": "From the live data I can talk about these: ",
    "kep_ar": "day-ahead electricity prices and the most favourable charging window",
    "kep_fogyasztas": "the consumption forecast for the coming hours and the "
                      "expected peak",
    "kep_megujulo": "the solar and wind generation plan and how it actually turns out",
    "kep_ido": "the temperature in Budapest and today's weather outlook",
    "kep_arfolyam": "the EUR/HUF exchange rate",
    "kep_modell": "the accuracy of the CatBoost forecast on completed days",
    "kep_stl": "the unusual hours found by STL and their explanations",
    "h_ar_fo": "The day-ahead price is currently {most} €/MWh, a {viszony} level "
               "compared with today's average of {atl} €/MWh. Today's prices moved "
               "between {lo} and {hi} €/MWh; the cheapest hour was {olcso} and the "
               "most expensive {draga}.",
    "h_ar_atlag": "Today's day-ahead prices average {atl} €/MWh, ranging from {lo} "
                  "to {hi} €/MWh.",
    "h_ar_egyszeru": "The day-ahead price is currently {most} €/MWh.",
    "h_holnap_van": "Tomorrow's prices are already published: the average is {atl} "
                    "€/MWh, the lowest slot {lo} and the highest {hi} €/MWh.",
    "h_holnap_nincs": "Tomorrow's day-ahead prices haven't been published yet; the "
                      "new price slots arrive from the market around 14:00.",
    "h_toltes_fo": "The best charging window is between {k} and {v}, when the "
                   "day-ahead price is {ar} €/MWh.",
    "h_toltes_negativ": " There is even a negative price slot in that window.",
    "h_toltes_most": " The current price is already in the favourable band, so "
                     "there's no need to wait.",
    "h_toltes_tovabbi": " Another good start time is {k} ({ar} €/MWh).",
    "h_fogy_mert": "The latest measured national consumption was {v} MWh at {ido}.",
    "h_fogy_jelen": "The CatBoost V10 model forecasts {akt} MWh of national "
                    "consumption for the current hour.",
    "h_fogy_csucs_hatra": " The peak is expected between {sav}, at {csucs} MWh.",
    "h_fogy_csucs_volt": " The peak was around {ido}, at {csucs} MWh.",
    "h_fogy_ablak": "The CatBoost V10 model forecasts the next {n} hours. The "
                    "highest load is expected between {sav}, peaking at {csucs} MWh.",
    "h_fogy_heti_tobb": " That is {sz} percent above the weekly average.",
    "h_fogy_heti_keves": " That is {sz} percent below the weekly average.",
    "h_meg_nap": "the solar plan by {v} MW on average",
    "h_meg_szel": "the wind plan by {v} MW on average",
    "h_meg_fo": "In today's hours where measured data already exists, {reszek} "
                "deviated from actual output.",
    "h_arfolyam": "The EUR/HUF exchange rate is {e} forint. It converts the "
                  "day-ahead price quoted in €/MWh into forint, so the domestic cost "
                  "depends on two things: the price level and the exchange rate.",
    "h_modell_nap": "On the last completed day ({nap}, {orak} evaluated hours) the "
                    "forecast CatBoost had made a day earlier deviated from actual "
                    "consumption by {mae} MWh on average.",
    "h_modell_frissitett": " The updated forecast, which also knows the latest "
                           "measured hours, is off by {mae} MWh.",
    "h_modell_heti": "So far this week {orak} completed hours have been evaluated; "
                     "the average error of the model's first forecast is {mae} MWh.",
    "h_modell_altalanos": "The CatBoost V10 machine-learning model estimates national "
                          "consumption hours ahead, while the STL method looks for "
                          "hours that deviate from the usual pattern. Accuracy is "
                          "always evaluated on completed days.",
    "h_stl_altalanos": "STL decomposes the consumption of the last {nap} days into "
                       "trend, daily pattern and residual; in that window it found "
                       "{db} unusual hours. The trend is currently {irany}.",
    "h_stl_stabil": "stable",
    "h_mert": "The last completed measured hour ({ido}) had national consumption of "
              "{v} MWh. Measured data inherently lags real time by an hour or two.",
    "h_ido": "It's {ido} in Budapest right now — {napszak}. Every time I mention is "
             "Budapest time.",
    "h_frissites": "The live data was last refreshed at {ido} (Budapest time). "
                   "Sources: ENTSO-E (prices, consumption, generation), Open-Meteo or "
                   "Visual Crossing (weather) and the ECB (exchange rate).",
    "h_frissites_hianyzo": " Currently unavailable: {lista}.",
    "h_elharitas": "Sorry, I can't help with that — I only deal with OkosMérő's live "
                   "data and the Hungarian energy situation. For that question a "
                   "general-purpose assistant is a better choice. ",
    "h_elharitas_nincs": "As soon as the live data arrives, I'll gladly tell you "
                         "about the energy market.",
})


def _t(nyelv, kulcs, **kw):
    """Egy szöveg a szótárból, nyelv szerint.

    Ha a kulcs valamiért hiányzik a kért nyelvből, a magyar változat megy ki
    — üres mondat helyett inkább egy másik nyelvű, de értelmes mondat. Az
    indulási ellenőrzés amúgy is kiírja, ha van ilyen kulcs."""
    tar = SZ.get(_nyelv_ok(nyelv), SZ[ALAP_NYELV])
    s = tar.get(kulcs)
    if s is None:
        s = SZ[ALAP_NYELV].get(kulcs, "")
    return s.format(**kw) if kw else s


# ---- Nyelvfelismerés a látogató kérdésén ----
#
# Nem nyelvfelismerő könyvtár, és nem is kell annak lennie: két nyelv között
# kell dönteni, rövid, tematikus kérdéseken. Két jel elég.
#
#   1) Magyar ékezetes betű  -> biztosan magyar. Ez a legerősebb jel.
#   2) Angol funkciószavak   -> ha van belőlük, és magyar jelző nincs, angol.
#
# Ha egyik sem dönt, `None` megy vissza: ilyenkor NEM billentjük át a
# kapcsolót, marad, amit a látogató beállított.

_MAGYAR_EKEZET = set("áéíóöőúüűÁÉÍÓÖŐÚÜŰ")

_MAGYAR_JELZOK = (
    "mennyi", "mikor", "milyen", "hogyan", "miert", "hany", "melyik", "van",
    "lesz", "volt", "nem", "igen", "kerem", "koszonom", "szia", "udv",
    "es ", "vagy ", "hogy ", "ez ", "az ", "most ", "ma ", "holnap",
)

_ANGOL_JELZOK = (
    "the ", " the", "what", "how ", "when", "why", "which", "who ",
    "is ", " is", "are ", "can you", "could you", "do you", "does ",
    "tell me", "show me", "give me", "please", "thanks", "thank you",
    "hello", "hi ", "hey ", "good morning", "good evening",
    "price", "consumption", "solar", "wind", "weather", "tomorrow",
    "today", "cheap", "expensive", "forecast", "accuracy", "charge",
)


def felismer_nyelv(kerdes):
    """'hu', 'en' vagy None (nem eldönthető).

    None esetén a hívó a beállított nyelvet tartja meg — a bizonytalan
    találgatás rosszabb, mint a semmi."""
    k = str(kerdes or "").strip().lower()
    if len(k) < 3:
        return None
    if any(ch in _MAGYAR_EKEZET for ch in k):
        return "hu"
    magyar = sum(1 for m in _MAGYAR_JELZOK if m in k)
    angol = sum(1 for m in _ANGOL_JELZOK if m in k)
    if angol and angol > magyar:
        return "en"
    if magyar and magyar > angol:
        return "hu"
    return None


# A napszakok belső kulcsai. A megjelenő NEVÜK nyelvfüggő (SZ["napszak_*"]),
# de a kulcs mindig ugyanaz — erre épül a köszönés és a cache-kulcs is.
_NAPSZAK_KULCSOK = ["hajnal", "reggel", "delelott", "del", "delutan",
                    "este", "ejszaka"]


def _napszak_kulcs(m=None):
    """Melyik napszakban vagyunk — budapesti falióra szerint, nyelvfüggetlenül."""
    h = (m or _most()).hour
    if h < 5:    return "hajnal"
    if h < 9:    return "reggel"
    if h < 12:   return "delelott"
    if h < 14:   return "del"
    if h < 18:   return "delutan"
    if h < 22:   return "este"
    return "ejszaka"


def _napszak(m=None, nyelv=ALAP_NYELV):
    """A napszak MEGJELENŐ neve — ez megy a promptba és a szövegbe."""
    return _t(nyelv, "napszak_" + _napszak_kulcs(m))


def _udvozles(nyelv=ALAP_NYELV, m=None):
    """Napszaknak megfelelő köszönés.

    FIGYELEM: a "Jó éjszakát!" magyarul ELKÖSZÖNÉS, nem üdvözlés — és a
    "Good night" ugyanígy angolul. Aki hajnali egykor nyitja meg az oldalt,
    azt nem elbúcsúztatni kell. Ezért hajnalban mindkét nyelven a semleges
    alak megy ki ("Szia!" / "Hi there!"). A "Jó estét!" és a "Good evening!"
    viszont este és éjfél körül is helyes üdvözlés."""
    return _t(nyelv, "udv_" + _napszak_kulcs(m))


def elo_koszonto(nyelv=ALAP_NYELV, m=None):
    """Élő köszöntő: napszaknak megfelelő köszönés.

    A pontos óra korábban benne volt a mondatban ("Most 10:47 van"), de
    látogatóként ez fölösleges: senki nem az energiapiaci irányítópulttól
    kérdezi meg, hány óra. A napszak szerinti köszönés bőven elég ahhoz,
    hogy élőnek hasson. Az időt Flux belül továbbra is pontosan ismeri —
    ez vezérli az igeidőket —, és ha valaki KIFEJEZETTEN rákérdez, meg is
    mondja."""
    return _t(nyelv, "koszonto_elo", udv=_udvozles(nyelv, m or _most()))

# Az ANGOL szerep-prompt. Nem fordítás, hanem ugyanaz a szerep angolul
# megfogalmazva: a szám-szabály betűre azonos, mert az a hitelesség alapja.
FLUX_SZEREP_KERDES_EN = (
    "You are Flux, the energy assistant of the OkosMérő dashboard. A visitor "
    "has just asked you a question on the site. Answer like a helpful, "
    "good-humoured expert colleague sitting next to them: in natural, living "
    "English, informal but professional. You are NOT writing a report, you are "
    "having a conversation.\n"
    "The answer should be two to four sentences:\n"
    "1) a short human reaction to the question,\n"
    "2) the substance from the live data, with the number inside the sentence,\n"
    "3) a brief useful addition or an offer of what else they could look at.\n"
    "IF THE QUESTION ASKS SEVERAL THINGS, answer ALL of them — not just the "
    "first. If there is no data for one of them, say so honestly for that one.\n"
    "GREETING: NEVER start the answer with a greeting — no 'Hi', no 'Good "
    "morning', no 'Hello'. The system inserts the greeting if one is needed. "
    "Start straight with the content.\n"
    "VARIETY: don't start every answer with the same structure. Sometimes lead "
    "with the number, sometimes with a reaction to the question, sometimes with "
    "a short observation — the way a person would talk.\n"
    "Take the time of day into account: don't write at 3am as if it were noon.\n"
    "PROFESSIONAL FREEDOM: within the topic, feel free to be substantive and "
    "independent. You may talk about what is currently moving the Hungarian and "
    "European energy market, why prices fluctuate, what the spread of renewables "
    "means for system operation, why consumption is hard to forecast. That is "
    "professional context, not data — just make sure that any CONCRETE NUMBER "
    "still comes only from the JSON.\n"
    "TOPIC BOUNDARY: you may only talk about OkosMérő's services and the "
    "Hungarian energy situation. If the visitor asks about anything else "
    "(recipes, jokes, politics, translation, everyday matters), politely say "
    "that you can't help with that and a general-purpose assistant would be a "
    "better choice — then offer what you can talk about. Don't invent an answer "
    "for the sake of the topic, and don't step out of your professional role.\n"
    "HARD RULE, never deviate from it: you may rely only on the JSON data given "
    "to you, and you may write a number only if it appears exactly in that data. "
    "You do not estimate, you do not re-round, you do not invent anything. If a "
    "piece of data is missing, say honestly that you don't have it right now."
)

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
#
# A KÖSZÖNÉST innen kivettük. Nem azért, mert nem kell, hanem mert a modell
# nem tartotta be: éjfél után is "Jó reggelt!"-tel kezdett, hiába állt a
# promptban a helyes alak. Amit nem lehet betartatni, azt nem a modellre
# bízzuk — a köszönést mostantól a `_koszones_javit()` teszi a helyére.
FLUX_SZEREP_KERDES = (
    "Te vagy Flux, az OkosMérő energiaasszisztense. Egy látogató most kérdezett "
    "tőled az oldalon. Úgy válaszolj, mint egy segítőkész, jókedvű szakértő kolléga, "
    "aki melletted ül: magyarul, tegezve, természetes, élő mondatokban. "
    "NEM jelentést írsz, hanem beszélgetsz.\n"
    "A válasz két-négy mondat legyen:\n"
    "1) rövid emberi reakció a kérdésre,\n"
    "2) a lényeg az élő adatokból, a számmal a mondatban,\n"
    "3) egy rövid, hasznos hozzáfűzés vagy felajánlás, hogy mit nézhet még meg.\n"
    "HA A KÉRDÉS TÖBB DOLGOT is firtat, MINDEGYIKRE válaszolj — ne csak az "
    "elsőre. Ha az egyikhez nincs adat, azt az egyet mondd meg őszintén.\n"
    "KÖSZÖNÉS: SOHA ne kezdd a választ köszönéssel — se 'Szia', se 'Jó reggelt', "
    "se 'Jó napot', se 'Üdv'. A köszönést a rendszer illeszti a válasz elé, ha "
    "kell. Te rögtön a tartalommal kezdj.\n"
    "VÁLTOZATOSSÁG: ne minden válasz ugyanazzal a szerkezettel induljon. "
    "Hol a számmal kezdj, hol a kérdésre adott reakcióval, hol egy rövid "
    "megfigyeléssel — úgy, ahogy egy ember beszélne.\n"
    "Vedd figyelembe a napszakot: hajnalban ne úgy írj, mintha dél lenne.\n"
    "SZAKMAI SZABADSÁG: a témán BELÜL nyugodtan légy tartalmas és önálló. "
    "Beszélhetsz arról, mi mozgatja most a magyar és európai energiapiacot, miért "
    "ingadoznak az árak, mit jelent a megújulók terjedése a rendszerirányításnak, "
    "miért nehéz előre jelezni a fogyasztást. Ez szakmai kontextus, nem adat — "
    "csak arra ügyelj, hogy KONKRÉT SZÁMOT továbbra is kizárólag a JSON-ból írhatsz.\n"
    "TÉMAHATÁR: kizárólag az OkosMérő szolgáltatásairól és a magyar "
    "energiahelyzetről beszélhetsz. Ha a látogató másról kérdez (recept, vicc, "
    "politika, fordítás, bármi hétköznapi), udvariasan mondd meg, hogy ebben nem "
    "tudsz segíteni, ehhez egy általános nyelvi asszisztens jobb választás — "
    "aztán ajánld fel, miről tudsz beszélni. Ne találj ki választ a téma "
    "kedvéért, és ne térj el a szakmai szereptől.\n"
    "KEMÉNY SZABÁLY, ettől soha nem térhetsz el: kizárólag a megkapott JSON "
    "adatokra támaszkodhatsz, és számot csak akkor írhatsz le, ha az pontosan "
    "szerepel az adatokban. Nem becsülsz, nem kerekítesz át, nem találsz ki semmit. "
    "Ha egy adat hiányzik, mondd meg őszintén, hogy arról most nincs adatod."
)


# Zárómondatok: nincs bennük élő adat, ezért mindig kimennek —
# a Gemini-változat végére is. Ezek hívják körbe a látogatót az oldalon.
# A modellek bemutatása. Ezek NEM élő adatok, hanem a dokumentált,
# offline validált eredmények — ezért fix szövegek, nem a Gemini írja.
def _modell_uzenetek(nyelv):
    return [{"sor": _t(nyelv, "modell_bemutatas"), "szam": None, "cimke": None}]


def _zaro_uzenetek(nyelv):
    """A záró körhinta. Magyar módban az UTOLSÓ sor angolul szól: ez árulja el
    a látogatónak, hogy Flux angolul is tud, és hol tudja átkapcsolni. Angol
    módban ez a sor üres, tehát ki sem kerül — aki már angolul olvas, annak
    nem kell felajánlani ugyanazt."""
    sorok = [
        {"sor": _t(nyelv, "zaro_kerdezz"), "szam": None, "cimke": None},
        {"sor": _t(nyelv, "zaro_fulek"), "szam": None, "cimke": None},
    ]
    tipp = _t(nyelv, "zaro_angol_tipp")
    if tipp:
        sorok.append({"sor": tipp, "szam": None, "cimke": None})
    return sorok


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
    # A `fc_nap` sorozat a célablak végéig tart, ami 14:00 után átnyúlik a
    # HOLNAPI napra. Ha ezen egyben veszünk maximumot, este a HOLNAPI déli
    # csúcs jelenik meg "ma még várható" értékként. Ezért minden órát a saját
    # dátuma szerint sorolunk be.
    meg = data.get("megujulo") or {}
    if meg.get("fc_nap") and meg.get("fc_szel"):
        idok = meg.get("ido") or []
        fc_nap, fc_szel = meg["fc_nap"], meg["fc_szel"]
        tny_nap = meg.get("tny_nap") or []
        tny_szel = meg.get("tny_szel") or []

        # (időbélyeg, érték) párokat tartunk, mert a csúcs ÓRÁJA is kell:
        # "13:00 körül tetőzik" hajnali egykor is értelmes mondat, a
        # "nap hátralévő részében várható" viszont ilyenkor képtelenség.
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


def _cache_key(facts_hash, nyelv=ALAP_NYELV):
    """A NYELV is a kulcs része.

    Enélkül a magyar látogatónak legyártott szöveg menne ki az angolnak is —
    ugyanaz az adatállapot, ugyanaz a kulcs. A napszak azért van benne, mert
    a köszönés különben átcsúszna a napszakhatáron ("Jó estét!" hajnalban)."""
    return (f"{facts_hash}:{GEMINI_MODEL}:{PROMPT_VERSION}:{_nyelv_ok(nyelv)}"
            f":{_napszak_kulcs()}")


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
# Ha egyszerre többen nyitják meg az oldalt, a Gemini ingyenes kerete percek
# alatt kimerülhet. Onnantól minden hívás 429-cel jön vissza — de mindegyik
# VÁRAKOZÁSSAL, tehát a látogató úgy éli meg, hogy Flux lassú és néma. Ezért
# az első kvóta-hiba után egy ideig meg sem próbáljuk.
_KVOTA_LOCK = threading.Lock()
_KVOTA_TILTAS_PERC = 10
# Modellenként külön tiltás: ha az egyik kerete betelt, a másiké még élhet.
_kvota_tiltva_eddig = {}

# A főoldali szöveg legyártása egyszerre csak EGY szálon fusson.
_GYARTAS_LOCK = threading.Lock()

# A kérdésekre adott válaszok is gyorsítótárba kerülnek.
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


def _frissit_koszonto(uzenetek, nyelv=ALAP_NYELV):
    """A köszöntő a NAPSZAKHOZ igazodik, a gyorsítótár viszont 35 percig él.
    A tárolt szöveg első eleme ezért mindig frissen készül — így a napszakhatár
    átlépésekor sem marad kint a régi köszönés."""
    if not uzenetek:
        return uzenetek
    elso = uzenetek[0]
    if isinstance(elso, dict) and elso.get("kezdo"):
        uj_lista = list(uzenetek)
        uj_lista[0] = {"sor": elo_koszonto(nyelv), "szam": None,
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

def _ido(iso, nyelv=ALAP_NYELV):
    """Óra:perc, elé 'holnap'/'tomorrow' vagy dátum, ha nem a mai napra esik."""
    t = _ts(iso)
    if t is None:
        return ""
    ma = _most().date()
    if t.date() == ma:
        return t.strftime("%H:%M")
    if (t.date() - ma).days == 1:
        return _t(nyelv, "holnap_elotag") + t.strftime("%H:%M")
    return t.strftime("%m.%d. %H:%M")


def _ora_sav(iso, nyelv=ALAP_NYELV):
    """A csúcsóra kezdő és záró időpontja: '18:00 és 19:00' / '18:00 and 19:00'."""
    t = _ts(iso)
    if t is None:
        return ""
    return (f"{_ido(iso, nyelv)}{_t(nyelv, 'es_kotoszo')}"
            f"{(t + timedelta(hours=1)):%H:%M}")


def _ezres(x, tizedes=0):
    return f"{x:,.{tizedes}f}".replace(",", " ")


def _nap_mondat(mg, nyelv=ALAP_NYELV):
    """A napelemes termelés mondata — a NAPSZAKHOZ igazítva.

    Három eset van:
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
        ido = _ido(mg.get("nap_mai_tetozes_ido") or mg.get("nap_mai_csucs_ido"), nyelv)
        sor = _t(nyelv, "nap_lecsengett", ido=ido, cs=cs)
        # A holnapi terv KÜLÖN mondatban, egyértelmű címkével.
        if mg.get("nap_holnapi_csucs_mw"):
            sor += _t(nyelv, "nap_holnapi_terv",
                      cs=_ezres(mg["nap_holnapi_csucs_mw"]))
        return {"sor": sor, "szam": f"{cs} MW", "cimke": _t(nyelv, "c_nap_tetozes")}

    if mg.get("nap_csucs_meg_hatravan") and mg.get("nap_mai_csucs_mw"):
        cs = _ezres(mg["nap_mai_csucs_mw"])
        ido = _ido(mg.get("nap_mai_csucs_ido"), nyelv)
        return {"sor": _t(nyelv, "nap_meg_hatra", ido=ido, cs=cs),
                "szam": f"{cs} MW", "cimke": _t(nyelv, "c_nap_csucs")}

    if mg.get("nap_hatralevo_csucs_mw"):
        # A napi csúcs már elmúlt, de még van termelés hátra.
        cs = _ezres(mg["nap_hatralevo_csucs_mw"])
        sor = ""
        if mg.get("nap_mai_csucs_mw"):
            sor = _t(nyelv, "nap_tetozes_volt",
                     ido=_ido(mg.get("nap_mai_csucs_ido"), nyelv),
                     cs=_ezres(mg["nap_mai_csucs_mw"]))
        sor += _t(nyelv, "nap_hatralevo", cs=cs)
        return {"sor": sor, "szam": f"{cs} MW", "cimke": _t(nyelv, "c_nap_hatra")}
    return None


def _tegnap_mondat(f, nyelv=ALAP_NYELV):
    """Összevetés a TEGNAPI azonos órával — mért adatból, nem jóslatból."""
    t = f.get("tegnapi_osszevetes") or {}
    sz = t.get("elteres_szazalek")
    if sz is None:
        return None
    ma_v, teg_v = _ezres(t["mai_mwh"]), _ezres(t["tegnapi_mwh"])
    if abs(sz) < 1:
        sor = _t(nyelv, "tegnap_azonos", ora=t["ora"], ma=ma_v, teg=teg_v)
    else:
        irany = _t(nyelv, "tegnap_tobb" if sz > 0 else "tegnap_kevesebb")
        sor = _t(nyelv, "tegnap_elter", ora=t["ora"], ma=ma_v,
                 sz=f"{abs(sz):.0f}", irany=irany, teg=teg_v)
    atl = t.get("atlag_elteres_szazalek")
    if atl is not None and abs(atl) >= 1:
        sor += _t(nyelv, "tegnap_atlag", sz=f"{abs(atl):.0f}",
                  irany=_t(nyelv, "tegnap_magasabb" if atl > 0
                           else "tegnap_alacsonyabb"))
    # A kiemelt szám és a mondat NEM mondhat mást. 0,76%-nál a mondat azt írja,
    # hogy "gyakorlatilag ugyanannyi", a `+.0f` viszont +1%-ra kerekített.
    if abs(sz) < 0.05:
        szam = "0%"
    elif abs(sz) < 1:
        szam = f"{sz:+.1f}%".replace(".", ",")
    else:
        szam = f"{sz:+.0f}%"
    return {"sor": sor, "szam": szam, "cimke": _t(nyelv, "c_tegnap")}


def _ho_mondat(ho, f, nyelv=ALAP_NYELV):
    """A hőmérséklet mondata a HŐMÉRSÉKLETHEZ igazítva.

    A hűtés csak melegben magyarázat, a fűtés csak hidegben; a kettő között
    egyik sem, ilyenkor a napi szélsőértékek mondanak többet."""
    ido = f.get("idojaras") or {}
    if ho >= 24:
        kulcs = "ho_meleg"
    elif ho <= 10:
        kulcs = "ho_hideg"
    else:
        kulcs = "ho_kozepes"
    sor = _t(nyelv, kulcs, ho=f"{ho:.0f}")
    if ido.get("mai_max_c") is not None and ido.get("mai_min_c") is not None:
        sor += _t(nyelv, "ho_szelso", lo=f"{ido['mai_min_c']:.0f}",
                  hi=f"{ido['mai_max_c']:.0f}")
    return sor


def _szel_mondat(mg, nyelv=ALAP_NYELV):
    """A széltermelés mondata — azzal kezdve, ami MÉG ELŐTTÜNK VAN."""
    if not mg or mg.get("szel_mai_csucs_mw") is None:
        return None

    hatra = mg.get("szel_hatralevo_csucs_mw")
    if hatra is not None:
        cs = _ezres(hatra)
        sor = _t(nyelv, "szel_hatra", cs=cs)
        if mg.get("szel_mai_atlag_mw") is not None:
            sor += _t(nyelv, "szel_atlag", a=_ezres(mg["szel_mai_atlag_mw"]))
        return {"sor": sor, "szam": f"{cs} MW", "cimke": _t(nyelv, "c_szel_hatra")}

    # A mai nap lezárult: ilyenkor a napi csúcs a mondanivaló, múlt időben.
    cs = _ezres(mg["szel_mai_csucs_mw"])
    sor = _t(nyelv, "szel_tetozott",
             ido=_ido(mg.get("szel_mai_csucs_ido"), nyelv), cs=cs)
    if mg.get("szel_mai_atlag_mw") is not None:
        sor += _t(nyelv, "szel_atlag", a=_ezres(mg["szel_mai_atlag_mw"]))
    return {"sor": sor, "szam": f"{cs} MW", "cimke": _t(nyelv, "c_szel_csucs")}


def sablon_uzenetek(f, nyelv=ALAP_NYELV):
    """Élő megállapítások, tárgyilagos megfogalmazásban — a kért nyelven."""
    u = [{"sor": _t(nyelv, "koszonto_statikus"), "szam": None, "cimke": None}]

    k = f.get("kartyak") or {}
    d = f.get("dam")
    t = f.get("toltes")
    fo = f.get("fogyasztas")
    ho = k.get("budapest_homerseklet_c")

    if k.get("jelenlegi_dam_ar_eur_mwh") is not None and d and d.get("mai_atlag_eur_mwh"):
        most = k["jelenlegi_dam_ar_eur_mwh"]
        atl = d["mai_atlag_eur_mwh"]
        viszony = _t(nyelv, "viszony_kedvezobb" if most < atl else "viszony_magasabb")
        u.append({
            "sor": _t(nyelv, "s_dam", most=f"{most:.0f}", atl=f"{atl:.0f}",
                      viszony=viszony),
            "szam": f"{most:.0f} €/MWh", "cimke": _t(nyelv, "c_dam_most")})

    if t and t.get("ajanlott_ar_eur_mwh") is not None:
        u.append({
            "sor": _t(nyelv, "s_toltes",
                      k=_ido(t["ajanlott_kezdet"], nyelv),
                      v=_ido(t["ajanlott_veg"], nyelv),
                      ar=f"{t['ajanlott_ar_eur_mwh']:.0f}"),
            "szam": f"{t['ajanlott_ar_eur_mwh']:.0f} €/MWh",
            "cimke": _t(nyelv, "c_legkedvezobb")})

    if fo and fo.get("elorejelzett_csucs_mwh"):
        csucs = _ezres(fo["elorejelzett_csucs_mwh"])
        if fo.get("aktualis_ora_mwh"):
            akt = _ezres(fo["aktualis_ora_mwh"])
            sor = _t(nyelv, "s_fogy_jelen", akt=akt)
            # Az `_ora_sav` maga kiírja a "holnap" szót, ha az időpont nem mai —
            # ezért a "mai" jelzőt csak akkor tesszük ki, ha ott nem hangzik el.
            mai = bool(fo.get("csucs_ma_van"))
            if fo.get("csucs_meg_hatravan"):
                sor += _t(nyelv, "s_csucs_hatra_mai" if mai else "s_csucs_hatra",
                          sav=_ora_sav(fo["csucs_idopont"], nyelv), csucs=csucs)
            else:
                sor += _t(nyelv, "s_csucs_volt_mai" if mai else "s_csucs_volt",
                          ido=_ido(fo["csucs_idopont"], nyelv), csucs=csucs)
            u.append({"sor": sor, "szam": f"{akt} MWh",
                      "cimke": _t(nyelv, "c_jelen_ora")})
        elif fo.get("csucs_meg_hatravan"):
            u.append({
                "sor": _t(nyelv, "s_csucs_onallo",
                          sav=_ora_sav(fo["csucs_idopont"], nyelv), csucs=csucs),
                "szam": f"{csucs} MWh", "cimke": _t(nyelv, "c_csucs")})
        else:
            u.append({
                "sor": _t(nyelv, "s_csucs_kialakult",
                          ido=_ido(fo["csucs_idopont"], nyelv), csucs=csucs),
                "szam": f"{csucs} MWh", "cimke": _t(nyelv, "c_csucs_volt")})
        if ho is not None and ho >= 35:
            u.append({"sor": _t(nyelv, "s_hoseg_tipp"), "szam": None, "cimke": None})
        sz_el = fo.get("elteres_heti_atlagtol_szazalek")
        if sz_el is not None and abs(sz_el) >= 1:
            u.append({
                "sor": _t(nyelv, "s_heti_tobb" if sz_el > 0 else "s_heti_keves",
                          sz=f"{abs(sz_el):.0f}"),
                "szam": f"{sz_el:+.0f}%", "cimke": _t(nyelv, "c_heti_elteres")})

    teg = _tegnap_mondat(f, nyelv)
    if teg:
        u.append(teg)

    if ho is not None:
        u.append({"sor": _ho_mondat(ho, f, nyelv), "szam": f"{ho:.0f} °C",
                  "cimke": _t(nyelv, "c_ho")})

    mg = f.get("megujulok")
    nap_sor = _nap_mondat(mg, nyelv)
    if nap_sor:
        u.append(nap_sor)

    szel_sor = _szel_mondat(mg, nyelv)
    if szel_sor:
        u.append(szel_sor)

    m = f.get("modell_pontossag")
    elso = (m or {}).get("catboost_elso_jóslat_mae_mwh")
    if m and elso is not None:
        u.append({
            "sor": _t(nyelv, "s_modell_mae", mae=f"{elso:.0f}"),
            "szam": f"{elso:.0f} MWh", "cimke": _t(nyelv, "c_modell_mae")})

    a = f.get("adatminoseg")
    if a and a["jelzes_7_nap"]:
        u.append(_anomalia_uzenet(a, nyelv))

    return u


def _anomalia_reszek(a, nyelv):
    """A szokatlan órák OKAI, felsorolásként. Külön függvény, mert a főoldali
    körhinta és a kérdésre adott válasz is ugyanezt a bontást használja —
    két helyen tartva biztosan elcsúszna."""
    reszek = []
    if a.get("extrem_idojaras_db"):
        reszek.append(_t(nyelv, "s_anom_extrem", db=a["extrem_idojaras_db"]))
    if a.get("alacsony_napsugarzas_db"):
        reszek.append(_t(nyelv, "s_anom_napelem", db=a["alacsony_napsugarzas_db"]))
    if a.get("homersekleti_fordulat_db"):
        reszek.append(_t(nyelv, "s_anom_fordulat", db=a["homersekleti_fordulat_db"]))
    return reszek


def _anomalia_uzenet(a, nyelv):
    sor = _t(nyelv, "s_anom_fo", db=a["jelzes_7_nap"])
    reszek = _anomalia_reszek(a, nyelv)
    if reszek:
        sor += _t(nyelv, "s_anom_hatter", reszek=", ".join(reszek))
    if a.get("meg_vizsgalando_db"):
        sor += _t(nyelv, "s_anom_nyitott", db=a["meg_vizsgalando_db"])
    return {"sor": sor, "szam": _t(nyelv, "u_ora", db=a["jelzes_7_nap"]),
            "cimke": _t(nyelv, "c_szokatlan")}


# ============================================================
# 5) GEMINI + ELLENŐRZÉS
# ============================================================

# A `tenyek()` által előállított JSON túlnyomó része szám és mezőnév — azzal a
# nyelvi modell bármelyik nyelven elboldogul. Két helyen van benne MAGYAR
# SZÖVEG mint érték, és pont ez a kettő okozna zavart angol módban: az STL
# trendjének iránya, és az anomália-kategória neve. Ezeket lefordítjuk,
# mielőtt a promptba kerülnének — így az angol válaszba nem szivárog be
# magyar szó.
_ERTEK_FORDITAS_EN = {
    "emelkedő": "rising",
    "csökkenő": "falling",
    "stabil": "stable",
    "extrem": "extreme weather",
    "napelem": "low solar irradiance",
    "fordulat": "temperature swing",
    "rejtely": "under investigation",
    "visszaeses": "unexpected drop",
    "besorolatlan": "unclassified",
}


def _tenyek_nyelvhez(f, nyelv):
    """A tényadatok másolata, a szöveges ÉRTÉKEKKEL a kért nyelven.

    A mezőNEVEK maradnak magyarul: azokra hivatkozik a prompt, és a modell
    kulcsként kezeli őket, nem olvasandó szövegként."""
    if _nyelv_ok(nyelv) != "en":
        return f
    masolat = json.loads(json.dumps(f, ensure_ascii=False, default=str))
    stl = masolat.get("stl") or {}
    if stl.get("trend_iranya") in _ERTEK_FORDITAS_EN:
        stl["trend_iranya"] = _ERTEK_FORDITAS_EN[stl["trend_iranya"]]
    legfr = (masolat.get("adatminoseg") or {}).get("legfrissebb") or {}
    if legfr.get("kategoria") in _ERTEK_FORDITAS_EN:
        legfr["kategoria"] = _ERTEK_FORDITAS_EN[legfr["kategoria"]]
    return masolat

class GeminiKvotaHiba(RuntimeError):
    """Elfogyott a nyelvi modell kerete (HTTP 429 / RESOURCE_EXHAUSTED).

    Ez nem programhiba, hanem átmeneti állapot: a keret idővel újratöltődik."""


class GeminiLassuHiba(RuntimeError):
    """A nyelvi modell nem válaszolt időben."""


def _gemini(prompt, sema, timeout=GEMINI_TIMEOUT, szerep=None, homerseklet=0.4):
    """Végigpróbálja az elérhető modelleket, amíg valamelyik válaszol."""
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
            # mező beengedte volna az 1-et az elfogadott számok közé.
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

def uzenetek(data, ajanlas=None, koszonto=None, nyelv=ALAP_NYELV):
    """A főoldali Flux-szövegek a kért nyelven.

    Sosem dob kivételt: hiba esetén sablonra vált."""
    nyelv = _nyelv_ok(nyelv)
    f, minoseg = tenyek(data, ajanlas)
    if f is None:
        return [{"sor": _t(nyelv, "varok_adatra"), "szam": None, "cimke": None}]

    # Alapertelmezesben elo koszonto keszul: napszak szerinti koszones.
    # A hivo felulirhatja sajat szoveggel.
    koszonto = koszonto or elo_koszonto(nyelv)

    tartalek = (sablon_uzenetek(f, nyelv) + _modell_uzenetek(nyelv)
                + _zaro_uzenetek(nyelv))
    if koszonto:
        # A "kezdo" jelzés miatt a böngésző a köszöntőt EGYSZER játssza le,
        # utána kihagyja a körből.
        tartalek[0] = {"sor": koszonto, "szam": None, "cimke": None, "kezdo": True}

    fh = _hash(f)
    ck = _cache_key(fh, nyelv)

    # 1) Folyamaton belüli gyorsítótár — nulla hálózat.
    kesz = _memo_olvas(ck)
    if kesz is not None:
        return _frissit_koszonto(kesz, nyelv)

    # 2) Adatbázis-gyorsítótár — másik worker már elkészíttethette.
    if _db_ok():
        try:
            tarolt = _summary_olvas(ck)
            if tarolt:
                ertek = json.loads(tarolt)
                _memo_ir(ck, ertek)
                return _frissit_koszonto(ertek, nyelv)
        except Exception as e:
            print(f"[FLUX] Gyorsítótár olvasás: {e}", flush=True)

    # Egyszerre csak egy szál gyárt. A zár NEM várakozó.
    if not _GYARTAS_LOCK.acquire(blocking=False):
        return tartalek
    try:
        kesz = _memo_olvas(ck)
        if kesz is not None:
            return _frissit_koszonto(kesz, nyelv)
        return _frissit_koszonto(
            _gyart(f, ck, fh, minoseg, koszonto, tartalek, nyelv), nyelv)
    finally:
        _GYARTAS_LOCK.release()


def _gyart(f, ck, fh, minoseg, koszonto, tartalek, nyelv=ALAP_NYELV):
    """A főoldali szöveg tényleges legyártása. Csak a `_GYARTAS_LOCK` alatt fut."""
    allapot, model_nev, hibak = "fallback", "deterministic-template-v1", []
    vegleges = tartalek

    if not FOOLDAL_GEMINI:
        # A saját szöveg megy ki, hívás nélkül. Így a napi keret érintetlen
        # marad a kérdésekre.
        _memo_ir(ck, vegleges)
        return vegleges

    try:
        prompt = (
            "Az alábbi JSON a magyar villamosenergia-rendszer élő adatait tartalmazza.\n"
            f"{json.dumps(_tenyek_nyelvhez(f, nyelv), ensure_ascii=False, default=str)}\n\n"
            + ("A VÁLASZ NYELVE ANGOL. Minden mondatot angolul írj — a mezőnevek "
               "magyarul vannak, de a szöveged angol legyen.\n"
               if _nyelv_ok(nyelv) == "en" else "")
            +
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
                        + jo + _modell_uzenetek(nyelv) + _zaro_uzenetek(nyelv))
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
# 7) A KÖSZÖNÉS — a szerver dolga, nem a modellé
#
# Három baj volt vele, és mindhárom ugyanabból fakadt: rábíztuk a nyelvi
# modellre.
#
#   1) Éjfél után "Jó reggelt!". A prompt megmondta a helyes alakot
#      (hajnalban "Szia!"), de a modell nem tartotta be.
#
#   2) Kiszámíthatatlan köszönés. Két helyről jöhet válasz: a Geminitől
#      (az köszönt) és a determinisztikus `_sajat_valasz`-tól (az soha).
#      Ha a Gemini kerete betelt vagy lassú volt, csendben átváltottunk a
#      másikra — a látogató szempontjából Flux hol köszönt, hol nem.
#
#   3) Kétnyelvűen ugyanez hatványozódna: a modell angol válasz elé is
#      odatehetne egy magyar köszönést, vagy fordítva.
#
# Mostantól egyetlen szabály van, MINDKÉT ágon és MINDKÉT nyelven: a modell
# köszönését levágjuk, és ha ez a látogató első kérdése, a napszakhoz és a
# nyelvhez illő köszönést mi tesszük elé.
# ============================================================

# A magyar ÉS angol köszönések a válasz elején. Toldalékkal, vesszővel,
# felkiáltójellel és a gyakori "Szia, X!" formával is — a névvel együtt
# levágjuk.
_KOSZONES_RE = re.compile(
    r"^\s*(?:jó\s+reggelt(?:\s+kívánok)?|jó\s+napot(?:\s+kívánok)?|"
    r"jó\s+estét(?:\s+kívánok)?|jó\s+éjszakát|jó\s+délutánt|"
    r"szervusz|szia(?:sztok)?|hell?ó|hello|hali|heló|"
    r"üdvözöllek|üdvözlöm|üdv|csá|cső|szeva|"
    r"good\s+morning|good\s+afternoon|good\s+evening|good\s+day|"
    r"hi\s+there|hi|hey\s+there|hey|greetings|welcome)"
    # A megszólítás is ide tartozik: a "Szia, Anna!" nevét egyben vágjuk le a
    # köszönéssel. Enélkül "Anna! A csúcs..." maradt volna a mondat elején —
    # ami rosszabb, mint az eredeti köszönés. Csak EGY nagybetűs szó eshet ide,
    # így a "Szia, a mai ár..." folytatásba nem harap bele.
    r"(?:\s*,\s*[A-ZÁÉÍÓÖŐÚÜŰ][\w\u00e1\u00e9\u00ed\u00f3\u00f6\u0151\u00fa\u00fc\u0171]{1,14})?"
    r"[\s,!.…-]*",
    re.IGNORECASE,
)


def _koszones_levag(sor):
    """Levágja a mondat elejéről a köszönést, és nagybetűsíti a maradékot.

    Ha a köszönés levágása után nem maradna semmi, az eredeti mondat megy
    vissza — nem csinálunk üres választ."""
    if not sor:
        return sor
    m = _KOSZONES_RE.match(sor)
    if not m:
        return sor
    torzs = sor[m.end():].lstrip()
    if not torzs:
        return sor
    return torzs[0].upper() + torzs[1:]


def _koszones_javit(u, mar_koszont, nyelv=ALAP_NYELV):
    """A válasz köszönésének egységesítése.

    `mar_koszont=True`  -> a látogató már járt itt ebben a munkamenetben:
                           minden köszönés lekerül, rögtön a lényeggel kezdünk.
    `mar_koszont=False` -> ez az első kérdése: a modell saját köszönését
                           levágjuk, és a NAPSZAKHOZ ÉS NYELVHEZ ILLŐT
                           tesszük elé.
    """
    if not u or not u.get("sor"):
        return u
    torzs = _koszones_levag(u["sor"])
    ki = dict(u)
    ki["sor"] = torzs if mar_koszont else f"{_udvozles(nyelv)} {torzs}"
    return ki


# ============================================================
# 8) A LÁTOGATÓ KÉRDÉSE
#
# Alapelv: Flux SOHA ne hallgasson el. Először determinisztikus, az élő
# adatokból számolt választ állítunk elő a kérdés témájára — ez akkor is
# kész van, ha nincs Gemini-kulcs, ha a Gemini időtúllépéssel elszáll, vagy
# ha a válasza elbukik a szám-ellenőrzésen. A Gemini csak akkor kerül a
# helyére, ha valóban átment minden ellenőrzésen.
#
# A NYELV ugyanaz a két ágon: mindkettő a kapott `nyelv` paraméterrel
# dolgozik, ezért nem fordulhat elő, hogy a nyelvi modell angolul felel, a
# tartalék pedig magyarul.
# ============================================================

def _u(sor, szam=None, cimke=None):
    return {"sor": sor, "szam": szam, "cimke": cimke}


# ============================================================
# BESZÉLGETÉS-ELŐZMÉNY
#
# Eddig minden kérdés önmagában állt: Flux egyetlen fordulóra emlékezett,
# arra sem — csak arra a mondatra, amit a látogató épp a képernyőn olvasott.
# Emiatt esett szét a beszélgetés. Aki megkérdezte, hogy "holnap mikor
# tölthetek?", majd megköszönte a választ, az egy általános "szívesen"-t
# kapott: Flux nem tudta, MIT köszönnek meg.
#
# Ez a néhány sor adja meg a folytonosságot. Az előzmény a böngészőben él
# (dcc.Store), és minden kérdéssel együtt visszaérkezik — a szerver
# állapotmentes marad, több worker mellett is működik.
#
# Miért csak az utolsó néhány forduló? Két okból. A prompt hossza pénz és
# idő; ennél fontosabb viszont, hogy a RÉGI válaszokban RÉGI SZÁMOK vannak.
# Ha tíz fordulót adnánk oda, a modell könnyen egy fél órával korábbi árat
# írna le mai adatként. Négy forduló elég a "és ez?", "köszi", "és holnap?"
# típusú visszautalásokhoz, és még nem elég ahhoz, hogy elavult szám
# szivárogjon vissza.
# ============================================================

ELOZMENY_MAX = 4          # ennyi kérdés-válasz párt viszünk magunkkal
ELOZMENY_KERDES_MAX = 200  # egy kérdésből ennyi karakter megy a promptba
ELOZMENY_VALASZ_MAX = 320  # egy válaszból ennyi


def elozmeny_bovit(elozmeny, kerdes, valasz_sor):
    """Az új forduló hozzáfűzése, a lista hosszának korlátozásával.

    Az app.py hívja, miután a válasz elkészült. Azért itt van és nem ott,
    mert a korlátok ide tartoznak: ha egyszer változtatni kell rajtuk, egy
    helyen legyen."""
    lista = list(elozmeny or [])
    k = str(kerdes or "").strip()
    v = str(valasz_sor or "").strip()
    if not k or not v:
        return lista[-ELOZMENY_MAX:]
    lista.append({"k": k[:ELOZMENY_KERDES_MAX], "v": v[:ELOZMENY_VALASZ_MAX]})
    return lista[-ELOZMENY_MAX:]


def _elozmeny_szoveg(elozmeny, nyelv):
    """Az előzmény promptba illeszthető alakja. Üres előzménynél üres szöveg."""
    sorok = []
    for forduló in (elozmeny or [])[-ELOZMENY_MAX:]:
        k = str(forduló.get("k") or "").strip()
        v = str(forduló.get("v") or "").strip()
        if not k or not v:
            continue
        if _nyelv_ok(nyelv) == "en":
            sorok.append(f"Visitor: {k}\nYou: {v}")
        else:
            sorok.append(f"Látogató: {k}\nTe: {v}")
    if not sorok:
        return ""
    if _nyelv_ok(nyelv) == "en":
        return ("THE CONVERSATION SO FAR (oldest first). Use it to resolve "
                "references like 'this', 'that one', 'and tomorrow?', and to "
                "avoid repeating yourself. CAUTION: the numbers in these earlier "
                "answers may already be out of date — any number you write must "
                "come from the JSON below, never from this history.\n"
                + "\n".join(sorok) + "\n\n")
    return ("AZ EDDIGI BESZÉLGETÉS (a legrégebbi elöl). Ebből tudod feloldani "
            "az 'ez', 'az', 'és holnap?' típusú visszautalásokat, és ebből "
            "látod, mit mondtál már el. FIGYELEM: a korábbi válaszokban lévő "
            "számok már elavultak lehetnek — amit leírsz, az KIZÁRÓLAG az "
            "alábbi JSON-ból jöhet, soha nem az előzményből.\n"
            + "\n".join(sorok) + "\n\n")


def _elozmeny_ujjlenyomat(elozmeny):
    """Rövid ujjlenyomat a gyorsítótár-kulcshoz.

    Enélkül a "köszönöm" MINDIG ugyanazt a tárolt választ adná vissza,
    függetlenül attól, mit köszönt meg a látogató — pedig épp az előzmény
    teszi értelmessé."""
    if not elozmeny:
        return "0"
    nyers = "|".join(f"{r.get('k','')}" for r in elozmeny[-ELOZMENY_MAX:])
    return hashlib.sha256(nyers.encode("utf-8")).hexdigest()[:10]


# Témák: (kulcsszavak, kezelő függvény). A sorrend számít — az első
# egyező téma nyer, ezért a specifikusabb kulcsszavak állnak elöl.
def _t_udvozles(f, nyelv):
    """Ha a látogató köszön vagy megszólít, Flux visszaköszön — a köszönést
    itt NEM tesszük bele, azt a `_koszones_javit()` illeszti a helyére."""
    kep = _t_kepessegek(f, nyelv)
    sor = _t(nyelv, "h_orulok")
    sor += kep["sor"] if kep else _t(nyelv, "h_orulok_nincs")
    return _u(sor, None, _t(nyelv, "c_amirol"))


def _t_koszonom(f, nyelv):
    return _u(random.choice([_t(nyelv, "h_szivesen1"), _t(nyelv, "h_szivesen2"),
                             _t(nyelv, "h_szivesen3")]), None, None)


def _t_kepessegek(f, nyelv):
    temak = []
    if f.get("dam") or f.get("toltes"):
        temak.append(_t(nyelv, "kep_ar"))
    if f.get("fogyasztas"):
        temak.append(_t(nyelv, "kep_fogyasztas"))
    if f.get("megujulok"):
        temak.append(_t(nyelv, "kep_megujulo"))
    if f.get("kartyak", {}).get("budapest_homerseklet_c") is not None or f.get("idojaras"):
        temak.append(_t(nyelv, "kep_ido"))
    if f.get("kartyak", {}).get("eur_huf") is not None:
        temak.append(_t(nyelv, "kep_arfolyam"))
    if f.get("modell_pontossag") or f.get("heti_merleg"):
        temak.append(_t(nyelv, "kep_modell"))
    if f.get("adatminoseg") or f.get("stl"):
        temak.append(_t(nyelv, "kep_stl"))
    if not temak:
        return None
    return _u(_t(nyelv, "h_kep_bevezeto") + "; ".join(temak) + ".",
              None, _t(nyelv, "c_amirol"))


def _t_ar(f, nyelv):
    d, t = f.get("dam") or {}, f.get("toltes") or {}
    k = f.get("kartyak") or {}
    most = k.get("jelenlegi_dam_ar_eur_mwh")
    if most is not None and d.get("mai_atlag_eur_mwh") is not None:
        atl = d["mai_atlag_eur_mwh"]
        if most < atl:
            viszony = _t(nyelv, "viszony_kedvezobb")
        elif most > atl:
            viszony = _t(nyelv, "viszony_magasabb")
        else:
            viszony = _t(nyelv, "viszony_azonos")
        sor = _t(nyelv, "h_ar_fo", most=f"{most:.0f}", atl=f"{atl:.0f}",
                 viszony=viszony, lo=f"{d['mai_min_eur_mwh']:.0f}",
                 hi=f"{d['mai_max_eur_mwh']:.0f}",
                 olcso=d["mai_legolcsobb_ora"], draga=d["mai_legdragabb_ora"])
        return _u(sor, f"{most:.0f} €/MWh", _t(nyelv, "c_dam_most"))
    if d.get("mai_atlag_eur_mwh") is not None:
        return _u(_t(nyelv, "h_ar_atlag", atl=f"{d['mai_atlag_eur_mwh']:.0f}",
                     lo=f"{d['mai_min_eur_mwh']:.0f}",
                     hi=f"{d['mai_max_eur_mwh']:.0f}"),
                  f"{d['mai_atlag_eur_mwh']:.0f} €/MWh",
                  _t(nyelv, "c_dam_mai_atlag"))
    if t.get("aktualis_ar_eur_mwh") is not None:
        return _u(_t(nyelv, "h_ar_egyszeru", most=f"{t['aktualis_ar_eur_mwh']:.0f}"),
                  f"{t['aktualis_ar_eur_mwh']:.0f} €/MWh", _t(nyelv, "c_dam_most"))
    return None


def _t_holnapi_ar(f, nyelv):
    d = f.get("dam") or {}
    if d.get("holnapi_atlag_eur_mwh") is not None:
        return _u(_t(nyelv, "h_holnap_van", atl=f"{d['holnapi_atlag_eur_mwh']:.0f}",
                     lo=f"{d['holnapi_min_eur_mwh']:.0f}",
                     hi=f"{d['holnapi_max_eur_mwh']:.0f}"),
                  f"{d['holnapi_atlag_eur_mwh']:.0f} €/MWh",
                  _t(nyelv, "c_holnapi_atlag"))
    if d and not d.get("holnapi_ar_publikalva"):
        return _u(_t(nyelv, "h_holnap_nincs"), None, _t(nyelv, "c_holnapi_arak"))
    return None


def _t_toltes(f, nyelv):
    t = f.get("toltes") or {}
    if t.get("ajanlott_ar_eur_mwh") is None:
        return None
    sor = _t(nyelv, "h_toltes_fo", k=_ido(t["ajanlott_kezdet"], nyelv),
             v=_ido(t["ajanlott_veg"], nyelv),
             ar=f"{t['ajanlott_ar_eur_mwh']:.0f}")
    if t.get("negativ_ar_az_ablakban"):
        sor += _t(nyelv, "h_toltes_negativ")
    if t.get("most_is_kedvezo"):
        sor += _t(nyelv, "h_toltes_most")
    if t.get("tovabbi_ablakok"):
        masodik = t["tovabbi_ablakok"][0]
        sor += _t(nyelv, "h_toltes_tovabbi", k=_ido(masodik["kezdet"], nyelv),
                  ar=f"{masodik['ar_eur_mwh']:.0f}")
    return _u(sor, f"{t['ajanlott_ar_eur_mwh']:.0f} €/MWh",
              _t(nyelv, "c_legkedvezobb"))


def _t_fogyasztas(f, nyelv):
    fo = f.get("fogyasztas") or {}
    if not fo.get("elorejelzett_csucs_mwh"):
        mert = f.get("mert_fogyasztas") or {}
        if mert.get("ertek_mwh"):
            return _u(_t(nyelv, "h_fogy_mert", v=_ezres(mert["ertek_mwh"]),
                         ido=mert["idopont"]),
                      f"{_ezres(mert['ertek_mwh'])} MWh", _t(nyelv, "c_mert_fogy"))
        return None
    csucs = _ezres(fo["elorejelzett_csucs_mwh"])
    # A kérdésre a JELEN órával kezdünk — a látogatót az érdekli, most mi van.
    if fo.get("aktualis_ora_mwh"):
        sor = _t(nyelv, "h_fogy_jelen", akt=_ezres(fo["aktualis_ora_mwh"]))
        if fo.get("csucs_meg_hatravan"):
            sor += _t(nyelv, "h_fogy_csucs_hatra",
                      sav=_ora_sav(fo["csucs_idopont"], nyelv), csucs=csucs)
        else:
            sor += _t(nyelv, "h_fogy_csucs_volt",
                      ido=_ido(fo["csucs_idopont"], nyelv), csucs=csucs)
    else:
        sor = _t(nyelv, "h_fogy_ablak", n=fo["orak_szama"],
                 sav=_ora_sav(fo["csucs_idopont"], nyelv), csucs=csucs)
    sz_el = fo.get("elteres_heti_atlagtol_szazalek")
    if sz_el is not None and abs(sz_el) >= 1:
        sor += _t(nyelv, "h_fogy_heti_tobb" if sz_el > 0 else "h_fogy_heti_keves",
                  sz=f"{abs(sz_el):.0f}")
    return _u(sor, f"{csucs} MWh", _t(nyelv, "c_csucs"))


def _t_napenergia(f, nyelv):
    mg = f.get("megujulok") or {}
    alap = _nap_mondat(mg, nyelv)
    if not alap:
        if mg.get("nap_holnapi_csucs_mw"):
            cs = _ezres(mg["nap_holnapi_csucs_mw"])
            return _u(_t(nyelv, "nap_holnapi_onallo", cs=cs), f"{cs} MW",
                      _t(nyelv, "c_nap_holnap"))
        return None
    # Kérdésre a mért értéket is hozzátesszük, ha van — az a "mi történik MOST".
    if mg.get("nap_eddigi_merte_csucs_mw"):
        alap = dict(alap)
        alap["sor"] += _t(nyelv, "nap_mert_ma",
                          cs=_ezres(mg["nap_eddigi_merte_csucs_mw"]))
    return alap


def _t_szel(f, nyelv):
    mg = f.get("megujulok") or {}
    alap = _szel_mondat(mg, nyelv)
    if not alap:
        return None
    if mg.get("szel_utolso_mert_mw") is not None:
        alap = dict(alap)
        alap["sor"] += _t(nyelv, "szel_utolso_mert",
                          v=_ezres(mg["szel_utolso_mert_mw"]),
                          ido=_ido(mg.get("szel_utolso_mert_idopont"), nyelv))
    return alap


def _t_megujulo_pontossag(f, nyelv):
    mg = f.get("megujulok") or {}
    if mg.get("nap_mai_mae_mw") is None and mg.get("szel_mai_mae_mw") is None:
        return None
    reszek = []
    if mg.get("nap_mai_mae_mw") is not None:
        reszek.append(_t(nyelv, "h_meg_nap", v=_ezres(mg["nap_mai_mae_mw"])))
    if mg.get("szel_mai_mae_mw") is not None:
        reszek.append(_t(nyelv, "h_meg_szel", v=_ezres(mg["szel_mai_mae_mw"])))
    kiemelt = mg.get("nap_mai_mae_mw", mg.get("szel_mai_mae_mw"))
    return _u(_t(nyelv, "h_meg_fo",
                 reszek=_t(nyelv, "es_kotoszo").join(reszek)),
              f"{_ezres(kiemelt)} MW", _t(nyelv, "c_megujulo_mae"))


def _t_homerseklet(f, nyelv):
    k = f.get("kartyak") or {}
    ido = f.get("idojaras") or {}
    ho = k.get("budapest_homerseklet_c")
    if ho is None and not ido:
        return None
    if ho is not None:
        sor = _ho_mondat(ho, f, nyelv)
    else:
        sor = _t(nyelv, "ho_nincs_mert")
        if ido.get("mai_max_c") is not None:
            sor += _t(nyelv, "ho_napi", hi=f"{ido['mai_max_c']:.0f}",
                      lo=f"{ido['mai_min_c']:.0f}")
    if ido.get("holnapi_max_c") is not None:
        sor += _t(nyelv, "ho_holnap", hi=f"{ido['holnapi_max_c']:.0f}")
    return _u(sor, f"{ho:.0f} °C" if ho is not None else None,
              _t(nyelv, "c_ho") if ho is not None else _t(nyelv, "c_idojaras"))


def _t_arfolyam(f, nyelv):
    e = (f.get("kartyak") or {}).get("eur_huf")
    if e is None:
        return None
    return _u(_t(nyelv, "h_arfolyam", e=f"{e:.1f}"), f"{e:.1f} Ft",
              _t(nyelv, "c_arfolyam"))


def _t_modell(f, nyelv):
    m = f.get("modell_pontossag") or {}
    h = f.get("heti_merleg") or {}
    if m.get("catboost_elso_jóslat_mae_mwh") is not None:
        elso = m["catboost_elso_jóslat_mae_mwh"]
        sor = _t(nyelv, "h_modell_nap", nap=m["nap"], orak=m["orak_szama"],
                 mae=f"{elso:.0f}")
        if m.get("catboost_frissitett_mae_mwh") is not None:
            sor += _t(nyelv, "h_modell_frissitett",
                      mae=f"{m['catboost_frissitett_mae_mwh']:.0f}")
        return _u(sor, f"{elso:.0f} MWh", _t(nyelv, "c_modell_mae"))
    if h.get("catboost_elso_jóslat_mae_mwh") is not None:
        return _u(_t(nyelv, "h_modell_heti", orak=h["kiertekelt_orak"],
                     mae=f"{h['catboost_elso_jóslat_mae_mwh']:.0f}"),
                  f"{h['catboost_elso_jóslat_mae_mwh']:.0f} MWh",
                  _t(nyelv, "c_heti_mae"))
    return _u(_t(nyelv, "h_modell_altalanos"), None, _t(nyelv, "c_ket_modell"))


def _t_anomalia(f, nyelv):
    a = f.get("adatminoseg") or {}
    s = f.get("stl") or {}
    if a.get("jelzes_7_nap"):
        return _anomalia_uzenet(a, nyelv)
    if s.get("vizsgalt_napok"):
        # A trend iránya magyar szó a tényadatban — angol módban lefordítjuk,
        # különben egyetlen magyar szó maradna az angol mondat közepén.
        irany = s.get("trend_iranya") or _t(nyelv, "h_stl_stabil")
        if _nyelv_ok(nyelv) == "en":
            irany = _ERTEK_FORDITAS_EN.get(irany, irany)
        return _u(_t(nyelv, "h_stl_altalanos", nap=s["vizsgalt_napok"],
                     db=s["szokatlan_orak_db"], irany=irany),
                  _t(nyelv, "u_ora", db=s["szokatlan_orak_db"]),
                  _t(nyelv, "c_szokatlan_stl"))
    return None


def _t_mert_fogyasztas(f, nyelv):
    mert = f.get("mert_fogyasztas") or {}
    if not mert.get("ertek_mwh"):
        return None
    return _u(_t(nyelv, "h_mert", ido=mert["idopont"],
                 v=_ezres(mert["ertek_mwh"])),
              f"{_ezres(mert['ertek_mwh'])} MWh", _t(nyelv, "c_mert_fogy"))


def _t_tegnap(f, nyelv):
    return _tegnap_mondat(f, nyelv)


def _t_ido_most(f, nyelv):
    """"Hány óra van?" / "what time is it?" — ettől látszik, hogy Flux
    tudja, mikor nézi valaki az oldalt."""
    meta = f.get("_meta") or {}
    if not meta.get("helyi_ido"):
        return None
    return _u(_t(nyelv, "h_ido", ido=meta["helyi_ido"],
                 napszak=_napszak(nyelv=nyelv)),
              meta["helyi_ido"], _t(nyelv, "c_pontos_ido"))


def _t_frissites(f, nyelv):
    meta = f.get("_meta") or {}
    if not meta.get("frissites"):
        return None
    sor = _t(nyelv, "h_frissites", ido=meta["frissites"])
    if meta.get("hianyzo_forrasok"):
        sor += _t(nyelv, "h_frissites_hianyzo",
                  lista=", ".join(meta["hianyzo_forrasok"]))
    return _u(sor, None, _t(nyelv, "c_frissites"))


# TÁRSALGÁSI témák. Ezek KÜLÖN listában vannak, és csak akkor kerülnek sorra,
# ha egyetlen ÉRDEMI téma sem talált. Amíg egy listában voltak, a "Szia,
# mennyi az ár?" kérdésre Flux csak visszaköszönt és felsorolta, mit tud —
# mert a köszönés hamarabb illeszkedett, mint az ár.
#
# Az angol kulcsszavak ugyanabban a listában vannak, mint a magyarok: a
# felismerés így nyelvtől függetlenül működik, és a "what's the price?"
# ugyanarra a kezelőre fut, mint a "mennyi az ár?".
_TARSALGAS = [
    (("köszönöm", "köszi", "kösz ", "hálás",
      "thank", "thanks", "cheers"), _t_koszonom),
    (("szia", "helló", "hello", "hali", "jó reggelt", "jó estét", "jó napot",
      "üdv", "csá",
      "=hi", "=hey", "good morning", "good evening", "good afternoon"),
     _t_udvozles),
    (("mit tudsz", "miben tudsz", "mire vagy", "ki vagy", "mit csinálsz", "segít",
      "miről tudsz", "mit kérdez", "mihez értesz", "mi ez az oldal",
      "what can you", "who are you", "what do you do", "=help", "what can i ask",
      "what is this", "what's this"), _t_kepessegek),
]


_TEMAK = [
    (("holnapi ár", "holnap ár", "holnapi day", "holnapi dam", "holnap mennyi lesz az ár",
      "holnapi árak",
      "tomorrow price", "tomorrow's price", "price tomorrow", "prices tomorrow"),
     _t_holnapi_ar),
    (("tölt", "mikor kapcsol", "mikor indít", "mosogép", "mosógép", "olcsó ablak",
      "legolcsóbb",
      "=charge", "charging", "when to charge", "cheapest hour", "cheapest time"),
     _t_toltes),
    (("napelem", "napenergia", "szolár", "napsugár",
      "=solar", "=pv", "photovolta"), _t_napenergia),
    (("szél", "szeler", "=wind", "turbine"), _t_szel),
    (("megújuló pontos", "termelés pontos", "napelőtti terv",
      "renewable accuracy", "generation accuracy", "day-ahead plan"),
     _t_megujulo_pontossag),
    (("árfolyam", "eur/huf", "eurhuf", "forint", "euró árfolyam", "huf",
      "exchange rate"), _t_arfolyam),
    (("mavir", "catboost", "modell", "pontos", "mae", "hiba", "előrejelzés jó",
      "mennyire pontos", "gépi tanul", "mesterséges intelligencia",
      "=model", "accura", "machine learning", "how good is the forecast",
      "artificial intelligence"), _t_modell),
    (("anomál", "szokatlan", "stl", "adatminőség", "eltérés a szokásos", "rendellenes",
      "nyitott eset",
      "anomal", "unusual", "data quality", "outlier"), _t_anomalia),
    (("tegnap", "tegnapi", "előző nap", "tegnaphoz", "elmúlt naphoz",
      "mennyivel nőtt", "mennyivel csökkent", "változott a fogyaszt",
      "yesterday"), _t_tegnap),
    (("hány óra", "mennyi az idő", "milyen napszak", "éjszaka van", "nappal van",
      "pontos idő",
      "what time", "time is it", "current time"), _t_ido_most),
    (("mért fogyasztás", "aktuális fogyasztás", "most mennyi a fogyasztás",
      "measured consumption", "actual consumption"), _t_mert_fogyasztas),
    (("fogyaszt", "terhel", "csúcs", "mwh", "mennyit fogyaszt", "rendszerterhel",
      "consumption", "=load", "=peak", "demand"), _t_fogyasztas),
    (("hőmérsék", "meleg", "hideg", "fok", "időjárás", "eső", "°c", "hány fok",
      "temperature", "weather", "degrees", "how warm", "how cold", "=rain"),
     _t_homerseklet),
    (("ár", "árak", "olcsó", "drága", "dam", "day-ahead", "tőzsd", "€", "eur/mwh",
      "piac",
      "=price", "cheap", "expensive", "market", "spot"), _t_ar),
    (("frissít", "adatforrás", "honnan", "mikor frissül", "milyen adat",
      "=update", "data source", "how often"), _t_frissites),
]


# Rövid, emberi felütések. Nem díszítés: enélkül minden válasz ugyanazzal a
# szikár adatmondattal indul, és Flux úgy hat, mint egy kijelzőtábla.
#
# A régi változat ÖT felütést használt, KÖRBEN. Öt kérdés után tehát pontosan
# ugyanaz jött vissza, ugyanabban a sorrendben — a látogató ezt hamar
# észreveszi, és pont az ellenkezőjét éri el annak, amiért bekerült. Több
# változat, véletlen sorrendben, és a legutóbbit kihagyjuk, hogy ne
# ismétlődjön kétszer egymás után.
_FELUTESEK = {
    "hu": [
        "Nézzük. ", "Épp jókor kérded. ", "Megnéztem az élő adatokat. ",
        "Erre tudok válaszolni. ", "Máris. ", "Jó kérdés. ",
        "Rögtön megnézem. ", "Erre pont van friss adatom. ",
        "Mindjárt mondom. ", "Épp erről beszélnek a mai számok. ",
        "Egy pillanat, itt van. ", "Erre könnyű válaszolni. ",
    ],
    "en": [
        "Let's see. ", "Good timing. ", "I've checked the live data. ",
        "I can answer that. ", "Right away. ", "Good question. ",
        "Let me look. ", "I have fresh data on exactly that. ",
        "Here you go. ", "Today's numbers speak to this. ",
        "One moment — here it is. ", "That's an easy one. ",
    ],
}
_utolso_felutes = {"szoveg": None}

# A felütés nélkül maradó válaszok kezdetei: ezek már eleve személyesek,
# nem kell eléjük semmi.
_FELUTES_KIVETEL = ("Szívesen", "Nagyon szívesen", "Örülök", "Most ",
                    "You're welcome", "Anytime", "Glad to help",
                    "Good to see", "It's ")


def _felutessel(u, nyelv=ALAP_NYELV):
    """Emberi felütés a válasz elé. A saját válaszainkra vonatkozik — a
    Gemini a saját szerepéből amúgy is így fogalmaz."""
    if not u or not u.get("sor"):
        return u
    if u["sor"].startswith(_FELUTES_KIVETEL):
        return u
    lista = _FELUTESEK.get(_nyelv_ok(nyelv), _FELUTESEK[ALAP_NYELV])
    jeloltek = [x for x in lista if x != _utolso_felutes["szoveg"]]
    felutes = random.choice(jeloltek or lista)
    _utolso_felutes["szoveg"] = felutes
    ki = dict(u)
    ki["sor"] = felutes + u["sor"]
    return ki


# Egyertelmuen TEMAN KIVULI kerdesek. Ezek a modellt EL SEM ERIK: azonnal
# udvarias elharitas megy ki. Ketto haszna van. Egyreszt szakmai: egy
# energiapiaci iranyitopult asszisztense ne adjon recepteket. Masreszt
# gyakorlati: a napi keret nem fogy el olyan kerdesekre, amelyekre amugy sem
# valaszolnank.
_TEMAN_KIVUL = (
    "recept", "főz", "foz", "süt", "sut", "vacsora", "ebéd", "étel", "koktél",
    "vicc", "mesélj", "vers", "novella", "sztori", "dalszöveg", "horoszkóp",
    "fordítsd", "forditsd", "translate", "kód", "programozz", "python",
    "focimeccs", "meccs", "sport", "film", "sorozat", "zene", "játék",
    "politika", "választás", "kormány", "miniszter", "háború",
    "gyógyszer", "betegség", "orvos", "diéta", "fogyó",
    "randi", "szerelem", "kapcsolat", "hogy vagy", "mit csinálsz ma este",
    # angol
    "recipe", "=cook", "cooking", "dinner", "cocktail", "=joke", "poem",
    "lyrics", "horoscope", "=code", "programming", "football", "movie",
    "series", "music", "=game", "politic", "election", "government", "=war",
    "medicine", "illness", "doctor", "=diet", "dating", "how are you",
)

# A biztonsagi halo: ha a kerdesben SEMMILYEN energiaval kapcsolatos szo nincs,
# a temán kívüli talalat valoban temán kívülit jelent.
_ENERGIA_SZAVAK = (
    "ár", "dam", "mwh", "mw", "€", "eur", "huf", "forint", "tölt", "fogyaszt",
    "terhel", "csúcs", "napelem", "napenergia", "szolár", "szél", "megújuló",
    "termel", "hőmérsék", "fok", "időjárás", "modell", "catboost", "mavir",
    "előrejelz", "pontos", "anomál", "szokatlan", "stl", "energia", "áram",
    "villany", "piac", "tőzsd", "adat", "flux", "okosmérő", "oldal", "grafikon",
    # angol
    "=price", "energy", "power", "electric", "consumption", "=solar", "=wind",
    "renewable", "=grid", "market", "forecast", "=model", "temperature",
    "weather", "=charge", "=data", "chart", "dashboard",
)


def _illeszkedik(k, minta):
    """Illeszkedik-e a minta a kérdésre?

    Három eset van:

    - `=szo`  -> PONTOS szóhatár mindkét oldalon, egyszerű többes számmal.
      Ez az angol rövid szavakhoz kell: a "wind" különben beleillene a
      "window"-ba, a "load" a "download"-ba, a "hi" pedig szinte mindenbe.

    - négy karakternél rövidebb minta -> szó eleji egyezés. Az "ár" három
      betűje ott van a "vacsorára", a "határ" és a "január" szóban is — puszta
      részszöveg-kereséssel a "receptet vacsorára" energiakérdésnek minősült
      volna.

    - minden más -> részszöveg. A magyar toldalékolás miatt ez kell:
      a "fogyaszt" illeszkedjen a "fogyasztásra" is.
    """
    if minta.startswith("="):
        szo = re.escape(minta[1:])
        return re.search(r"(?<!\w)" + szo + r"(?:s|es|ing|ed)?(?!\w)", k) is not None
    if len(minta) >= 4:
        return minta in k
    return re.search(r"(?<!\w)" + re.escape(minta), k) is not None


def _teman_kivul(kerdes):
    k = str(kerdes).lower()
    if not any(_illeszkedik(k, m) for m in _TEMAN_KIVUL):
        return False
    # Ha energiával kapcsolatos szó is van benne, inkább válaszolunk:
    # a "mennyibe kerül az áram főzéshez?" jogos kérdés.
    return not any(_illeszkedik(k, m) for m in _ENERGIA_SZAVAK)


def _elharitas(f, nyelv):
    kep = _t_kepessegek(f, nyelv)
    sor = _t(nyelv, "h_elharitas")
    sor += kep["sor"] if kep else _t(nyelv, "h_elharitas_nincs")
    return _u(sor, None, _t(nyelv, "c_amirol"))


def _elozo_kerdesek(elozmeny):
    """A korábbi látogatói kérdések, a LEGFRISSEBBTŐL visszafelé.

    Azért lista és nem egyetlen kérdés: ha az előző forduló csak egy
    "köszönöm" volt, abban nincs téma, és eggyel korábbra kell lépni. A
    "holnap mikor tölthetek? / köszi / és holnap?" sorozat így is a töltési
    ablaknál köt ki, nem egy újabb köszönetnél."""
    return [str(r.get("k") or "").strip()
            for r in reversed(elozmeny or []) if str(r.get("k") or "").strip()]


def _sajat_valasz(kerdes, f, nyelv=ALAP_NYELV, csak_erdemi=False):
    """Determinisztikus válasz a kérdés témájára, kizárólag az élő adatokból.

    Ez a válasz akkor is elkészül, ha a Gemini nem elérhető — ezért Flux
    sosem marad néma. A számok közvetlenül a `tenyek()` mezőiből jönnek,
    ezért ellenőrzésre nincs szükség: nincs honnan kitalálni semmit.

    `csak_erdemi=True` esetén a társalgási témák (köszönés, köszönet,
    képességek) ki vannak zárva. Ez akkor kell, amikor egy visszautaló
    kérdés ("és holnap?") témáját az ELŐZMÉNYBŐL keressük: ha az előző
    forduló épp egy "köszönöm" volt, abból nem szabad témát csinálni —
    különben Flux másodszor is megköszöni a köszönetet."""
    k = str(kerdes).lower().strip()
    if not k:
        return None

    def _fut(lista):
        talalatok = []
        for minta, kezelo in lista:
            if not any(_illeszkedik(k, m) for m in minta):
                continue
            try:
                v = kezelo(f, nyelv)
            except Exception as e:
                print(f"[FLUX] Téma ({kezelo.__name__}): {e}", flush=True)
                v = None
            if v:
                talalatok.append(v)
        return talalatok

    # 1) ÉRDEMI témák. Egy kérdésben több dolog is szerepelhet ("mennyi az ár
    # és mennyi a napenergia?") — ilyenkor mindkettőre válaszolunk, nem csak
    # az elsőre. Kettőnél többet nem fűzünk össze, mert olvashatatlan lenne.
    talalt = _fut(_TEMAK)
    if talalt:
        if len(talalt) == 1:
            return _felutessel(talalt[0], nyelv)
        egyesitett = dict(talalt[0])
        egyesitett["sor"] = " ".join(t["sor"] for t in talalt[:2])
        return _felutessel(egyesitett, nyelv)

    # 2) Csak ha SEMMILYEN érdemi téma nem talált, jön a köszönés vagy a
    # képességek felsorolása. Így a "Szia, mennyi az ár?" az árra válaszol.
    if csak_erdemi:
        return None
    tars = _fut(_TARSALGAS)
    if tars:
        return tars[0]
    return None


def valasz(kerdes, data, ajanlas=None, kontextus=None, mar_koszont=False,
           nyelv=ALAP_NYELV, elozmeny=None):
    """A látogató kérdésére adott egyetlen válasz.

    Sorrend: (1) determinisztikus válasz a témára, (2) Gemini, ha átmegy az
    ellenőrzésen, (3) ha a téma sem talált, elmondjuk, miről tudunk beszélni.

    NYELV: a `nyelv` paraméter a HU/EN kapcsoló állása. Ha viszont a kérdés
    egyértelműen a másik nyelven érkezett, a felismerés felülírja — és a
    visszaadott dict `nyelv` mezőjében jelezzük is, hogy az app.py át tudja
    billenteni a kapcsolót. Így nem fordulhat elő, hogy egy angol válasz után
    magyarul folytatódik a körhinta.

    ELŐZMÉNY: az utolsó néhány kérdés-válasz pár. Ettől lesz beszélgetés a
    kérdezősködésből — a "köszi", az "és holnap?" és a "miért?" csak ebből
    érthető meg. A számok viszont SOHA nem az előzményből jönnek: a prompt
    ezt külön kimondja, a szám-ellenőrzés pedig az élő tényadatokhoz méri a
    választ, tehát egy elavult ár nem tud visszaszivárogni.

    Kivételt sosem dob."""
    nyelv = _nyelv_ok(nyelv)
    felismert = felismer_nyelv(kerdes)
    if felismert and felismert != nyelv:
        print(f"[FLUX] Nyelvváltás felismerve: {nyelv} -> {felismert}", flush=True)
        nyelv = felismert

    f, _ = tenyek(data, ajanlas)
    if f is None:
        return {**_u(_t(nyelv, "varok_adatra_kerdes")), "nyelv": nyelv}
    if not str(kerdes).strip():
        return {**_u(_t(nyelv, "ures_kerdes"), None, _t(nyelv, "c_amirol")),
                "nyelv": nyelv}

    # 0/a) Témán kívüli kérdés: a modellt el sem érjük, így keretet sem fogyaszt.
    if _teman_kivul(kerdes):
        print(f"[FLUX] Témán kívüli kérdés: {str(kerdes)[:80]!r}", flush=True)
        return {**_koszones_javit(_elharitas(f, nyelv), mar_koszont, nyelv),
                "nyelv": nyelv}

    # 0/b) Ugyanarra a kérdésre, ugyanabban az adatállapotban ne hívjuk újra a
    # modellt. A köszönés-jelző, a nyelv ÉS az előzmény is a kulcs része:
    # különben az első látogatónak készült, köszönéssel kezdődő válasz menne
    # ki annak is, aki már régóta itt van; a magyar válasz az angolul
    # kérdezőnek; és ugyanaz a "köszönöm" annak is, aki egészen másra kérdezett
    # rá az előző fordulóban.
    valasz_kulcs = (f"{_hash(f)}:{nyelv}:{int(bool(mar_koszont))}:"
                    f"{_elozmeny_ujjlenyomat(elozmeny)}:"
                    f"{' '.join(str(kerdes).lower().split())[:120]}")
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
    biztos = _sajat_valasz(kerdes, f, nyelv)
    if biztos is None:
        # "És holnap?" — a kérdésben nincs téma, mert az előző fordulóban volt.
        # Előbb az ELŐZŐ KÉRDÉST próbáljuk: az a látogató saját szava, tehát
        # pontosabban mondja meg a témát, mint a képernyőn futó szöveg.
        for elozo in _elozo_kerdesek(elozmeny):
            biztos = _sajat_valasz(elozo, f, nyelv, csak_erdemi=True)
            if biztos is not None:
                break
    if biztos is None and kontextus:
        # "És ez?" — a kérdés a képernyőn olvasott mondatra utal vissza.
        biztos = _sajat_valasz(kontextus, f, nyelv)
    if biztos is None:
        # Naplózzuk, mire nem találtunk témát — ebből bővíthető a kulcsszólista.
        print(f"[FLUX] Nem talált témát a kérdésre: {str(kerdes)[:120]!r}", flush=True)

    # 2) A Gemini csak akkor kerül a helyére, ha valóban jobb és hiteles.
    elonezet = ""
    try:
        meta = f.get("_meta") or {}
        angol = nyelv == "en"
        prompt = (
            (f"It is {meta.get('helyi_ido')} in Budapest, so it is "
             f"{_napszak(nyelv=nyelv)}. Match the tone and the tenses to that.\n"
             "Do NOT greet at the start of your answer — no 'Hi', no 'Good "
             "morning', no other greeting. The system inserts the greeting if "
             "one is needed.\n"
             "ANSWER IN ENGLISH. The field names in the JSON are Hungarian, but "
             "your sentences must be English.\n\n"
             "Live data:\n"
             if angol else
             f"Most {meta.get('helyi_ido')} van Budapesten, tehát "
             f"{_napszak(nyelv=nyelv)} van. Ehhez igazítsd a hangnemet és az "
             f"igeidőket.\n"
             "NE köszönj a válasz elején — sem 'Szia', sem 'Jó reggelt', sem más "
             "köszönés. A köszönést a rendszer illeszti oda, ha kell.\n\n"
             "Élő adatok:\n")
            + f"{json.dumps(_tenyek_nyelvhez(f, nyelv), ensure_ascii=False, default=str)}\n\n"
            + _elozmeny_szoveg(elozmeny, nyelv)
            + ((f"The visitor was reading EXACTLY THIS statement on screen when "
                f"they asked: \"{str(kontextus).strip()[:300]}\"\n"
                f"If the question refers back to it ('this', 'that', 'why'), it "
                f"relates to this.\n\n"
                if angol else
                f"A látogató ÉPPEN EZT a megállapítást olvasta a képernyőn, "
                f"amikor kérdezett: \"{str(kontextus).strip()[:300]}\"\n"
                f"Ha a kérdés visszautal rá ('ez', 'erről', 'miért'), erre "
                f"vonatkozik.\n\n") if kontextus else "")
            + (f"The visitor's question: {str(kerdes).strip()[:300]}\n\n" if angol
               else f"A látogató kérdése: {str(kerdes).strip()[:300]}\n\n")
            + ("Answer with ONE item in the given format. 'sor' is the "
               "conversational answer (2-4 sentences), 'szam' is a single "
               "highlighted value with its unit from the data, and 'cimke' says in "
               "2-5 words what that number is. You may use the 'megujulok', "
               "'fogyasztas', 'tegnapi_osszevetes', 'dam', 'toltes', 'kartyak', "
               "'idojaras', 'modell_pontossag', 'heti_merleg', 'stl' and "
               "'adatminoseg' fields.\n"
               "TIME: if a timestamp is not today, say that it is tomorrow's.\n"
               "SOLAR: 'nap_csucs_meg_hatravan' tells you whether today's peak is "
               "still ahead. If 'nap_termeles_mara_lezarult' is present, you may "
               "only speak about today's output in the past tense; "
               "'nap_holnapi_csucs_mw' is TOMORROW's plan, always name it as such.\n"
               "Only say you cannot answer if there really is no field in the data "
               "for the topic of the question."
               if angol else
               "Válaszolj EGY elemmel a megadott formában. A 'sor' a beszélgető "
               "válasz (2-4 mondat), a 'szam' egyetlen kiemelt érték mértékegységgel "
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
               "TÉNYLEG nincs mező az adatokban.")
        )
        v = _gemini(prompt, _UZENET_SEMA, timeout=GEMINI_KERDES_TIMEOUT,
                    szerep=(FLUX_SZEREP_KERDES_EN if angol else FLUX_SZEREP_KERDES),
                    homerseklet=GEMINI_KERDES_HOFOK)
        jo, hibak = _ellenoriz(v.get("uzenetek", [])[:1], f)
        if jo:
            # A modell köszönését levágjuk, és ha kell, a NAPSZAKHOZ ÉS NYELVHEZ
            # ILLŐT tesszük elé. Ez az a pont, ahol az éjfél utáni "Jó reggelt!"
            # végleg megszűnik: nem kérjük a modelltől, hanem mi tesszük oda.
            kesz = {**_koszones_javit(jo[0], mar_koszont, nyelv), "nyelv": nyelv}
            _valasz_memo_ir(valasz_kulcs, kesz)
            return kesz
        print(f"[FLUX] Kérdés elbukott az ellenőrzésen: {hibak}", flush=True)
    except GeminiKvotaHiba as e:
        # Ez nem hiba, hanem átmeneti állapot — a látogató megérdemli, hogy
        # megtudja, miért lett Flux hirtelen szűkszavú.
        print(f"[FLUX] Kérdés — kvóta: {e}", flush=True)
        elonezet = _t(nyelv, "kvota_elonezet")
    except GeminiLassuHiba as e:
        # Az időtúllépés MÚLÉKONY: a következő kérdésnél már rendben lehet.
        # Ilyenkor nem magyarázkodunk, egyszerűen válaszolunk a saját, adatból
        # számolt mondattal — az is pontos.
        print(f"[FLUX] Kérdés — lassú: {e}", flush=True)
    except Exception as e:
        print(f"[FLUX] Kérdés: {e}", flush=True)

    if biztos:
        if elonezet:
            biztos = dict(biztos)
            # A magyarázó mondat után a saját mondat kisbetűvel folytatódik.
            sor = biztos["sor"]
            biztos["sor"] = elonezet + (sor[0].lower() + sor[1:] if sor else sor)
            return {**_koszones_javit(biztos, mar_koszont, nyelv), "nyelv": nyelv}
        # A determinisztikus ág is köszön, ha ez az első kérdés — enélkül a
        # látogató attól függően kapott köszönést, hogy épp élt-e a Gemini
        # kerete. Ez volt a "néha köszön, néha nem" oka.
        kesz = {**_koszones_javit(biztos, mar_koszont, nyelv), "nyelv": nyelv}
        # Csak a "tiszta" választ tesszük el. A kvóta-jelzés átmeneti
        # állapotot ír le, azt nem szabad 10 percre bebetonozni.
        _valasz_memo_ir(valasz_kulcs, kesz)
        return kesz

    # 3) Nem találtunk témát: ne egy üres "nem tudom" menjen ki, hanem az,
    # hogy pontosan miről lehet kérdezni ebben a pillanatban.
    kepessegek = _t_kepessegek(f, nyelv)
    if kepessegek:
        return {**_koszones_javit(
            _u(_t(nyelv, "nincs_valasz_de") + kepessegek["sor"], None,
               _t(nyelv, "c_amirol")), mar_koszont, nyelv), "nyelv": nyelv}
    return {**_koszones_javit(_u(_t(nyelv, "nincs_valasz")), mar_koszont, nyelv),
            "nyelv": nyelv}


# ============================================================
# 9) INDULÁSI DIAGNOSZTIKA
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
    f" | adatbázis: {'igen' if _db_ok() else 'nem'}"
    f" | nyelvek: {', '.join(NYELVEK)}",
    flush=True,
)


# ---- A két nyelvi szótár egyezésének ellenőrzése ----
#
# Ez az ára annak, hogy a mondatok szótárban vannak: ha később átírsz egy
# magyar mondatot és elfelejted az angolt, az csendben elcsúszna. Ez a néhány
# sor ezt kizárja: indulásnál kiírja a Render naplójába, melyik kulcs
# hiányzik, illetve melyik maradt üresen. Nem állítja meg az alkalmazást —
# a `_t()` amúgy is visszaesik a magyar változatra —, de nem is hallgat róla.
def _szotar_ellenorzes():
    hu, en = set(SZ["hu"]), set(SZ["en"])
    hianyzo_en = sorted(hu - en)
    hianyzo_hu = sorted(en - hu)
    # A szándékosan üres kulcsok (pl. az angol módban nem kellő nyelvi tipp)
    # nem hibák — ezeket külön soroljuk fel.
    szandekosan_ures = {"zaro_angol_tipp"}
    ures = sorted(k for ny in NYELVEK for k in SZ[ny]
                  if not str(SZ[ny][k]).strip() and k not in szandekosan_ures)
    if hianyzo_en or hianyzo_hu or ures:
        print(f"[FLUX] Nyelvi szótár — hiányzik angolból: {hianyzo_en or '-'} | "
              f"hiányzik magyarból: {hianyzo_hu or '-'} | üres: {ures or '-'}",
              flush=True)
    else:
        print(f"[FLUX] Nyelvi szótár rendben: {len(hu)} kulcs mindkét nyelven",
              flush=True)


_szotar_ellenorzes()
