# Spec: Körlogg i webb-GUI:t

## Syfte
Visa i GUI:t vad varje runda gjort: hur många produkter som lästs,
uppdaterats, kontrollerats mot produktsida, larmats om, och vad som gått fel.

## 1. Begrepp
- **Runda**: en körning av bevakningen (schemalagd, manuell eller probe).
- **Händelse**: något som hände med en produkt eller rundan under en runda.
- **Läst**: produkten hittades på listsidan i rundan.
- **Uppdaterad**: något sparat om produkten ändrades i rundan
  (pris, tillgänglighet eller Prime-status). Oförändrade produkter
  räknas som lästa men inte uppdaterade.

## 2. Rundsammanfattning (en rad per runda)
| Fält | Beskrivning |
|---|---|
| start, slut, tid | tidpunkter och varaktighet |
| typ | schemalagd / manuell / probe |
| lista | listnamn + list-ID (flera listor → en runda per lista) |
| sidor | antal listsidor hämtade |
| lästa | produkter hittade på listsidan |
| prisändrade | produkter med nytt pris jämfört med senaste kända |
| nya / borttagna | produkter som tillkommit / försvunnit från listan |
| produktsidor | antal produktsidor hämtade (vid behov + bakgrund) |
| prime_ändrade | produkter där Prime-status ändrades |
| larm | antal larm skickade / misslyckade |
| fel | antal fel (HTTP, parsning) |
| captcha | ja/nej, och i så fall var (listsida / produktsida) |
| resultat | OK / DELVIS OK / FEL (som probens avslutskoder, men saknat pris ger inte DELVIS) |

## 3. Händelser (detaljer per runda)
Varje händelse: tidpunkt, runda, typ, ASIN (om tillämpligt),
produktnamn (kortat), före → efter, fritext.

Typer:
- `pris_ändrat` (gammalt → nytt pris i kr)
- `ny_produkt`, `borttagen_produkt`
- `ej_i_lager`, `åter_i_lager`
- `prime_ändrad` (före → efter)
- `produktsida_hämtad` (orsak: larmkontroll / bakgrund)
- `larm_skickat`, `larm_misslyckades` (med Apprise-felmeddelande)
- `saknar_pris` (pris kunde inte läsas)
- `http_fel`, `parsningsfel`, `captcha`

Oförändrade produkter loggas inte som händelser, bara i räknaren "lästa".

## 4. GUI
- Ny sida **Logg**:
  - Tabell med rundor, nyast först, med kolumnerna från avsnitt 2.
  - Resultat färgmarkerat (OK / DELVIS OK / FEL).
  - Klick på en runda → dess händelser, filtrerbara på händelsetyp.
- Från en produkts vy: länk till dess händelser (filter på ASIN).
- Ingen inloggning (enligt tidigare beslut om GUI utan lösenord).

## 5. Lagring och rensning
- Rundsammanfattningar och händelser lagras i samma databas som
  resten av appen.
- **Prishistorik** är separat data och rensas aldrig av loggrensningen.
- Lagringstid för rundor och händelser: en gemensam inställning,
  standard **30 dagar**, justerbar i GUI:t (inställningssida).
- Rensning körs automatiskt en gång per dygn och tar bort rundor
  äldre än inställningen tillsammans med deras händelser.

## 6. Antaganden (bekräfta eller ändra)
- Probe-körningar loggas också, som typ = probe.
- Tekniska detaljer (stack traces, HTTP-headers) visas inte i GUI:t
  utan skrivs till stdout/journald för felsökning.
- En runda per lista om flera listor bevakas.
## 7. Beslut 2026-10-25
- Antagandena i avsnitt 6 gäller.
- **Flera listor:** en körning ger en runda per lista med gemensam
  `omgång`. En produkt i flera listor räknas som läst i varje runda,
  men pris och händelser skrivs bara i den första.
- **Händelser bara vid ändring**, även för `saknar_pris` och
  `ej_i_lager`. Räknarna räknar alla produkter varje runda.
- **Probe** skriver bara `run` och `event`, aldrig produkter eller priser.
- **Notislogg:** larm-, drift- och testnotiser är händelser med egen
  lagringstid `notislogg_lagringstid_dagar` (standard 365), ställbar i
  GUI:t. Övriga händelser och rundor rensas efter `logg_lagringstid_dagar`.
- **Resultat:** `fel` vid CAPTCHA, HTTP-fel eller 0 lästa; `delvis` om
  fel uppstod (t.ex. parserfel, fel på en produktsida) eller ett larm
  misslyckades; annars `ok`. En produkt som saknar pris är inget fel
  (beslutat 2026-10-27) och ger inte `delvis`; probens avslutskod 1 gäller
  fortfarande.
- Tabellerna beskrivs under Datamodell i huvudspecen.
