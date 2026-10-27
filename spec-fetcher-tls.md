# Spec: HTTP-klient som ser ut som Chrome (curl_cffi) + bestående cookies

Status: förslag, 2026-10-27. Förutsättningen är verifierad (se "Testresultat").

## Testresultat 2026-10-27 (från LXC:n, inom några minuter)

| Klient | Protokoll | Resultat |
|---|---|---|
| `listwatch probe` (httpx, agentens inställningar) | IPv6 | 503 på 0,1 s |
| curl med Chrome-User-Agent | IPv6 | 200 |
| curl med Chrome-User-Agent | IPv4 | 200 |
| curl_cffi `impersonate="chrome"` | IPv6 | 200, ingen CAPTCHA, 586 kB |
| curl_cffi `impersonate="chrome"` | IPv4 | 200, ingen CAPTCHA, 577 kB |

- LXC:n går ut på IPv6 med en fast adress byggd på MAC-adressen. Datorn går ut i samma /64-nät med en temporär adress.
- Slutsats: varken adressen eller frekvensen är spärrad. Amazon nekar på grund av hur httpx-förfrågan ser ut.

## Bakgrund

- Från 26 okt svarar amazon.se med HTTP 503 på önskelistan (`/hz/wishlist/ls/...`). Även omförsöket efter 5–15 min nekas, så omgången räknas som blockerad.
- Samma lista öppnas utan problem i Chrome och Edge (incognito) från samma nätverk. IP-adressen är alltså inte blockerad.
- `fetcher.py` skickar redan Chrome-lika headers (User-Agent Chrome 156, `Sec-CH-UA*`, `Sec-Fetch-*`, `Accept-Language` sv-SE). Blockeringen fortsatte efter att de lades till.
- Det som skiljer agenten från en webbläsare:
  1. **TLS-fingeravtryck.** httpx använder Pythons OpenSSL, och handskakningen ser inte ut som Chromes. En Chrome-User-Agent ovanpå ett Python-fingeravtryck är en tydlig bot-signatur.
  2. **HTTP/1.1.** Klienten saknar `http2=True` och `h2` finns inte i låsfilen. Chrome använder alltid HTTP/2.
  3. **Inga cookies.** Varje omgång skapar en ny klient (`amazon_source()` → `make_fetcher()`). Varje omgång ser därför ut som en ny besökare som går direkt till en djup URL.

## Mål

- Agentens förfrågningar ska vara konsekventa med en riktig Chrome: TLS, HTTP/2, headers och cookies.
- Inget ändras utanför HTTP-lagret. `Fetch = Callable[[str], FetchResult]` och `FetchResult` är oförändrade, så `AmazonSource`, collector, scheduler och GUI påverkas inte.
- Hövlighetsreglerna (5–15 s, grupper, omförsök, backoff) är oförändrade.

## Icke-mål

- Ingen proxy, ingen IP-rotation, ingen headless-webbläsare.
- Ingen CAPTCHA-lösning. En CAPTCHA eller 503 hanteras som i dag (backoff och driftnotis).
- Ingen ändring av intervall eller tempo.

## Ändringar

### 1. Beroenden

- Lägg till `curl_cffi` (fast version) i `pyproject.toml` och `requirements.lock`. Det finns färdiga manylinux-hjul för x86_64 och aarch64 och Python 3.13, så inga nya apt-paket behövs och `install.sh` är oförändrat.
- `httpx` tas bort ur körberoendena om inget annat i `src/` importerar det. Det ligger kvar i `dev`, eftersom FastAPI:s `TestClient` behöver det.
- Docker-imagen använder samma låsfil och behöver ingen egen ändring.

### 2. `fetcher.py`

- `make_fetcher()` skapar en `curl_cffi.requests.Session` med `impersonate=IMPERSONATE`.
  - `IMPERSONATE` är en modulkonstant, t.ex. `"chrome"`. Vilken Chrome-version det motsvarar bestäms av den låsta curl_cffi-versionen. Att uppdatera fingeravtrycket innebär alltså att höja versionen i låsfilen.
- **De handskrivna headers tas bort:** User-Agent, `Sec-CH-UA*`, `Sec-Fetch-*`, `Accept`, `Upgrade-Insecure-Requests`. Impersonation sätter dem själv, i rätt ordning och i samklang med TLS-fingeravtrycket. Egna värden riskerar att motsäga fingeravtrycket, och det är värre än inga alls.
- Den enda header som sätts uttryckligen är `Accept-Language: sv-SE,sv;q=0.9,en;q=0.5`.
- Oförändrat beteende:
  - redirects följs
  - timeout 30 s
  - nätverksfel (curl_cffi:s undantag) ger `FetchResult(status=0, html="", seconds=…)`
  - `html` är den avkodade texten.
- **Loggning:** httpx-loggens rad `HTTP Request: GET … "HTTP/1.1 503"` försvinner. Fetchern loggar i stället en egen INFO-rad per förfrågan i formen `GET <url> → <status> (HTTP/<version>, <sek> s)`, så journalen innehåller samma information som i dag.
- Testbarhet: parametern `transport` (httpx-specifik) ersätts med en injicerbar session, eller en "gör förfrågan"-funktion, som tester kan byta ut. Inga tester får gå mot nätet.

### 3. Bestående cookies

- Fil: `/var/lib/listwatch/cookies.json`. Ägare `listwatch`, rättigheter 0600. Bara cookies för `.amazon.se` sparas.
- Cookies laddas när `make_fetcher()` skapar sessionen och sparas efter varje omgång, oavsett resultat.
- **En blockerad omgång tömmer filen**, alltså när även omförsöket nekats. Nästa omgång börjar då med en ny session i stället för en som Amazon redan flaggat.
- En trasig eller oläsbar fil räknas som tom och skrivs över. Det ger ett varningsloggmeddelande men inget fel.
- `listwatch probe` använder samma HTTP-inställningar men en egen tom session och **sparar inte** cookies. Proben ska inte påverka agentens session.

### 4. Ingen uppvärmning, och HTTP 202 räknas som nekad

*Ändrat 2026-10-27 efter drift.* Den första versionen hämtade `https://www.amazon.se/` före första listan när sessionen saknade cookies. I drift svarade startsidan **202** tre gånger i rad. 202 är Amazons JavaScript-utmaning (AWS WAF), och den kan curl_cffi inte klara. Koden räknade 202 som ett vanligt fel, så listan lästes inte och inget omförsök gjordes. Själva önskelistan gav samtidigt 200 direkt, och den sätter egna cookies.

**Uppvärmningen tas bort helt.**
- En omgång går direkt till första listans URL, även när sessionen saknar cookies. Cookies från listsidorna sparas som i avsnitt 3.
- Flaggan som håller reda på om sessionen är uppvärmd (`_warm` i `AmazonSource`) och uppvärmningsanropet tas bort. `CookieSession.has_cookies` behövs inte längre för detta. Den får vara kvar bara om något annat använder den.
- Docstringen överst i `amazon.py` uppdateras.

**HTTP 202 räknas som en nekad förfrågan, precis som 503 och CAPTCHA.**
- Det gäller överallt där blockering avgörs:
  - listläsningen (`_blocked()` i `amazon.py` och kontrollen i `collector.py`)
  - produktsidorna (`collector.py`).
- En nekad listsida ger alltså omförsök efter `RETRY_WAIT`. Om även omförsöket nekas räknas omgången som blockerad, med backoff och tömda cookies. En nekad produktsida avbryter produktsidorna för resten av omgången.
- Orsaken loggas och visas som `HTTP 202 (JS-utmaning)`, så att den skiljs från 503.
- Driftnotisens text och loggraden "blockerad av Amazon (CAPTCHA eller HTTP 503)" ändras till "(CAPTCHA, HTTP 503 eller HTTP 202)". Det gäller `scheduler.py` och eventuella GUI-texter.
- Övriga statuskoder som inte är 200 (404, 500 och så vidare) räknas som i dag: fel, inte blockering.
- Blockerande statuskoder samlas på ett ställe, en konstant med {202, 503}, i stället för att 503 skrivs ut i flera moduler.

**Probe:** en 202 rapporteras som blockerad (avslutskod 2), med texten "JS-utmaning (HTTP 202)".

### 5. Dokumentation

- `CLAUDE.md`: ändra stackraden "httpx + selectolax" till "curl_cffi (Chrome-impersonation) + selectolax". Lägg till en mening om cookie-filen och regeln att handskrivna webbläsarheaders inte ska läggas till.
- `deploy/listwatch.env.example` behöver ingen ändring.

## Tester

- Ett nätverksfel ger `status=0`.
- En statuskod och HTML förs vidare oförändrade.
- Ingen egen User-Agent eller `Sec-*`-header skickas. `Accept-Language` skickas.
- Cookies laddas, sparas och laddas igen till samma uppsättning (via en temporär katalog).
- En trasig cookie-fil ger en tom session och en varning.
- En blockerad omgång tömmer cookie-filen. En lyckad omgång gör det inte.
- Ingen förfrågan går till startsidan, inte heller med tom session. Första förfrågan i en omgång är listans URL.
- 202 på en listsida ger omförsök. 202 även på omförsöket ger en blockerad omgång, tömda cookies och ökad backoff.
- 202 på en produktsida avbryter produktsidorna för omgången och räknas som blockering.
- 404 och 500 räknas fortfarande som fel, inte blockering.
- Probe ger avslutskod 2 vid 202.
- Probe skriver inte till cookie-filen.
- Befintliga tester för pacing, scheduler och collector passerar utan ändring, förutom att `test_fetcher_cli.py` byter från `httpx.MockTransport` till den nya injektionspunkten.

## Utrullning

1. Kör `install.sh` som vanligt. Det installerar från låsfilen.
2. Agenten står troligen i backoff, upp till 24 h. Starta därför en manuell omgång från GUI:t efter uppdateringen.
3. Kör `listwatch probe <lista>` en gång som kontroll.

## Godkänt när

- `listwatch probe` från LXC:n blockeras inte mot listan "Köket": avslutskod 0 eller 1, inte 2. Avslutskod 1 betyder bara att några produkter saknar pris på Amazon ("Visa alla köpalternativ").
- Tre schemalagda omgångar i rad är oblockerade och läser alla listor (produktsidor hämtas bara för enstaka ASIN-bevakningar).
- Journalen visar `HTTP/2` i fetcherns loggrad.
- Journalen visar ingen `GET https://www.amazon.se/`. Första förfrågan i varje omgång är en listsida.
- Alla tester passerar.

## Risker

- Amazon kan med tiden känna igen äldre impersonation-profiler. Åtgärden är att höja curl_cffi-versionen i låsfilen. Det är en driftuppgift, ingen kodändring.
- curl_cffi innehåller en binär del, libcurl-impersonate. Om det någon gång saknas hjul för en plattform faller installationen. Det gäller inte x86_64 eller aarch64 i dag.
- Amazon kan börja skicka JS-utmaningar (202) även på listsidor. Då fångas det som blockering med backoff och driftnotis, men det kan inte lösas utan en riktig webbläsare. Det ligger utanför v1.
- Blockering som beror på frekvens eller IP löser den här ändringen inte. Därför finns förutsättningen överst: testet måste visa att curl_cffi lyckas där httpx misslyckas.
