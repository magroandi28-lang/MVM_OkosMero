OkosMérő

Automatizált villamosenergia-adatelemző, fogyasztás-előrejelző és energiapiaci döntéstámogató rendszer

Az OkosMérő az MVM DataInsight diplomamunkából továbbfejlesztett, működő villamosenergia-adatelemző és előrejelző alkalmazás. Több külső adatforrás integrálásával órás fogyasztási, energiapiaci, időjárási és megújulóenergia-adatokat dolgoz fel, gépi tanulási előrejelzést készít, anomáliákat azonosít, és interaktív monitoringfelületeket biztosít.

A projekt az eredetileg kézzel összegyűjtött és feldolgozott napi fogyasztási, valamint HUPX DAM-adattábláktól egy teljesen automatizált, API-alapú energiadat-rendszerig fejlődött. A jelenlegi OkosMérő modellfejlesztése már egy 100 549 rekordos, órás felbontású mestertáblára épül, miközben az alkalmazás automatikusan gyűjti és dolgozza fel az élő fogyasztási, energiapiaci, időjárási, árfolyam- és megújulóenergia-adatokat. Az elkészített előrejelzések, a később beérkező tényadatok, a modellhibák és az anomáliák egy külön, folyamatosan bővülő Supabase PostgreSQL-adatbázisba kerülnek.

A rendszer tehát nemcsak lényegesen nagyobb adatmennyiséget kezel, mint az eredeti diplomamunka, hanem az adatgyűjtéstől az előrejelzésen át az éles visszamérésig automatizált folyamatot valósít meg.

A projekt fejlődése

Az OkosMérő előzménye az MVM DataInsight diplomamunka volt. Az eredeti rendszer négy fő modulban kapcsolta össze:

az energiaadatok összegyűjtését és egységesítését;
az anomáliadetektálást és trendelemzést;
a fogyasztás-előrejelző modelleket;
a CrewAI-alapú automatizációt.

A jelenlegi OkosMérő ezekre a tapasztalatokra építve már órás felbontású historikus mestertáblával, automatikus élő adatkapcsolatokkal, CatBoost-modellel, Supabase PostgreSQL-adatbázissal és interaktív Dash–Plotly alkalmazással működik.

1. MVM DataInsight – az eredeti diplomamunka
I. modul – Adatgyűjtés és mestertáblák

A projekt első szakaszában eltérő szerkezetű Excel- és CSV-adatforrásokat gyűjtöttem össze és alakítottam egységes, elemezhető táblákká.

Négy feldolgozott mestertábla készült.

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
731 napi rekord.
4. Napi HUPX DAM-ár
24 havi HUPX Excel-fájl;
2024–2025 közötti időszak;
731 napi rekord.

A fogyasztás-előrejelző modul két közvetlen alaptáblája tehát:

a 731 soros napi fogyasztási tábla;
a 731 soros napi HUPX DAM-ártábla.

A két táblát dátum szerint kapcsoltam össze. Ebből jött létre a napi predikciós mestertábla, amelyet később 2026 eleji fogyasztási, hőmérsékleti, energiapiaci és naptári adatokkal bővítettem.

Az eredeti rendszer adattárolási technológiája Azure Database for PostgreSQL volt.

II. modul – Anomáliadetektálás és trendelemzés

A modul statisztikai, felügyelet nélküli és felügyelt gépi tanulási módszereket kapcsolt össze.

IQR

A havi fogyasztási adatokon szezonálisan értelmezett interkvartilis terjedelem alapú vizsgálat készült. A módszer a fogyasztási értékeket nem egyetlen általános küszöbhöz, hanem az adott időszak jellemző eloszlásához viszonyította.

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

A modell értékelése tartalmazta:

tanító- és teszthalmaz szétválasztását;
feature importance vizsgálatát;
classification reportot;
precision-, recall- és F1-mutatókat;
konfúziós mátrixot.
SMOTE

Az anomáliák alacsony száma miatt a kisebbségi osztályt SMOTE segítségével egyensúlyoztam ki.

A módszer kizárólag a tanítóhalmazon hozott létre szintetikus anomáliapéldákat. Az osztályozó teljesítményét a SMOTE alkalmazása előtt és után külön konfúziós mátrix és recall-érték alapján hasonlítottam össze.

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
III. modul – PyTorch fogyasztás-előrejelzés
A napi predikciós mestertábla bővítése

A két, egyenként 731 soros fogyasztási és DAM-tábla összekapcsolása után a mestertáblát 2026. januári és februári adatokkal bővítettem.

A kibővített V1-adatállomány:

790 napi rekordot;
kilenc modellváltozót

tartalmazott.

Az időrendben elkülönített adatokból:

632 rekord került a V1 tanítóhalmazába;
158 rekord maradt a tesztidőszakban.
V1 PyTorch neurális háló

Az első előrejelző modell saját PyTorch neurális háló volt.

Architektúrája:

9 bemeneti változó;
64 neuronos első rejtett réteg;
ReLU aktiváció;
32 neuronos második rejtett réteg;
ReLU aktiváció;
1 kimeneti neuron.

A tanítás során alkalmazott eszközök:

PyTorch;
MinMaxScaler;
Adam-optimalizáló;
MSE-veszteségfüggvény;
200 tanítási epoch;
modell- és scalermentés.
Célzott hibaanalízis

A modell hibáit nemcsak összesített mutatóként, hanem hónap és hőmérséklet szerint is megvizsgáltam.

Az elemzés megmutatta, hogy:

a tanítóhalmazban nem szerepelt elegendő −5 °C alatti nap;
a tesztidőszakban viszont több extrémhideg-nap is előfordult;
a modell hibája ezekben az esetekben jelentősen magasabb volt.
Adatvezérelt szintetikus adatgenerálás

A hiányzó extrémhideg-helyzetek pótlására 30 szintetikus rekordot generáltam.

A szintetikus minták:

15 darab −5 és −7 °C közötti;
15 darab −7 és −10 °C közötti

helyzetet modelleztek.

A fogyasztási értékek nem önkényesen készültek. A generálás a valós téli adatok hőmérsékleti sávonként kiszámított fogyasztási átlagaira épült.

A szintetikus sorokhoz hozzáillesztettem:

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

A V2 javította az extrém hideg napok előrejelzését, ugyanakkor a normál hőmérsékletű időszakokban a V1 teljesített jobban.

Kapuzott ensemble

A két modell erősségeit szabályalapú ensemble kapcsolta össze:

−5 °C alatt a V2;
−5 °C felett a V1

adta az előrejelzést.

Ez egy feltételes, kapuzott mixture-of-experts megoldás volt.

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

Az adatszivárgást több különböző módszerrel ellenőriztem:

időbeli shift-vizsgálat;
feature-megvonási teszt;
loss-görbék összehasonlítása;
SHAP-változófontosság;
multi-seed stabilitásvizsgálat.

A validációs notebook további módszerei:

bootstrap konfidenciaintervallum;
Diebold–Mariano-teszt;
walk-forward keresztvalidáció;
több különböző random seeddel végzett modellvizsgálat.

A módszertani felülvizsgálat eredménye vezetett a teljes előrejelzési folyamat időben helyes újratervezéséhez.

IV. modul – CrewAI-alapú automatizáció

Az MVM DataInsight negyedik modulja CrewAI-alapú adatgyűjtő és előrejelzést támogató rendszert tartalmazott.

Saját CrewAI-toolok

Két saját API-eszköz készült.

Időjárás-lekérő

Feladata:

az Open-Meteo API meghívása;
Budapest következő hét napos időjárás-előrejelzésének lekérése;
a modell számára használható eredmény előállítása.
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
a kickoff vagy aszinkron kickoff_async hívással indította el az adatgyűjtést;
az eredményeket továbbította az alkalmazásnak.

Az eredeti rendszer felülete Streamlitben készült, GitHub-verziókezeléssel és Render deploymenttel.

2. OkosMérő – a jelenlegi rendszer
I. modul – Automatizált órás adatpipeline

A jelenlegi rendszerben a napi felbontású modellezést órás felbontású adatfeldolgozás váltotta fel.

A historikus ENTSO-E fogyasztási lekérés 2015 és 2026 között több mint 400 000 nyers negyedórás időpontot tartalmazott.

A tisztítás, időzóna-kezelés, duplikációk eltávolítása és órás aggregálás után létrejött modellfejlesztési mestertábla:

100 549 órás rekordot;
21 alapváltozót

tartalmazott.

A jelenlegi modell tehát nagyságrendileg lényegesen nagyobb és részletesebb adatállományra épül, mint az eredeti napi rendszer.

A mestertábla fő változói
villamosenergia-fogyasztás;
DAM-ár;
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
Élő, automatizált adatgyűjtés

A jelenlegi alkalmazás már nem kézzel feltöltött Excel- vagy CSV-állományokból működik.

Az OkosMérő automatikusan lekéri:

a magyar villamosenergia-rendszer mért terhelését;
a hivatalos terhelési előrejelzést;
a day-ahead villamosenergia-árakat;
a nap- és szélerőművi termelési előrejelzéseket;
a mért nap- és széltermelést;
az időjárási adatokat;
az EUR/HUF árfolyamot;
a naptári és ünnepnapi információkat.

A fő adatforrások:

ENTSO-E Transparency Platform;
Open-Meteo;
Visual Crossing tartalék időjárási forrás;
ECB;
magyar ünnepnap-adatok.

Az alkalmazás:

Europe/Budapest időzónára egységesíti az adatokat;
ellenőrzi az adatok rendelkezésre állását;
létrehozza a modell bemeneti változóit;
automatikusan elkészíti az előrejelzést;
frissíti a felhasználói felületet;
eltárolja a jóslatokat;
később összekapcsolja őket a tényleges adatokkal.
II. modul – CatBoost fogyasztás-előrejelzés

A jelenlegi modell fejlesztése során több gépi tanulási megközelítést vizsgáltam:

FLAML AutoML;
XGBoost;
LightGBM;
CatBoost;
többmodell-ensemble.

Az éles alkalmazásban használt modell a CatBoost V10.

A modell közvetlenül készít órás előrejelzéseket. Nem a saját előző jóslatát használja a következő óra bemeneteként.

Felhasznált jellemzők

A modell többek között az alábbi információkat használja:

24, 48, 72, 96, 120, 144, 168 és 336 órás fogyasztási késleltetések;
az előző hetek azonos óráinak átlag-, medián-, minimum- és maximumértékei;
rövid és heti fogyasztási trendek;
hőmérséklet;
páratartalom;
napsugárzás;
szélsebesség;
csapadék;
fűtési fokérték;
hűtési fokérték;
DAM-ár;
árkülönbségi jellemzők;
napenergia-előrejelzés;
szélenergia-előrejelzés;
naptári jellemzők;
napi, heti és éves ciklikus jellemzők;
EUR/HUF árfolyam.
Offline modellértékelés

A kódban rögzített validációs eredmény:

MAE: 108,48 MWh
MAPE: 2,50%

Ugyanazon referencia-időszakban a hivatalos MAVIR-előrejelzés MAE-je 244,45 MWh volt.

A jelenlegi rendszer dokumentációjában csak a kiválasztott CatBoost V10 modell és annak ellenőrzött eredményei szerepelnek. A modellfejlesztés során elvetett köztes változatok és zsákutcás kísérletek nem részei a bemutatásnak.

III. modul – Supabase PostgreSQL és élő modellvalidáció

A jelenlegi OkosMérő mögött külön, folyamatosan bővülő Supabase PostgreSQL-adatbázis működik.

Ez nem azonos a modell tanításához használt 100 549 soros historikus mestertáblával.

A két adatréteg szerepe:

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
folyamatosan növekvő validációs előzmény.
Előrejelzési életciklus

A jövőbeli célórák előrejelzései először a forecast_pending táblába kerülnek.

A rendszer célóránként megőrzi:

az első CatBoost-előrejelzést;
a legfrissebb CatBoost-előrejelzést;
az előrejelzés készítésének időpontját;
az előrejelzési horizontot;
a MAVIR-előrejelzést;
a modellverziót;
a bemeneti adatok minőségi állapotát.

Amikor megérkezik a lezárt tényleges fogyasztási adat, a rendszer:

összekapcsolja a célórával;
kiszámítja a CatBoost abszolút hibáját;
kiszámítja a MAVIR abszolút hibáját;
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

Ezáltal a modell teljesítménye nemcsak egy egyszeri tesztadaton, hanem folyamatosan gyűlő éles adatokon is ellenőrizhető.

IV. modul – STL-anomáliadetektálás

Az alkalmazás robusztus STL-dekompozícióval vizsgálja a fogyasztási idősort.

A felbontás összetevői:

hosszabb távú trend;
napi szezonális komponens;
reziduum.

A ±2,5 szórásos küszöbön kívül eső reziduumok anomáliaként kerülnek naplózásra.

Az anomáliákhoz tárolt kontextus:

tényleges fogyasztás;
várt fogyasztás;
reziduum;
hőmérséklet;
szélsebesség;
napsugárzás;
csapadék;
DAM-ár;
napszak;
hétvége;
ünnepnap.

A kategorizálási szabályok:

időjárási extrém;
alacsony napsugárzáshoz kapcsolódó nappali eltérés;
jelentős 24 órás hőmérsékleti fordulat;
további vizsgálatot igénylő eltérés.

Az anomáliák a Supabase PostgreSQL-adatbázisban tartósan megmaradnak.

V. modul – Interaktív Dash–Plotly alkalmazás

A jelenlegi felhasználói felület Python Dash és Plotly technológiával készült.

Főoldal

A főoldal a day-ahead villamosenergia-árak alapján töltési döntéstámogatást biztosít.

Funkciói:

negyedórás DAM-árak megjelenítése;
a következő kedvező, összefüggő töltési időszak kiválasztása;
negatív árú időszakok felismerése;
alternatív töltési ablakok ajánlása;
aktuális töltési döntés támogatása.
Energiaelemzés

Az oldal megjeleníti:

a következő elérhető órák CatBoost-előrejelzését;
a várható minimum- és csúcsterhelést;
a hőmérséklet és fogyasztás kapcsolatát;
a várható energiaköltséget;
a CatBoost és a MAVIR eredményének összehasonlítását;
a napi és heti élő validációs mutatókat.
Megújulók

A megújulóenergia-modul:

összehasonlítja a napenergia-előrejelzést a mért termeléssel;
összehasonlítja a szélenergia-előrejelzést a mért termeléssel;
kiszámítja az aktuális előrejelzési hibákat;
megjeleníti a várható termelési csúcsokat;
összekapcsolja a megújuló termelést a DAM-árral;
kiemeli a negatív árú időszakokat.
ML Modell Labor

A modell-labor megjeleníti:

az STL-reziduumot;
az anomáliaküszöböket;
a kategorizált anomáliákat;
az anomáliák időjárási és energiapiaci kontextusát;
az adatminőség állapotát;
a közelmúlt anomálianaplóját.
Adatminőség és hibatűrés

Az OkosMérő:

Europe/Budapest időzónában kezeli az időpontokat;
csak lezárt órás adatot használ tényadatként;
forrásonként külön gyorsítótárazást alkalmaz;
automatikusan frissíti az adatokat;
day-ahead publikálás után érvényteleníti az elavult gyorsítótárat;
Open-Meteo-hiba esetén Visual Crossing tartalékforrást használhat;
átmeneti API-hiba esetén az utolsó sikeresen lekért valós adatot használhatja;
megkülönbözteti a teljes, részleges és kritikus adatkapcsolati állapotot;
nem hoz létre kitalált helyettesítő adatokat;
automatikus és kézi frissítést is biztosít.
Technológiai háttér
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
Ágensek és automatizáció
CrewAI
saját API-toolok
CrewAI Agent
CrewAI Task
szekvenciális Crew-orchestráció
OpenAI gpt-4o-mini
promptvezérelt, AI-native fejlesztési folyamat
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
AI-native fejlesztési megközelítés

A projekt első energiaadatait még kézzel gyűjtöttem össze és egységesítettem.

A további adatfeldolgozási, modellezési, automatizációs és alkalmazásfejlesztési lépések AI-native munkafolyamatban készültek. Én határoztam meg:

a megoldandó problémát;
a rendszer moduljait;
az adatforrásokat;
a modellezési követelményeket;
az ellenőrzési és validációs szempontokat;
a felhasználói funkciókat;
a javítási irányokat.

Az implementáció részletes promptutasításokkal, iteratív teszteléssel és ellenőrzéssel történt.

A jelenlegi rendszerben már az adatgyűjtés is automatizált: az MI-támogatással létrehozott alkalmazáskód közvetlenül kapcsolódik a külső API-khoz, feldolgozza az adatokat, elkészíti az előrejelzéseket, majd Supabase PostgreSQL-adatbázisban folyamatosan visszaméri azok eredményét.

Projektstátusz

Az OkosMérő működő, nyilvánosan elérhető portfólióprojekt.

A rendszer jelenleg:

automatikusan gyűjti az élő energiapiaci és időjárási adatokat;
százezres nagyságrendű órás mestertáblára épül;
CatBoost-modellel fogyasztási előrejelzést készít;
negyedórás day-ahead árakat elemez;
kedvező töltési időszakokat ajánl;
figyeli a nap- és szélenergia-termelést;
STL-alapú anomáliákat azonosít;
Supabase PostgreSQL-adatbázisban tárolja az előrejelzéseket és az anomáliákat;
a jóslatokat a tényleges fogyasztással és a hivatalos MAVIR-előrejelzéssel is összehasonlítja;
folyamatosan bővíti az éles validációs adatbázist.
