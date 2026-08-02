/* Flux gépelése — teljesen a Dash frissítési körén kívül.

   Miért így: a Dash 15 mp-enként újraépítheti az oldal tartalmát. Ha a
   gépelést callback hajtaná, minden újraépítés nullázná a szöveget — ezért
   villogott és kezdte elölről. Ez a hurok saját állapotot tart, és minden
   képkockán megkeresi a cél-elemeket; ha a Dash kicseréli őket, a szöveg
   egy pillanat alatt visszaíródik ugyanoda, ahol tartott. */

(function () {
  const KARAKTER_MS = 55;      // ~18 karakter / másodperc
  const KITARTAS_MS = 14000;   // ennyi ideig marad kint a kész megállapítás

  const st = {
    i: 0, k: 0, utolso: 0, varakozasKezdet: 0,
    valaszKulcs: null, valaszAktiv: false,
  };

  const el = (id) => document.getElementById(id);

  function lista() {
    const d = window.FLUX_DATA || {};
    const uz = Array.isArray(d.uzenetek) ? d.uzenetek : [];
    if (d.valasz) {
      const kulcs = JSON.stringify(d.valasz);
      if (st.valaszKulcs !== kulcs) {
        st.valaszKulcs = kulcs;
        st.valaszAktiv = true;
        st.i = 0; st.k = 0; st.varakozasKezdet = 0;
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
    } else if (most - st.varakozasKezdet >= KITARTAS_MS) {
      st.varakozasKezdet = 0;
      st.k = 0;
      if (st.valaszAktiv) { st.valaszAktiv = false; st.i = 0; }
      else { st.i = (st.i + 1) % l.length; }
      return;
    }

    const reszlet = sor.slice(0, st.k);
    if (szoveg.textContent !== reszlet) szoveg.textContent = reszlet;

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

  function hurok(most) {
    try { lepes(most); } catch (e) { /* az oldal ettől ne álljon meg */ }
    requestAnimationFrame(hurok);
  }

  window.fluxIndit = function () {};   // a Dash callback ezt hívja; az állapot már fut
  requestAnimationFrame(hurok);
})();
