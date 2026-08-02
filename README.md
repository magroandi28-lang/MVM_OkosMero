OkosMérő

Automatizált villamosenergia-adatelemző, fogyasztás-előrejelző és energiapiaci döntéstámogató rendszer

Az OkosMérő az MVM DataInsight diplomamunkából továbbfejlesztett, működő villamosenergia-adatelemző és előrejelző alkalmazás. A rendszer több külső adatforrásból származó fogyasztási, energiapiaci, időjárási, árfolyam- és megújulóenergia-adatot integrál, majd ezekből órás fogyasztási előrejelzést, day-ahead árelemzést, megújulóenergia-monitoringot, anomáliavizsgálatot és interaktív döntéstámogató felületeket készít.

A projekt az eredeti, napi felbontású kutatási rendszertől egy automatizált, API-alapú, órás energiadat-platformig fejlődött. A jelenlegi modellfejlesztési adatállomány 100 549 órás rekordot tartalmaz; az éles alkalmazás pedig automatikusan lekéri és feldolgozza az aktuális terhelési, energiapiaci, időjárási, árfolyam- és megújulóenergia-adatokat.

Az előrejelzések, a később beérkező tényadatok, a modellhibák és az anomáliák külön, folyamatosan bővülő Supabase PostgreSQL-adatbázisba kerülnek. A működő Flux intelligens asszisztens az alkalmazás aktuális, élő eredményeit közérthető természetes nyelvű összefoglalókká és válaszokká alakítja.

Élő alkalmazás

https://okosmero.onrender.com

Fő funkciók

órás országos villamosenergia-fogyasztási előrejelzés CatBoost V10 modellel;

CatBoost- és MAVIR-előrejelzések folyamatos, éles összehasonlítása;

negyedórás day-ahead villamosenergiaárak elemzése;

kedvező, összefüggő töltési időszakok azonosítása;

negatív árú időszakok felismerése;

nap- és szélerőművi előrejelzések összevetése a mért termeléssel;

STL-alapú anomáliadetektálás és kontextusalapú kategorizálás;

napi és heti élő modellvalidáció;

előrejelzési, tényadat-, hiba- és anomálianapló Supabase PostgreSQL-ben;

többforrású adatlekérés, gyorsítótárazás és tartalék adatforrások;

élő adatokra épülő, ellenőrzött Flux intelligens asszisztens.

Rendszerarchitektúra

flowchart LR
    A[Historikus adatforrások] --> B[Python adatpipeline]
    B --> C[100 549 soros órás mestertábla]
    C --> D[Modellfejlesztés és offline validáció]
    D --> E[CatBoost V10]

    F[Élő API-források] --> G[Adattisztítás és feature engineering]
    G --> H[Órás előrejelzés és elemzések]
    E --> H

    H --> I[Dash–Plotly felület]
    H --> J[Supabase PostgreSQL]
    J --> K[Élő modellvalidáció]

    H --> L[Strukturált Flux-adatcsomag]
    L --> M[Gemini]
    M --> N[Tartalmi ellenőrzés]
    N --> I
    N --> J

    O[Determinisztikus fallback] --> I

A rendszer három elkülönülő adatréteget kezel:

historikus modellfejlesztési adatállomány – tanítás, feature engineering és offline validáció;

élő adatpipeline – az alkalmazás aktuális működéséhez szükséges API-adatok;

operatív Supabase-adatbázis – előrejelzések, tényadatok, hibák, anomáliák, Flux-cache és auditinformációk.

1. OkosMérő – a jelenlegi rendszer

1.1. Automatizált órás adatpipeline

A napi felbontású modellezést a jelenlegi rendszerben órás adatfeldolgozás váltotta fel.

A 2015 és 2026 közötti historikus ENTSO-E terhelési lekérés:

402 892 nyers negyedórás adatpontot tartalmazott;

automatikus Python-lekérdezéssel készült;

nem az éles alkalmazás aktuális adatfolyamával azonos, hanem a modellfejlesztést szolgáló historikus adatréteg.

A tisztítás, időzóna-kezelés, duplikációk eltávolítása és órás aggregálás után létrejött modellfejlesztési mestertábla:

100 549 órás rekordot;

21 alapváltozót

tartalmazott.

A mestertábla fő változói

villamosenergia-fogyasztás;

day-ahead ár;

hőmérséklet;

páratartalom;

napsugárzás;

szélsebesség;

csapadék;

EUR/HUF árfolyam;

óra;

hét napja;

hónap;

ünnepnap;

hétvége;

extrém hideg;

extrém meleg;

fogyasztási késleltetések;

napenergia-termelés;

szélenergia-termelés.

Historikus adatforrások

Forrás

Felhasználás

ENTSO-E Transparency Platform

terhelés, day-ahead árak, nap- és szélenergia-adatok

Open-Meteo

historikus órás időjárási adatok

ECB

EUR/HUF árfolyam

Magyar ünnepnap-adatok

naptári és ünnepnapi jellemzők

1.2. Élő, automatizált adatgyűjtés

Az éles OkosMérő nem manuálisan feltöltött Excel- vagy CSV-állományokból működik. Az alkalmazás minden frissítési ciklusban közvetlenül lekéri és feldolgozza az aktuálisan elérhető adatokat.

A rendszer automatikusan kezeli:

a magyar villamosenergia-rendszer mért terhelését;

a hivatalos terhelési előrejelzést;

a day-ahead villamosenergiaárakat;

a nap- és szélerőművi termelési előrejelzéseket;

a mért nap- és széltermelést;

az órás időjárási adatokat;

az EUR/HUF árfolyamot;

a naptári és ünnepnapi információkat.

Élő adatforrások

ENTSO-E Transparency Platform;

Open-Meteo;

Visual Crossing tartalék időjárási forrásként;

ECB;

magyar ünnepnap-adatok.

Az élő adatfolyam lépései

API-adatok lekérése;

időpontok egységesítése Europe/Budapest időzónára;

adatelérhetőség és adatminőség ellenőrzése;

a modell bemeneti jellemzőinek előállítása;

órás előrejelzés és kapcsolódó elemzések elkészítése;

a Dash-felület frissítése;

az előrejelzések tartós mentése;

visszamérés a lezárt tényadatok beérkezése után.

1.3. CatBoost V10 fogyasztás-előrejelzés

A modellfejlesztés során több gépi tanulási megközelítés vizsgálata történt:

FLAML AutoML;

XGBoost;

LightGBM;

CatBoost;

többmodell-ensemble.

Az éles alkalmazás kiválasztott fogyasztás-előrejelző modellje a CatBoost V10.

A modell közvetlenül készít órás előrejelzéseket: nem a saját korábbi jóslatát használja a következő célóra bemeneteként. A publikált forrásadatoktól függően legfeljebb 24 órás előrejelzési horizontot állít elő.

Felhasznált jellemzők

Fogyasztási előzmények

24, 48, 72, 96, 120, 144, 168 és 336 órás fogyasztási késleltetések;

az előző hetek azonos óráinak átlaga;

medián-, minimum- és maximumértékek;

rövid távú és heti fogyasztási trendek;

a 24 és 168 órás késleltetés különbsége.

Időjárási jellemzők

hőmérséklet;

páratartalom;

napsugárzás;

szélsebesség;

csapadék;

fűtési fokérték;

hűtési fokérték;

extrém hideg- és melegjelzők.

Energiapiaci és megújulóenergia-jellemzők

day-ahead ár;

árkülönbségi jellemzők;

napenergia-előrejelzés;

szélenergia-előrejelzés;

megújulóenergia-eltérések;

EUR/HUF árfolyam.

Idő- és naptárjellemzők

óra;

hét napja;

hónap;

hétvége;

ünnepnap;

napi, heti és éves ciklikus jellemzők.

A jelenlegi éles feature-készlet 44 modellbemenetet tartalmaz.

Offline modellértékelés

A CatBoost V10 kódban rögzített validációs eredménye:

Mutató

CatBoost V10

MAE

108,48 MWh

MAPE

2,50%

Ugyanezen referencia-időszakban a hivatalos MAVIR-előrejelzés MAE-je 244,45 MWh volt.

A dokumentáció kizárólag a kiválasztott éles modell ellenőrzött eredményeit mutatja be. A modellfejlesztés során elvetett köztes változatok és zsákutcás kísérletek nem képezik a jelenlegi rendszer teljesítményének igazolását.

1.4. Supabase PostgreSQL és élő modellvalidáció

A jelenlegi OkosMérő mögött külön, folyamatosan bővülő Supabase PostgreSQL-adatbázis működik.

Ez az operatív adatbázis nem azonos a modell tanításához használt 100 549 soros historikus mestertáblával.

A két adatréteg eltérő szerepe

Historikus mestertábla

modellfejlesztés;

feature engineering;

tanítás;

offline validáció;

100 549 órás rekord.

Supabase PostgreSQL operatív adatbázis

élő előrejelzések tárolása;

első és legfrissebb modelljóslat megőrzése;

MAVIR-előrejelzések tárolása;

tényleges fogyasztási adatok hozzákapcsolása;

előrejelzési hibák kiszámítása;

modellverziók naplózása;

bemeneti adatminőség rögzítése;

STL-anomáliák tárolása;

Flux-összefoglalók gyorsítótárazása és auditálása;

folyamatosan növekvő éles validációs előzmény.

forecast_pending

A még jövőbeli célórákhoz tartozó előrejelzéseket tárolja.

Célóránként megőrzi:

az első CatBoost-előrejelzést;

a legfrissebb CatBoost-előrejelzést;

az előrejelzés készítésének időpontját;

az előrejelzési horizontot;

a MAVIR-előrejelzést;

a modellverziót;

a bemeneti adatok minőségi állapotát;

az adatforrás típusát.

forecast_log

A lezárt órák végleges validációs rekordjait tartalmazza.

Amikor rendelkezésre áll:

a korábban mentett CatBoost-előrejelzés;

a MAVIR-előrejelzés;

a lezárt tényleges fogyasztási adat,

a rendszer:

összekapcsolja az adatokat a célórával;

kiszámítja a CatBoost abszolút hibáját;

kiszámítja a MAVIR abszolút hibáját;

kiszámítja az első és a legfrissebb CatBoost-jóslat hibáját;

végleges rekordot hoz létre a forecast_log táblában;

eltávolítja a lezárt célórát az ideiglenes táblából.

A lezárt rekordokból számítható:

napi CatBoost MAE;

napi MAVIR MAE;

heti kumulált hiba;

CatBoost–MAVIR nyerési arány;

legjobb előrejelzési óra;

legnehezebb előrejelzési óra;

első és legfrissebb jóslat hibája.

A modell teljesítménye így nemcsak egyszeri tesztadaton, hanem folyamatosan gyűlő éles adatokon is ellenőrizhető.

stl_anomalia

Az STL-módszerrel azonosított eltéréseket és azok magyarázó környezetét tárolja.

1.5. STL-alapú anomáliadetektálás

Az alkalmazás robusztus STL-dekompozícióval vizsgálja a fogyasztási idősort.

Az STL a fogyasztási görbét három összetevőre bontja:

hosszabb távú trend;

napi szezonális komponens;

reziduum.

A rendszer a reziduumot, vagyis a trenddel és a szokásos napi mintázattal nem magyarázható eltérést figyeli. A ±2,5 szórásos küszöbön kívül eső reziduumok anomáliaként kerülnek naplózásra.

Az anomáliákhoz tárolt kontextus

tényleges fogyasztás;

várt fogyasztás;

reziduum;

hőmérséklet;

szélsebesség;

napsugárzás;

csapadék;

day-ahead ár;

napszak;

hétvége;

ünnepnap.

Kategorizálási szabályok

időjárási extrém;

alacsony napsugárzáshoz kapcsolódó nappali eltérés;

jelentős, 24 órán belüli hőmérsékleti fordulat;

további vizsgálatot igénylő eltérés.

Az anomáliák a Supabase PostgreSQL-adatbázisban tartósan megmaradnak, és a Modell Labor felületén időjárási, naptári és energiapiaci kontextusukkal együtt jelennek meg.

1.6. Interaktív Dash–Plotly alkalmazás

A jelenlegi felhasználói felület Python Dash, Dash Bootstrap Components és Plotly technológiával készült.

Főoldal

A megújult főoldal megtartja az alkalmazás aktuális eredményeit megjelenítő felső kártyasort és navigációs választósávot, miközben Flux közérthetően bemutatja az OkosMérő működését és az aktuális eredményeket.

DAM-árak és töltés

A day-ahead villamosenergiaárak alapján töltési döntéstámogatást biztosít.

Funkciói:

negyedórás DAM-árak megjelenítése;

a következő kedvező, összefüggő 60 perces töltési időszak kiválasztása;

negatív árú időszakok felismerése;

alternatív töltési ablakok ajánlása;

aktuális töltési döntés támogatása.

Energiaelemzés

Az oldal megjeleníti:

a következő elérhető órák CatBoost-előrejelzését;

a várható minimum- és csúcsterhelést;

a hőmérséklet és fogyasztás kapcsolatát;

a várható energiaköltséget;

a CatBoost- és MAVIR-előrejelzés összehasonlítását;

a napi és heti élő validációs mutatókat.

Megújulók

A megújulóenergia-modul:

összehasonlítja a napenergia-előrejelzést a mért termeléssel;

összehasonlítja a szélenergia-előrejelzést a mért termeléssel;

kiszámítja az aktuális előrejelzési hibákat;

megjeleníti a várható termelési csúcsokat;

összekapcsolja a megújuló termelést a day-ahead árral;

kiemeli a negatív árú időszakokat.

ML Modell Labor

A modell-labor megjeleníti:

az STL-reziduumot;

az anomáliaküszöböket;

a kategorizált anomáliákat;

az anomáliák időjárási és energiapiaci kontextusát;

az adatminőség állapotát;

a közelmúlt anomálianaplóját.

1.7. Adatminőség és hibatűrés

Az OkosMérő:

Europe/Budapest időzónában kezeli az időpontokat;

csak lezárt órás adatot használ tényadatként;

forrásonként külön gyorsítótárazást alkalmaz;

automatikusan frissíti az adatokat;

a day-ahead publikálás után érvényteleníti az elavult gyorsítótárat;

Open-Meteo-hiba esetén Visual Crossing tartalékforrást használhat;

átmeneti API-hiba esetén az utolsó sikeresen lekért valós adatot használhatja;

megkülönbözteti a teljes, részleges és kritikus adatkapcsolati állapotot;

nem hoz létre kitalált helyettesítő adatokat;

automatikus és kézi frissítést is biztosít.

2. Flux – élő adatokra épülő intelligens asszisztens

Flux az OkosMérő új főoldalán működő intelligens értelmezési réteg. Feladata, hogy a rendszer technikai eredményeit közérthető, természetes nyelvű összefoglalókká alakítsa, bemutassa az alkalmazás működését, és válaszoljon az aktuális adatokkal kapcsolatos felhasználói kérdésekre.

Flux:

bemutatja az OkosMérő célját és fő funkcióit;

közérthetően összefoglalja az aktuális fogyasztási előrejelzést;

értelmezi a day-ahead árakat és a töltési ajánlást;

bemutatja a nap- és szélerőművi előrejelzések eredményeit;

összefoglalja az STL-alapú anomáliavizsgálat megállapításait;

válaszol az alkalmazás adataival és működésével kapcsolatos kérdésekre;

szélsőséges időjárás és magas várható terhelés esetén rövid, tárgyilagos figyelmeztetést ad.

2.1. Élő adatokon alapuló válaszadás

Flux nem előre eltárolt válaszokból és nem a Supabase-adatbázisból válaszol a felhasználói kérdésekre.

A kérdés pillanatában a Gemini strukturált adatcsomagban megkapja az OkosMérő aktuális, élő eredményeit, többek között:

a mért és előrejelzett fogyasztási adatokat;

a CatBoost- és MAVIR-előrejelzések eredményeit;

a day-ahead árakat;

a töltési ajánlást;

a nap- és szélerőművi adatokat;

az anomáliavizsgálat eredményeit;

az adatforrások minőségi és elérhetőségi állapotát.

A Gemini kizárólag ebből az átadott adatcsomagból készíthet természetes nyelvű választ.

2.2. Supabase-alapú gyorsítótár

A Supabase ebben a folyamatban nem a kérdések megválaszolásához szükséges adatforrás, hanem gyorsítótár és auditnapló.

A generált válaszokhoz tartozó adatállapot egyedi azonosítót kap. Ha ugyanahhoz az adatállapothoz már készült érvényes összefoglaló, a rendszer azt használja fel, és nem indít új Gemini-hívást.

Ennek előnye:

gyorsabb oldalbetöltés;

alacsonyabb API-költség;

kevesebb ismételt modellhívás;

azonos adatokhoz következetes válasz.

Új Gemini-hívás csak akkor szükséges, ha az alapul szolgáló energia-, időjárási, árfolyam- vagy modelleredmények megváltoztak.

2.3. Auditálható szöveggenerálás

Minden létrehozott összefoglalóhoz eltárolható:

a generálás időpontja;

az alapul szolgáló adatállapot azonosítója;

a használt modell;

a prompt verziója;

a válasz típusa;

az ellenőrzés eredménye;

a generálás státusza.

A lehetséges státuszok:

gemini – a Gemini által készített és elfogadott válasz;

fallback – determinisztikus sablonból készült válasz;

rejected – az ellenőrzésen elutasított modellválasz.

Ez lehetővé teszi annak utólagos ellenőrzését, hogy egy adott szöveg:

milyen adatállapotból készült;

melyik modellt használta;

melyik promptverzió alapján jött létre;

átment-e a tartalmi ellenőrzésen;

Gemini-válaszként, fallbackként vagy elutasított eredményként került-e naplózásra.

2.4. Determinisztikus hibatűrés

Ha a Gemini:

nem érhető el;

túllépi az időkorlátot;

hibás választ ad;

nem az átadott adatokat használja;

vagy a válasz nem felel meg az ellenőrzési szabályoknak,

a rendszer nem jeleníti meg a generált szöveget.

Ilyenkor az élő adatokból programozott szabályokkal összeállított, determinisztikus összefoglaló jelenik meg.

A fallback működés biztosítja, hogy:

az oldal Gemini nélkül is használható marad;

kitalált szám ne kerülhessen a felületre;

csak az aktuális adatcsomagban ténylegesen szereplő értékek jelenjenek meg;

az asszisztens hibája ne akadályozza az OkosMérő többi funkcióját.

Flux így nem egyszerű chatbot, hanem az OkosMérő ellenőrzött adataira épülő, gyorsítótárazott, auditálható és hibatűrő természetes nyelvű értelmezési rétege.

3. A projekt fejlődése – MVM DataInsight

Az OkosMérő előzménye az MVM DataInsight diplomamunka volt. Az eredeti kutatási rendszer négy fő modulban kapcsolta össze:

az energiaadatok feldolgozását és egységesítését;

az anomáliadetektálást és trendelemzést;

a fogyasztás-előrejelző modelleket;

a CrewAI-alapú automatizációt.

A jelenlegi OkosMérő ezekre a tapasztalatokra építve már órás felbontású historikus mestertáblával, automatikus élő adatkapcsolatokkal, CatBoost-modellel, Supabase PostgreSQL-adatbázissal, Flux intelligens asszisztenssel és interaktív Dash–Plotly alkalmazással működik.

3.1. I. modul – Adatfeldolgozás és mestertáblák

A projekt első szakaszában MEKH- és HUPX-forrásfájlokra épülő, AI-támogatott Python-feldolgozási folyamatok készültek. A munkafolyamatok felismerték az eltérő Excel- és CSV-struktúrákat, tisztították és egységesítették az adatokat, majd elemzésre és modellezésre alkalmas mestertáblákat hoztak létre.

1. Havi országos villamosenergia-felhasználás

MEKH éves Excel-fájlok;

2014–2024 közötti időszak;

132 havi rekord;

eltérő munkalap- és táblaszerkezetek egységesítése.

2. Lakossági fogyasztás és rezsi-gap

egyetemes szolgáltatói és kereskedői fogyasztás;

KSH háztartásszám-adatok;

2014–2024 közötti időszak;

132 havi rekord;

háztartásonkénti fogyasztás és rezsi-gap számítása.

3. Napi országos fogyasztás

24 havi MEKH Excel-fájl;

2024–2025 közötti időszak;

idősoros és profilos fogyasztás;

hőmérsékleti és naptári jellemzők;

731 napi rekord;

hiányzó érték nélküli feldolgozott tábla.

4. Napi HUPX DAM-ár

24 havi HUPX Excel-fájl;

2024–2025 közötti időszak;

731 napi rekord;

hiányzó érték nélküli feldolgozott tábla.

A fogyasztás-előrejelző modul két közvetlen alaptáblája:

a 731 soros napi fogyasztási tábla;

a 731 soros napi HUPX DAM-ártábla.

A két tábla dátum szerinti összekapcsolásával jött létre a napi predikciós mestertábla, amely később 2026 eleji fogyasztási, hőmérsékleti, energiapiaci és naptári adatokkal bővült.

Az eredeti rendszer adattárolási technológiája Azure Database for PostgreSQL volt.

3.2. II. modul – Anomáliadetektálás és trendelemzés

A modul statisztikai, felügyelet nélküli és felügyelt gépi tanulási módszereket kapcsolt össze.

IQR

A havi fogyasztási adatokon szezonálisan értelmezett, interkvartilis terjedelemre épülő vizsgálat készült. A módszer a fogyasztási értékeket nem egyetlen általános küszöbhöz, hanem az adott időszak jellemző eloszlásához viszonyította.

Isolation Forest

A felügyelet nélküli modell több változó együttes mintázata alapján azonosította a szokatlan eseteket.

A felhasznált információk között szerepelt:

villamosenergia-fogyasztás;

átlag- és maximum-hőmérséklet;

hőségnapok;

hónap;

szezonális környezet.

Random Forest

Az IQR és az Isolation Forest eredményeiből létrehozott anomáliacímkék alapján felügyelt Random Forest osztályozó készült.

A modellértékelés részei:

tanító- és teszthalmaz szétválasztása;

feature importance vizsgálat;

classification report;

precision-, recall- és F1-mutatók;

konfúziós mátrix.

SMOTE

Az anomáliák alacsony száma miatt a kisebbségi osztály kiegyensúlyozása SMOTE segítségével történt.

A módszer kizárólag a tanítóhalmazon hozott létre szintetikus anomáliapéldákat. Az osztályozó teljesítményét a SMOTE alkalmazása előtt és után külön konfúziós mátrix és recall-érték alapján hasonlítottam össze.

A recall 25%-ról 75%-ra javult, miközben az osztályozó pontossága 85% volt.

Többmódszeres ellenőrzés

A rendszer egymás mellett vizsgálta:

az IQR;

az Isolation Forest;

a SMOTE-tal tanított Random Forest

jelzéseit.

A több módszer által egyaránt azonosított esetek magasabb megbízhatóságú anomáliaként jelentek meg.

Trendelemzés

A modul további vizsgálatai:

rezsi-gap;

energiaár-olló;

HUPX- és ENTSO-E-piaci árak;

ECB EUR/HUF árfolyam;

KSH háztartásszám-adatok;

hőmérséklet és fogyasztás kapcsolata;

szezonális mintázatok;

fogyasztási szélsőértékek.

3.3. III. modul – PyTorch fogyasztás-előrejelzés

A napi predikciós mestertábla bővítése

A két, egyenként 731 soros fogyasztási és DAM-tábla összekapcsolása után a mestertábla 2026. januári és februári adatokkal bővült.

A V1-adatállomány:

790 napi rekordot;

9 modellváltozót

tartalmazott.

Az időrendben elkülönített adatokból:

632 rekord került a V1 tanítóhalmazába;

158 rekord maradt a valós tesztidőszakban.

V1 PyTorch neurális háló

Az első előrejelző modell saját PyTorch neurális háló volt.

Architektúra

9 bemeneti változó;

64 neuronos első rejtett réteg;

ReLU aktiváció;

32 neuronos második rejtett réteg;

ReLU aktiváció;

1 kimeneti neuron.

Tanítási eszközök

PyTorch;

MinMaxScaler;

Adam-optimalizáló;

MSE-veszteségfüggvény;

200 tanítási epoch;

modell- és scalermentés.

Célzott hibaanalízis

A modellhibák értékelése nemcsak összesített mutatók, hanem hónap és hőmérséklet szerint is megtörtént.

Az elemzés kimutatta, hogy:

a tanítóhalmazban nem szerepelt elegendő −5 °C alatti nap;

a tesztidőszakban viszont több extrémhideg-nap is előfordult;

a modell hibája ezekben az esetekben jelentősen magasabb volt.

Adatvezérelt szintetikus adatgenerálás

A hiányzó extrémhideg-helyzetek pótlására 30 szintetikus rekord készült:

15 rekord −5 és −7 °C között;

15 rekord −7 és −10 °C között.

A fogyasztási értékek nem önkényesen készültek. A generálás a valós téli adatok hőmérsékleti sávonként kiszámított fogyasztási átlagaira épült.

A szintetikus sorok tartalmazták:

a hőmérsékletet;

a fogyasztási értéket;

a DAM-árat;

a hét napját;

a hónapot;

az ünnepnap- és hétvégejelzőt;

az extrémhideg-jellemzőt.

A reprodukálhatóság érdekében a generálás rögzített random seeddel történt.

V2 PyTorch neurális háló

A V2 azonos neurális architektúrát használt, de a tanítóhalmazba bekerült a 30 szintetikus extrémhideg-rekord.

Az adatméretek:

790 eredeti rekord;

30 szintetikus rekord;

összesen 820 rekord;

662 tanítósor, ebből 30 szintetikus;

158 valós tesztrekord.

A V2 javította az extrém hideg napok előrejelzését, ugyanakkor normál hőmérsékletű időszakokban a V1 teljesített jobban.

Kapuzott ensemble

A két modell erősségeit szabályalapú ensemble kapcsolta össze:

−5 °C alatt a V2;

−5 °C felett a V1

adta az előrejelzést.

Ez feltételes, kapuzott mixture-of-experts megoldásként működött.

Baseline-modellek

A PyTorch-modelleket egyszerűbb referenciaeljárásokkal is összehasonlítottam:

előző napi naiv előrejelzés;

előző hét azonos napját használó előrejelzés;

SARIMA.

SHAP-modellmagyarázat

A neurális háló magyarázatához SHAP DeepExplainer készült.

A vizsgált változók:

korábbi fogyasztás;

hőmérséklet;

hét napja;

hónap;

DAM-ár;

ünnepnap;

hétvége;

extrém hideg;

extrém meleg.

Módszertani felülvizsgálat

A későbbi validáció feltárta, hogy az eredeti modellfelállásban adatszivárgás volt jelen. Emiatt a korai, rendkívül magas pontossági eredményeket nem használom a jelenlegi rendszer teljesítményének igazolására.

Az adatszivárgás vizsgálata több különböző módszerrel történt:

időbeli shift-vizsgálat;

feature-megvonási teszt;

loss-görbék összehasonlítása;

SHAP-változófontosság;

multi-seed stabilitásvizsgálat.

A validációs notebook további módszerei:

bootstrap konfidenciaintervallum;

Diebold–Mariano-teszt;

walk-forward keresztvalidáció;

tíz különböző random seeddel végzett modellvizsgálat.

A módszertani felülvizsgálat eredménye vezetett a teljes előrejelzési folyamat időben helyes újratervezéséhez.

3.4. IV. modul – CrewAI-alapú automatizáció

Az MVM DataInsight negyedik modulja CrewAI-alapú adatgyűjtő és előrejelzést támogató rendszert tartalmazott.

Saját CrewAI-toolok

Két saját API-eszköz készült.

Időjárás-lekérő

Feladata:

az Open-Meteo API meghívása;

Budapest következő hét napos időjárás-előrejelzésének lekérése;

a modell számára használható, strukturált eredmény előállítása.

DAMár-lekérő

Feladata:

ENTSO-E API-kapcsolat;

a legfrissebb elérhető DAM-ár lekérése;

korábbi időszak átlagárának kiszámítása;

tartalék időszak lekérdezése;

többlépcsős fallback logika.

CrewAI-ágensek

Két külön ágens működött:

Időjárás Adatgyűjtő;

Energiatőzsdei Adatgyűjtő.

Az ágensek:

külön szerepet;

külön célt;

háttérleírást;

saját API-toolt;

strukturált feladatot

kaptak.

A nyelvi modell gpt-4o-mini volt.

Feladatok és Crew-folyamat

A rendszer:

külön Taskot rendelt mindkét ágenshez;

előre meghatározta az elvárt kimenetet;

szekvenciális Crew-folyamatot használt;

kickoff vagy aszinkron kickoff_async hívással indította az adatgyűjtést;

az eredményeket továbbította az alkalmazásnak.

Az eredeti rendszer felülete Streamlitben készült, GitHub-verziókezeléssel és Render deploymenttel.

4. Technológiai háttér

Programozás és adatfeldolgozás

Python

pandas

NumPy

requests

joblib

Excel- és CSV-feldolgozás

idősoros feature engineering

API-integráció

Gépi tanulás és statisztika

CatBoost

XGBoost

LightGBM

FLAML AutoML

PyTorch

scikit-learn

Random Forest

Isolation Forest

SMOTE

SARIMA

ensemble-modellek

statsmodels

STL-dekompozíció

Validáció és modellmagyarázat

időalapú tanító- és teszthalmaz

naiv baseline-modellek

MAE

MAPE

RMSE

R²

SHAP DeepExplainer

bootstrap konfidenciaintervallum

Diebold–Mariano-teszt

walk-forward validáció

multi-seed stabilitásvizsgálat

feature-megvonási teszt

adatszivárgás-vizsgálat

élő CatBoost–MAVIR összehasonlítás

Intelligens asszisztens és automatizáció

Gemini

CrewAI

saját API-toolok

CrewAI Agent

CrewAI Task

szekvenciális Crew-orchestráció

OpenAI gpt-4o-mini

adatállapot-alapú gyorsítótár

promptverziózás

auditálható LLM-kimenet

determinisztikus fallback

Adatforrások

MEKH

HUPX

ENTSO-E Transparency Platform

Open-Meteo

Visual Crossing

ECB

KSH

magyar ünnepnap-adatok

Alkalmazás és vizualizáció

Dash

Dash Bootstrap Components

Plotly

Streamlit az eredeti rendszerben

Adattárolás

Azure Database for PostgreSQL az eredeti rendszerben

Supabase PostgreSQL a jelenlegi rendszerben

psycopg

Supavisor kapcsolatkezelés

Üzemeltetés

GitHub

Render

Gunicorn

környezeti változók

automatikus deployment

5. AI-native fejlesztési megközelítés

A projekt AI-native fejlesztési munkafolyamatban készült: fejlett nyelvi modelleket és fejlesztői ágenseket használtam kutatáshoz, rendszertervezéshez, implementációhoz, hibakereséshez, teszteléshez és dokumentációhoz.

A rendszer szakmai és funkcionális kereteit én határoztam meg, többek között:

a megoldandó problémát;

a rendszer moduljait;

az adatforrásokat;

az adatfeldolgozási és modellezési követelményeket;

az ellenőrzési és validációs szempontokat;

a felhasználói funkciókat;

az adatminőségi és hibatűrési szabályokat;

a fejlesztési és javítási irányokat.

Az implementáció iteratív tervezéssel, részletes specifikációkkal, futtatott tesztekkel, eredményellenőrzéssel és módszertani felülvizsgálattal történt.

A jelenlegi rendszerben az adatgyűjtés, az adat-előkészítés, az előrejelzés, a tartós tárolás, az éles visszamérés és a Flux-asszisztens ellenőrzött szöveggenerálása egyetlen integrált alkalmazási folyamatot alkot.

6. Helyi futtatás

Követelmények

Python 3.10 vagy újabb

ENTSO-E API-kulcs

opcionálisan Visual Crossing API-kulcs

Supabase PostgreSQL-kapcsolat

Telepítés

git clone https://github.com/magroandi28-lang/MVM_V3_ORAS.git
cd MVM_V3_ORAS
python -m venv .venv

Windows

.venv\Scripts\activate

Linux / macOS

source .venv/bin/activate

pip install -r requirements.txt

Környezeti változók

ENTSOE_API_KEY=...
VISUAL_CROSSING_KEY=...
DATABASE_URL=...

Fejlesztői futtatás

python app.py

Alapértelmezett cím:

http://127.0.0.1:8050

Produkciós indítás

gunicorn app:server

7. Projektstátusz

Az OkosMérő működő, nyilvánosan elérhető portfólióprojekt.

A rendszer jelenleg:

automatikusan gyűjti az élő energiapiaci és időjárási adatokat;

100 549 rekordos órás modellfejlesztési mestertáblára épül;

CatBoost V10 modellel fogyasztási előrejelzést készít;

negyedórás day-ahead árakat elemez;

kedvező töltési időszakokat ajánl;

figyeli a nap- és szélenergia-termelést;

STL-alapú anomáliákat azonosít;

Supabase PostgreSQL-adatbázisban tárolja az előrejelzéseket, tényadatokat, hibákat és anomáliákat;

a jóslatokat a tényleges fogyasztással és a hivatalos MAVIR-előrejelzéssel is összehasonlítja;

folyamatosan bővíti az éles validációs adatbázist;

működő Flux intelligens asszisztenssel mutatja be és értelmezi az aktuális eredményeket;

Gemini-hiba vagy elutasított válasz esetén determinisztikus fallbacket használ;

nem jelenít meg kitalált vagy az aktuális adatcsomaggal nem igazolható számot.
Flux – élő adatokra épülő intelligens asszisztens



Az OkosMérő új főoldalán Flux, a rendszer beépített intelligens asszisztense segíti az alkalmazás használatát és az elemzési eredmények értelmezését.



Flux:



bemutatja az OkosMérő célját és fő funkcióit;

közérthetően összefoglalja az aktuális fogyasztási előrejelzést;

értelmezi a day-ahead árakat és a töltési ajánlást;

bemutatja a nap- és szélerőművi előrejelzések eredményeit;

összefoglalja az STL-alapú anomáliavizsgálat megállapításait;

válaszol az alkalmazás adataival és működésével kapcsolatos kérdésekre.

Élő adatokon alapuló válaszadás



Flux nem előre eltárolt válaszokból és nem a Supabase-adatbázisból válaszol a felhasználói kérdésekre.



A kérdés pillanatában a Gemini egy strukturált adatcsomagban megkapja az OkosMérő aktuális, élő eredményeit, többek között:



az aktuális és előrejelzett fogyasztási adatokat;

a CatBoost- és MAVIR-előrejelzések eredményeit;

a day-ahead árakat;

a töltési ajánlást;

a nap- és szélerőművi adatokat;

az anomáliavizsgálat eredményeit;

az adatforrások minőségi és elérhetőségi állapotát.



A Gemini kizárólag ebből az átadott adatcsomagból készíthet természetes nyelvű választ.



Supabase-alapú gyorsítótár



A generált válaszokhoz tartozó adatállapot egyedi azonosítót kap. Ha ugyanahhoz az adatállapothoz már készült érvényes összefoglaló, a rendszer azt a gyorsítótárból használja fel, és nem indít új Gemini-hívást.



Ennek előnye:



gyorsabb oldalbetöltés;

alacsonyabb API-költség;

kevesebb ismételt modellhívás;

azonos adatokhoz következetes válasz.



Új Gemini-hívás csak akkor szükséges, ha az alapul szolgáló energia-, időjárási, árfolyam- vagy modelleredmények megváltoztak.



Auditálható szöveggenerálás



Minden létrehozott összefoglalóhoz eltárolható:



a generálás időpontja;

az alapul szolgáló adatállapot azonosítója;

a használt modell;

a prompt verziója;

a válasz típusa;

az ellenőrzés eredménye;

a generálás státusza.



A lehetséges státuszok:



gemini – a Gemini által készített és elfogadott válasz;

fallback – determinisztikus sablonból készült válasz;

rejected – az ellenőrzésen elutasított modellválasz.



Ez lehetővé teszi annak utólagos ellenőrzését, hogy egy adott szöveg milyen adatokból, melyik modell- és promptverzióval készült, valamint átment-e a rendszer ellenőrzésén.



Determinisztikus hibatűrés



Ha a Gemini nem érhető el, túllépi az időkorlátot, hibás választ ad, vagy a válasz nem felel meg az ellenőrzési szabályoknak, a rendszer nem jeleníti meg a generált szöveget.



Ilyenkor egy determinisztikus, az élő adatokból programozott szabályokkal összeállított összefoglaló jelenik meg.



A fallback működés biztosítja, hogy:



az oldal Gemini nélkül is használható marad;

kitalált szám ne kerülhessen a felületre;

csak az aktuális adatcsomagban ténylegesen szereplő értékek jelenjenek meg;

az asszisztens hibája ne akadályozza az OkosMérő többi funkcióját.



Flux így nem egyszerű chatbot, hanem az OkosMérő ellenőrzött adataira épülő, gyorsítótárazott, auditálható és hibatűrő természetes nyelvű értelmezési rétege.
