/* Flux gépelése — teljesen a Dash frissítési körén kívül.

   Miért így: a Dash 15 mp-enként újraépítheti az oldal tartalmát. Ha a
   gépelést callback hajtaná, minden újraépítés nullázná a szöveget — ezért
   villogott és kezdte elölről. Ez a hurok saját állapotot tart, és minden
   képkockán megkeresi a cél-elemeket; ha a Dash kicseréli őket, a szöveg
   egy pillanat alatt visszaíródik ugyanoda, ahol tartott. */

(function () {
  const FLUX_VERZIO = "2026-08-04-a";
  const KARAKTER_MS = 95;      // ~10 karakter / másodperc — olvasható tempó
  const KITARTAS_MIN = 9000;   // rövid megállapítás ennyit marad kint
  const KITARTAS_MAX = 22000;  // hosszú megállapítás legfeljebb ennyit

  const VARAKOZO_SOR = "Egy pillanat, megnézem az élő adatokat…";

  /* Mennyi ideig maradjon kint a kész mondat.

     A korábbi 1,8-3,5 másodperc arra épült, hogy a látogató a gépelés KÖZBEN
     már olvas. Ez tévedés: a szemünk nem tud a megjelenő karakterekkel egy
     ütemben haladni, ráadásul a mondat vége — ahol a szám van — csak a
     legvégén kerül ki. Így a lényeget alig két másodpercre lehetett látni,
     megjegyezni pedig végképp nem. Egy 200 karakteres mondat elolvasása
     nyugodt tempóban 8-10 másodperc, ezért a kitartás mostantól a mondat
     hosszához igazodik, és jóval bőkezűbb. */
  function kitartas(sor) {
    const ms = 5000 + sor.length * 60;
    return Math.min(KITARTAS_MAX, Math.max(KITARTAS_MIN, ms));
  }

  const st = {
    i: 0, k: 0, utolso: 0, varakozasKezdet: 0,
    valaszKulcs: null, valaszAktiv: false, varakozott: false,
    kezdoLatott: false, korPozicio: 0,
    fokusz: false, gepeltUtoljara: 0, egerRajta: false,
  };

  const el = (id) => document.getElementById(id);

  function ujrakezd() {
    st.i = 0; st.k = 0; st.varakozasKezdet = 0;
  }

  function lista() {
    const d = window.FLUX_DATA || {};
    let uz = Array.isArray(d.uzenetek) ? d.uzenetek : [];

    /* A köszöntő EGYSZER megy le, utána kikerül a körből. Korábban minden
       fordulóban — és minden válasz után azonnal — újrakezdte a
       "Szia, Flux vagyok..." mondattal az egész felsorolást. */
    if (st.kezdoLatott) uz = uz.filter((m) => !m.kezdo);

    /* A kérdés elküldése és a válasz megérkezése között akár néhány másodperc
       is eltelhet (a szerver ilyenkor kérdezi meg az élő adatokat). Ha ez alatt
       a szokásos körbe-körbe járó szöveg futna tovább, a látogató azt hiszi,
       Flux nem is hallotta meg a kérdést. Ezért erre az időre egyetlen,
       egyértelmű sor marad kint. */
    if (window.FLUX_VARAKOZAS) {
      if (!st.varakozott) { st.varakozott = true; ujrakezd(); }
      return [{ sor: VARAKOZO_SOR, szam: null, cimke: null }];
    }
    if (st.varakozott) { st.varakozott = false; ujrakezd(); }

    if (d.valasz) {
      /* A kulcsban benne van a szerver által adott sorszám is, ezért ugyanaz a
         kérdés másodszorra is lejátszódik — korábban a változatlan szöveg miatt
         a második kérdésre látszólag semmi nem történt. */
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
        /* A válasz után OTT folytatjuk, ahol a kör tartott — nem ugrunk
           vissza a lista elejére, tehát nem indul újra a köszöntővel. */
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

  /* Amíg a látogató a kérdés-mezőben van, a szöveg MEGÁLL azon a
     megállapításon, amit éppen olvas. Enélkül mire megfogalmazta a kérdést,
     Flux már két témával odébb járt, és a kérdés a semmire vonatkozott. */
  /* A fókusz- és gépelés-figyelés a DOCUMENT szintjén megy, nem magán a
     mezőn. Ez azért fontos, mert a Dash az oldal újraépítésekor kicseréli a
     beviteli mezőt egy ÚJ elemre; a régire aggatott figyelő ilyenkor a
     semmivel maradna, és a szünet néma módon abbamaradna. A delegált
     figyelő az elemcserét is túléli. */
  document.addEventListener("focusin", function (e) {
    if (e.target && e.target.id === "flux-kerdes") st.fokusz = true;
  });
  document.addEventListener("focusout", function (e) {
    if (e.target && e.target.id === "flux-kerdes") st.fokusz = false;
  });
  document.addEventListener("input", function (e) {
    if (e.target && e.target.id === "flux-kerdes") st.gepeltUtoljara = Date.now();
  });
  document.addEventListener("mouseover", function (e) {
    st.egerRajta = !!(e.target && e.target.closest &&
                      e.target.closest(".flux-reteg"));
  });

  function szunetel() {
    // 1) A mezőben áll a kurzor.
    if (st.fokusz) return true;
    // 2) Az utolsó leütés óta 8 másodpercen belül vagyunk.
    if (st.gepeltUtoljara && Date.now() - st.gepeltUtoljara < 8000) return true;
    // 3) Egérrel a szöveg fölött — aki újra el akarja olvasni vagy le akarja
    //    írni a számot, ne rohanjon el előle a mondat.
    if (st.egerRajta) return true;
    // 4) A látogató épp KIJELÖLTE a szöveget, mert ki akarja másolni.
    //    A gépelő hurok minden képkockán újraírja a tartalmat, ami eldobná
    //    a kijelölést — ezért ilyenkor hozzá sem nyúlunk.
    try {
      const kijeloles = window.getSelection();
      if (kijeloles && !kijeloles.isCollapsed && kijeloles.rangeCount > 0) {
        const kozos = kijeloles.getRangeAt(0).commonAncestorContainer;
        const elem = kozos.nodeType === 1 ? kozos : kozos.parentElement;
        if (elem && elem.closest && elem.closest(".flux-reteg")) return true;
      }
    } catch (e) { /* nem baj, csak a kijelölés-szünet marad el */ }
    // 5) Van beírt, még el nem küldött szöveg.
    const mezo = el("flux-kerdes");
    return !!(mezo && mezo.value && mezo.value.trim());
  }

  function hurok(most) {
    try {
      if (szunetel()) {
        /* Megállunk, de a félbehagyott mondatot végiggépeljük, hogy ne
           csonka szöveg maradjon a képernyőn. */
        const l = lista();
        const uz = l[Math.min(st.i, l.length - 1)] || {};
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
