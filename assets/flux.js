/* Flux gépelése — teljesen a Dash frissítési körén kívül.

   Miért így: a Dash bármikor újraépítheti az oldal tartalmát. Ha a gépelést
   callback hajtaná, minden újraépítés nullázná a szöveget — ezért villogott és
   kezdte elölről. Ez a hurok saját állapotot tart, és minden képkockán
   megkeresi a cél-elemeket; ha a Dash kicseréli őket, a szöveg egy pillanat
   alatt visszaíródik ugyanoda, ahol tartott. */

(function () {
  const FLUX_VERZIO = "2026-08-15-b";
  const KARAKTER_MS = 35;      // ~29 karakter / másodperc — gyorsan kiírja
  const KITARTAS_MIN = 6000;   // rövid megállapítás ennyit marad kint
  const KITARTAS_MAX = 10000;  // hosszú megállapítás legfeljebb ennyit

  /* A várakozó sor NYELVFÜGGŐ. A szerver a `window.FLUX_NYELV`-be teszi a
     HU/EN kapcsoló állását; ha még nincs beállítva, magyar az alapértelmezés.
     Enélkül az angolra váltott látogató minden kérdésnél kapott egy magyar
     mondatot — pont abban a pillanatban, amikor a legjobban figyel. */
  const VARAKOZO_SOROK = {
    hu: "Egy pillanat, megnézem az élő adatokat…",
    en: "One moment, let me check the live data…",
  };

  function varakozoSor() {
    return VARAKOZO_SOROK[window.FLUX_NYELV] || VARAKOZO_SOROK.hu;
  }

  /* Mennyi ideig maradjon kint a kész mondat.

     Nem a GÉPELÉSNEK kell lassúnak lennie — azt nézni idegőrlő —, hanem a
     kész mondatnak kell sokáig kint maradnia. Ezért a gépelés gyors, a
     kitartás viszont a mondat hosszához igazodik: rövidnél 6, hosszúnál
     10 másodperc. */
  function kitartas(sor) {
    const ms = 3000 + sor.length * 35;
    return Math.min(KITARTAS_MAX, Math.max(KITARTAS_MIN, ms));
  }

  const st = {
    i: 0, k: 0, utolso: 0, varakozasKezdet: 0,
    valaszKulcs: null, valaszAktiv: false, varakozott: false,
    kezdoLatott: false, korPozicio: 0,
    gepeltUtoljara: 0, egerRajta: false,
    passzivSzunetKezdet: 0,
  };

  const el = (id) => document.getElementById(id);

  function ujrakezd() {
    st.i = 0; st.k = 0; st.varakozasKezdet = 0;
  }

  function lista() {
    const d = window.FLUX_DATA || {};
    let uz = Array.isArray(d.uzenetek) ? d.uzenetek : [];

    /* A köszöntő EGYSZER megy le, utána kikerül a körből. */
    if (st.kezdoLatott) uz = uz.filter((m) => !m.kezdo);

    /* A kérdés elküldése és a válasz megérkezése között akár néhány másodperc
       is eltelhet. Ha ez alatt a szokásos körbe-körbe járó szöveg futna
       tovább, a látogató azt hiszi, Flux nem is hallotta meg a kérdést. */
    if (window.FLUX_VARAKOZAS) {
      if (!st.varakozott) { st.varakozott = true; ujrakezd(); }
      return [{ sor: varakozoSor(), szam: null, cimke: null }];
    }
    if (st.varakozott) { st.varakozott = false; ujrakezd(); }

    if (d.valasz) {
      /* A kulcsban benne van a szerver által adott sorszám is, ezért ugyanaz a
         kérdés másodszorra is lejátszódik. */
      const kulcs = JSON.stringify(d.valasz);
      if (st.valaszKulcs !== kulcs) {
        st.valaszKulcs = kulcs;
        st.valaszAktiv = true;
        st.korPozicio = st.i;   // hova térjünk vissza a válasz után
        ujrakezd();
      }
      if (st.valaszAktiv) return [d.valasz].concat(uz);
    }
    return uz;
  }

  function lepes(most) {
    const l = lista();
    const szoveg = el("flux-szoveg");
    if (!szoveg || !l.length) return;

    if (st.i >= l.length) st.i = 0;
    const uz = l[st.i] || {};
    const sor = uz.sor || "";

    if (st.k < sor.length) {
      if (most - st.utolso >= KARAKTER_MS) {
        st.utolso = most;
        st.k += 1;
      }
    } else if (!st.varakozasKezdet) {
      st.varakozasKezdet = most;
    } else if (most - st.varakozasKezdet >= kitartas(sor)) {
      st.varakozasKezdet = 0;
      st.k = 0;
      if (uz.kezdo) st.kezdoLatott = true;
      if (st.valaszAktiv) {
        /* A válasz után OTT folytatjuk, ahol a kör tartott. */
        st.valaszAktiv = false;
        st.i = st.korPozicio;
      } else {
        st.i = st.i + 1;
      }
      return;
    }

    const reszlet = sor.slice(0, st.k);
    if (szoveg.textContent !== reszlet) szoveg.textContent = reszlet;
    // Amit a látogató ÉPPEN lát — ez megy el a kérdéssel együtt a szervernek.
    if (!window.FLUX_VARAKOZAS) window.FLUX_AKTUALIS = sor;

    const kesz = st.k >= sor.length;
    const szam = el("flux-szam");
    const cimke = el("flux-cimke");
    const doboz = el("flux-szam-doboz");
    const ertek = kesz ? (uz.szam || "") : "";
    if (szam && szam.textContent !== ertek) szam.textContent = ertek;
    if (cimke) {
      const c = kesz ? (uz.cimke || "") : "";
      if (cimke.textContent !== c) cimke.textContent = c;
    }
    if (doboz) doboz.style.display = ertek ? "flex" : "none";
  }

  /* ------------------------------------------------------------------
     A SZÜNET — újraírva.

     A régi változat a fókuszt ESEMÉNYBŐL tartotta nyilván (`focusin` /
     `focusout`). Ez akkor romlik el, amikor a Dash újraépíti az oldalt: a
     beviteli mező ilyenkor ÚJ elemre cserélődik, a régi eltűnik — `focusout`
     viszont nem mindig fut le rá. A jelző ilyenkor hamis állapotba ragad, és
     onnantól vagy örökre szünetel, vagy soha többé nem szünetel.

     Emiatt viselkedett úgy, hogy CSAK AZ ELSŐ kérdésnél állt meg: a második
     gépeléskor a jelző már nem a valóságot mutatta.

     Mostantól minden képkockán a `document.activeElement`-et nézzük meg. Ez
     mindig a valóság, elemcsere után is.

     A szünet-okok két csoportba kerülnek:

       AKTÍV  — a látogató épp dolgozik: a mezőben a kurzor, friss leütés,
                vagy beírt, még el nem küldött szöveg. Ezt SEMMI nem szakítja
                félbe; ameddig gépel, addig áll a szöveg.

       PASSZÍV — az egér ottfelejtődött a szövegen, vagy megmaradt egy régi
                kijelölés. Ezek be tudnak ragadni, ezért 45 másodperc után a
                kör mindenképp folytatódik.
     ------------------------------------------------------------------ */
  const PASSZIV_SZUNET_MAX = 45000;
  const GEPELES_UTAN_MS = 8000;   // az utolsó leütés után ennyit még várunk

  document.addEventListener("input", function (e) {
    if (e.target && e.target.id === "flux-kerdes") st.gepeltUtoljara = Date.now();
  });
  /* A kattintás/fókusz is számít friss aktivitásnak: aki most kattintott a
     mezőbe, még le sem ütött egy billentyűt sem, de már gondolkodik. */
  document.addEventListener("focusin", function (e) {
    if (e.target && e.target.id === "flux-kerdes") st.gepeltUtoljara = Date.now();
  });
  document.addEventListener("mouseover", function (e) {
    st.egerRajta = !!(e.target && e.target.closest &&
                      e.target.closest(".flux-reteg"));
  });

  function mezoben() {
    const a = document.activeElement;
    return !!(a && a.id === "flux-kerdes");
  }

  function aktivSzunet() {
    // 1) A mezőben áll a kurzor — élő állapotból olvasva, nem eseményből.
    if (mezoben()) return true;
    // 2) Az utolsó leütés óta még nem telt el elég idő.
    if (st.gepeltUtoljara && Date.now() - st.gepeltUtoljara < GEPELES_UTAN_MS) {
      return true;
    }
    // 3) Van beírt, még el nem küldött szöveg.
    const mezo = el("flux-kerdes");
    return !!(mezo && mezo.value && mezo.value.trim());
  }

  function passzivSzunet() {
    // Egérrel a szöveg fölött — aki újra el akarja olvasni vagy le akarja
    // írni a számot, ne rohanjon el előle a mondat.
    if (st.egerRajta) return true;
    // A látogató épp KIJELÖLTE a szöveget, mert ki akarja másolni.
    try {
      const kijeloles = window.getSelection();
      if (kijeloles && !kijeloles.isCollapsed && kijeloles.rangeCount > 0) {
        const kozos = kijeloles.getRangeAt(0).commonAncestorContainer;
        const elem = kozos.nodeType === 1 ? kozos : kozos.parentElement;
        if (elem && elem.closest && elem.closest(".flux-reteg")) return true;
      }
    } catch (e) { /* nem baj, csak a kijelölés-szünet marad el */ }
    return false;
  }

  function szunetel() {
    /* A VÁLASZT semmi nem tarthatja fel: Enter után a kurzor a mezőben marad,
       tehát az aktív feltétel örökre igaz lenne, és a válasz kint ragadna. */
    if (st.valaszAktiv) { st.passzivSzunetKezdet = 0; return false; }

    if (aktivSzunet()) {
      // Aktív szünet: nincs időkorlát, és a passzív mérő is nullázódik.
      st.passzivSzunetKezdet = 0;
      return true;
    }

    if (passzivSzunet()) {
      if (!st.passzivSzunetKezdet) st.passzivSzunetKezdet = Date.now();
      return Date.now() - st.passzivSzunetKezdet <= PASSZIV_SZUNET_MAX;
    }

    st.passzivSzunetKezdet = 0;
    return false;
  }

  function hurok(most) {
    try {
      if (szunetel()) {
        /* Megállunk, de a félbehagyott mondatot végiggépeljük, hogy ne
           csonka szöveg maradjon a képernyőn. */
        const l = lista();
        const uz = l[Math.min(st.i, Math.max(0, l.length - 1))] || {};
        const sor = uz.sor || "";
        if (st.k < sor.length) lepes(most);
        else { st.varakozasKezdet = most; lepes(most); }
      } else {
        lepes(most);
      }
    } catch (e) { /* az oldal ettől ne álljon meg */ }
    requestAnimationFrame(hurok);
  }

  window.fluxIndit = function () { window.FLUX_VARAKOZAS = false; };
  window.fluxVarakozas = function () { window.FLUX_VARAKOZAS = true; };
  /* A szerver ezt kapja meg a kérdéssel együtt: így tudja, MELYIK
     megállapításra vonatkozik az "és ez mit jelent?" típusú kérdés. */
  window.fluxAktualis = function () { return window.FLUX_AKTUALIS || ""; };
  console.log("[FLUX] flux.js verzió: " + FLUX_VERZIO +
              " | gépelés " + KARAKTER_MS + " ms/karakter");
  requestAnimationFrame(hurok);
})();

