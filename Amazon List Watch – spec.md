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

Priserna läses i första hand direkt från önskelistans sidor. Där syns aktuellt pris för varje produkt, så en runda kräver bara några tiotal förfrågningar i stället för \~1 000. Amazon har inget öppet API för önskelistor, så insamlingen bygger på att läsa HTML.

1. **Lista:** hämta listsidan och följ sidbläddringen tills alla produkter är lästa. Plocka ASIN, titel, bild-URL, pris, valuta och tillgänglighet.
2. **Fallback per produkt:** saknas priset i listan (t.ex. "se alternativ") hämtas produktsidan `amazon.se/dp/<ASIN>`, max ett begränsat antal per runda. Lokala ASIN-bevakningar läses alltid från produktsidan.
3. **Tolkning:** svenska prisformat ("1 299,00 kr") normaliseras till öre som heltal. Varje mätning sparas med en flagga för om erbjudandet är Prime. Visar listsidan inte Prime-status hämtas produktsidan.
4. **Rimlighetskontroll:** ett pris under 10 % av medianen markeras som misstänkt och utlöser inget larm förrän det bekräftats vid nästa mätning.

**Hövlighet mot Amazon:** max en förfrågan var 5–15:e sekund (slumpat), en realistisk User-Agent och `Accept-Language: sv-SE`. Vid CAPTCHA eller HTTP 503 pausas rundan med exponentiell backoff, och efter 3 blockerade rundor i följd skickas en driftnotis.

**Parser i egen modul**, med sparade HTML-exempel som testfall, så att den går snabbt att laga när Amazon ändrar sidlayouten. Insamlingen ligger bakom ett gemensamt källgränssnitt, så att fler källor (t.ex. Hagglezon) kan läggas till senare utan ändringar i larm eller GUI.

## Datamodell

Fem kärntabeller i SQLite räcker. Priser lagras i öre som heltal för att undvika avrundningsfel. Med \~1 000 produkter × 4 mätningar/dygn blir det \~1,5 miljoner rader per år, vilket SQLite klarar utan problem.

| Tabell | Viktiga fält |
| --- | --- |
| `wishlist` | id, typ (amazon/lokal), namn, url, list\_id, standard\_procent, standard\_aktiv, senast\_synkad, status |
| `product` | asin (unik), titel, bild\_url, url, aktiv, första\_sedd |
| `wishlist_product` | wishlist\_id, asin, tillagd, borttagen |
| `price_point` | asin, tidpunkt, pris\_öre (null = ej tillgänglig), prime (ja/nej), källa (lista/produktsida), misstänkt |
| `alert_rule` | asin, mål\_pris\_öre, mål\_procent, ärver\_standard, lägsta\_någonsin\_notis, larm\_aktivt, senaste\_larm\_pris, senaste\_larm\_tid |

Dessutom en tabell `notification_log` (tid, asin, typ, meddelande, lyckad) och en enkel `settings` med Apprise-URL:er och intervall.

En produkt som finns i flera listor lagras en gång och mäts en gång. Larmregeln gäller per produkt; ärver den listans standard används den lägsta procentnivån bland listorna produkten finns i.

## Larmlogik

Ett larm går när priset korsar en nivå nedåt, och sedan inte igen förrän priset sjunker ytterligare eller först har gått upp över nivån.

**Referenspris:** medianen av alla giltiga mätningar de senaste 90 dagarna. Finns mindre än 90 dagars historik används det som finns, och GUI:t visar "referens baserad på X dagar". Inget procentlarm förrän minst 7 dagars data finns, för att undvika falsklarm direkt efter import.

**Villkor**, kontrolleras efter varje ny mätning:

- **Fast pris:** pris ≤ mål\_pris.
- **Procent:** pris ≤ referens × (1 − mål\_procent / 100).
- Larm om *något* aktivt villkor är uppfyllt. Saknas egen regel används listans standardprocent.

**Spärr mot upprepning:**

1. Första gången villkoret uppfylls: notis, och priset sparas som senaste\_larm\_pris.
2. Fortsätter priset att ligga under nivån: ingen ny notis, utom om det sjunker minst 5 % till under senaste\_larm\_pris.
3. Går priset över nivån igen nollställs spärren, så nästa korsning ger ny notis.
4. Misstänkta priser (se Datainsamling) utlöser inget larm. Endast Prime-erbjudanden larmar, och referenspriset räknas bara på Prime-mätningar.

**Lägsta pris någonsin:** valfritt per produkt, går när priset understiger allt tidigare uppmätt, efter minst 30 dagars historik.

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
  Pris läst:        54 / 57  (95 %)
  Prime-status:     51 / 57  (89 %)
  Ej i lager:        3
  Saknar pris:       3  → B0C1234567, B09ABCDEF1, B08XYZ9876

Produktsidor behövs per runda:  ~6  (saknat pris eller Prime-status)

Sparat:  /var/lib/listwatch/probe/2026-10-26_0914/
  page-1.html  page-2.html  page-3.html  report.json

Resultat: DELVIS OK (avslutskod 1)
```

- **Avslutskod 0, allt läsbart:** listsidan räcker för pris och Prime på alla produkter.
- **Avslutskod 1, delvis:** listan går att läsa, men vissa produkter kräver produktsidan.
- **Avslutskod 2, blockerad eller fel:** CAPTCHA, HTTP 503, privat lista eller en sida som inte går att tolka.

HTML-sidorna blir testfall för parsern. `report.json` innehåller samma siffror plus en rad per produkt (ASIN, titel, pris, Prime ja/nej). Raden **Produktsidor behövs per runda** avgör om intervall och fördröjning behöver justeras: nära 0 betyder att listsidorna räcker.

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

**Fas 2 (idéer):** export till CSV, veckosammanfattning via Apprise, fler källor som Hagglezon.
