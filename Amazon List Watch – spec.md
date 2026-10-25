# Amazon List Watch – spec

Oct 25, 2026 · @Master

## Syfte och omfattning

Amazon List Watch läser delade önskelistor på amazon.se, sparar prishistorik per produkt och skickar en notis via Apprise när ett pris faller under en vald nivå. Tjänsten körs självhostad i en LXC i Proxmox och har ett eget webbgränssnitt.

- **Omfattning v1:** upp till 10 listor med upp till 100 produkter vardera (max \~1 000 produkter), plus enstaka produkter via ASIN.
- **Källa:** endast amazon.se: listor med sekretess *Delad* eller *Offentlig*, samt lokala bevakningar via ASIN.
- **Ingen integration** med Home Assistant.

## Krav och avgränsningar

| ID | Krav | Prioritet |
| --- | --- | --- |
| K1 | Lägga till en önskelista via dess delningslänk; produkterna importeras automatiskt | Måste |
| K2 | Synka listan vid varje körning: nya produkter läggs till, borttagna markeras som inaktiva (historiken behålls) | Måste |
| K3 | Hämta pris var 6:e timme, justerbart i GUI:t (t.ex. tätare under kampanjdagar), med slumpad fördröjning | Måste |
| K4 | Spara varje prismätning med tidsstämpel, även "ej i lager" | Måste |
| K5 | Larm på fast pris och/eller procent under referenspris | Måste |
| K6 | Standardnivå per lista, överstyrbar per produkt | Måste |
| K7 | Notiser via Apprise till en eller flera URL:er | Måste |
| K8 | Webbgränssnitt med listor, produkter, grafer och larminställningar | Måste |
| K9 | Valfri notis vid "lägsta pris någonsin" | Bör |
| K10 | Lägga till enstaka produkter som lokal bevakning via ASIN eller produkt-URL | Måste |

**Utanför v1:** andra Amazon-butiker och prisbevakningstjänster, privata listor med inloggning, flera användare, köp eller varukorg, Home Assistant.

## Arkitektur och teknikval

Allt körs som en enda Python-process i LXC:n: schemaläggare, insamlare, larmmotor och webbgränssnitt delar samma SQLite-databas. Det räcker gott för \~1 000 produkter och håller driften enkel.

&#91;embedded content: komponenter · en process, sex delar\]

Efter varje insamlingsrunda kör larmmotorn reglerna mot de nya mätningarna och skickar eventuella notiser via Apprise.

| Del | Val | Motivering |
| --- | --- | --- |
| Språk | Python 3.13 (Debian 13:s egen) | Bäst stöd för scraping och Apprise |
| Webb/API | FastAPI | Samma stack som Birdwatch |
| Gränssnitt | Jinja2-mallar + HTMX | Ingen separat frontend-build |
| Grafer | Chart.js | Enkel tidsseriegraf med referens- och mållinje |
| Databas | SQLite (WAL-läge) | En fil, enkel backup |
| HTTP + parsing | httpx + selectolax | Snabb HTML-tolkning |
| Schemaläggning | APScheduler | Intervall med jitter i samma process |
| Notiser | Apprise (Python-bibliotek) | Ingen separat Apprise-server behövs |

## Datainsamling från Amazon.se

Priserna läses direkt från önskelistans sidor. Där syns aktuellt pris för varje produkt, så en runda kräver bara några tiotal förfrågningar i stället för \~1 000. Amazon har inget öppet API för önskelistor, så insamlingen bygger på att läsa HTML.

1. **Lista:** hämta listsidan och följ sidbläddringen tills alla produkter är lästa. Plocka ASIN, titel, bild-URL, pris och tillgänglighet. Listsidan är källan till pris i varje runda; ingen produktsida hämtas för att läsa pris. Lokala ASIN-bevakningar läses alltid från produktsidan.
2. **Status per produkt:** varje läsning får en av tre statusar:
   - `pris`: ett pris hittades.
   - `ej_i_lager`: tillgänglighetsmarkering för slutsåld ("Inte tillgänglig för närvarande", "Currently unavailable", "Ej i lager" m.fl.).
   - `saknar_pris`: varken pris eller tillgänglighetsmarkering hittades.

   Probe 2026-10-25 visade att listsidan saknar markering för slutsålda produkter: de visar bara "Visa alla köpalternativ". Därför avgör produktsidan vid nästa bakgrundsuppdatering av Prime-cachen om en `saknar_pris`-produkt är `ej_i_lager`. Efter det kan ett `saknar_pris` från listan inte ändra statusen tillbaka, men ett pris på listan kan det (`åter_i_lager`). Mätningen sparas ändå som den lästes.
3. **Prime-status (hämtning vid behov + cache):** listsidan visar *inte* Prime-status, bara produktsidan gör det. Prime-status cachas per ASIN: status (`prime` / `inte_prime` / `okänd`) och tidpunkt för senaste kontroll. Produktsidan `amazon.se/dp/<ASIN>` hämtas bara när:
   - a) **Inför larm:** priset når larmvillkoret (målpris eller procent) och Prime-statusen är `okänd` eller äldre än `prime_fresh_for_alert`. Larmet skickas först efter kontrollen och bara om status = `prime`.
   - b) **Bakgrundsuppdatering:** cachen är äldre än `prime_ttl`. Högst `max_product_pages_per_round` per omgång, äldsta först (aldrig kontrollerade först). Uppdateringen görs direkt efter att varje lista lästs, för just den listans produkter, och loggas i listans runda.

   Lokala ASIN-bevakningar läser produktsidan varje runda och uppdaterar då även Prime-cachen. De räknas inte mot taket och loggar ingen `produktsida_hämtad`-händelse.

   Produktsidans parser läser Prime-indikator, pris, tillgänglighet och avsändare. Utloggad visar amazon.se ingen Prime-märkning, inte ens för varor som Amazon själva skickar (kontrollerat 2026-10-25). Därför gäller: **Prime-märkning eller "Avsändare: Amazon" = `prime`**, annan avsändare = `inte_prime`, ingen av delarna synlig = `okänd`. Regeln är en första approximation och kan skärpas senare. Samma väntetid (5–15 s) gäller för produktsidor som för listsidor. Vid CAPTCHA eller HTTP-fel på en produktsida avbryts produktsidehämtningarna för resten av rundan, statusen lämnas oförändrad och inga larm som kräver kontroll skickas.
4. **Tolkning av pris:** alla prisformat normaliseras till öre som heltal. Följande varianter förekommer beroende på språkinställning:
   - svenskt: "1 299,00 kr", "115,66 kr" (mellanslag, NBSP eller smal NBSP som tusentalsavgränsare)
   - svenskt med valutan först: "kr217,90"
   - engelskt: "kr1,299.00", "kr115.66"

   Samma pris kan stå flera gånger på en rad ("217,90 kr kr217,90"). Parsern tar ett pris per produkt och summerar eller dubbelräknar aldrig.
5. **Dolda felmallar:** listsidan innehåller vid varje produkt dolda felmallar ("Ett fel inträffade…", "Amazon har stött på ett fel") i `aok-hidden`/`a-hidden`-element. De ignoreras och tolkas aldrig som produktfel eller blockering.
6. **Rimlighetskontroll:** ett pris under 10 % av medianen (giltiga, icke misstänkta prismätningar de senaste 90 dagarna) markeras som misstänkt och utlöser inget larm förrän det bekräftats vid nästa mätning.

**Hövlighet mot Amazon:** max en förfrågan var 5–15:e sekund (slumpat), en realistisk User-Agent och `Accept-Language: sv-SE`. Vid CAPTCHA eller HTTP 503 pausas rundan med exponentiell backoff, och efter 3 blockerade rundor i följd skickas en driftnotis.

| Inställning | Standard | Betydelse |
| --- | --- | --- |
| `prime_ttl` | 7 dagar | Prime-cache äldre än så uppdateras i bakgrunden |
| `prime_fresh_for_alert` | 24 h | Prime-status måste vara så här färsk för att ett larm ska gå utan ny kontroll |
| `max_product_pages_per_round` | 10 | Tak för bakgrundsuppdateringar per runda (kontroller inför larm räknas inte mot taket) |

**Parser i egen modul**, med sparade HTML-exempel som testfall, så att den går snabbt att laga när Amazon ändrar sidlayouten. Insamlingen ligger bakom ett gemensamt källgränssnitt, så att fler källor (t.ex. Hagglezon) kan läggas till senare utan ändringar i larm eller GUI.

## Datamodell

SQLite i WAL-läge med främmande nycklar påslagna. Priser lagras i öre som heltal för att undvika avrundningsfel. Tidpunkter lagras som ISO-8601 i UTC och visas i Europe/Stockholm. Fält med fasta värden lagras som text med `CHECK`. I SQL skrivs kolumnnamnen utan å/ä/ö (`pris_öre` → `pris_ore`). Schemaversionen hålls i `PRAGMA user_version`, och `listwatch migrate` kör numrerade migreringar efter en säkerhetskopia. Med \~1 000 produkter × 4 mätningar/dygn blir det \~1,5 miljoner prisrader per år, vilket SQLite klarar utan problem.

**Kärndata** (rensas aldrig av loggrensningen):

| Tabell | Viktiga fält |
| --- | --- |
| `wishlist` | id, typ (amazon/lokal), namn, url, list\_id (unik, null för lokal), standard\_procent, standard\_aktiv, status (ok/blockerad/privat/fel), senast\_synkad, skapad |
| `product` | asin (nyckel), titel, bild\_url, url, aktiv, första\_sedd; Prime-cache: prime\_status (prime/inte\_prime/okänd), prime\_kontrollerad; senaste kända läge: senaste\_status, senaste\_pris\_öre, senast\_läst |
| `wishlist_product` | wishlist\_id, asin (gemensam nyckel), tillagd, borttagen (null = finns i listan) |
| `price_point` | asin, tidpunkt, status (pris/ej\_i\_lager/saknar\_pris), pris\_öre (null om status ≠ pris), prime (Prime-cachen vid mättillfället), källa (lista/produktsida), misstänkt, run\_id |
| `alert_rule` | asin (nyckel; saknas rad ärvs listans standard), mål\_pris\_öre, mål\_procent, ärver\_standard, lägsta\_någonsin\_notis, larm\_aktivt, senaste\_larm\_pris, senaste\_larm\_tid (null = spärren nollställd) |
| `settings` | nyckel, värde: Apprise-URL:er, intervall, `prime_ttl`, `prime_fresh_for_alert`, `max_product_pages_per_round`, `logg_lagringstid_dagar` (30), `notislogg_lagringstid_dagar` (365) |

`product.senaste_*` är en medveten denormalisering. Varje runda jämför mot den för att avgöra om en produkt är uppdaterad och vilka händelser som ska skrivas, utan att leta upp senaste `price_point`.

**Körlogg** (se `docs/spec-logg.md`; rensas en gång per dygn):

| Tabell | Viktiga fält |
| --- | --- |
| `run` | id, omgång, typ (schemalagd/manuell/probe), wishlist\_id, list\_namn och list\_id (ögonblicksbild), start, slut, räknare (sidor, lästa, prisändrade, nya, borttagna, produktsidor, prime\_ändrade, larm\_skickade, larm\_misslyckade, fel), captcha (null/listsida/produktsida), resultat (ok/delvis/fel) |
| `event` | id, run\_id (null utanför runda, t.ex. testnotis), tid, typ, asin, före, efter (råvärden som GUI:t formaterar), text |

Räknarna sparas på `run`, eftersom oförändrade produkter inte loggas som händelser och "lästa" därför inte går att räkna fram. `event.asin` har ingen främmande nyckel, och produktnamnet hämtas via join. Det finns ingen separat `notification_log`. Notisloggen i Inställningar är `event` filtrerad på larm-, drift- och testnotiser (`larm_skickat`, `larm_misslyckades`, `drift_notis`, `test_notis`). Sådana händelser rensas efter `notislogg_lagringstid_dagar`, övriga händelser och rundor efter `logg_lagringstid_dagar`. När en runda rensas blir dess kvarvarande notishändelser kvar med `run_id` = null. Båda lagringstiderna går att ställa in i GUI:t.

En produkt som finns i flera listor lagras en gång och mäts en gång. En körning ger en runda per lista, och rundorna delar samma `omgång`. Hittas produkten i flera listor räknas den som läst i varje runda, men `price_point` och händelser skrivs bara i den första. Larmregeln gäller per produkt; ärver den listans standard används den lägsta procentnivån bland listorna produkten finns i.

## Larmlogik

Ett larm går när priset korsar en nivå nedåt, och sedan inte igen förrän priset sjunker ytterligare eller först har gått upp över nivån.

**Referenspris:** medianen av alla giltiga mätningar (status `pris`, Prime, inte misstänkta) de senaste 90 dagarna, utan den mätning som bedöms. Finns mindre än 90 dagars historik används det som finns, och GUI:t visar "referens baserad på X dagar". Inget procentlarm förrän minst 7 dagars data finns, för att undvika falsklarm direkt efter import.

**Villkor**, kontrolleras efter varje ny mätning:

- **Fast pris:** pris ≤ mål\_pris.
- **Procent:** pris ≤ referens × (1 − mål\_procent / 100).
- Larm om *något* aktivt villkor är uppfyllt. Saknas egen regel används listans standardprocent.

**Spärr mot upprepning:**

1. Första gången villkoret uppfylls: notis, och priset sparas som senaste\_larm\_pris.
2. Fortsätter priset att ligga under nivån: ingen ny notis, utom om det sjunker minst 5 % till under senaste\_larm\_pris.
3. Går priset över nivån igen nollställs spärren, så nästa korsning ger ny notis.
4. Misstänkta priser (se Datainsamling) utlöser inget larm. Endast Prime-erbjudanden larmar, och referenspriset räknas bara på Prime-mätningar.
5. Är Prime-statusen `okänd` eller äldre än `prime_fresh_for_alert` när villkoret uppfylls, hämtas produktsidan först (se Datainsamling). Larmet skickas bara om kontrollen ger `prime`. Misslyckas kontrollen (CAPTCHA, HTTP-fel) skickas inget larm i den rundan, och spärren påverkas inte. Kontrollens resultat skrivs också in i den aktuella mätningens Prime-fält.
6. Mätningar utan pris (`ej_i_lager`, `saknar_pris`) bedöms inte och nollställer inte spärren. Samma låga pris larmar alltså inte igen bara för att varan varit slut emellan.
7. Ett misstänkt pris bekräftas om nästa mätning ligger inom ±10 % av det. Den bekräftande mätningen är då inte misstänkt och kan larma.
8. Spärren (`senaste_larm_pris`) sätts bara när notisen faktiskt skickades. Ett misslyckat utskick loggas som `larm_misslyckades` och prövas igen nästa runda.
9. `larm_aktivt` = av stänger alla larm för produkten, även lägsta pris någonsin.

**Lägsta pris någonsin:** valfritt per produkt, går när priset understiger allt tidigare uppmätt (giltiga mätningar, oavsett Prime), efter minst 30 dagars historik. Larmet kräver Prime som andra larm men använder inte spärren: varje nytt bottenpris ger en notis. Utlöses flera regler samtidigt blir det en notis.

**Notisinnehåll:** produktnamn, nytt pris, referenspris, rabatt i procent, vilken regel som utlöste larmet och länk till Amazon. Exempel: *"Sony WH-1000XM5: 2 990 kr (−25 % mot referens 3 990 kr). Under ditt mål på 3 200 kr."*

## Webbgränssnitt

Fyra vyer räcker för v1, och alla fungerar i mobilen. Ingen inloggning: gränssnittet nås bara på det lokala nätet eller via Tailscale.

| Vy | Innehåll | Åtgärder |
| --- | --- | --- |
| Översikt | Alla bevakade produkter: bild, namn, aktuellt pris, referens, rabatt i %, trendpil, larmstatus. Filter per lista, sortering på rabatt | Öppna produkt, slå av/på larm |
| Listor | Namn, antal produkter, senaste synk, status (OK/blockerad/fel) | Lägg till lista via länk eller enstaka produkt via ASIN, sätt standardprocent, synka nu, ta bort |
| Produkt | Prisgraf (30 d / 90 d / 1 år / allt) med referenslinje och målnivå inritade; lägsta, högsta och median | Sätt fast mål och/eller procent, ärv standard, lägsta-någonsin-notis |
| Inställningar | Apprise-URL:er, intervall (standard 6 h, lägst 1 h), tillfälligt kampanjintervall med slutdatum, fördröjning, notislogg, driftstatus | Skicka testnotis |

Grafen visar luckor där produkten varit slut i lager, i stället för att dra en linje igenom.

## Drift, konfiguration och risker

Tjänsten körs som en enda process i en Debian 13-LXC, startad av systemd och installerad med ett skript (se Installation). Samma kod ska gå att köra som Docker-container.

- **LXC:** Debian 13, unprivileged, 1 vCPU, 512 MB RAM, 4 GB disk. Kod i `/opt/listwatch`, data i `/var/lib/listwatch` (SQLite + loggar).
- **Konfiguration:** miljövariabler eller `.env` för port, datakatalog, intervall och loggnivå. Apprise-URL:er och larmnivåer sätts i GUI:t och sparas i databasen.
- **Backup:** Proxmox-backup av LXC:n räcker; dessutom en daglig `sqlite3 .backup`-fil i datakatalogen.
- **Loggning:** strukturerade loggar till journald, med rundans resultat (antal produkter, fel, blockeringar).
- **Driftnotiser** via Apprise: blockerad 3 rundor i följd, lista ej åtkomlig (t.ex. gjord privat), parserfel på fler än 20 % av produkterna.

| Risk | Konsekvens | Åtgärd |
| --- | --- | --- |
| Amazon ändrar HTML | Priser kan inte läsas | Isolerad parser med testfall, driftnotis vid parserfel |
| Blockering/CAPTCHA | Luckor i historiken | Låg frekvens, slumpad fördröjning, backoff |
| Listan görs privat | Synk misslyckas | Statusflagga och driftnotis; befintlig historik behålls |
| Felaktigt pris tolkas | Falsklarm | Rimlighetskontroll och bekräftelse vid nästa mätning |
| Amazons användarvillkor | Automatiserad läsning är inte uttryckligen tillåten | Endast privat bruk, låg volym |

## Installation (install.sh)

Ett bash-skript, `install.sh` i repots rot, gör en tom Debian 13-LXC till en körande tjänst på under 5 minuter. Samma skript körs igen för att uppdatera. Koden hämtas från ett GitHub-repo.

**Körning**, som root i LXC:n:

```
curl -fsSL https://raw.githubusercontent.com/emilgil/list-watch/main/install.sh | bash -s -- --wishlist <delningslänk>
```

| Flagga | Standard | Betydelse |
| --- | --- | --- |
| `--repo` | `https://github.com/emilgil/list-watch.git` | Repo att klona |
| `--branch` | `main` | Gren eller tagg |
| `--port` | `8080` | Port för webbgränssnittet |
| `--wishlist` | (ingen) | Kör steg 0-proben mot denna lista efter installationen |
| `--deploy-key` | (ingen) | Sökväg till SSH-nyckel, bara om repot görs privat senare |
| `--no-start` | av | Installera men starta inte tjänsten |

**Steg i skriptet:**

1. **Förkontroll:** root, Debian 13 (trixie) enligt `/etc/os-release`, amd64 eller arm64, att github.com och amazon.se nås. Avbryt med tydligt fel annars.
2. **Paket:** `apt-get update` och installation av git, python3, python3-venv, sqlite3, curl, ca-certificates, tzdata. Tidszon Europe/Stockholm.
3. **Systemanvändare** `listwatch` utan inloggningsskal, hem i `/var/lib/listwatch`.
4. **Kod:** första gången `git clone` till `/opt/listwatch`; därefter `git fetch` och `git reset --hard origin/<gren>`. Privat repo använder `--deploy-key` via `GIT_SSH_COMMAND`.
5. **Python-miljö:** venv i `/opt/listwatch/.venv` och `pip install` från repots låsta beroendefil.
6. **Konfiguration:** `/etc/listwatch/listwatch.env` (port, datakatalog, loggnivå) skapas bara om den saknas och skrivs aldrig över.
7. **Databas:** `listwatch migrate` skapar eller uppgraderar SQLite-schemat. Före en uppgradering tas en säkerhetskopia av databasen.
8. **systemd:** kopierar `deploy/listwatch.service` från repot, aktiverar och startar. Tjänsten körs som `listwatch` med `Restart=on-failure`, `NoNewPrivileges` och `ProtectSystem=strict` (skrivrätt bara till datakatalogen).
9. **Backup:** `listwatch-backup.timer` gör en daglig `sqlite3 .backup`, 14 kopior sparas.
10. **Hälsokontroll:** väntar upp till 30 s på `http://127.0.0.1:<port>/health`. Misslyckas den visas de senaste 30 loggraderna från journald.
11. **Steg 0-prob** (om `--wishlist` angetts): kör `listwatch probe <url>` som tjänsteanvändaren.
12. **Sammanfattning:** adress (`http://<LXC-IP>:<port>`), sökvägar, hur man läser loggar (`journalctl -u listwatch`) och hur man uppdaterar (kör skriptet igen).

**Steg 0-proben** `listwatch probe <url>` hämtar listan med samma inställningar som tjänsten och ger en rapport i terminalen, sparade filer och en avslutskod. Exempel:

```
$ listwatch probe https://www.amazon.se/hz/wishlist/ls/ABC123XYZ

List Watch – probe  2026-10-26 09:14
Lista:        "Önskelista teknik" (ABC123XYZ)
HTTP:         200 OK (3 sidor, 2,1 s/sida i snitt)
CAPTCHA:      nej

Produkter:    57
  Pris läst:        51 / 57  (89 %)
  Prime-status:     finns ej på listsidan (förväntat)
  Ej i lager:        3
  Saknar pris:       3  → B0C1234567, B09ABCDEF1, B08XYZ9876

Produktsidor behövs per runda:  ~3  (saknat pris; Prime hämtas vid behov och cachas)

Sparat:  /var/lib/listwatch/probe/2026-10-26_0914/
  page-1.html  page-2.html  page-3.html  report.json

Resultat: DELVIS OK (avslutskod 1)
```

- **Avslutskod 0, allt läsbart:** listsidan räcker för pris eller tillgänglighet på alla produkter.
- **Avslutskod 1, delvis:** listan går att läsa, men vissa produkter har status `saknar_pris` och kräver produktsidan.
- **Avslutskod 2, blockerad eller fel:** CAPTCHA, HTTP 503, privat lista eller en sida som inte går att tolka.

Att Prime-status saknas på listsidan är förväntat (se Datainsamling) och påverkar inte avslutskoden. Visar en listsida ändå Prime-märkning räknas den och redovisas som "X / N".

HTML-sidorna blir testfall för parsern. `report.json` innehåller samma siffror plus en rad per produkt (ASIN, titel, status, pris, Prime ja/nej/okänd). Raden **Produktsidor behövs per runda** visar hur många produkter som saknar pris: nära 0 betyder att listsidorna räcker.

**Robusthet:** `set -euo pipefail`, en `trap` som skriver ut rad och kommando vid fel, och allt loggas även till `/var/log/listwatch-install.log`. Skriptet ska kunna köras flera gånger i rad utan att förstöra data eller konfiguration.

**Krav på repot** för att skriptet ska fungera: `install.sh`, låst beroendefil, Python-paketet med CLI-kommandona `serve`, `migrate`, `probe` och `backup`, en `/health`-endpoint samt `deploy/` med service- och timerfiler.

**Utanför skriptet:** att skapa LXC:n i Proxmox (Debian 13-mall, unprivileged, 1 vCPU, 512 MB RAM, 4 GB disk), Tailscale, HTTPS och omvänd proxy.

**Klart när:**

- [ ] Skriptet går igenom utan fel i en nyskapad Debian 13-LXC.
- [ ] Webbgränssnittet svarar på port 8080 från LAN:et.
- [ ] Tjänsten startar igen efter omstart av LXC:n.
- [ ] En andra körning uppdaterar koden och behåller data och `listwatch.env`.
- [ ] Proben skriver rapport och sparar HTML för en delad lista.

**Repo:** publikt, `list-watch` på GitHub. Skriptet körs därför direkt med `curl … | bash` och behöver ingen deploy-nyckel.

## Beslut och fas 2

**Beslutat 2026-10-25:**

- **Intervall:** 6 h som standard, justerbart i GUI:t, med tillfälligt kampanjintervall och slutdatum.
- **Larm:** endast Prime-erbjudanden.
- **Åtkomst:** inget lösenord; gränssnittet nås bara via LAN eller Tailscale.
- **Teknik:** stacken under Arkitektur gäller.
- **Lokal bevakning via ASIN** ingår i v1 (K10).
- **Andra källor:** inga andra sajter eller prisbevakningstjänster i v1, men källgränssnittet håller dörren öppen för t.ex. Hagglezon.

**Steg 0, prototyp innan resten byggs:** kommandot listwatch probe (se Installation), som körs från LXC:n och verifierar att

1. listsidan går att läsa från LXC:ns IP utan CAPTCHA,
2. pris, ASIN och Prime-status syns i listans HTML,
3. sidbläddringen fungerar för en lista med 50+ produkter.

Sparade HTML-sidor blir testfall för parsern. Om listan inte visar Prime-status avgör det hur många produktsidor varje runda kräver.

**Resultat av probe 2026-10-25** (lista "Hemmet", 55 produkter, 6 sidor, ingen CAPTCHA):

- Sidbläddringen fungerar.
- Pris lästes för 44 av 55 produkter. De övriga 11 visar bara "Visa alla köpalternativ", och minst en av dem var "Currently unavailable" på produktsidan.
- Listsidan visar ingen Prime-status. Därför hämtas Prime vid behov och cachas (se Datainsamling).
- Amazon kan visa priser i engelskt format ("kr115.66") beroende på språkinställning.
- Sparade sidor är korrekt UTF-8; trasiga tecken vid visning beror på webbläsaren.

**Fas 2 (idéer):** export till CSV, veckosammanfattning via Apprise, fler källor som Hagglezon.
